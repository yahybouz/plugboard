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
import hmac
import json
import logging
import re
import secrets
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from plugboard import admin_mfa, admin_sessions
from plugboard.config import SecretUnreadable, _decrypt_token, _encrypt_token
from plugboard.storage import StoreUnreadableError, atomic_write_json, data_dir

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


# Characters that print as nothing, or hide what follows them: format,
# control, private-use and surrogate code points, line and paragraph
# separators, every Default_Ignorable_Code_Point, and the braille blank.
# Two names would otherwise look the same.
_INVISIBLE_CATEGORIES = frozenset({"Cf", "Cc", "Co", "Cs", "Zl", "Zp"})
# Default_Ignorable_Code_Point, as listed in Unicode's
# DerivedCoreProperties.txt (checked against Unicode 15.0); ``unicodedata``
# does not expose it. It holds the soft hyphen, the combining grapheme
# joiner, the Hangul fillers, the Khmer inherent vowels, the Mongolian free
# variation selectors and vowel separator, the zero width and bidi controls,
# the variation selectors, the tags and the reserved points kept ignorable.
_DEFAULT_IGNORABLE = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
_BRAILLE_BLANK = 0x2800
_COMBINING = frozenset({"Mn", "Me"})


def _invisible(char: str) -> bool:
    point = ord(char)
    return (
        unicodedata.category(char) in _INVISIBLE_CATEGORIES
        or point == _BRAILLE_BLANK
        or any(low <= point <= high for low, high in _DEFAULT_IGNORABLE)
    )


def _mark_without_base(name: str) -> bool:
    """A combining mark that opens the name or sits on a space prints alone.

    Every other invisible base is refused on its own (``_invisible``).
    """
    return any(
        unicodedata.category(char) in _COMBINING and (i == 0 or name[i - 1] == " ")
        for i, char in enumerate(name)
    )


def _clean_name(name: str) -> str:
    """The trimmed name, or ``invalid_name``.

    Refused: an empty or too long name, a non-printable or invisible
    character, a combining mark at the start or on a space, two spaces in a
    row. Outer spaces are trimmed, never stored.
    """
    cleaned = name.strip() if isinstance(name, str) else ""
    if (
        not 1 <= len(cleaned) <= NAME_MAX
        or not cleaned.isprintable()
        or any(_invisible(char) for char in cleaned)
        or _mark_without_base(cleaned)
        or "  " in cleaned
    ):
        raise PersonError("invalid_name", _INVALID_NAME)
    return cleaned


def _fold(name: str) -> str:
    """The form two names are compared in: NFKC, then casefold.

    Full-width and compatibility forms collide with their plain form; the
    outer NFKC keeps the folded text in normal form too.
    """
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", name).casefold())


def create(name: str, *, created_by: str) -> dict:
    """Create a pending personal key; the raw key and secret are returned once."""
    name = _clean_name(name)
    with _LOCK:
        people, readable = _load()
        if not readable:
            raise PeopleStoreUnreadable("admin_people.json is unreadable")
        folded = _fold(name)
        if any(
            p.get("revoked_at") is None and _fold(str(p.get("name", ""))) == folded
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


def is_readable() -> bool:
    """The store can be read (a missing file is readable and empty)."""
    return _load()[1]


def _hash_bytes(record: dict) -> bytes:
    stored = record.get("key_hash")
    return stored.encode("utf-8") if isinstance(stored, str) else b""


def find_by_key(raw_key: str) -> Optional[dict]:
    """The live record whose ``key_hash`` matches ``raw_key``, else ``None``.

    Every record is compared in constant time, with no early exit; a revoked
    record never matches and an unreadable store matches nothing.
    """
    if not isinstance(raw_key, str) or not raw_key.startswith(KEY_PREFIX):
        return None
    people, readable = _load()
    if not readable:
        return None
    digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest().encode("ascii")
    found = None
    for person in people:
        if hmac.compare_digest(_hash_bytes(person), digest) and found is None:
            found = person
    if found is None or found.get("revoked_at"):
        return None
    return found


def get(person_id: str) -> Optional[dict]:
    """The stored record (revoked included), or ``None`` (absent, bad id, unreadable)."""
    if not isinstance(person_id, str) or not ID_PATTERN.fullmatch(person_id):
        return None
    people, readable = _load()
    if not readable:
        return None
    return next((p for p in people if p.get("id") == person_id), None)


def verify_code(person_id: str, code: str) -> str:
    """Check ``code`` against that person's own secret, once.

    ``"confirmed"`` for the first accepted code (the key turns active),
    ``"ok"`` for a later one, ``"refused"`` otherwise: malformed, wrong,
    replayed or older than the last accepted step, person revoked or absent,
    store unreadable, secret not encrypted or not decryptable, or the store
    could not be written. The step is recorded before the answer.

    A shortcut kept for the tests: the server calls ``check_code``, which
    tells a fault of the server from a wrong code.
    """
    outcome = check_code(person_id, code)
    return "refused" if outcome == "unavailable" else outcome


def check_code(person_id: str, code: str) -> str:
    """``verify_code``, telling a fault of the server from a wrong code.

    ``"unavailable"`` when the store is unreadable or cannot be written, or
    the person's secret or state is malformed: no code could be checked, so
    the caller must refuse without counting a wrong code. Every other answer
    is the one of ``verify_code``.
    """
    if not isinstance(person_id, str) or not ID_PATTERN.fullmatch(person_id):
        return "refused"
    with _LOCK:
        people, readable = _load()
        if not readable:
            return "unavailable"
        record = next((p for p in people if p.get("id") == person_id), None)
        if record is None or record.get("revoked_at"):
            return "refused"
        stored = record.get("totp_secret_enc")
        if not isinstance(stored, str) or not stored.startswith("enc:"):
            log.error("Personal TOTP secret of %s is not encrypted", person_id)
            return "unavailable"
        try:
            secret = _decrypt_token(stored)
        except SecretUnreadable:
            log.error("Personal TOTP secret of %s cannot be decrypted", person_id)
            return "unavailable"
        # The state is checked before the code, so a malformed record answers
        # the same whether the code is right or wrong.
        last = record.get("last_step")
        if last is not None and (isinstance(last, bool) or not isinstance(last, int)):
            log.error("Personal TOTP state of %s is malformed", person_id)
            return "unavailable"
        step = admin_mfa.matching_step(secret, code, at=time.time())
        if step is None:
            return "refused"
        if last is not None and step <= last:
            log.warning(
                "Personal TOTP code replayed (time-step already used) for %s",
                person_id,
            )
            return "refused"
        now = _now()
        outcome = "ok" if record.get("totp_confirmed") else "confirmed"
        record["last_step"] = step
        record["last_used_at"] = now
        if outcome == "confirmed":
            record["totp_confirmed"] = True
            record["confirmed_at"] = now
        try:
            _save(people)
        except Exception as exc:
            log.error(
                "Failed to record the TOTP step of %s: %s",
                person_id,
                type(exc).__name__,
            )
            return "unavailable"
    return outcome


def revoke(person_id: str, *, revoked_by: str) -> Optional[tuple[dict, bool]]:
    """Revoke a personal key and cut its sessions; idempotent.

    ``None`` for an id outside the pattern or unknown, else ``(view,
    changed)``: only the first call stamps ``revoked_at``/``revoked_by`` and
    changes the ``generation``. Sessions are cut on every call; a session
    store that cannot be rewritten is logged and ignored, since the revoked
    status already refuses them. An unreadable people store raises
    ``PeopleStoreUnreadable``.
    """
    if not isinstance(person_id, str) or not ID_PATTERN.fullmatch(person_id):
        return None
    with _LOCK:
        people, readable = _load()
        if not readable:
            raise PeopleStoreUnreadable("admin_people.json is unreadable")
        record = next((p for p in people if p.get("id") == person_id), None)
        if record is None:
            return None
        changed = not record.get("revoked_at")
        if changed:
            record["revoked_at"] = _now()
            record["revoked_by"] = revoked_by
            record["generation"] = secrets.token_hex(16)
            _save(people)
        view = public_view(record)
    try:
        admin_sessions.revoke_person(person_id)
    except (OSError, StoreUnreadableError) as exc:
        log.error(
            "Sessions of %s not cut: %s (refused by the revoked status)",
            person_id,
            type(exc).__name__,
        )
    return view, changed
