"""Re-encrypt every stored secret under a new ENCRYPTION_KEY.

Secrets encrypted at rest with the install's key:

- ``providers.json``: each provider's ``bearer_token``;
- ``credentials.db``: ``oauth_credentials.access_token`` and ``refresh_token``;
- ``admin_mfa.json``: ``secret_enc``.

The rotation is all or nothing: every value is decrypted with the old key
first, and nothing is written when one of them cannot be read. Before
writing, the files are copied to ``<data>/backups/rekey-<UTC time>/``. A
value that the new key already reads is left as it is, so an interrupted
rotation can simply be run again. Legacy plain-text values are encrypted
with the new key.

Stop Plugboard before rotating: a running server keeps the old key in
memory and would write new secrets with it. Reports name where a value
sits, never the value.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from cryptography.fernet import Fernet, InvalidToken
from filelock import FileLock

from plugboard.config import fernet_for_key
from plugboard.storage import atomic_write_json, atomic_write_text

log = logging.getLogger(__name__)

_PREFIX = "enc:"
PROVIDERS = "providers.json"
CREDENTIALS = "credentials.db"
ADMIN_MFA = "admin_mfa.json"
KEY_FILE = ".encryption_key"


@dataclass
class RekeyReport:
    rotated: int = 0  # values moved from the old key to the new one
    encrypted_plain: int = 0  # legacy plain-text values now encrypted
    already_new: int = 0  # values the new key already reads
    unreadable: List[str] = field(default_factory=list)  # locations only
    damaged: List[str] = field(default_factory=list)  # files that cannot be read
    backup_dir: Optional[Path] = None
    written: bool = False

    @property
    def ok(self) -> bool:
        return not self.unreadable


class _Converter:
    def __init__(self, old: Fernet, new: Fernet, report: RekeyReport) -> None:
        self._old = old
        self._new = new
        self._report = report

    def __call__(self, value: Any, where: str) -> Any:
        if not isinstance(value, str) or not value:
            return value
        if not value.startswith(_PREFIX):
            self._report.encrypted_plain += 1
            return _PREFIX + self._new.encrypt(value.encode()).decode()
        token = value[len(_PREFIX) :].encode()
        try:
            plain = self._old.decrypt(token)
        except InvalidToken:
            try:
                self._new.decrypt(token)
            except InvalidToken:
                self._report.unreadable.append(where)
                return value
            self._report.already_new += 1
            return value
        self._report.rotated += 1
        return _PREFIX + self._new.encrypt(plain).decode()


def is_strong_key(key: str) -> bool:
    """True for a real Fernet key (32 random bytes, urlsafe base64)."""
    try:
        Fernet(key.encode("ascii"))
    except Exception:
        return False
    return True


def _read_json(path: Path, report: RekeyReport) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report.damaged.append(f"{path.name} (not readable JSON)")
        report.unreadable.append(f"{path.name} (not readable JSON)")
        return None
    if not isinstance(data, dict):
        report.damaged.append(f"{path.name} (unexpected content)")
        report.unreadable.append(f"{path.name} (unexpected content)")
        return None
    return data


def _read_credentials(path: Path, report: RekeyReport) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'oauth_credentials'"
            ).fetchone()
            rows = (
                conn.execute(
                    "SELECT source, access_token, refresh_token FROM oauth_credentials"
                ).fetchall()
                if has_table
                else []
            )
        finally:
            conn.close()
    except sqlite3.Error:
        report.damaged.append(f"{path.name} (cannot read the database)")
        report.unreadable.append(f"{path.name} (cannot read the database)")
        return []
    return [
        {"source": src, "access_token": acc, "refresh_token": ref}
        for src, acc, ref in rows
    ]


def _backup(data: Path, names: List[str]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (data / "backups").mkdir(parents=True, exist_ok=True, mode=0o700)
    target = data / "backups" / f"rekey-{stamp}"
    suffix = 1
    while True:
        try:
            target.mkdir(mode=0o700)
            break
        except FileExistsError:
            suffix += 1
            target = data / "backups" / f"rekey-{stamp}-{suffix}"
    for name in names:
        src = data / name
        if not src.exists():
            continue
        dst = target / name
        if name == CREDENTIALS:
            src_conn = sqlite3.connect(str(src))
            dst_conn = sqlite3.connect(str(dst))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
                src_conn.close()
        else:
            shutil.copy2(src, dst)
        os.chmod(dst, 0o600)
    return target


def rotate(
    data: Path, old_key: str, new_key: str, *, dry_run: bool = False
) -> RekeyReport:
    """Re-encrypt the secrets stored under ``data`` from ``old_key`` to ``new_key``.

    Nothing is written when ``dry_run`` is set or when a value cannot be read
    with either key (``report.unreadable`` lists where).
    """
    data = Path(data)
    report = RekeyReport()
    convert = _Converter(fernet_for_key(old_key), fernet_for_key(new_key), report)

    data.mkdir(parents=True, exist_ok=True)
    with FileLock(str(data / f"{PROVIDERS}.lock")):
        providers = _read_json(data / PROVIDERS, report)
        if providers is not None:
            for p in providers.get("providers", []):
                if isinstance(p, dict) and "bearer_token" in p:
                    where = f"{PROVIDERS}: provider {p.get('id', '?')}"
                    p["bearer_token"] = convert(p["bearer_token"], where)

        mfa = _read_json(data / ADMIN_MFA, report)
        if mfa is not None and "secret_enc" in mfa:
            mfa["secret_enc"] = convert(mfa["secret_enc"], f"{ADMIN_MFA}: secret")

        credentials = _read_credentials(data / CREDENTIALS, report)
        for row in credentials:
            for column in ("access_token", "refresh_token"):
                where = f"{CREDENTIALS}: {row['source']} {column}"
                row[column] = convert(row[column], where)

        if dry_run or not report.ok:
            return report
        if not (report.rotated or report.encrypted_plain):
            return report  # every value already uses the new key

        report.backup_dir = _backup(data, [PROVIDERS, ADMIN_MFA, CREDENTIALS, KEY_FILE])
        if credentials:
            conn = sqlite3.connect(str(data / CREDENTIALS))
            try:
                with conn:
                    conn.executemany(
                        "UPDATE oauth_credentials SET access_token = ?, "
                        "refresh_token = ? WHERE source = ?",
                        [
                            (r["access_token"], r["refresh_token"], r["source"])
                            for r in credentials
                        ],
                    )
            finally:
                conn.close()
        if providers is not None:
            atomic_write_json(data / PROVIDERS, providers, mode=0o600)
        if mfa is not None:
            atomic_write_json(data / ADMIN_MFA, mfa, mode=0o600)
        report.written = True
    log.info(
        "Encryption key rotated: %d values re-encrypted, %d plain values "
        "encrypted, %d already on the new key; backup in %s",
        report.rotated,
        report.encrypted_plain,
        report.already_new,
        report.backup_dir,
    )
    return report


def unreadable_secrets(data: Path, key: str) -> RekeyReport:
    """Which secrets stored under ``data`` ``key`` cannot decrypt.

    A dry rotation from the key to itself: nothing is written. ``unreadable``
    names their locations (never the values); ``damaged`` lists the files
    that cannot be read at all, whatever the key (BUG-57's boot check).
    """
    return rotate(data, key, key, dry_run=True)


def replace_key_file(data: Path, new_key: str) -> Path:
    """Write ``new_key`` to ``<data>/.encryption_key`` (0600) and return its path.

    Only for installs whose key comes from that file (ENCRYPTION_KEY unset).
    """
    path = Path(data) / KEY_FILE
    atomic_write_text(path, new_key, mode=0o600)
    return path
