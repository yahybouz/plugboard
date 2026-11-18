"""The people routes: admin schema only, outside the client contract (PB-AUTH-01)."""

from plugboard import api_contract


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


def test_the_client_contract_version_does_not_move():
    assert api_contract.CONTRACT_VERSION == "3.0.1"
