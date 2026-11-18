"""Moving the auth core out of app.py changes nothing (PLU-97): the same
dependencies on every route, by name and by object; no name gone from
plugboard.app; the same plugboard modules loaded at import, in the same order
as 919f01f."""

import json

from tests import auth_core_snapshot

KNOWN_DEPENDENCIES = {"_require_admin", "_resolve_client"}


def _expected(key):
    return json.loads(auth_core_snapshot.FIXTURE.read_text(encoding="utf-8"))[key]


def test_route_dependencies_are_unchanged():
    from plugboard.app import app

    assert auth_core_snapshot.dependency_calls(app) == _expected("dependency_calls")


def test_dependencies_of_the_app_module_are_its_own_objects():
    """Every dependency of plugboard.app is the object the module exposes under
    that name: dependency_overrides and identity checks keep working."""
    import plugboard.app as app_module

    calls = auth_core_snapshot.all_dependency_calls(app_module.app)
    own = [c for c in calls if c.__module__ == "plugboard.app"]
    assert KNOWN_DEPENDENCIES <= {c.__qualname__ for c in own}
    wrong = sorted(
        {
            c.__qualname__
            for c in own
            if getattr(app_module, c.__qualname__, None) is not c
        }
    )
    assert wrong == []


def test_no_name_left_plugboard_app():
    gone = sorted(set(_expected("app_surface")) - set(auth_core_snapshot.app_surface()))
    assert gone == []


def test_import_order_is_unchanged():
    assert auth_core_snapshot.import_order() == _expected("import_order")
