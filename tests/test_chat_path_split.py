"""Invariants of moving the chat path out of app.py (PLU-95)."""

import ast
import copy
import importlib
import inspect
import pkgutil
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from tests.factories import FAKE_COMPLETION
from tests.test_admin_routes_split import PATCH_POINTS, _app_imports

ROOT = Path(__file__).resolve().parents[1]
CHAT_PATH_DIR = ROOT / "plugboard" / "chat_path"
CHAT_PATH_PACKAGE = "plugboard.chat_path"
APP_MODULE = "plugboard.app"
# The modules of the design (Q2 = yes, D-69: "account" included).
EXPECTED_MODULES = frozenset(
    {
        "account",
        "billing",
        "catalogue",
        "completions",
        "limits",
        "responses",
        "streaming",
        "v2",
    }
)


def _app_module():
    return importlib.import_module(APP_MODULE)


def _chat_path_modules() -> list:
    """Every submodule of plugboard.chat_path; none before the move."""
    try:
        package = importlib.import_module(CHAT_PATH_PACKAGE)
    except ModuleNotFoundError as exc:
        if exc.name != CHAT_PATH_PACKAGE:
            raise
        return []
    return [
        importlib.import_module(f"{CHAT_PATH_PACKAGE}.{info.name}")
        for info in pkgutil.iter_modules(package.__path__)
    ]


def _short_name(module) -> str:
    return module.__name__.rsplit(".", 1)[-1]


def test_no_chat_path_module_binds_a_patch_point():
    bound = {
        module.__name__: sorted(PATCH_POINTS & set(vars(module)))
        for module in _chat_path_modules()
    }
    assert {name: names for name, names in bound.items() if names} == {}


def test_chat_path_modules_never_import_the_app_module():
    found = {}
    for path in sorted(CHAT_PATH_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hits = _app_imports(tree) + [
            (node.lineno, "relative import")
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level
        ]
        if hits:
            found[path.name] = hits
    assert found == {}


def test_each_chat_path_module_registers_through_register():
    signatures = {
        module.__name__: list(inspect.signature(module.register).parameters)
        for module in _chat_path_modules()
    }
    assert {
        name: params for name, params in signatures.items() if params != ["app", "core"]
    } == {}


def test_chat_path_code_logs_under_the_app_logger():
    app_log = _app_module().log
    others = [
        module.__name__
        for module in _chat_path_modules()
        if hasattr(module, "log") and module.log is not app_log
    ]
    assert others == []


def test_chat_path_modules_are_designed_ones():
    """Exactly the modules of the design: none missing, none extra."""
    found = {_short_name(module) for module in _chat_path_modules()}
    assert sorted(found) == sorted(EXPECTED_MODULES)


def _is_namespace_export(node: ast.Name, parents: dict) -> bool:
    """``SimpleNamespace(x=x)`` at the end of ``register()``: the closure handed
    back to app.py, which re-exports it, not a call that skips ``core.``."""
    keyword = parents.get(node)
    call = parents.get(keyword)
    return (
        isinstance(keyword, ast.keyword)
        and keyword.arg == node.id
        and isinstance(call, ast.Call)
        and getattr(call.func, "id", "") == "SimpleNamespace"
    )


def _bare_patch_points(tree: ast.AST, names: frozenset) -> list:
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in names:
            if not _is_namespace_export(node, parents):
                found.append((node.lineno, node.id))
        elif isinstance(node, ast.arg) and node.arg in names:
            found.append((node.lineno, node.arg))
        elif isinstance(node, ast.alias) and (node.asname or node.name) in names:
            found.append((node.lineno, node.asname or node.name))
    return sorted(found)


def test_chat_path_reads_patch_points_only_through_core():
    """A patch point defined in plugboard.app is read as ``core.<name>`` at call
    time, even one whose closure is defined in the same register(): a bare
    name would miss the tests' patches of plugboard.app (R3, PLU-95)."""
    app = _app_module()
    names = frozenset(name for name in PATCH_POINTS if hasattr(app, name))
    found = {}
    for path in sorted(CHAT_PATH_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hits = _bare_patch_points(tree, names)
        if hits:
            found[path.name] = hits
    assert found == {}


def _usage_calls(path: Path) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name in {"append_usage", "calculate_cost"}:
                found.append((name, node.lineno))
    return found


def test_chat_path_prices_and_writes_usage_only_through_chat_billing():
    """Mirror of test_chat_usage_is_priced_and_written_in_one_place, which only
    reads app.py and router.py: the moved chat code also prices and writes
    usage only through plugboard.chat_billing."""
    for path in sorted(CHAT_PATH_DIR.glob("*.py")):
        assert _usage_calls(path) == [], path.name
        assert "def _replay_completion_as_sse" not in path.read_text(encoding="utf-8")


@pytest.fixture
def two_imports(monkeypatch):
    """plugboard.app imported twice, as E-25 does (PLU-36's pattern): the
    first module object is put back in sys.modules at teardown."""
    import plugboard

    first = importlib.import_module(APP_MODULE)
    monkeypatch.setitem(sys.modules, APP_MODULE, first)  # restored at teardown
    monkeypatch.setattr(plugboard, "app", first)
    del sys.modules[APP_MODULE]
    second = importlib.import_module(APP_MODULE)
    assert second is not first
    return first, second


def test_billing_target_reads_the_module_that_registered_it(two_imports, monkeypatch):
    first, second = two_imports
    monkeypatch.setattr(first, "_served_by", lambda: ({"id": "first"}, "m-first"))
    monkeypatch.setattr(second, "_served_by", lambda: ({"id": "second"}, "m-second"))
    assert first._billing_target({"model": "x"}) == ({"id": "first"}, "m-first")


def test_model_access_reads_the_module_that_registered_it(two_imports, monkeypatch):
    first, second = two_imports

    def _guard_raising(name):
        def _guard(provider, native):
            raise RuntimeError(name)

        return lambda *a, **k: _guard

    for module, name in ((first, "first"), (second, "second")):
        monkeypatch.setattr(module, "_model_route_guard", _guard_raising(name))
        monkeypatch.setattr(
            module, "resolve_provider_and_model", lambda m: ({"id": "p"}, "m")
        )
    with pytest.raises(RuntimeError, match="^first$"):
        first._check_model_access("acme", "p:m")


async def test_chat_of_the_first_import_calls_its_own_forward(
    _providers_file, two_imports, monkeypatch
):
    """POST /v1/chat/completions on the first module's app reaches the first
    module's forward, not the second's (PLU-95)."""
    import plugboard.ratelimit as rl

    first, second = two_imports
    calls = []

    def _forward_of(name):
        async def _forward(body, **_kwargs):
            calls.append(name)
            return copy.deepcopy(FAKE_COMPLETION)

        return _forward

    monkeypatch.setattr(rl, "_LIMIT", 0)
    for module, name in ((first, "first"), (second, "second")):
        monkeypatch.setattr(module, "_API_KEY", "testkey")
        monkeypatch.setattr(module, "_ADMIN_KEY", "testkey")
        monkeypatch.setattr(module, "forward", _forward_of(name))
    async with AsyncClient(
        transport=ASGITransport(app=first.app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer testkey"},
            json={
                "model": "test-provider:fake:model-v1",
                "messages": [{"role": "user", "content": "Hi"}],
                "cache": False,
            },
        )
    assert resp.status_code == 200, resp.text
    assert calls == ["first"]
