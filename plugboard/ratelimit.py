# plugboard/ratelimit.py
from __future__ import annotations

import collections
import os
import time
from typing import Dict

_LIMIT = int(os.environ.get("RATE_LIMIT", "60"))
_WINDOW = 60.0  # seconds

_buckets: Dict[str, collections.deque] = {}

_call_count = 0
_PURGE_INTERVAL = 1000


def _purge_stale() -> None:
    """Remove IPs whose deque is empty or all timestamps are expired."""
    now = time.monotonic()
    stale = [ip for ip, dq in _buckets.items() if not dq or dq[-1] < now - _WINDOW]
    for ip in stale:
        del _buckets[ip]


def check_rate_limit(ip: str) -> bool:
    """Return True if the request is allowed, False if rate limit exceeded."""
    global _call_count

    if _LIMIT == 0:
        return True
    now = time.monotonic()
    dq = _buckets.setdefault(ip, collections.deque())
    # Evict timestamps older than the window
    while dq and dq[0] < now - _WINDOW:
        dq.popleft()
    if len(dq) >= _LIMIT:
        return False
    dq.append(now)

    # Periodic purge to prevent memory leak from stale IPs
    _call_count += 1
    if _call_count % _PURGE_INTERVAL == 0:
        _purge_stale()

    return True


# -- Per-client rate limiting ------------------------------------------------

_client_buckets: Dict[str, collections.deque] = {}


def check_client_rate_limit(client_id: str, rpm_limit: int) -> bool:
    """Return True if allowed, False if client exceeded their per-minute RPM limit.
    rpm_limit=0 means unlimited."""
    if rpm_limit <= 0:
        return True
    now = time.monotonic()
    dq = _client_buckets.setdefault(client_id, collections.deque())
    while dq and dq[0] < now - _WINDOW:
        dq.popleft()
    if len(dq) >= rpm_limit:
        return False
    dq.append(now)
    return True


# -- Brute-force protection (auth endpoints) ---------------------------------

_AUTH_LIMIT = 5  # max attempts per window
_AUTH_WINDOW = 60.0  # 1 minute window
_LOCKOUT_DURATION = 900.0  # 15 minutes lockout after exceeding limit
_auth_buckets: Dict[str, collections.deque] = {}
_lockouts: Dict[str, float] = {}


def check_auth_rate_limit(ip: str) -> bool:
    """Return True if auth attempt allowed, False if locked out.
    Tracks failed auth attempts per IP. After _AUTH_LIMIT failures
    within _AUTH_WINDOW, locks out the IP for _LOCKOUT_DURATION."""
    now = time.monotonic()

    # Check if currently locked out
    lockout_until = _lockouts.get(ip, 0)
    if lockout_until > now:
        return False

    # Clean expired lockout
    if ip in _lockouts and _lockouts[ip] <= now:
        del _lockouts[ip]

    return True


def record_auth_failure(ip: str) -> bool:
    """Record a failed auth attempt. Returns True if IP is now locked out."""
    now = time.monotonic()
    dq = _auth_buckets.setdefault(ip, collections.deque())
    # Evict old timestamps
    while dq and dq[0] < now - _AUTH_WINDOW:
        dq.popleft()
    dq.append(now)

    if len(dq) >= _AUTH_LIMIT:
        _lockouts[ip] = now + _LOCKOUT_DURATION
        _auth_buckets.pop(ip, None)
        return True
    return False


def get_lockout_remaining(ip: str) -> int:
    """Return seconds remaining in lockout, or 0 if not locked out."""
    now = time.monotonic()
    lockout_until = _lockouts.get(ip, 0)
    if lockout_until > now:
        return int(lockout_until - now)
    return 0
