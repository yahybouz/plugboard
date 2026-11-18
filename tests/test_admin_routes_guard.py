"""The admin routes split changes nothing (PLU-36): same route table, same
admin OpenAPI document, same import effects as 672118e."""

import json

from tests import route_snapshot


def _expected(key):
    return json.loads(route_snapshot.FIXTURE.read_text(encoding="utf-8"))[key]


def test_route_table_is_unchanged():
    from plugboard.app import app

    assert route_snapshot.route_table(app) == _expected("routes")


def test_admin_openapi_document_is_unchanged():
    from plugboard.app import app

    assert route_snapshot.admin_openapi(app) == _expected("openapi")


def test_import_has_the_same_effects():
    assert route_snapshot.import_effects() == _expected("import_effects")
