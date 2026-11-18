"""Invariants of moving the auth core out of app.py (PLU-97)."""

import ast
import importlib
import inspect
import pkgutil
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from tests import auth_core_snapshot
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


# --- R13, hardened: no early binding (security review I2) -------------------
# The moved code reads plugboard.app only as ``core.<name>`` inside a closure
# body, at call time. Every other way to reach an app value (a capture when
# register() runs, a default argument, a renamed closure, module state, the
# package attribute, globals(), the value's own source) is refused.

# The plugboard imports the moved code makes, module-level or local (P4):
# (module, imported name). Constants of the package itself are added per file.
ALLOWED_PLUGBOARD_IMPORTS = frozenset(
    {
        ("plugboard", "admin_mfa"),
        ("plugboard", "admin_people"),
        ("plugboard", "admin_sessions"),
        ("plugboard", "apikeys"),
        ("plugboard", "audit"),
        ("plugboard", "local_login"),
        ("plugboard.apikeys", "KEY_PREFIXES"),
        ("plugboard.ratelimit", "check_auth_rate_limit"),
        ("plugboard.ratelimit", "record_auth_failure"),
    }
)
# The only environment variables the moved code reads itself, at call time.
ENV_READS = frozenset({"PLUGBOARD_BIND_HOST", "PLUGBOARD_LOCAL_AUTO_LOGIN"})
# Introspection that reaches another module's globals.
FORBIDDEN_MODULES = frozenset(
    {"builtins", "ctypes", "gc", "importlib", "inspect", "sys"}
)
FORBIDDEN_NAMES = frozenset(
    {
        "__builtins__",
        "__import__",
        "compile",
        "eval",
        "exec",
        "globals",
        "locals",
        "plugboard",
        "vars",
    }
)
# Methods that change a set, a dict or a list in place.
MUTATORS = frozenset(
    {
        "add",
        "append",
        "clear",
        "difference_update",
        "discard",
        "extend",
        "insert",
        "intersection_update",
        "pop",
        "popitem",
        "remove",
        "reverse",
        "setdefault",
        "sort",
        "symmetric_difference_update",
        "update",
    }
)


def _constants(short: str) -> frozenset:
    """The constants a module of the package defines (upper-case moved names)."""
    return frozenset(n for n in MOVED.get(short, ()) if n.lstrip("_").isupper())


def _app_value_sources(app) -> tuple:
    """Where the late-bound values of plugboard.app come from: the functions'
    own modules, and the string literals and environment variables app.py
    assigns them from. Reading one of these directly skips ``core``."""
    functions = set()
    for name in LATE_BOUND:
        value = getattr(app, name, None)
        module = getattr(value, "__module__", None)
        if (
            callable(value)
            and isinstance(module, str)
            and module.startswith("plugboard.")
            and module != APP_MODULE
        ):
            functions.add((module, value.__name__))
    strings = set()
    tree = ast.parse(Path(app.__file__).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id in LATE_BOUND for t in targets):
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            strings.add(value.value)
        for call in ast.walk(value):
            if (
                isinstance(call, ast.Call)
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and isinstance(call.args[0].value, str)
            ):
                strings.add(call.args[0].value)
    return frozenset(functions), frozenset(s for s in strings if s)


def _runs_at_call_time(node: ast.AST, parents: dict) -> bool:
    """True when ``node`` is evaluated only when a closure of register() runs:
    not in register()'s own body, a default value, a decorator or at import."""
    child, parent = node, parents.get(node)
    while parent is not None:
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            body = parent.body if isinstance(parent.body, list) else [parent.body]
            if child in body:
                return not (
                    isinstance(parent, ast.FunctionDef)
                    and parent.name == "register"
                    and isinstance(parents.get(parent), ast.Module)
                )
        child, parent = parent, parents.get(parent)
    return False


def _env_read_is_allowed(node: ast.Attribute, parents: dict) -> bool:
    """``os.environ.get(<name>…)``, ``os.environ[<name>]`` or
    ``os.getenv(<name>…)`` with a name of ENV_READS, nothing else."""

    def allowed(arg) -> bool:
        return isinstance(arg, ast.Constant) and arg.value in ENV_READS

    parent = parents.get(node)
    if node.attr == "getenv":
        return (
            isinstance(parent, ast.Call)
            and bool(parent.args)
            and allowed(parent.args[0])
        )
    if isinstance(parent, ast.Subscript):
        return allowed(parent.slice)
    call = parents.get(parent)
    return (
        isinstance(parent, ast.Attribute)
        and parent.attr == "get"
        and isinstance(call, ast.Call)
        and call.func is parent
        and bool(call.args)
        and allowed(call.args[0])
    )


def _register_body_findings(register: ast.FunctionDef, short: str) -> list:
    """register() holds the moved closures and returns them, nothing else (P3)."""
    found = []
    moved = set(MOVED.get(short, ()))
    body = list(register.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    for stmt in body[:-1]:
        if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.append((stmt.lineno, f"register() runs {type(stmt).__name__}"))
        elif stmt.name not in moved:
            found.append((stmt.lineno, f"closure {stmt.name} is not a moved name"))
    last = body[-1] if body else register
    call = last.value if isinstance(last, ast.Return) else None
    if not (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "SimpleNamespace"
        and not call.args
    ):
        found.append(
            (last.lineno, "register() does not end with return SimpleNamespace")
        )
        return found
    for keyword in call.keywords:
        if not (
            keyword.arg in moved
            and isinstance(keyword.value, ast.Name)
            and keyword.value.id == keyword.arg
        ):
            found.append(
                (keyword.value.lineno, f"exported {keyword.arg} is not name=name")
            )
    return found


def _module_level_findings(tree: ast.Module, short: str) -> list:
    """At module level: a docstring, imports, the module's constants, ``log``,
    the moved functions and register() (P1, P2)."""
    found = []
    targets = _constants(short) | {"log"}
    functions = set(MOVED.get(short, ())) | {"register"}
    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            names = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for target in names:
                if not (isinstance(target, ast.Name) and target.id in targets):
                    found.append((stmt.lineno, f"module state {ast.unparse(target)}"))
            continue
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if stmt.name not in functions:
                found.append((stmt.lineno, f"function {stmt.name} is not a moved name"))
            if stmt.name == "register":
                found += _register_body_findings(stmt, short)
            continue
        found.append((stmt.lineno, f"module-level {type(stmt).__name__}"))
    return found


def _import_findings(tree: ast.Module, short: str, functions: frozenset) -> tuple:
    """Imports outside the allowed list, and the module aliases they bind."""
    found = []
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root == "plugboard" or root in FORBIDDEN_MODULES:
                    found.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if root in FORBIDDEN_MODULES or any(a.name == "*" for a in node.names):
                found.append((node.lineno, f"from {node.module} import"))
            if node.module == "os" and any(
                a.name in {"environ", "environb", "getenv", "getenvb"}
                for a in node.names
            ):
                found.append((node.lineno, "from os import environ/getenv"))
            if root != "plugboard":
                continue
            for alias in node.names:
                pair = (node.module, alias.name)
                own = node.module.startswith(
                    AUTH_CORE_PACKAGE + "."
                ) and alias.name in (_constants(node.module.rsplit(".", 1)[-1]))
                if (
                    pair not in ALLOWED_PLUGBOARD_IMPORTS and not own
                ) or pair in functions:
                    found.append(
                        (node.lineno, f"from {node.module} import {alias.name}")
                    )
                if node.module == "plugboard":
                    aliases[alias.asname or alias.name] = f"plugboard.{alias.name}"
    return found, aliases


def _early_binding_findings(
    tree: ast.Module, short: str, functions: frozenset, strings: frozenset
) -> list:
    """Every way the auth core could hold an app value instead of reading
    ``core.<name>`` at call time (R13, hardened after review I2)."""
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    found = _module_level_findings(tree, short)
    import_hits, aliases = _import_findings(tree, short, functions)
    found += import_hits
    frozen = {n for module in MOVED for n in _constants(module)} | {"KEY_PREFIXES"}
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.decorator_list
        ):
            found.append((node.lineno, f"decorated {node.name}"))
        elif isinstance(node, (ast.ClassDef, ast.Global, ast.Nonlocal)):
            found.append((node.lineno, type(node).__name__))
        elif isinstance(node, ast.Name) and node.id == "core":
            attr = parents.get(node)
            if not (isinstance(attr, ast.Attribute) and attr.value is node):
                found.append((node.lineno, "core used other than core.<name>"))
            elif not _runs_at_call_time(attr, parents):
                found.append((node.lineno, f"core.{attr.attr} read before call time"))
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            found.append((node.lineno, node.id))
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            owner = node.value.id
            if owner == "os" and node.attr in {
                "environ",
                "environb",
                "getenv",
                "getenvb",
            }:
                if not _env_read_is_allowed(node, parents):
                    found.append((node.lineno, f"os.{node.attr} outside ENV_READS"))
            elif (aliases.get(owner), node.attr) in functions:
                found.append((node.lineno, f"{owner}.{node.attr} skips core"))
            elif owner in frozen and node.attr in MUTATORS:
                found.append((node.lineno, f"{owner}.{node.attr}() changes a constant"))
        elif isinstance(node, ast.Subscript) and not isinstance(node.ctx, ast.Load):
            if isinstance(node.value, ast.Name) and node.value.id in frozen:
                found.append((node.lineno, f"{node.value.id}[…] changes a constant"))
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            if node.target.id in frozen:
                found.append((node.lineno, f"{node.target.id} changed in place"))
        elif isinstance(node, ast.Constant) and node.value in strings:
            found.append((node.lineno, f"literal {node.value!r} of a late-bound value"))
    return sorted(set(found))


def _guard_findings(tree: ast.Module, short: str, app) -> list:
    """Everything the AST guards of this file refuse in one module."""
    functions, strings = _app_value_sources(app)
    names = frozenset(name for name in LATE_BOUND if hasattr(app, name))
    relative = [
        (node.lineno, f"relative import level {node.level}")
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level
    ]
    return sorted(
        set(
            _early_binding_findings(tree, short, functions, strings)
            + _bare_patch_points(tree, names)
            + _app_imports(tree)
            + relative
        )
    )


def test_auth_core_binds_no_app_value_early():
    """The auth core holds no app value: ``core.<name>`` is read inside a
    closure, at call time, and nowhere else; no capture in register(), default
    value, renamed closure, module state, package attribute, globals(), direct
    read of the value's source, nor change of a moved constant (R13, I2)."""
    app = _app_module()
    functions, strings = _app_value_sources(app)
    found = {}
    for name, tree in _auth_core_trees():
        short = name.removesuffix(".py")
        hits = _early_binding_findings(tree, short, functions, strings)
        if hits:
            found[name] = hits
    assert found == {}


def test_frozen_constants_are_the_moved_ones():
    """The structure snapshot freezes the value of every moved constant."""
    moved = {n for module in MOVED for n in _constants(module)}
    assert moved == set(auth_core_snapshot.MOVED_CONSTANTS)


def test_app_value_sources_are_known():
    """The sources the early-binding guard refuses are really found."""
    functions, strings = _app_value_sources(_app_module())
    assert {
        ("plugboard.audit", "log_action"),
        ("plugboard.apikeys", "get_key_record"),
    } <= (functions)
    assert {
        "ADMIN_KEY",
        "API_KEY",
        "COOKIE_SECURE",
        "plugboard_admin_session",
    } <= strings


# A faithful move, in the shape of the plan (P1 to P5): the guards accept it.
_FAITHFUL_SESSIONS = '''\
"""Admin sessions, moved verbatim from plugboard/app.py (PLU-97)."""

from __future__ import annotations

import hmac
import logging
import os
from types import ModuleType, SimpleNamespace

from fastapi import FastAPI, Request

from plugboard import admin_sessions
from plugboard import audit as _audit_mod
from plugboard.apikeys import KEY_PREFIXES
from plugboard.auth_core.scopes import _ADMIN_SCOPE

log = logging.getLogger("plugboard.app")

_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_CROSS_SITE_REFUSED = "Cross-site request refused"


def register(app: FastAPI, core: ModuleType) -> SimpleNamespace:
    """Wire the closures on the registering module."""

    def _effective_admin_key() -> str:
        return core._ADMIN_KEY

    def _is_admin_key(token: str) -> bool:
        key = core._effective_admin_key()
        if not key or token.startswith(KEY_PREFIXES):
            return False
        return hmac.compare_digest(token.encode(), key.encode())

    def _should_use_secure_cookie(request: Request) -> bool:
        from plugboard import admin_mfa as _mfa
        from plugboard.ratelimit import record_auth_failure

        bind = os.environ.get("PLUGBOARD_BIND_HOST", "")
        core._warned_auto_login_values.add(bind)
        core._refusal_audit_seen[(bind, _ADMIN_SCOPE)] = 0.0
        _audit_mod.set_principal(_CROSS_SITE_REFUSED, "")
        if request.method in _STATE_CHANGING_METHODS:
            record_auth_failure(bind)
        core._audit("x", ip=bind)
        return core._COOKIE_SECURE_MODE == "on" and bool(_mfa) and bool(admin_sessions)

    return SimpleNamespace(
        _effective_admin_key=_effective_admin_key,
        _is_admin_key=_is_admin_key,
        _should_use_secure_cookie=_should_use_secure_cookie,
    )
'''
# Each one holds an app value without reading ``core.`` at call time:
# (text of the faithful move, replacement) pairs.
_ADMIN_KEY_READ = "        return core._ADMIN_KEY\n"
_EARLY_BINDINGS = {
    "capture_in_register": [
        (
            "    def _effective_admin_key() -> str:\n" + _ADMIN_KEY_READ,
            "    key = core._ADMIN_KEY\n\n"
            "    def _effective_admin_key() -> str:\n        return key\n",
        )
    ],
    "default_argument": [
        (
            "    def _effective_admin_key() -> str:",
            "    def _effective_admin_key(_k=core._ADMIN_KEY) -> str:",
        )
    ],
    "decorator_caches_the_first_answer": [
        (
            "    def _effective_admin_key() -> str:",
            "    @functools.lru_cache\n    def _effective_admin_key() -> str:",
        )
    ],
    "renamed_closure_called_bare": [
        ("    def _effective_admin_key() -> str:", "    def effective_key() -> str:"),
        ("        key = core._effective_admin_key()", "        key = effective_key()"),
        (
            "        _effective_admin_key=_effective_admin_key,",
            "        _effective_admin_key=effective_key,",
        ),
    ],
    "closure_exported_under_another_name": [
        (
            "        _is_admin_key=_is_admin_key,",
            "        _is_admin_key=_effective_admin_key,",
        )
    ],
    "module_state_renamed": [
        (
            '_CROSS_SITE_REFUSED = "Cross-site request refused"\n',
            '_CROSS_SITE_REFUSED = "Cross-site request refused"\n_seen = {}\n',
        )
    ],
    "state_kept_by_register": [
        (
            '    """Wire the closures on the registering module."""\n',
            '    """Wire the closures on the registering module."""\n    seen = {}\n',
        )
    ],
    "global_statement": [(_ADMIN_KEY_READ, "        global _held\n" + _ADMIN_KEY_READ)],
    "class_holds_state": [
        (
            "log = logging.getLogger",
            "class _Held:\n    key = None\n\n\nlog = logging.getLogger",
        )
    ],
    "package_attribute": [
        ("import hmac\n", "import hmac\nimport plugboard\n"),
        (_ADMIN_KEY_READ, "        return plugboard.app._ADMIN_KEY\n"),
    ],
    "globals_lookup": [(_ADMIN_KEY_READ, '        return globals()["_ADMIN_KEY"]\n')],
    "core_passed_around": [
        (_ADMIN_KEY_READ, '        return getattr(core, "_ADMIN_KEY")\n')
    ],
    "config_read_directly": [
        (
            "from plugboard import admin_sessions\n",
            "from plugboard import admin_sessions\nfrom plugboard.config import ADMIN_KEY\n",
        )
    ],
    "environment_read_directly": [
        (_ADMIN_KEY_READ, '        return os.environ.get("ADMIN_KEY", "")\n')
    ],
    "environment_aliased": [
        (
            '        bind = os.environ.get("PLUGBOARD_BIND_HOST", "")\n',
            '        env = os.environ\n        bind = env.get("PLUGBOARD_BIND_HOST", "")\n',
        )
    ],
    "audit_function_imported": [
        (
            "from plugboard import admin_sessions\n",
            "from plugboard import admin_sessions\nfrom plugboard.audit import log_action\n",
        )
    ],
    "audit_function_through_module": [
        (
            '        core._audit("x", ip=bind)\n',
            '        _audit_mod.log_action("x", ip=bind)\n',
        )
    ],
    "patch_point_imported": [
        (
            "from plugboard.apikeys import KEY_PREFIXES\n",
            "from plugboard.apikeys import KEY_PREFIXES, get_key_record\n",
        )
    ],
    "cookie_name_hard_coded": [
        (_ADMIN_KEY_READ, '        return "plugboard_admin_session"\n')
    ],
    "moved_constant_changed": [
        (
            "            record_auth_failure(bind)\n",
            "            record_auth_failure(bind)\n"
            '            _STATE_CHANGING_METHODS.discard("DELETE")\n',
        )
    ],
    "sys_modules": [
        (
            _ADMIN_KEY_READ,
            '        import sys\n\n        return sys.modules["plugboard.app"]._ADMIN_KEY\n',
        )
    ],
    "relative_import": [
        (
            "from plugboard.auth_core.scopes import _ADMIN_SCOPE\n",
            "from .scopes import _ADMIN_SCOPE\n",
        )
    ],
}


def _trapped(form: str) -> str:
    source = _FAITHFUL_SESSIONS
    for old, new in _EARLY_BINDINGS[form]:
        assert source.count(old) == 1, (form, old)
        source = source.replace(old, new)
    return source


def test_the_guards_accept_a_faithful_move():
    tree = ast.parse(_FAITHFUL_SESSIONS)
    assert _guard_findings(tree, "sessions", _app_module()) == []


@pytest.mark.parametrize("form", sorted(_EARLY_BINDINGS))
def test_the_guards_refuse_every_early_binding(form):
    tree = ast.parse(_trapped(form))
    assert _guard_findings(tree, "sessions", _app_module()) != [], form


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
    """Each import checks its own admin key: neither the last import nor the
    first one wins."""
    first, second = two_admin_keys
    assert first._admin_request_auth(_request(FIRST_KEY)) == "ok"
    assert first._admin_request_auth(_request(SECOND_KEY)) == "denied"
    assert second._admin_request_auth(_request(SECOND_KEY)) == "ok"
    assert second._admin_request_auth(_request(FIRST_KEY)) == "denied"


def test_rate_limit_scope_reads_the_module_that_registered_it(two_admin_keys):
    first, second = two_admin_keys
    assert first._rate_limit_scope(_request(FIRST_KEY)) == "key:admin"
    assert first._rate_limit_scope(_request(SECOND_KEY)) == "ip:192.0.2.10"
    assert second._rate_limit_scope(_request(SECOND_KEY)) == "key:admin"
    assert second._rate_limit_scope(_request(FIRST_KEY)) == "ip:192.0.2.10"


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
    # The same refusal is new to the second import: it fills its own dict only.
    assert second._should_audit_refusal("192.0.2.10", "proxied") is True
    assert second._should_audit_refusal("192.0.2.10", "proxied") is False
    assert list(second._refusal_audit_seen) == [("192.0.2.10", "proxied")]
    assert list(first._refusal_audit_seen) == [("192.0.2.10", "proxied")]
    assert first._refusal_audit_seen is not second._refusal_audit_seen


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


async def test_admin_route_of_the_second_import_takes_its_own_admin_key(
    two_admin_keys, monkeypatch
):
    """Mirror of the first import's test: the new app does not check the admin
    key of the module imported before it."""
    import plugboard.ratelimit as rl

    _, second = two_admin_keys
    monkeypatch.setattr(rl, "_LIMIT", 0)
    async with AsyncClient(
        transport=ASGITransport(app=second.app), base_url="http://test"
    ) as client:
        ok = await client.get(
            "/metrics", headers={"Authorization": f"Bearer {SECOND_KEY}"}
        )
        refused = await client.get(
            "/metrics", headers={"Authorization": f"Bearer {FIRST_KEY}"}
        )
    assert ok.status_code == 200, ok.text
    assert refused.status_code == 401


def test_dependencies_of_each_import_are_its_own_objects(two_imports):  # noqa: F811
    """Every plugboard.app dependency of each app is the object its own module
    exposes under that name: the second app never runs the first one's
    _require_admin or _resolve_client (M1)."""
    first, second = two_imports
    assert second._require_admin is not first._require_admin
    assert second._resolve_client is not first._resolve_client
    for module in (first, second):
        own = [
            call
            for call in auth_core_snapshot.all_dependency_calls(module.app)
            if call.__module__ == APP_MODULE
        ]
        assert {"_require_admin", "_resolve_client"} <= {c.__qualname__ for c in own}
        wrong = sorted(
            {c.__qualname__ for c in own if getattr(module, c.__qualname__) is not c}
        )
        assert wrong == [], module
