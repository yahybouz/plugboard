"""Snapshot of what moving the chat path out of app.py must not change (PLU-95).

The client routes of the route table with their place, the client API
contract, the exception handlers and the middleware stack are frozen in
``tests/fixtures/chat_path_snapshot.json`` and compared by
``tests/test_chat_path_guard.py``. The fixture is written once on the code
before the move, with ``python -m tests.chat_path_snapshot``. The whole route
table, the admin OpenAPI document and the import effects stay frozen by the
PLU-36 guard (``tests/route_snapshot.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

from tests import route_snapshot

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "chat_path_snapshot.json"
CLIENT_PREFIXES = ("/v1/", "/v2/", "/health", "/usage/")
ALSO_FROZEN = frozenset({"/admin/inflight"})  # declared among the client routes


def _paths(row: dict) -> list:
    if row["kind"] == "included_router":
        return [p for inner in row["routes"] for p in _paths(inner)]
    return [row.get("path") or ""]


def client_routes(app) -> list:
    """[place in app.routes, row] of every entry that serves a client path."""
    return [
        [place, row]
        for place, row in enumerate(route_snapshot.route_table(app))
        if any(p.startswith(CLIENT_PREFIXES) or p in ALSO_FROZEN for p in _paths(row))
    ]


def client_contract() -> dict:
    """The client API contract, version included (CONTRACT_VERSION)."""
    from plugboard.api_contract import build_openapi

    return json.loads(json.dumps(build_openapi(), sort_keys=True))


def _qualified(obj) -> str:
    return f"{obj.__module__}.{obj.__qualname__}"


def exception_handlers(app) -> list:
    """[exception class or status code, handler], in registration order."""
    return [
        [key if isinstance(key, int) else _qualified(key), _qualified(handler)]
        for key, handler in app.exception_handlers.items()
    ]


def middleware(app) -> list:
    """The user middleware stack as Starlette keeps it (outermost first)."""
    return [
        [
            _qualified(m.cls),
            json.loads(
                json.dumps([list(m.args), m.kwargs], sort_keys=True, default=repr)
            ),
        ]
        for m in app.user_middleware
    ]


def main() -> None:
    import tests.conftest  # noqa: F401  (same environment as the suite)
    from plugboard.app import app

    snapshot = {
        "client_routes": client_routes(app),
        "client_contract": client_contract(),
        "exception_handlers": exception_handlers(app),
        "middleware": middleware(app),
    }
    FIXTURE.write_text(
        json.dumps(snapshot, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
