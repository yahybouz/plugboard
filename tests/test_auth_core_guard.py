"""Moving the auth core out of app.py changes nothing (PLU-97): the same
dependencies on every route, by name and by object; no name gone from
plugboard.app; the same plugboard modules loaded at import, in the same order
as 919f01f; the same authentication decisions (tests/auth_decisions.py)."""

import ast
import json
from pathlib import Path

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


# hmac.compare_digest sites of the auth core, wherever it lives: plugboard/app.py
# before the move, plugboard/auth_core/ after it. No decision shows a
# comparison that leaks timing, so the sites are frozen here.
COMPARE_DIGEST_SITES = {"_is_admin_key": 1, "_admin_session_nonce": 1}
COMPARED_SECRETS = {"token", "effective_admin", "signature", "expected_signature"}


def _auth_core_sources() -> list:
    plugboard_dir = Path(auth_decisions.ROOT) / "plugboard"
    files = [plugboard_dir / "app.py"]
    files += sorted((plugboard_dir / "auth_core").glob("**/*.py"))
    return [ast.parse(f.read_text(encoding="utf-8"), filename=str(f)) for f in files]


def _is_compare_digest(node) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "compare_digest"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "hmac"
    )


def test_secrets_are_compared_in_constant_time():
    """The admin key and the session signature are compared with
    hmac.compare_digest, never ``==`` or ``!=``, in app.py and auth_core/
    together (PLU-97 security review, finding 6)."""
    trees = _auth_core_sources()
    total = sum(1 for t in trees for n in ast.walk(t) if _is_compare_digest(n))
    assert total == sum(COMPARE_DIGEST_SITES.values())
    found = {}
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in COMPARE_DIGEST_SITES:
                assert node.name not in found, f"{node.name} defined twice"
                calls = [n for n in ast.walk(node) if _is_compare_digest(n)]
                plain = [
                    ast.unparse(n)
                    for n in ast.walk(node)
                    if isinstance(n, ast.Compare)
                    and any(isinstance(op, (ast.Eq, ast.NotEq)) for op in n.ops)
                    and {
                        x.id
                        for x in [n.left, *n.comparators]
                        if isinstance(x, ast.Name)
                    }
                    & COMPARED_SECRETS
                ]
                found[node.name] = (len(calls), plain)
    assert found == {name: (count, []) for name, count in COMPARE_DIGEST_SITES.items()}
