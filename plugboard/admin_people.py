"""Named personal dashboard keys (PB-AUTH-01).

Each person who runs the instance gets their own admin key and their own TOTP
secret, so an audit entry names who acted and one key can be revoked alone.

Records live in ``data/admin_people.json`` (mode 0600) as
``{"people": [ ... ]}``, one record per person, in creation order. The raw key
and the TOTP secret are handed out once, at creation: the store keeps only the
key's SHA-256 (``key_hash``), its first characters for display
(``key_prefix``) and the secret encrypted like ``admin_mfa`` encrypts its own
(``totp_secret_enc``).

An unreadable file refuses every personal key: nothing is created and nothing
is listed until it is fixed, and the error is logged by its type only (a
decoding error can quote the file's bytes).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from plugboard import admin_mfa
from plugboard.config import _encrypt_token
from plugboard.storage import atomic_write_json, data_dir

log = logging.getLogger(__name__)

KEY_PREFIX = "pba_"
NAME_MAX = 64
ID_PATTERN = re.compile(r"ap_[0-9a-f]{12}")
VIEW_FIELDS = frozenset(
    {
        "id",
        "name",
        "key_prefix",
        "status",
        "created_at",
        "created_by",
        "confirmed_at",
        "last_used_at",
        "revoked_at",
        "revoked_by",
    }
)

_INVALID_NAME = "Name must be 1 to 64 printable characters"
_NAME_TAKEN = "A live personal key already has this name"
_LOCK = threading.Lock()


class PersonError(ValueError):
    """A request the store refuses; ``code`` is ``invalid_name`` or ``name_taken``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PeopleStoreUnreadable(RuntimeError):
    """``admin_people.json`` cannot be read: every personal key is refused."""


def _path() -> Path:
    return data_dir() / "admin_people.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> tuple[list[dict], bool]:
    """(people, readable): a missing file is readable and empty.

    Anything else than ``{"people": [<object>, ...]}`` is unreadable: the
    store fails closed and is never rewritten over what it could not parse.
    """
    path = _path()
    try:
        if not path.exists():
            return [], True
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # The type only: a decoding error can quote the file's bytes.
        log.error("Failed to read %s: %s", path, type(exc).__name__)
        return [], False
    people = data.get("people") if isinstance(data, dict) else None
    if not isinstance(people, list) or not all(isinstance(p, dict) for p in people):
        log.error("Failed to read %s: not a list of people", path)
        return [], False
    return people, True


def _save(people: list[dict]) -> None:
    atomic_write_json(_path(), {"people": people}, mode=0o600)


def status_of(record: dict) -> str:
    if record.get("revoked_at"):
        return "revoked"
    if record.get("totp_confirmed"):
        return "active"
    return "pending"


def public_view(record: dict) -> dict:
    """What an admin may see of a person: never a hash or a secret."""
    view = {field: record.get(field) for field in VIEW_FIELDS}
    view["status"] = status_of(record)
    return view


def _clean_name(name: str) -> str:
    cleaned = name.strip() if isinstance(name, str) else ""
    if not 1 <= len(cleaned) <= NAME_MAX or not cleaned.isprintable():
        raise PersonError("invalid_name", _INVALID_NAME)
    return cleaned


def create(name: str, *, created_by: str) -> dict:
    """Create a pending personal key; the raw key and secret are returned once."""
    name = _clean_name(name)
    with _LOCK:
        people, readable = _load()
        if not readable:
            raise PeopleStoreUnreadable("admin_people.json is unreadable")
        folded = name.casefold()
        if any(
            p.get("revoked_at") is None and str(p.get("name", "")).casefold() == folded
            for p in people
        ):
            raise PersonError("name_taken", _NAME_TAKEN)
        taken = {p.get("id") for p in people}
        person_id = "ap_" + secrets.token_hex(6)
        while person_id in taken:
            person_id = "ap_" + secrets.token_hex(6)
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        secret = admin_mfa.generate_secret()
        record = {
            "id": person_id,
            "name": name,
            "key_hash": hashlib.sha256(key.encode("utf-8")).hexdigest(),
            "key_prefix": key[:10],
            "totp_secret_enc": _encrypt_token(secret),
            "totp_confirmed": False,
            "last_step": None,
            "generation": secrets.token_hex(16),
            "created_at": _now(),
            "created_by": created_by,
            "confirmed_at": None,
            "last_used_at": None,
            "revoked_at": None,
            "revoked_by": None,
        }
        _save(people + [record])
    return {
        "person": public_view(record),
        "key": key,
        "totp_secret": secret,
        "otpauth_uri": admin_mfa.provisioning_uri(secret, account=name),
    }


def list_people() -> list[dict]:
    """Every person in creation order, as public views."""
    people, readable = _load()
    if not readable:
        raise PeopleStoreUnreadable("admin_people.json is unreadable")
    return [public_view(p) for p in people]
