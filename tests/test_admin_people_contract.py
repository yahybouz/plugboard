"""The people routes: admin schema only, outside the client contract (PB-AUTH-01)."""

import json
from pathlib import Path

from plugboard import api_contract

ROOT = Path(__file__).resolve().parents[1]


def test_people_routes_are_in_the_admin_schema_only():
    from plugboard.app import app

    paths = app.openapi()["paths"]
    assert {"get", "post"} <= set(paths["/admin/people"])
    assert "delete" in paths["/admin/people/{person_id}"]
    people = {
        ("GET", "/admin/people"),
        ("POST", "/admin/people"),
        ("DELETE", "/admin/people/{person_id}"),
    }
    assert not people & api_contract.PUBLIC_ROUTES


def test_people_routes_stay_out_of_the_published_contract():
    """PB-AUTH-01 adds nothing to the client contract: its routes are absent
    from the committed ``openapi.json`` paths and from ``mcp.json``.

    The contract version is no longer pinned here (D-81): another change may
    publish a newer one (3.1.0, ADR-039). The current version is kept the same
    in the code, both documents and the CHANGELOG by
    ``tests/test_api_contract.py::test_the_contract_version_is_the_same_everywhere``.
    """
    docs = ROOT / "docs" / "api"
    openapi = json.loads((docs / "openapi.json").read_text("utf-8"))
    assert not [path for path in openapi["paths"] if path.startswith("/admin/people")]
    mcp = json.loads((docs / "mcp.json").read_text("utf-8"))
    assert "/admin/people" not in json.dumps(mcp)
    names = [
        str(item.get("name") or item.get("uri") or "")
        for kind in ("tools", "resources", "prompts")
        for item in mcp.get(kind) or []
        if isinstance(item, dict)
    ]
    assert names  # the MCP document lists what it serves
    assert not [name for name in names if "people" in name.lower()]
