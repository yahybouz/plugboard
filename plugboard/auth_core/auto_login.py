"""Local auto-login: who may open the dashboard without a key, and its proof.

The parsers of the Host header and of the peer address, the proxy headers and
fetch-metadata values that refuse auto-login, the interface the server listens
on, the secret a local browser shows (remembered-browser cookie or single-use
token), the throttled audit of suspicious refusals, the auto-login mode and
the login token written at startup. Moved verbatim from ``plugboard/app.py``
(PLU-97), behaviour unchanged. ``core`` is the ``plugboard.app`` module that
registers them: any name defined or re-exported by ``app.py`` is read as
``core.<name>`` when it runs, even a function of this module (R13).
"""

from __future__ import annotations

import ipaddress
import logging
import os
import time
from types import ModuleType, SimpleNamespace

from fastapi import FastAPI, Request

log = logging.getLogger("plugboard.app")

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_FLAG_ON = {"1", "true", "yes", "on"}
_FLAG_OFF = {"0", "false", "no", "off"}
# Any of these means the request went through a proxy: the TCP peer is then
# the proxy itself, so a loopback peer proves nothing about the real client.
_PROXY_HEADERS = (
    "forwarded",
    "via",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-forwarded-port",
    "x-forwarded-server",
    "x-original-forwarded-for",
    "x-real-ip",
    "x-client-ip",
    "x-cluster-client-ip",
    "cf-connecting-ip",
    "true-client-ip",
    "fly-client-ip",
    "fastly-client-ip",
)
# Fetch-metadata values a browser sends when the user opens the dashboard
# itself (address bar, bookmark, or a page of the same origin). A page served
# on another localhost port sends "same-site" and is refused.
_LOCAL_NAVIGATION_SITES = {"none", "same-origin"}
# Refusals that mean someone claimed to be local without being so: audited,
# at most once per peer and reason per window, and for at most
# _REFUSAL_AUDIT_MAX_KEYS peers per window, so they cannot flood the log.
_SUSPICIOUS_REFUSALS = {"proxied", "non_loopback_peer", "bad_token"}


def _host_header_name(request: Request) -> str:
    """Hostname from the raw Host header, without port or brackets.

    Starlette's ``request.url.hostname`` falls back to the local socket
    address when the Host header is malformed, which would read as loopback.
    """
    raw = request.headers.get("host", "").strip().lower()
    if raw.startswith("["):
        end = raw.find("]")
        return raw[1:end] if end > 0 else ""
    name, sep, port = raw.rpartition(":")
    if sep and port.isdigit():
        return name
    return raw


def _is_loopback_address(value: str) -> bool:
    if value.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return (mapped or address).is_loopback


def _bind_host() -> str:
    """Interface the server really listens on, as exported by run.py.

    HOST alone is not enough: ``uvicorn --host 0.0.0.0`` ignores it. A server
    started another way counts as non-local unless auto-login is opted in.
    """
    return os.environ.get("PLUGBOARD_BIND_HOST", "")


def _local_login_proof(request: Request) -> tuple:
    """("device" | "token", "") when the browser shows a secret only the
    operator holds, else ("", refusal reason).

    The loopback and header checks prove nothing against a same-machine
    container, VM or forwarder; this does: a remembered-browser cookie, or
    the single-use token from data/.login_token (opened by ``make dev``).
    """
    from plugboard import local_login

    if local_login.is_remembered(request.cookies.get(local_login.DEVICE_COOKIE, "")):
        return "device", ""
    token = request.query_params.get("token", "")
    if not token:
        return "", "no_local_secret"
    if local_login.consume_token(token):
        return "token", ""
    return "", "bad_token"


def register(app: FastAPI, core: ModuleType) -> SimpleNamespace:
    def _should_audit_refusal(peer: str, reason: str) -> bool:
        now = time.monotonic()
        key = (peer, reason)
        last = core._refusal_audit_seen.get(key)
        if last is not None and now - last < core._REFUSAL_AUDIT_WINDOW_S:
            return False
        if len(core._refusal_audit_seen) >= core._REFUSAL_AUDIT_MAX_KEYS:
            for seen_key, seen_at in list(core._refusal_audit_seen.items()):
                if now - seen_at >= core._REFUSAL_AUDIT_WINDOW_S:
                    del core._refusal_audit_seen[seen_key]
            # Still full: many distinct peers within one window. Stop auditing new
            # ones (the log line still records them) instead of forgetting all.
            if len(core._refusal_audit_seen) >= core._REFUSAL_AUDIT_MAX_KEYS:
                return False
        core._refusal_audit_seen[key] = now
        return True

    def _auto_login_mode() -> str:
        """Return "on", "off" or "auto" from PLUGBOARD_LOCAL_AUTO_LOGIN.

        "auto" (unset or empty) allows auto-login only when the server listens on
        a loopback address, i.e. a local install. Unknown values fail closed.
        """
        raw = os.environ.get("PLUGBOARD_LOCAL_AUTO_LOGIN", "").strip().lower()
        if raw in {"", "auto"}:
            return "auto"
        if raw in _FLAG_ON:
            return "on"
        if raw not in _FLAG_OFF and raw not in core._warned_auto_login_values:
            core._warned_auto_login_values.add(raw)
            log.warning(
                "PLUGBOARD_LOCAL_AUTO_LOGIN=%r is not recognised; auto-login is off",
                raw,
            )
        return "off"

    def _auto_login_refusal(request: Request) -> str:
        """Return why local auto-login is refused, or "" when it is allowed.

        Auto-login is for a local install opened in the operator's browser. A
        deployed instance (listening on a public interface, or behind a proxy)
        always asks for a key. The Host header is client-controlled, so it never
        counts on its own.
        """
        mode = core._auto_login_mode()
        if mode == "off":
            return "disabled"
        if not core._effective_admin_key():
            return "no_admin_key"
        if mode == "auto" and not core._is_loopback_address(core._bind_host()):
            return "not_local_install"
        if request.query_params.get("logged_out"):
            return "logged_out"
        if any(h in request.headers for h in _PROXY_HEADERS):
            return "proxied"
        peer = request.client.host if request.client else ""
        if not core._is_loopback_address(peer):
            return "non_loopback_peer"
        if core._host_header_name(request) not in _LOOPBACK_HOSTS:
            return "non_loopback_host"
        site = request.headers.get("sec-fetch-site", "").lower()
        navigation = request.headers.get("sec-fetch-mode", "").lower()
        if site not in _LOCAL_NAVIGATION_SITES or navigation != "navigate":
            return "not_navigation"
        from plugboard import admin_mfa as _mfa

        if _mfa.is_required():
            return "mfa_required"
        return ""

    def _issue_local_login_token() -> None:
        """At startup, write a fresh login token when auto-login can be used."""
        mode = core._auto_login_mode()
        if mode == "off" or not core._effective_admin_key():
            return
        if mode == "auto" and not core._is_loopback_address(core._bind_host()):
            return
        from plugboard import local_login

        try:
            local_login.issue_token()
        except OSError as exc:
            log.warning("auth.auto_login cannot write the login token: %s", exc)
            return
        log.info(
            "auth.auto_login sign in once with the link `make dev` opens, or "
            "/login?token=<content of %s>; the browser is then remembered",
            local_login.token_path(),
        )

    return SimpleNamespace(
        _should_audit_refusal=_should_audit_refusal,
        _auto_login_mode=_auto_login_mode,
        _auto_login_refusal=_auto_login_refusal,
        _issue_local_login_token=_issue_local_login_token,
    )
