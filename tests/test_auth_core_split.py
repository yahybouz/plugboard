"""Invariants of moving the auth core out of app.py (PLU-97)."""

import ast
import importlib
import inspect
import pkgutil
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from tests.test_admin_routes_split import PATCH_POINTS, _app_imports
from tests.test_chat_path_split import _bare_patch_points, two_imports  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
AUTH_CORE_DIR = ROOT / "plugboard" / "auth_core"
AUTH_CORE_PACKAGE = "plugboard.auth_core"
APP_MODULE = "plugboard.app"
EXPECTED_MODULES = frozenset({"admin", "auto_login", "scopes", "sessions"})
# Every name moved, by module: re-exported by app.py, always (R14).
MOVED = {
    "scopes": (
        "_ADMIN_SCOPE",
        "_ADMIN_KEY_PRINCIPAL",
        "_person_principal",
        "_person_scope",
        "_set_principal",
        "_client_address",
        "_address_scope",
        "_key_scope",
        "_person_bearer_scope",
        "_rate_limit_scope",
    ),
    "sessions": (
        "_STATE_CHANGING_METHODS",
        "_CROSS_SITE_REFUSED",
        "_origin_is_this_host",
        "_effective_admin_key",
        "_is_admin_key",
        "_create_admin_session_token",
        "_admin_session_nonce",
        "_admin_session_principal",
        "_is_admin_session",
        "_is_cross_origin_browser_request",
        "_refuse_cross_site_session",
        "_should_use_secure_cookie",
    ),
    "admin": (
        "_ADMIN_TOTP_HEADER",
        "_person_record_is_sound",
        "_admin_request_auth",
        "_person_request_auth",
        "_prove_person_code",
        "_require_mfa_step_up",
    ),
    "auto_login": (
        "_LOOPBACK_HOSTS",
        "_FLAG_ON",
        "_FLAG_OFF",
        "_PROXY_HEADERS",
        "_LOCAL_NAVIGATION_SITES",
        "_SUSPICIOUS_REFUSALS",
        "_host_header_name",
        "_is_loopback_address",
        "_bind_host",
        "_local_login_proof",
        "_should_audit_refusal",
        "_auto_login_mode",
        "_auto_login_refusal",
        "_issue_local_login_token",
    ),
}
# Names defined in (or re-exported by) app.py that the auth core reads: always
# as core.<name>, even a function of the same module (R13). The moved
# functions, and the values that stay in app.py; constants are not listed.
LATE_BOUND = PATCH_POINTS | frozenset(
    {
        "_person_principal",
        "_person_scope",
        "_set_principal",
        "_client_address",
        "_address_scope",
        "_key_scope",
        "_person_bearer_scope",
        "_rate_limit_scope",
        "_origin_is_this_host",
        "_effective_admin_key",
        "_is_admin_key",
        "_create_admin_session_token",
        "_admin_session_nonce",
        "_admin_session_principal",
        "_is_admin_session",
        "_is_cross_origin_browser_request",
        "_refuse_cross_site_session",
        "_should_use_secure_cookie",
        "_person_record_is_sound",
        "_admin_request_auth",
        "_person_request_auth",
        "_prove_person_code",
        "_require_mfa_step_up",
        "_host_header_name",
        "_is_loopback_address",
        "_bind_host",
        "_local_login_proof",
        "_should_audit_refusal",
        "_auto_login_mode",
        "_auto_login_refusal",
        "_issue_local_login_token",
        "_ADMIN_COOKIE_NAME",
        "_ADMIN_SESSION_MAX_AGE",
        "_COOKIE_SECURE_MODE",
        "_refusal_audit_seen",
        "_warned_auto_login_values",
    }
)
FIRST_KEY = "first-admin-key-0123456789"  # synthetic
SECOND_KEY = "second-admin-key-0123456789"  # synthetic
# Calls on the app object that would declare a route, a middleware or a handler.
APP_REGISTRATIONS = frozenset(
    {
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "api_route",
        "include_router",
        "add_middleware",
        "middleware",
        "exception_handler",
    }
)


def _app_module():
    return importlib.import_module(APP_MODULE)


def _auth_core_modules() -> list:
    """Every submodule of plugboard.auth_core; none before the move."""
    try:
        package = importlib.import_module(AUTH_CORE_PACKAGE)
    except ModuleNotFoundError as exc:
        if exc.name != AUTH_CORE_PACKAGE:
            raise
        return []
    return [
        importlib.import_module(f"{AUTH_CORE_PACKAGE}.{info.name}")
        for info in pkgutil.iter_modules(package.__path__)
    ]


def _short_name(module) -> str:
    return module.__name__.rsplit(".", 1)[-1]


def _auth_core_trees() -> list:
    """(file name, AST) of every Python file of the package, sorted."""
    return [
        (path.name, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for path in sorted(AUTH_CORE_DIR.glob("*.py"))
    ]


def _request(bearer: str = "", peer: str = "192.0.2.10") -> Request:
    headers = [(b"host", b"testserver")]
    if bearer:
        headers.append((b"authorization", f"Bearer {bearer}".encode("latin-1")))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "query_string": b"",
            "client": (peer, 50000),
            "server": ("127.0.0.1", 8000),
            "scheme": "http",
        }
    )


def test_no_auth_core_module_binds_a_patch_point():
    bound = {
        module.__name__: sorted(PATCH_POINTS & set(vars(module)))
        for module in _auth_core_modules()
    }
    assert {name: names for name, names in bound.items() if names} == {}


def test_auth_core_modules_never_import_the_app_module():
    """No import of plugboard.app, sys.modules, import_module, nor any relative
    import: the package reaches the app module only through ``core``."""
    found = {}
    for name, tree in _auth_core_trees():
        hits = _app_imports(tree) + [
            (node.lineno, f"relative import level {node.level}")
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level
        ]
        if hits:
            found[name] = sorted(hits)
    assert found == {}


def test_each_auth_core_module_registers_through_register():
    for module in _auth_core_modules():
        assert list(inspect.signature(module.register).parameters) == [
            "app",
            "core",
        ], module.__name__


def test_auth_core_code_logs_under_the_app_logger():
    app_module = _app_module()
    for module in _auth_core_modules():
        if hasattr(module, "log"):
            assert module.log is app_module.log, module.__name__


def test_auth_core_modules_are_designed_ones():
    """Only modules of the design are found; all of them once the move is done."""
    found = {_short_name(module) for module in _auth_core_modules()}
    assert found <= EXPECTED_MODULES


def test_auth_core_reads_app_names_only_through_core():
    """A name defined in (or re-exported by) plugboard.app is read as
    ``core.<name>`` at call time, even a function of the same module: a bare
    name would miss a patch of plugboard.app and, after a re-import, read
    another module (R13)."""
    app = _app_module()
    names = frozenset(name for name in LATE_BOUND if hasattr(app, name))
    found = {}
    for name, tree in _auth_core_trees():
        hits = _bare_patch_points(tree, names)
        if hits:
            found[name] = hits
    assert found == {}


def test_every_moved_name_is_on_the_app_module():
    """Every moved name stays on plugboard.app, as the very object the auth
    core module holds or the closure its register() handed back (R14)."""
    app = _app_module()
    for module in _auth_core_modules():
        short = _short_name(module)
        for name in MOVED[short]:
            if name in vars(module):
                expected = getattr(module, name)
            else:
                expected = getattr(getattr(app, f"_ac_{short}_ns"), name)
            assert getattr(app, name) is expected, f"{short}.{name}"


def test_no_auth_core_module_touches_the_app():
    """The package declares no route, no middleware and no exception handler:
    no call (decorators included) of a registration method on ``app``."""
    found = {}
    for name, tree in _auth_core_trees():
        hits = sorted(
            (node.lineno, node.func.attr)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "app"
            and node.func.attr in APP_REGISTRATIONS
        )
        if hits:
            found[name] = hits
    assert found == {}


@pytest.fixture
def two_admin_keys(two_imports, monkeypatch, tmp_path):  # noqa: F811
    """Two imports of plugboard.app, each with its own admin key, MFA off."""
    from plugboard import admin_mfa

    first, second = two_imports
    monkeypatch.setattr(admin_mfa, "_MFA_FILE", tmp_path / "admin_mfa.json")
    monkeypatch.delenv("PLUGBOARD_ADMIN_MFA_REQUIRED", raising=False)
    monkeypatch.setattr(first, "_ADMIN_KEY", FIRST_KEY)
    monkeypatch.setattr(second, "_ADMIN_KEY", SECOND_KEY)
    return first, second


def test_admin_auth_reads_the_module_that_registered_it(two_admin_keys):
    first, _ = two_admin_keys
    assert first._admin_request_auth(_request(FIRST_KEY)) == "ok"
    assert first._admin_request_auth(_request(SECOND_KEY)) == "denied"


def test_rate_limit_scope_reads_the_module_that_registered_it(two_admin_keys):
    first, _ = two_admin_keys
    assert first._rate_limit_scope(_request(FIRST_KEY)) == "key:admin"
    assert first._rate_limit_scope(_request(SECOND_KEY)) == "ip:192.0.2.10"


def test_refusal_audit_state_is_the_registering_module_s(two_imports, monkeypatch):  # noqa: F811
    """The refusal audit reads the state of plugboard.app at call time: a
    patched dict is the one filled, and the other import's stays empty."""
    first, second = two_imports
    monkeypatch.setattr(first, "_refusal_audit_seen", {})
    monkeypatch.setattr(second, "_refusal_audit_seen", {})
    assert first._should_audit_refusal("192.0.2.10", "proxied") is True
    assert first._should_audit_refusal("192.0.2.10", "proxied") is False
    assert list(first._refusal_audit_seen) == [("192.0.2.10", "proxied")]
    assert second._refusal_audit_seen == {}


async def test_admin_route_of_the_first_import_takes_its_own_admin_key(
    two_admin_keys, monkeypatch
):
    import plugboard.ratelimit as rl

    first, _ = two_admin_keys
    monkeypatch.setattr(rl, "_LIMIT", 0)
    async with AsyncClient(
        transport=ASGITransport(app=first.app), base_url="http://test"
    ) as client:
        ok = await client.get(
            "/metrics", headers={"Authorization": f"Bearer {FIRST_KEY}"}
        )
        refused = await client.get(
            "/metrics", headers={"Authorization": f"Bearer {SECOND_KEY}"}
        )
    assert ok.status_code == 200, ok.text
    assert refused.status_code == 401
