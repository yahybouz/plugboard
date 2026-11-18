"""Authentication decisions of the auth core, frozen before the move (PLU-97).

Every case builds a synthetic request; keys, TOTP codes and sessions are made
by the code under test in private files (no real value anywhere). For each
function of FUNCTIONS it records, call after call, what the function decides
(value, or HTTP status and detail; admin principal and its name, MFA proof,
key id), the audit entries and the log lines written (``plugboard.app`` and
the modules it calls) and the lockout counters left behind, each (case,
function) in a fresh world. Written once on 919f01f with
``python -m tests.auth_decisions``, compared by
``tests/test_auth_core_guard.py``; never regenerated during PLU-97.

The clock: TOTP codes are checked against a fixed time (``admin_mfa.time``
and ``admin_people.time`` are replaced, as the MFA tests do); sessions are
signed and checked on the real clock, ``plugboard.app`` and the standard
library modules it imports are never patched. The expired and future
sessions are signed here, with the test admin key.

Log lines are kept formatted, every generated value (keys, sessions, TOTP
codes, the world's directory) replaced by a marker: a secret that reaches a
log shows as a marker where 919f01f had none, and the guard fails.

Not in the table: ``_should_audit_refusal``, ``_issue_local_login_token`` and
``_prove_person_code(via="login")`` (tests/test_auth_auto_login.py and the
personal key login tests cover them); a moved function that calls another
directly instead of through ``core.`` only shows here when the callee is
patched, the static ``LATE_BOUND`` check of tests/test_auth_core_split.py
refuses the rest.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
import time
import types
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from tests import people_helpers

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "auth_decisions.json"
ADMIN_KEY = "adminkey-decisions-0123456789"  # synthetic
API_KEY = "apikey-decisions-0123456789"  # synthetic
# A client key shape that no store holds, and values no test credential has.
UNKNOWN_CLIENT_KEY = "pb_" + "0" * 64  # synthetic
UNKNOWN_LEGACY_KEY = "lle_" + "0" * 64  # synthetic
GARBAGE_BEARER = "not-a-key-decisions"  # synthetic
WRONG_LOCAL_TOKEN = "wrong-login-token-decisions"  # synthetic
UNKNOWN_DEVICE = "unknown-device-decisions"  # synthetic
TOTP_HEADER = "x-plugboard-admin-totp"
SESSION_COOKIE = "plugboard_admin_session"
DEVICE_COOKIE = "plugboard_local_device"
# Functions of plugboard.app, called with the case's request. "a+b" calls a
# then b on the same request (the order of an admin route that changes MFA),
# stopping at the first refusal. "a[body]" calls a(request, code) with the
# case's TOTP code taken off the header and passed as the request body's; when
# the case sets body_totp, the header keeps its code and the body gets that one.
FUNCTIONS = (
    "_key_scope",
    "_person_bearer_scope",
    "_rate_limit_scope",
    "_resolve_client",
    "_admin_request_auth",
    "_require_admin",
    "_check_dashboard_auth",
    "_refuse_cross_site_session",
    "_require_mfa_step_up",
    "_require_mfa_step_up[body]",
    "_require_admin+_require_mfa_step_up",
    "_auto_login_refusal",
    "_local_login_proof",
    "_should_use_secure_cookie",
)


def _memory_dir():
    """/dev/shm when writable with 64 MiB free at import, else None (disk).

    The stores are written with fsync, which makes a disk-backed run several
    times slower. A world weighs a few KiB and is removed when it closes; a
    /dev/shm that fills up during the run fails the guard (ENOSPC), and a
    killed process leaves its ``plu97-auth-*`` there until the next reboot.
    """
    try:
        stat = os.statvfs("/dev/shm")
    except OSError:
        return None
    free = stat.f_bavail * stat.f_frsize
    return "/dev/shm" if os.access("/dev/shm", os.W_OK) and free >= 64 << 20 else None


_WORLD_DIR = _memory_dir()
# Browser headers of a dashboard opened from the address bar.
NAV = (("sec-fetch-site", "none"), ("sec-fetch-mode", "navigate"))
CROSS_SITE = (("sec-fetch-site", "cross-site"),)


@dataclasses.dataclass(frozen=True)
class Case:
    name: str
    keys: str = "both"  # "both" | "no_api_key" | "no_admin_key" | "none"
    # "off" | "required" (admin secret enrolled, flag set) | "enrolled" (secret,
    # no flag) | "flag_only" (flag, no secret) | "unreadable" (flag, the MFA
    # store damaged after the sessions were opened)
    mfa: str = "off"
    bearer: str = ""  # "" | "admin" | "admin_raw" (no "Bearer ") | "admin_lower"
    #                   | "client" | "client_revoked" | "client_unknown"
    #                   | "client_reserved" (client_id "admin") | "client_blank"
    #                   | "client_legacy" ("lle_" key) | "client_legacy_unknown"
    #                   | "garbage" | "person" | "person_revoked"
    totp: str = ""  # "" | "admin" | "admin_wrong" | "person" | "person_wrong"
    body_totp: str = ""  # as totp, or "absent": the body code of "[body]" calls
    cookie: str = ""  # "" | "admin" | "admin_no_mfa" | "person" | "person_revoked"
    #                   | "person_rekeyed" | "forged" | "expired" | "future"
    #                   | "unregistered" | "pre_enrollment" | "malformed_no_dots"
    #                   | "malformed_issued_at" | "malformed_empty_nonce"
    #                   | "malformed_empty_signature"
    people: str = ""  # "" | "malformed" (hand-edited id) | "unreadable"
    #                   | "unavailable" (admin_people.check_code answers so)
    local: str = ""  # "" | "token" | "token_wrong" | "device" | "device_unknown"
    prelock: tuple = ()  # counters locked beforehand: "admin" | "person" | "ip"
    method: str = "GET"
    headers: tuple = ()  # extra (name, value): Sec-Fetch-*, Origin, proxy headers
    peer: str = "127.0.0.1"
    host: str = "localhost:8000"
    query: str = ""
    auto_login: str = ""  # PLUGBOARD_LOCAL_AUTO_LOGIN ("" = unset)
    bind_host: str = "127.0.0.1"  # PLUGBOARD_BIND_HOST ("" = unset)
    cookie_secure: str = "auto"  # plugboard.app._COOKIE_SECURE_MODE
    repeat: int = 1  # calls in a row (lockout and single-use cases)


CASES = (
    # Rows 1 to 52 of the plan (task 2), in its order.
    Case("no_credentials"),
    Case("admin_key", bearer="admin"),
    Case("client_key", bearer="client"),
    Case("client_key_revoked", bearer="client_revoked"),
    Case("client_key_unknown", bearer="client_unknown"),
    Case("garbage_bearer", bearer="garbage"),
    Case("no_api_key_anonymous", keys="no_api_key"),
    Case("no_api_key_client_key", keys="no_api_key", bearer="client"),
    Case("no_api_key_client_unknown", keys="no_api_key", bearer="client_unknown"),
    Case("no_api_key_admin_key", keys="no_api_key", bearer="admin"),
    Case("keyless_anonymous", keys="none"),
    Case("no_admin_key_admin_bearer", keys="no_admin_key", bearer="admin"),
    Case("admin_session", cookie="admin"),
    Case("admin_session_forged", cookie="forged"),
    Case("admin_session_expired", cookie="expired"),
    Case("admin_session_unregistered", cookie="unregistered"),
    Case("client_key_over_admin_session", bearer="client", cookie="admin"),
    Case("person_session", cookie="person"),
    Case("person_session_after_revocation", cookie="person_revoked"),
    Case("mfa_session_without_mfa", mfa="required", cookie="admin_no_mfa"),
    Case("mfa_session_with_mfa", mfa="required", cookie="admin"),
    Case("mfa_admin_key_no_code", mfa="required", bearer="admin"),
    Case("mfa_admin_key_good_code", mfa="required", bearer="admin", totp="admin"),
    Case(
        "mfa_admin_key_wrong_code", mfa="required", bearer="admin", totp="admin_wrong"
    ),
    Case(
        "mfa_admin_key_lockout",
        mfa="required",
        bearer="admin",
        totp="admin_wrong",
        repeat=7,
    ),
    Case("mfa_step_up_session_no_code", mfa="required", cookie="admin"),
    Case("mfa_step_up_wrong_code", mfa="required", cookie="admin", totp="admin_wrong"),
    Case("person_key_no_code", bearer="person"),
    Case("person_key_good_code", bearer="person", totp="person"),
    Case("person_key_wrong_code", bearer="person", totp="person_wrong"),
    Case("person_key_lockout", bearer="person", totp="person_wrong", repeat=7),
    Case("person_key_revoked", bearer="person_revoked", totp="person"),
    Case(
        "person_key_without_admin_key",
        keys="no_admin_key",
        bearer="person",
        totp="person",
    ),
    Case("person_key_admin_code", mfa="required", bearer="person", totp="admin"),
    Case("session_post_cross_site", cookie="admin", method="POST", headers=CROSS_SITE),
    Case(
        "session_post_same_site",
        cookie="admin",
        method="POST",
        headers=(("sec-fetch-site", "same-site"),),
    ),
    Case(
        "session_post_same_origin",
        cookie="admin",
        method="POST",
        headers=(("sec-fetch-site", "same-origin"),),
    ),
    Case(
        "session_post_origin_other_host",
        cookie="admin",
        method="POST",
        headers=(("origin", "http://other.example"),),
    ),
    Case(
        "session_post_origin_this_host",
        cookie="admin",
        method="POST",
        headers=(("origin", "http://localhost:8000"),),
    ),
    Case(
        "admin_key_post_cross_site", bearer="admin", method="POST", headers=CROSS_SITE
    ),
    Case("auto_login_local_navigation", headers=NAV),
    Case("auto_login_off", headers=NAV, auto_login="0"),
    Case("auto_login_unknown_value", headers=NAV, auto_login="maybe", repeat=2),
    Case("auto_login_public_bind", headers=NAV, bind_host="0.0.0.0"),
    Case("auto_login_on_public_bind", headers=NAV, auto_login="1", bind_host="0.0.0.0"),
    Case("auto_login_proxied", headers=NAV + (("x-forwarded-for", "192.0.2.20"),)),
    Case("auto_login_remote_peer", headers=NAV, peer="192.0.2.10"),
    Case("auto_login_remote_host", headers=NAV, host="plugboard.example"),
    Case(
        "auto_login_not_navigation",
        headers=(("sec-fetch-site", "none"), ("sec-fetch-mode", "cors")),
    ),
    Case("auto_login_logged_out", headers=NAV, query="logged_out=1"),
    Case("auto_login_mfa_required", headers=NAV, mfa="required"),
    Case("auto_login_no_admin_key", headers=NAV, keys="no_admin_key"),
    # Preflight n° 4: the other MFA states.
    Case("mfa_enrolled_admin_key", mfa="enrolled", bearer="admin"),
    Case("mfa_enrolled_session_no_code", mfa="enrolled", cookie="admin_no_mfa"),
    Case(
        "mfa_enrolled_session_good_code",
        mfa="enrolled",
        cookie="admin_no_mfa",
        totp="admin",
    ),
    Case("mfa_flag_only_admin_key", mfa="flag_only", bearer="admin"),
    Case("mfa_flag_only_session", mfa="flag_only", cookie="admin_no_mfa"),
    Case(
        "mfa_unreadable_admin_key_code", mfa="unreadable", bearer="admin", totp="admin"
    ),
    Case("mfa_unreadable_session", mfa="unreadable", cookie="admin"),
    Case(
        "mfa_enrolled_pre_enrollment_session", mfa="enrolled", cookie="pre_enrollment"
    ),
    Case(
        "mfa_required_pre_enrollment_session", mfa="required", cookie="pre_enrollment"
    ),
    Case("mfa_step_up_good_code", mfa="required", cookie="admin", totp="admin"),
    # Preflight n° 5: personal keys.
    Case("person_rekeyed_session", cookie="person_rekeyed"),
    Case("person_record_malformed", bearer="person", totp="person", people="malformed"),
    Case(
        "person_store_unavailable", bearer="person", totp="person", people="unavailable"
    ),
    Case(
        "person_store_unreadable", bearer="person", totp="person", people="unreadable"
    ),
    Case("person_session_store_unreadable", cookie="person", people="unreadable"),
    Case(
        "person_session_post_cross_site",
        cookie="person",
        method="POST",
        headers=CROSS_SITE,
    ),
    Case(
        "person_key_over_admin_session",
        bearer="person",
        totp="person_wrong",
        cookie="admin",
    ),
    # Preflight n° 6: cross-site requests.
    Case(
        "admin_key_and_session_post_cross_site",
        bearer="admin",
        cookie="admin",
        method="POST",
        headers=CROSS_SITE,
    ),
    Case("session_get_cross_site", cookie="admin", headers=CROSS_SITE),
    Case(
        "session_delete_cross_site", cookie="admin", method="DELETE", headers=CROSS_SITE
    ),
    Case(
        "session_patch_cross_site", cookie="admin", method="PATCH", headers=CROSS_SITE
    ),
    Case("session_put_cross_site", cookie="admin", method="PUT", headers=CROSS_SITE),
    Case(
        "session_post_origin_null",
        cookie="admin",
        method="POST",
        headers=(("origin", "null"),),
    ),
    Case(
        "session_post_origin_default_port",
        cookie="admin",
        method="POST",
        headers=(("origin", "http://localhost"),),
    ),
    Case(
        "session_post_origin_default_port_match",
        cookie="admin",
        method="POST",
        host="localhost",
        headers=(("origin", "http://localhost"),),
    ),
    Case(
        "session_post_origin_https_port_443",
        cookie="admin",
        method="POST",
        host="plugboard.example:443",
        headers=(("origin", "https://plugboard.example"),),
    ),
    Case(
        "session_post_fetch_site_wins_over_origin",
        cookie="admin",
        method="POST",
        headers=(("sec-fetch-site", "same-origin"), ("origin", "http://other.example")),
    ),
    Case("session_post_no_browser_headers", cookie="admin", method="POST"),
    Case(
        "client_key_over_admin_session_post_cross_site",
        bearer="client",
        cookie="admin",
        method="POST",
        headers=CROSS_SITE,
    ),
    Case("garbage_bearer_with_admin_session", bearer="garbage", cookie="admin"),
    # Client keys the store holds but refuses, and the older key prefix.
    Case("client_key_reserved_client_id", bearer="client_reserved"),
    Case("client_key_blank_client_id", bearer="client_blank"),
    Case("no_api_key_client_reserved", keys="no_api_key", bearer="client_reserved"),
    Case("client_key_legacy_prefix", bearer="client_legacy"),
    Case(
        "no_api_key_legacy_unknown", keys="no_api_key", bearer="client_legacy_unknown"
    ),
    # Preflight n° 7: auto-login, the Host and peer parsers, the local secret.
    Case("auto_login_ipv6_host", headers=NAV, host="[::1]:8000", peer="::1"),
    Case("auto_login_ipv6_peer", headers=NAV, peer="::1"),
    Case("auto_login_mapped_peer", headers=NAV, peer="::ffff:127.0.0.1"),
    Case("auto_login_mapped_remote_peer", headers=NAV, peer="::ffff:192.0.2.10"),
    Case("auto_login_host_without_port", headers=NAV, host="localhost"),
    Case("auto_login_malformed_host", headers=NAV, host="[::1"),
    Case("auto_login_bind_host_unset", headers=NAV, bind_host=""),
    Case("auto_login_bind_host_unset_on", headers=NAV, bind_host="", auto_login="1"),
    Case("auto_login_bind_ipv6_loopback", headers=NAV, bind_host="::1"),
    Case("auto_login_explicit_auto", headers=NAV, auto_login="auto"),
    Case(
        "auto_login_same_site",
        headers=(("sec-fetch-site", "same-site"), ("sec-fetch-mode", "navigate")),
    ),
    Case(
        "auto_login_same_origin",
        headers=(("sec-fetch-site", "same-origin"), ("sec-fetch-mode", "navigate")),
    ),
    Case("auto_login_local_token", headers=NAV, local="token"),
    Case("auto_login_local_token_reused", headers=NAV, local="token", repeat=2),
    Case("auto_login_local_token_wrong", headers=NAV, local="token_wrong"),
    Case("auto_login_local_device", headers=NAV, local="device"),
    Case("auto_login_local_device_unknown", headers=NAV, local="device_unknown"),
    # Preflight n° 8: smaller paths.
    Case(
        "mfa_step_up_lockout",
        mfa="required",
        cookie="admin",
        totp="admin_wrong",
        repeat=7,
    ),
    Case("admin_session_future", cookie="future"),
    Case("admin_key_raw_header", bearer="admin_raw"),
    Case("admin_key_lowercase_bearer", bearer="admin_lower"),
    Case("keyless_client_key", keys="none", bearer="client"),
    Case("keyless_client_unknown", keys="none", bearer="client_unknown"),
    Case("keyless_admin_bearer", keys="none", bearer="admin"),
    Case("no_admin_key_session", keys="no_admin_key", cookie="admin"),
    Case("secure_cookie_forced_on", cookie_secure="true"),
    Case("secure_cookie_forced_off", cookie_secure="false", host="plugboard.example"),
    Case("secure_cookie_auto_test_host", host="test"),
    # Preflight n° 2: which counter a refusal locks, and which one it reads.
    Case(
        "mfa_admin_key_good_code_admin_locked",
        mfa="required",
        bearer="admin",
        totp="admin",
        prelock=("admin",),
    ),
    Case(
        "mfa_admin_key_good_code_person_locked",
        mfa="required",
        bearer="admin",
        totp="admin",
        prelock=("person",),
    ),
    Case(
        "mfa_admin_key_good_code_address_locked",
        mfa="required",
        bearer="admin",
        totp="admin",
        prelock=("ip",),
    ),
    Case(
        "person_key_good_code_admin_locked",
        bearer="person",
        totp="person",
        prelock=("admin",),
    ),
    Case(
        "person_key_good_code_person_locked",
        bearer="person",
        totp="person",
        prelock=("person",),
    ),
    Case(
        "mfa_step_up_good_code_admin_locked",
        mfa="required",
        cookie="admin",
        totp="admin",
        prelock=("admin",),
    ),
    Case(
        "mfa_admin_key_code_replayed",
        mfa="required",
        bearer="admin",
        totp="admin",
        repeat=2,
    ),
    Case("person_key_code_replayed", bearer="person", totp="person", repeat=2),
    # Task 2 security review, findings 1 to 4, 7 and 9.
    # One case per proxy header, written out (not read from _PROXY_HEADERS).
    Case(
        "auto_login_proxied_forwarded", headers=NAV + (("forwarded", "for=192.0.2.20"),)
    ),
    Case("auto_login_proxied_via", headers=NAV + (("via", "1.1 proxy.example"),)),
    Case(
        "auto_login_proxied_x_forwarded_host",
        headers=NAV + (("x-forwarded-host", "localhost:8000"),),
    ),
    Case(
        "auto_login_proxied_x_forwarded_proto",
        headers=NAV + (("x-forwarded-proto", "https"),),
    ),
    Case(
        "auto_login_proxied_x_forwarded_port",
        headers=NAV + (("x-forwarded-port", "443"),),
    ),
    Case(
        "auto_login_proxied_x_forwarded_server",
        headers=NAV + (("x-forwarded-server", "proxy.example"),),
    ),
    Case(
        "auto_login_proxied_x_original_forwarded_for",
        headers=NAV + (("x-original-forwarded-for", "192.0.2.20"),),
    ),
    Case("auto_login_proxied_x_real_ip", headers=NAV + (("x-real-ip", "192.0.2.20"),)),
    Case(
        "auto_login_proxied_x_client_ip",
        headers=NAV + (("x-client-ip", "192.0.2.20"),),
    ),
    Case(
        "auto_login_proxied_x_cluster_client_ip",
        headers=NAV + (("x-cluster-client-ip", "192.0.2.20"),),
    ),
    Case(
        "auto_login_proxied_cf_connecting_ip",
        headers=NAV + (("cf-connecting-ip", "192.0.2.20"),),
    ),
    Case(
        "auto_login_proxied_true_client_ip",
        headers=NAV + (("true-client-ip", "192.0.2.20"),),
    ),
    Case(
        "auto_login_proxied_fly_client_ip",
        headers=NAV + (("fly-client-ip", "192.0.2.20"),),
    ),
    Case(
        "auto_login_proxied_fastly_client_ip",
        headers=NAV + (("fastly-client-ip", "192.0.2.20"),),
    ),
    # Two refusal reasons at once: the first one checked is reported.
    Case(
        "auto_login_proxied_remote_peer",
        headers=NAV + (("via", "1.1 proxy.example"),),
        peer="192.0.2.10",
    ),
    Case(
        "auto_login_off_no_admin_key",
        headers=NAV,
        auto_login="0",
        keys="no_admin_key",
    ),
    Case(
        "auto_login_logged_out_proxied",
        headers=NAV + (("x-forwarded-for", "192.0.2.20"),),
        query="logged_out=1",
    ),
    # Personal sessions and keys while admin MFA is required.
    Case("mfa_required_person_session", mfa="required", cookie="person"),
    Case(
        "mfa_required_person_session_post_cross_site",
        mfa="required",
        cookie="person",
        method="POST",
        headers=CROSS_SITE,
    ),
    Case(
        "mfa_required_person_key_good_code",
        mfa="required",
        bearer="person",
        totp="person",
    ),
    # A missing code is answered before the lockout is read.
    Case(
        "mfa_admin_key_no_code_admin_locked",
        mfa="required",
        bearer="admin",
        prelock=("admin",),
    ),
    Case(
        "mfa_step_up_no_code_admin_locked",
        mfa="required",
        cookie="admin",
        prelock=("admin",),
    ),
    Case("person_key_no_code_person_locked", bearer="person", prelock=("person",)),
    Case("person_record_malformed_no_code", bearer="person", people="malformed"),
    # Session cookies that do not have the token's shape.
    Case("admin_session_malformed_no_dots", cookie="malformed_no_dots"),
    Case("admin_session_malformed_issued_at", cookie="malformed_issued_at"),
    Case("admin_session_malformed_empty_nonce", cookie="malformed_empty_nonce"),
    Case("admin_session_malformed_empty_signature", cookie="malformed_empty_signature"),
    # Task 2 re-review, N1: the body's code comes before the header's.
    Case(
        "mfa_step_up_body_wrong_header_good",
        mfa="required",
        cookie="admin",
        totp="admin",
        body_totp="admin_wrong",
    ),
    Case(
        "mfa_step_up_body_good_header_wrong",
        mfa="required",
        cookie="admin",
        totp="admin_wrong",
        body_totp="admin",
    ),
    Case(
        "mfa_step_up_body_absent_header_good",
        mfa="required",
        cookie="admin",
        totp="admin",
        body_totp="absent",
    ),
    Case(
        "mfa_step_up_body_absent_header_wrong",
        mfa="required",
        cookie="admin",
        totp="admin_wrong",
        body_totp="absent",
    ),
    Case(
        "mfa_step_up_body_good_header_good",
        mfa="required",
        cookie="admin",
        totp="admin",
        body_totp="admin",
    ),
    Case(
        "mfa_admin_key_body_wrong_header_good",
        mfa="required",
        bearer="admin",
        totp="admin",
        body_totp="admin_wrong",
    ),
    Case(
        "mfa_enrolled_step_up_body_wrong_header_good",
        mfa="enrolled",
        cookie="admin_no_mfa",
        totp="admin",
        body_totp="admin_wrong",
    ),
)


# The auth core's logger, then those of the modules it calls: a line that
# moves to another logger, changes level or message, appears or goes away is
# a change.
LOGGERS = (
    "plugboard.app",
    "plugboard.admin_mfa",
    "plugboard.admin_people",
    "plugboard.admin_sessions",
    "plugboard.apikeys",
    "plugboard.config",
    "plugboard.local_login",
    "plugboard.storage",
)


class _LogRecorder(logging.Handler):
    """[logger, level, unformatted message, formatted message] of each line."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(
            [record.name, record.levelname, str(record.msg), record.getMessage()]
        )


@contextlib.contextmanager
def _logs():
    """Every level of LOGGERS, kept off the other handlers, whatever an
    earlier test or the app's logging setup left on them."""
    recorder = _LogRecorder()
    saved = []
    for name in LOGGERS:
        logger = logging.getLogger(name)
        saved.append((logger, logger.level, logger.disabled, logger.propagate))
        logger.setLevel(logging.DEBUG)
        logger.disabled = False
        logger.propagate = False
        logger.addHandler(recorder)
    try:
        yield recorder
    finally:
        for logger, level, disabled, propagate in saved:
            logger.removeHandler(recorder)
            logger.setLevel(level)
            logger.disabled = disabled
            logger.propagate = propagate


@contextlib.contextmanager
def _world(case: Case):
    """Private stores, a fixed TOTP clock, the case's keys and environment,
    and recorders of the audit entries and log lines."""
    import plugboard.app as app_module
    from plugboard import admin_mfa, apikeys, local_login

    with (
        pytest.MonkeyPatch.context() as mp,
        tempfile.TemporaryDirectory(prefix="plu97-auth-", dir=_WORLD_DIR) as tmp,
    ):
        steps = people_helpers.isolate(Path(tmp), mp)
        env = next(steps)
        try:
            # The same frozen TOTP clock as the ``adm`` fixture of
            # tests/test_spec_v7_residuals_parent.py.
            mp.setattr(
                admin_mfa, "time", types.SimpleNamespace(time=lambda: env.clock.t)
            )
            keys_file = Path(tmp) / "api_keys.json"
            keys_file.write_text('{"keys": []}', encoding="utf-8")
            mp.setattr(apikeys, "_DATA_DIR", Path(tmp))
            mp.setattr(apikeys, "KEYS_FILE", keys_file)
            mp.setattr(apikeys, "KEYS_LOCK", Path(tmp) / "api_keys.json.lock")
            apikeys._invalidate_cache()
            mp.setattr(local_login, "token_path", lambda: Path(tmp) / "login_token")
            mp.setattr(
                local_login, "_devices_path", lambda: Path(tmp) / "local_devices.json"
            )
            # Credentials are made while both keys are set; the case's keys
            # apply afterwards (a session outlives a key removed later).
            mp.setattr(app_module, "_API_KEY", API_KEY)
            mp.setattr(app_module, "_ADMIN_KEY", ADMIN_KEY)
            mp.setattr(app_module, "_COOKIE_SECURE_MODE", case.cookie_secure)
            mp.setattr(app_module, "_refusal_audit_seen", {})
            mp.setattr(app_module, "_warned_auto_login_values", set())
            credentials = _credentials(case, env, mp, Path(tmp))
            mp.setattr(
                app_module,
                "_API_KEY",
                API_KEY if case.keys in ("both", "no_admin_key") else "",
            )
            mp.setattr(
                app_module,
                "_ADMIN_KEY",
                ADMIN_KEY if case.keys in ("both", "no_api_key") else "",
            )
            audit: list = []
            mp.setattr(
                app_module,
                "_audit",
                lambda action, **kw: audit.append([action, sorted(kw.items())]),
            )
            with _logs() as logs:
                yield app_module, credentials, audit, logs.lines
        finally:
            apikeys._invalidate_cache()
            next(steps, None)


def _sign(issued_at: int, nonce: str) -> str:
    """A session token signed like plugboard.app signs it, with the test key."""
    payload = f"{issued_at}.{nonce}"
    signature = hmac.new(
        ADMIN_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{signature}"


def _credentials(case: Case, env, mp, tmp: Path) -> dict:
    """Make the case's keys, people, MFA state and sessions with the code
    under test; return what the request presents and the markers of every
    generated value."""
    import plugboard.app as app_module
    from plugboard import admin_mfa, admin_people, admin_sessions, apikeys
    from plugboard import local_login, ratelimit
    from plugboard.storage import atomic_write_json

    markers: dict = {}
    made: dict = {}

    def mark(value, marker: str):
        if isinstance(value, str) and value:
            markers[value] = marker
        return value

    mark(str(tmp), "<world>")
    mark(ADMIN_KEY, "<admin-key>")
    mark(API_KEY, "<api-key>")

    # Client keys: one active, one revoked; the unknown one is in no store.
    active = apikeys.create_key(client_id="client-a", label="decisions")
    revoked = apikeys.create_key(client_id="client-revoked", label="decisions")
    apikeys.revoke_key(revoked["key_id"])
    made["client"] = mark(active["key"], "<client-key>")
    mark(active["key_id"], "<client-key-id>")
    made["client_revoked"] = mark(revoked["key"], "<revoked-client-key>")
    mark(revoked["key_id"], "<revoked-client-key-id>")
    made["client_unknown"] = UNKNOWN_CLIENT_KEY
    made["client_legacy_unknown"] = UNKNOWN_LEGACY_KEY
    # Keys issued before the client_id checks (BUG-64, BUG-60): refused.
    if case.bearer in ("client_reserved", "client_blank"):
        client_id = "admin" if case.bearer == "client_reserved" else ""
        refused = apikeys.create_key(client_id=client_id, label="decisions")
        made[case.bearer] = mark(refused["key"], "<refused-client-key>")
        mark(refused["key_id"], "<refused-client-key-id>")
    # A key issued before the Plugboard rename keeps its "lle_" prefix.
    if case.bearer == "client_legacy":
        record, raw = apikeys._new_key("client-legacy", "decisions")
        raw = "lle_" + raw.removeprefix("pb_")
        record.update(key_hash=apikeys._hash_key(raw), key_prefix=raw[:8])
        apikeys._write({"keys": apikeys.load_keys() + [record]})
        made["client_legacy"] = mark(raw, "<legacy-client-key>")
        mark(record["key_id"], "<legacy-client-key-id>")
    made["garbage"] = GARBAGE_BEARER
    made["admin"] = ADMIN_KEY
    made["admin_raw"] = ADMIN_KEY
    made["admin_lower"] = ADMIN_KEY

    # The local login secret (token file and remembered browser).
    if case.local:
        made["local_token"] = mark(local_login.issue_token(), "<login-token>")
        made["local_device"] = mark(local_login.remember_device(), "<device>")

    # One person, when the case uses a personal key or its counter.
    uses_person = (
        case.bearer.startswith("person")
        or case.cookie.startswith("person")
        or "person" in case.prelock
    )
    person = None
    if uses_person:
        created = people_helpers.new_person()
        person = created["person"]
        mark(person["id"], "<person>")
        made["person"] = mark(created["key"], "<person-key>")
        made["person_revoked"] = made["person"]
        mark(created["totp_secret"], "<person-secret>")
        made["person_code"] = people_helpers.code_now(env, created)

    sessions_file = admin_sessions._path()

    # A session opened before the admin secret was enrolled: enrolment
    # revokes it, and the store is put back as if it could not be rewritten,
    # so only its MFA generation refuses it.
    if case.cookie == "pre_enrollment":
        made["cookie"] = app_module._create_admin_session_token(mfa=True)
        before = sessions_file.read_bytes()

    if case.mfa in ("required", "enrolled", "unreadable"):
        secret = admin_mfa.generate_secret()
        mark(secret, "<admin-secret>")
        admin_mfa.enroll(secret)
        made["admin_code"] = admin_mfa._generate_code(secret, at=env.clock.t)
    if case.cookie == "pre_enrollment":
        sessions_file.write_bytes(before)
    if case.mfa in ("required", "flag_only", "unreadable"):
        mp.setenv("PLUGBOARD_ADMIN_MFA_REQUIRED", "1")

    now = int(time.time())
    max_age = app_module._ADMIN_SESSION_MAX_AGE
    generation = admin_mfa.generation()
    if case.cookie == "admin":
        made["cookie"] = app_module._create_admin_session_token(mfa=True)
    elif case.cookie == "admin_no_mfa":
        made["cookie"] = app_module._create_admin_session_token(mfa=False)
    elif case.cookie in ("person", "person_revoked", "person_rekeyed"):
        record = admin_people.get(person["id"])
        made["cookie"] = app_module._create_admin_session_token(person=record)
    elif case.cookie == "forged":
        token = app_module._create_admin_session_token(mfa=True)
        made["cookie"] = token[:-1] + ("0" if token[-1] != "0" else "1")
    elif case.cookie in ("expired", "future"):
        issued_at = now - max_age - 3600 if case.cookie == "expired" else now + 3600
        nonce = f"{case.cookie}-session-nonce-decisions"  # synthetic
        admin_sessions.register(
            nonce, issued_at, mfa=True, max_age=max_age, mfa_generation=generation
        )
        made["cookie"] = _sign(issued_at, nonce)
    elif case.cookie == "unregistered":
        made["cookie"] = _sign(now, "unregistered-session-nonce-decisions")
    elif case.cookie.startswith("malformed_"):
        made["cookie"] = {  # synthetic, none of them a signed token
            "malformed_no_dots": "malformed-session-decisions",
            "malformed_issued_at": "soon.malformed-nonce.0123abcd",
            "malformed_empty_nonce": f"{now}..0123abcd",
            "malformed_empty_signature": f"{now}.malformed-nonce.",
        }[case.cookie]
    if made.get("cookie") and not case.cookie.startswith("malformed_"):
        mark(made["cookie"], "<session>")
        mark(made["cookie"].split(".")[1], "<session-nonce>")

    # Changes made after the credentials exist.
    if case.bearer == "person_revoked" or case.cookie == "person_revoked":
        sessions = sessions_file.read_bytes() if sessions_file.exists() else None
        admin_people.revoke(person["id"], revoked_by="admin-key")
        if case.cookie == "person_revoked" and sessions is not None:
            # As if the session store could not be rewritten: only the
            # revoked status refuses the session.
            sessions_file.write_bytes(sessions)
    people_file = env.store
    if case.cookie == "person_rekeyed" or case.people == "malformed":
        people = json.loads(people_file.read_text(encoding="utf-8"))["people"]
        if case.cookie == "person_rekeyed":
            people[0]["generation"] = "f" * 32  # synthetic
        if case.people == "malformed":
            people[0]["id"] = "ap_malformed"
        atomic_write_json(people_file, {"people": people}, mode=0o600)
    if case.people == "unreadable":
        people_file.write_text("{not json", encoding="utf-8")
    if case.people == "unavailable":
        mp.setattr(admin_people, "check_code", lambda *a, **kw: "unavailable")
    if case.mfa == "unreadable":
        admin_mfa._MFA_FILE.write_text("{not json", encoding="utf-8")

    for counter in case.prelock:
        scope = {
            "admin": "key:admin",
            "person": f"key:admin:{person['id'] if person else ''}",
            "ip": "ip:" + case.peer,
        }[counter]
        for _ in range(ratelimit._AUTH_LIMIT):
            ratelimit.record_auth_failure(scope)

    # Environment of the auto-login checks.
    if case.auto_login:
        mp.setenv("PLUGBOARD_LOCAL_AUTO_LOGIN", case.auto_login)
    else:
        mp.delenv("PLUGBOARD_LOCAL_AUTO_LOGIN", raising=False)
    if case.bind_host:
        mp.setenv("PLUGBOARD_BIND_HOST", case.bind_host)
    else:
        mp.delenv("PLUGBOARD_BIND_HOST", raising=False)

    codes = {
        "admin": made.get("admin_code", ""),
        "person": made.get("person_code", ""),
    }
    totp = ""
    if case.totp:
        base = codes[case.totp.removesuffix("_wrong")]
        totp = people_helpers.wrong(base) if case.totp.endswith("_wrong") else base
    body_totp = ""
    if case.body_totp and case.body_totp != "absent":
        base = codes[case.body_totp.removesuffix("_wrong")]
        wrong_code = case.body_totp.endswith("_wrong")
        body_totp = people_helpers.wrong(base) if wrong_code else base
    authorization = ""
    if case.bearer:
        value = made[case.bearer]
        prefix = {"admin_raw": "", "admin_lower": "bearer "}.get(case.bearer, "Bearer ")
        authorization = prefix + value
    cookies = []
    if made.get("cookie"):
        cookies.append(f"{SESSION_COOKIE}={made['cookie']}")
    query = case.query
    if case.local in ("token", "token_wrong"):
        token = made["local_token"] if case.local == "token" else WRONG_LOCAL_TOKEN
        query = "&".join(q for q in (query, f"token={token}") if q)
    if case.local in ("device", "device_unknown"):
        device = made["local_device"] if case.local == "device" else UNKNOWN_DEVICE
        cookies.append(f"{DEVICE_COOKIE}={device}")
    # TOTP codes are six digits: replaced only where they stand alone.
    code_values = {codes["admin"], codes["person"], totp} - {""}
    if codes["admin"]:
        code_values.add(people_helpers.wrong(codes["admin"]))
    if codes["person"]:
        code_values.add(people_helpers.wrong(codes["person"]))
    return {
        "authorization": authorization,
        "totp": totp,
        "cookie": "; ".join(cookies),
        "query": query,
        "markers": markers,
        "body_totp": body_totp if case.body_totp else None,
        "codes": sorted(code_values),
    }


def _request(case: Case, credentials: dict, *, totp_header: bool = True) -> Request:
    headers = [("host", case.host)]
    if credentials["authorization"]:
        headers.append(("authorization", credentials["authorization"]))
    if credentials["totp"] and totp_header:
        headers.append((TOTP_HEADER, credentials["totp"]))
    if credentials["cookie"]:
        headers.append(("cookie", credentials["cookie"]))
    headers.extend(case.headers)
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": case.method,
            "scheme": "http",
            "path": "/admin/x",
            "raw_path": b"/admin/x",
            "root_path": "",
            "query_string": credentials["query"].encode("latin-1"),
            "headers": [
                (name.lower().encode("latin-1"), value.encode("latin-1"))
                for name, value in headers
            ],
            "client": (case.peer, 50000),
            "server": ("127.0.0.1", 8000),
        }
    )


def _decide(app_module, name: str, request, body_code: str = "") -> list:
    from plugboard import audit as audit_mod

    holder = audit_mod.begin_request()
    try:
        value = None
        for part in name.split("+"):
            if part.endswith("[body]"):
                value = getattr(app_module, part.removesuffix("[body]"))(
                    request, body_code
                )
            else:
                value = getattr(app_module, part)(request)
    except HTTPException as exc:
        outcome = ["http", exc.status_code, exc.detail]
    except Exception as exc:  # recorded: a new crash is a changed decision
        outcome = ["raised", type(exc).__name__]
    else:
        outcome = ["ok", value]
    finally:
        principal = list(audit_mod.current_principal())
        audit_mod.end_request(holder)
    state = request.state
    record = getattr(state, "key_record", None)
    outcome.append(
        {
            "key_id": getattr(state, "key_id", None),
            "key_record": record.get("key_id") if isinstance(record, dict) else None,
            "admin_principal": getattr(state, "admin_principal", None),
            "admin_principal_name": getattr(state, "admin_principal_name", None),
            "admin_mfa_verified": getattr(state, "admin_mfa_verified", None),
            "audit_principal": principal,
        }
    )
    return outcome


def run_case(case: Case) -> dict:
    from plugboard import ratelimit

    out = {}
    for name in FUNCTIONS:
        with _world(case) as (app_module, credentials, audit, logs):
            calls = []
            body = name.endswith("[body]")
            for _ in range(case.repeat):
                # Without body_totp the code moves from the header to the body.
                own_body = credentials["body_totp"] is not None
                code = credentials["body_totp"] if own_body else credentials["totp"]
                request = _request(case, credentials, totp_header=own_body or not body)
                outcome = _decide(app_module, name, request, code)
                calls.append(
                    {
                        "outcome": outcome,
                        "audit": list(audit),
                        "log": list(logs),
                        "auth_buckets": sorted(
                            [scope, len(attempts)]
                            for scope, attempts in ratelimit._auth_buckets.items()
                        ),
                        "lockouts": sorted(ratelimit._lockouts),
                    }
                )
                audit.clear()
                logs.clear()
            out[name] = _normalize(calls, credentials)
    return out


def _normalize(value, credentials: dict):
    """The value as plain JSON, every generated value replaced by its marker;
    refuses to keep any generated value."""
    text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    markers = credentials["markers"]
    for raw in sorted(markers, key=len, reverse=True):
        text = text.replace(raw, markers[raw])
    for code in credentials["codes"]:
        text = re.sub(rf"(?<![0-9]){code}(?![0-9])", "<totp>", text)
    leaked = [m for raw, m in markers.items() if raw in text]
    assert not leaked, f"generated values left in the table: {leaked}"
    return json.loads(text)


def run_all() -> dict:
    names = [case.name for case in CASES]
    assert len(set(names)) == len(names), "two cases share a name"
    return {case.name: run_case(case) for case in CASES}


def main() -> None:
    import tests.conftest  # noqa: F401  (same environment as the suite)

    FIXTURE.write_text(
        json.dumps(run_all(), indent=1, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
