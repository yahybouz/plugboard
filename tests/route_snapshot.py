"""Snapshot of what the admin routes split must not change (PLU-36).

The route table, the admin OpenAPI document and the effects of a cold import
of ``plugboard.app`` are frozen in ``tests/fixtures/admin_routes_snapshot.json``
and compared by ``tests/test_admin_routes_guard.py``. The fixture is written
once on the code before the split, with ``python -m tests.route_snapshot``.
"""

from __future__ import annotations

import copy
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from fastapi.routing import APIRoute

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "admin_routes_snapshot.json"
_PLUGBOARD_PATH = re.compile(r"\bplugboard(?:\.\w+)*\.(\w+)")

# Run in a fresh interpreter: import the app, then report what the import left
# behind (loaded plugboard modules, files written under the data directory).
_PROBE = """
import json
import os
import sys
from pathlib import Path

import plugboard.app  # noqa: F401

data = Path(os.environ["PLUGBOARD_DATA"])
effects = {
    "modules": sorted(
        name
        for name in sys.modules
        if name.startswith("plugboard") and not name.startswith(("plugboard.admin_routes", "plugboard.chat_path"))
    ),
    "data_files": sorted(
        p.relative_to(data).as_posix() for p in data.rglob("*") if p.is_file()
    ) if data.exists() else [],
}
sys.stdout.flush()
print("EFFECTS " + json.dumps(effects, sort_keys=True), flush=True)
"""


def _type_name(t) -> str | None:
    """A type's name without its module path: a model may change module."""
    if t is None:
        return None
    t = getattr(t, "value", t)  # fastapi DefaultPlaceholder
    text = t.__qualname__ if isinstance(t, type) else repr(t)
    if " at 0x" in text:
        raise ValueError(f"no stable name for {text}")
    return _PLUGBOARD_PATH.sub(r"\1", text)


def _call_name(call) -> str:
    return f"{call.__module__}.{call.__qualname__}"


def _dependencies(dependant) -> list:
    return [[_call_name(d.call), _dependencies(d)] for d in dependant.dependencies]


def _params(dependant) -> list:
    out = []
    for where in ("path", "query", "header", "cookie", "body"):
        for f in getattr(dependant, f"{where}_params"):
            out.append(
                [
                    where,
                    f.name,
                    f.alias,
                    f.field_info.is_required(),
                    _type_name(f.field_info.annotation),
                ]
            )
    return out


def _include_context(route) -> dict:
    """What an include_router() call adds to every route of the router.

    Kept outside the inner routes: a dependency added at inclusion changes
    neither the inner route table nor the OpenAPI document.
    """
    ctx = route.include_context
    return {
        "prefix": ctx.prefix,
        "tags": list(ctx.tags),
        "dependencies": [
            [_call_name(d.dependency), list(getattr(d, "scopes", None) or [])]
            for d in ctx.dependencies
        ],
        "deprecated": ctx.deprecated,
        "include_in_schema": ctx.include_in_schema,
    }


def _route(route) -> dict:
    if not isinstance(route, APIRoute):
        inner = getattr(route, "original_router", None)
        if inner is not None:
            return {
                "kind": "included_router",
                "include_context": _include_context(route),
                "routes": [_route(r) for r in inner.routes],
            }
        return {"kind": type(route).__name__, "path": getattr(route, "path", None)}
    return {
        "kind": "api",
        "path": route.path,
        "methods": sorted(route.methods),
        "name": route.name,
        "coroutine": inspect.iscoroutinefunction(route.endpoint),
        "response_model": _type_name(route.response_model),
        "status_code": route.status_code,
        "response_class": _type_name(route.response_class),
        "include_in_schema": route.include_in_schema,
        "summary": route.summary,
        "description": route.description,
        "tags": list(route.tags or []),
        "deprecated": route.deprecated,
        "operation_id": route.operation_id,
        "unique_id": route.unique_id,
        "dependencies": _dependencies(route.dependant),
        "params": _params(route.dependant),
    }


def route_table(app) -> list:
    return [_route(r) for r in app.routes]


def admin_openapi(app) -> dict:
    doc = copy.deepcopy(app.openapi())
    doc["info"]["version"] = "<version>"  # a release must not break the guard
    return json.loads(json.dumps(doc, sort_keys=True))


def _log_line(line: str) -> list:
    if line.startswith("{"):
        record = json.loads(line)
        return [record.get("level"), record.get("logger"), record.get("msg")]
    return ["raw", "", line]


def import_effects() -> dict:
    """Logs, loaded plugboard modules and data files of a cold import."""
    with tempfile.TemporaryDirectory(prefix="plu36-import-") as tmp:
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
    logs: list = []
    effects: dict = {}
    for line in (proc.stdout + proc.stderr).splitlines():
        if line.startswith("EFFECTS "):
            effects = json.loads(line[len("EFFECTS ") :])
        else:
            logs.append(_log_line(line))
    return {
        "logs": logs,
        "modules": effects["modules"],
        "data_files": effects["data_files"],
    }


def main() -> None:
    import tests.conftest  # noqa: F401  (same environment as the suite)
    from plugboard.app import app

    snapshot = {
        "routes": route_table(app),
        "openapi": admin_openapi(app),
        "import_effects": import_effects(),
    }
    FIXTURE.write_text(
        json.dumps(snapshot, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
