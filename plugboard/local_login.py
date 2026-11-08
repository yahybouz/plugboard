"""Secrets that prove a browser belongs to the local operator (auto-login).

Auto-login trusted a loopback TCP peer plus browser-like headers. A container
or VM whose traffic ends on the host loopback, a forwarder in front of a
loopback-bound install, or a local process forging headers passed those
checks. It now also needs a secret only the operator holds:

- a login token, written at each server start to ``data/.login_token``
  (mode 0600) and opened by ``make dev`` as ``/login?token=...``. It is
  single-use: a successful login replaces it with a fresh one;
- then a "remember this browser" cookie, valid ``DEVICE_MAX_AGE`` and sent
  only to ``/login``. Only SHA-256 digests of these cookies are stored, in
  ``data/local_devices.json`` (mode 0600); deleting that file forgets every
  browser.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Dict

from plugboard.storage import atomic_write_json, atomic_write_text, data_dir

log = logging.getLogger(__name__)

TOKEN_FILE = ".login_token"
DEVICES_FILE = "local_devices.json"
DEVICE_COOKIE = "plugboard_local_device"
DEVICE_MAX_AGE = 30 * 24 * 3600
_MAX_DEVICES = 16
_LOCK = threading.Lock()


def token_path() -> Path:
    return data_dir() / TOKEN_FILE


def _devices_path() -> Path:
    return data_dir() / DEVICES_FILE


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def issue_token() -> str:
    """Write a fresh login token (0600) and return it."""
    token = secrets.token_urlsafe(32)
    atomic_write_text(token_path(), token + "\n", mode=0o600)
    return token


def consume_token(candidate: str) -> bool:
    """True when ``candidate`` is the current token, which is then replaced."""
    if not candidate:
        return False
    with _LOCK:
        try:
            stored = token_path().read_text(encoding="utf-8").strip()
        except OSError:
            return False
        if not stored or not hmac.compare_digest(
            stored.encode("utf-8"), candidate.encode("utf-8")
        ):
            return False
        try:
            issue_token()  # single use
        except OSError as exc:
            # Without a new token the used one would stay valid: refuse.
            log.error("Cannot replace the used login token: %s", exc)
            return False
    return True


def _load_devices() -> Dict[str, Dict[str, float]]:
    path = _devices_path()
    if not path.exists():
        return {}
    try:
        devices = json.loads(path.read_text(encoding="utf-8")).get("devices")
    except (OSError, ValueError, AttributeError) as exc:
        log.error("Failed to read %s: %s", path, exc)  # fail closed
        return {}
    return devices if isinstance(devices, dict) else {}


def remember_device() -> str:
    """Register a new browser and return the cookie value to give it."""
    nonce = secrets.token_urlsafe(32)
    now = time.time()
    with _LOCK:
        devices = {
            key: rec
            for key, rec in _load_devices().items()
            if now - float(rec.get("created_at", 0)) < DEVICE_MAX_AGE
        }
        devices[_digest(nonce)] = {"created_at": now}
        newest = sorted(devices.items(), key=lambda kv: kv[1]["created_at"])
        atomic_write_json(
            _devices_path(), {"devices": dict(newest[-_MAX_DEVICES:])}, mode=0o600
        )
    return nonce


def is_remembered(cookie: str) -> bool:
    """True when ``cookie`` belongs to a browser registered less than 30 days ago."""
    if not cookie:
        return False
    record = _load_devices().get(_digest(cookie))
    if not isinstance(record, dict):
        return False
    return time.time() - float(record.get("created_at", 0)) < DEVICE_MAX_AGE


def forget_devices() -> None:
    """Forget every remembered browser."""
    with _LOCK:
        if _devices_path().exists():
            atomic_write_json(_devices_path(), {"devices": {}}, mode=0o600)
