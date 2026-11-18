"""Invariants of the admin routes split out of app.py (PLU-36)."""

import ast
import importlib
import inspect
import pkgutil
import sys
from pathlib import Path

from fastapi.routing import APIRoute

ROOT = Path(__file__).resolve().parents[1]
ADMIN_ROUTES_DIR = ROOT / "plugboard" / "admin_routes"
ADMIN_ROUTES_PACKAGE = "plugboard.admin_routes"
APP_MODULE = "plugboard.app"

# Names the tests patch on plugboard.app (AST inventory of tests/ at 672118e):
# an admin route module must read them through `core.`, never bind them.
PATCH_POINTS = frozenset(
    {
        "_ADMIN_KEY",
        "_API_KEY",
        "_BUDGET_RESERVE_TOKENS",
        "_BULK_INTERNET_CONCURRENCY",
        "_CACHE_RESPONSE_FLOOR_MS",
        "_DEV_MODE",
        "_INFLIGHT_CHAT_MAX",
        "_INFLIGHT_WAIT_S",
        "_LAST_REFRESH_AT",
        "_MAX_BODY_BYTES",
        "_MODEL_REFRESH_COOLDOWN_S",
        "_REFUSAL_AUDIT_MAX_KEYS",
        "_REFUSAL_AUDIT_WINDOW_S",
        "_STATIC_DIR",
        "_STATIC_DIR_RESOLVED",
        "_admin_request_auth",
        "_apply_key_record",
        "_audit",
        "_bill_streaming",
        "_check_and_alert",
        "_check_price_known",
        "_check_stored_secrets",
        "_client_address",
        "_get_request_key_record",
        "_hold_budget",
        "_inflight_chat_active",
        "_inflight_chat_sem",
        "_is_admin_key",
        "_is_admin_session",
        "_model_is_compatible",
        "_model_route_guard",
        "_record_session_turn",
        "_require_public_host",
        "_schedule_initial_discovery",
        "_send_spending_alert",
        "_served_by",
        "_test_openai_endpoint",
        "_usage",
        "check_all_models_internet_access",
        "check_model_internet_access",
        "cli_refusal",
        "discover_models",
        "forward",
        "forward_stream",
        "get_key_record",
        "get_provider",
        "invalidate_models_cache",
        "list_models",
        "load_providers",
        "resolve_provider_and_model",
        "update_provider",
    }
)
# Read on plugboard.app by the tests (directly or as "module:attr") and by
# decision_api.py / embeddings_api.py: these names must stay reachable there.
READ_SURFACE = frozenset(
    {
        "HTTPException",
        "KEY_PREFIXES",
        "_ADMIN_COOKIE_NAME",
        "_ADMIN_SESSION_MAX_AGE",
        "_BULK_INTERNET_PER_MODEL_TIMEOUT_S",
        "_BodySizeLimitMiddleware",
        "_LOGIN_HTML_TEMPLATE",
        "_MAX_HELD",
        "_ModelNotAllowed",
        "_RequestIdMiddleware",
        "_auto_login_mode",
        "_background_tasks",
        "_bill_completion",
        "_billing_target",
        "_bind_host",
        "_check_dashboard_auth",
        "_check_model_access",
        "_check_spending_limit",
        "_cli_error_response",
        "_complete_events",
        "_create_admin_session_token",
        "_default_monthly_limit_usd",
        "_default_rpm_limit",
        "_done_event_at",
        "_estimate_call_cost",
        "_get_inflight_sem",
        "_host_header_name",
        "_inflight_chat_dep",
        "_is_loopback_address",
        "_issue_local_login_token",
        "_lifespan",
        "_model_not_allowed_handler",
        "_model_not_served_response",
        "_pad_cache_hit_response",
        "_providers_busy_response",
        "_quota_error_response",
        "_refusal_audit_seen",
        "_reject_comment_valued_secrets",
        "_require_admin",
        "_require_admin_key_outside_dev",
        "_require_mfa_step_up",
        "_resolve_client",
        "_spending_scope",
        "_suggestion_action",
        "_take_budget_hold",
        "_value_error_handler",
        "_warn_trusted_proxies",
        "_warned_auto_login_values",
        "admin_catalog_history",
        "admin_models_refresh",
        "app",
        "chat_completions",
        "get_providers",
        "health_detailed",
        "log",
        "models",
        "models_v2",
        "oauth_disconnect",
        "trusted_proxy_warning",
    }
)


# The modules of the design (one per contiguous block of admin routes): the
# invariants above must not pass on an empty package.
EXPECTED_MODULES = frozenset(
    {
        "auth",
        "direct_providers",
        "jobs",
        "keys",
        "model_probes",
        "monitoring",
        "oauth",
        "ollama",
        "pricing",
        "providers",
        "usage",
    }
)


def _app_module():
    return importlib.import_module(APP_MODULE)


def _admin_route_modules() -> list:
    """Every submodule of plugboard.admin_routes; none before the split."""
    try:
        package = importlib.import_module(ADMIN_ROUTES_PACKAGE)
    except ModuleNotFoundError as exc:
        if exc.name != ADMIN_ROUTES_PACKAGE:
            raise
        return []
    return [
        importlib.import_module(f"{ADMIN_ROUTES_PACKAGE}.{info.name}")
        for info in pkgutil.iter_modules(package.__path__)
    ]


def _absolute(node: ast.ImportFrom) -> str:
    """The module an ImportFrom names, relative imports resolved."""
    if not node.level:
        return node.module or ""
    base = ADMIN_ROUTES_PACKAGE.split(".")[: -(node.level - 1) or None]
    return ".".join(base + ([node.module] if node.module else []))


def _is_app_module(name: str) -> bool:
    return name == APP_MODULE or name.startswith(APP_MODULE + ".")


def _app_imports(tree: ast.AST) -> list:
    """Every way a module could reach the app module object, by line."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [
                (node.lineno, a.name) for a in node.names if _is_app_module(a.name)
            ]
        elif isinstance(node, ast.ImportFrom):
            module = _absolute(node)
            if _is_app_module(module):
                found.append((node.lineno, f"from {module} import"))
            elif module == "plugboard" and any(a.name == "app" for a in node.names):
                found.append((node.lineno, "from plugboard import app"))
        elif isinstance(node, ast.Attribute):
            if (
                node.attr == "modules"
                and isinstance(node.value, ast.Name)
                and node.value.id == "sys"
            ):
                found.append((node.lineno, "sys.modules"))
        elif isinstance(node, ast.Call):
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name in ("import_module", "__import__"):
                found.append((node.lineno, name))
    return found


def test_no_admin_route_module_binds_a_patch_point():
    bound = {
        module.__name__: sorted(PATCH_POINTS & set(vars(module)))
        for module in _admin_route_modules()
    }
    assert {name: names for name, names in bound.items() if names} == {}


def test_admin_route_modules_never_import_the_app_module():
    found = {}
    for path in sorted(ADMIN_ROUTES_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hits = _app_imports(tree)
        if hits:
            found[path.name] = hits
    assert found == {}


def test_each_module_registers_through_register():
    for module in _admin_route_modules():
        assert list(inspect.signature(module.register).parameters) == [
            "app",
            "core",
        ], module.__name__


def test_moved_code_logs_under_the_app_logger():
    app_module = _app_module()
    for module in _admin_route_modules():
        if hasattr(module, "log"):
            assert module.log is app_module.log, module.__name__


def test_names_read_on_the_app_module_are_still_there():
    app_module = _app_module()
    missing = sorted(
        n for n in PATCH_POINTS | READ_SURFACE if not hasattr(app_module, n)
    )
    assert missing == []


def test_reexported_handlers_are_the_registered_endpoints():
    app_module = _app_module()
    endpoints = {
        r.name: r.endpoint for r in app_module.app.routes if isinstance(r, APIRoute)
    }
    for name in sorted((PATCH_POINTS | READ_SURFACE) & endpoints.keys()):
        assert getattr(app_module, name) is endpoints[name], name


def test_admin_route_modules_are_designed_ones():
    """Some module is found, and only modules of the design (the last move
    tightens this to equality)."""
    found = {m.__name__.rsplit(".", 1)[1] for m in _admin_route_modules()}
    assert found
    assert sorted(found - EXPECTED_MODULES) == []


async def test_reexported_probe_reads_the_module_that_registered_it(monkeypatch):
    """After plugboard.app is imported again (E-25 does it), the first module's
    bulk probe still reads the first module's patch points (PLU-36)."""
    import plugboard

    first = importlib.import_module("plugboard.app")
    monkeypatch.setitem(sys.modules, "plugboard.app", first)  # restored at teardown
    monkeypatch.setattr(plugboard, "app", first)
    del sys.modules["plugboard.app"]
    second = importlib.import_module("plugboard.app")
    assert second is not first

    probed = []

    async def _list_models():
        return [
            {
                "id": "anthropic-direct:claude-haiku-5-5",
                "owned_by": "anthropic-direct",
                "provider": "anthropic-direct",
            }
        ]

    async def _probe(body, _=None):
        probed.append(body["model_id"])
        return {"model_id": body["model_id"], "internet_access": False, "status": "ok"}

    monkeypatch.setattr(first, "list_models", _list_models)
    monkeypatch.setattr(
        first,
        "load_providers",
        lambda: [
            {
                "id": "anthropic-direct",
                "name": "Anthropic Direct",
                "base_url": "https://api.anthropic.com",
                "provider_type": "cloud",
            }
        ],
    )
    monkeypatch.setattr(first, "check_model_internet_access", _probe)
    await first.check_all_models_internet_access({}, _=None)
    assert probed == ["anthropic-direct:claude-haiku-5-5"]


async def test_refresh_stamp_is_written_on_the_app_module(monkeypatch):
    """The refresh cooldown reads and writes _LAST_REFRESH_AT on plugboard.app
    (PLU-36): a second refresh within the cooldown answers 429."""
    app_module = importlib.import_module("plugboard.app")
    import plugboard.model_availability as availability

    async def _probes():
        return {"last_run_at": "now", "models": {"m": {}}}

    monkeypatch.setattr(availability, "run_connectivity_probes", _probes)
    monkeypatch.setattr(app_module, "_LAST_REFRESH_AT", 0.0)
    monkeypatch.setattr(app_module, "_MODEL_REFRESH_COOLDOWN_S", 60.0)
    monkeypatch.setattr(app_module, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "invalidate_models_cache", lambda: None)

    first = await app_module.admin_models_refresh(_=None)
    assert first["model_count"] == 1
    assert app_module._LAST_REFRESH_AT > 0
    second = await app_module.admin_models_refresh(_=None)
    assert second.status_code == 429
