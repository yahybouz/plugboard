"""Snapshot of what moving the auth core out of app.py must not change (PLU-97).

The dependencies of every route (by qualified name), the names plugboard.app
exposes and the order in which a cold import loads the plugboard modules are
frozen in ``tests/fixtures/auth_core_snapshot.json`` and compared by
``tests/test_auth_core_guard.py``. The fixture is written once on the code
before the move, with ``python -m tests.auth_core_snapshot``. The route table,
the admin OpenAPI document and the sorted import effects stay frozen by the
PLU-36 guard; the client routes, exception handlers and middleware by the
PLU-95 guard.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

from fastapi.routing import APIRoute

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "auth_core_snapshot.json"
# Packages whose modules register code on the app: not import effects.
REGISTERING = ("plugboard.admin_routes", "plugboard.chat_path", "plugboard.auth_core")

_PROBE = f"""
import json
import sys

import plugboard.app  # noqa: F401

order = [
    name
    for name in sys.modules
    if name.startswith("plugboard") and not name.startswith({REGISTERING!r})
]
print("ORDER " + json.dumps(order), flush=True)
"""


def _qualified(call) -> str:
    return f"{call.__module__}.{call.__qualname__}"


def _nested(dependant):
    for d in dependant.dependencies:
        yield d.call
        yield from _nested(d)


def _entries(routes, prefix=""):
    """(path, callables) of every route and include context, routers walked."""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            ctx = route.include_context
            yield ctx.prefix, [d.dependency for d in ctx.dependencies]
            yield from _entries(inner.routes, ctx.prefix)
        elif isinstance(route, APIRoute):
            yield prefix + route.path, list(_nested(route.dependant))


def all_dependency_calls(app) -> list:
    """Every dependency callable of the app, nested and include-level ones."""
    return [call for _, calls in _entries(app.routes) for call in calls]


def dependency_calls(app) -> list:
    """[path, [qualified name of each dependency, in order]] per entry."""
    return [
        [path, [_qualified(c) for c in calls]] for path, calls in _entries(app.routes)
    ]


def app_surface() -> list:
    """Names of plugboard.app that are neither modules nor typing aliases."""
    import plugboard.app as app_module

    return sorted(
        name
        for name, value in vars(app_module).items()
        if not name.startswith("__")
        and not isinstance(value, types.ModuleType)
        and getattr(value, "__module__", None) != "typing"
    )


def import_order() -> list:
    """The plugboard modules a cold import loads, in load order."""
    with tempfile.TemporaryDirectory(prefix="plu97-import-") as tmp:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": tmp,
            "PLUGBOARD_DATA": str(Path(tmp) / "data"),
            "CODEX_HOME": str(Path(tmp) / "codex"),
            "PLUGBOARD_SKIP_DOTENV": "1",
            "PLUGBOARD_OLLAMA_CLOUD_AUTOSYNC": "0",
            "API_KEY": "testkey-conftest",
            "ADMIN_KEY": "adminkey-conftest",
        }
        proc = subprocess.run(
            [sys.executable, "-c", _PROBE],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
    for line in proc.stdout.splitlines():
        if line.startswith("ORDER "):
            return json.loads(line[len("ORDER ") :])
    raise AssertionError("the import probe printed no ORDER line")


def main() -> None:
    import tests.conftest  # noqa: F401  (same environment as the suite)
    from plugboard.app import app

    snapshot = {
        "dependency_calls": dependency_calls(app),
        "app_surface": app_surface(),
        "import_order": import_order(),
    }
    FIXTURE.write_text(
        json.dumps(snapshot, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
