"""Moving the auth core out of app.py changes nothing (PLU-97): the same
dependencies on every route, by name and by object; no name gone from
plugboard.app; the same plugboard modules loaded at import, in the same order
as 919f01f; the same authentication decisions (tests/auth_decisions.py)."""

import json

from tests import auth_core_snapshot, auth_decisions

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


def test_moved_constant_values_are_unchanged():
    """The values of the moved constants, not only their names: a proxy
    header, a loopback host or a state-changing method lost or added in the
    copy is a change of behaviour (security review I3)."""
    assert auth_core_snapshot.moved_constants() == _expected("moved_constants")


def test_import_order_is_unchanged():
    assert auth_core_snapshot.import_order() == _expected("import_order")


def test_auth_decisions_are_unchanged():
    """The same answer, principal, MFA proof, key id, audit entries, log lines
    and lockout counters as 919f01f for every case of the matrix (PLU-97)."""
    expected = json.loads(auth_decisions.FIXTURE.read_text(encoding="utf-8"))
    actual = auth_decisions.run_all()
    changed = sorted(
        f"{case}/{name}"
        for case, by_function in expected.items()
        for name, calls in by_function.items()
        if actual.get(case, {}).get(name) != calls
    )
    assert changed == []
    assert actual == expected
