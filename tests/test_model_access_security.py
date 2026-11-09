"""Negative security tests for per-key model access (exact effective identity)."""

import pytest
from fastapi import HTTPException

import plugboard.router as router
from plugboard.app import _model_is_compatible, _model_route_guard
from plugboard.model_identity import model_access_matches

_A = {"id": "test-provider"}
_B = {"id": "other-provider"}


@pytest.mark.parametrize(
    "allowed, owner, native, expected",
    [
        (["test-provider:fake:model-v1"], "test-provider", "fake:model-v1", True),
        (["fake:model-v1"], "test-provider", "fake:model-v1", True),
        # substring / prefix / suffix must never grant access
        (["fake"], "test-provider", "fake:model-v1", False),
        (["test-provider"], "test-provider", "fake:model-v1", False),
        (["model-v1"], "test-provider", "fake:model-v1", False),
        (["fake:model"], "test-provider", "fake:model-v1", False),
        (["fake:model-v1"], "test-provider", "fake:model-v1-pro", False),
        # cross-provider: qualified grant does not cover another owner
        (["test-provider:fake:model-v1"], "other-provider", "fake:model-v1", False),
        # case and whitespace are not normalised into a grant
        (["FAKE:MODEL-V1"], "test-provider", "fake:model-v1", False),
        ([" fake:model-v1"], "test-provider", "fake:model-v1", False),
    ],
)
def test_model_access_matches_exact(allowed, owner, native, expected):
    assert model_access_matches(allowed, owner, native) is expected


def test_guard_none_for_admin_and_unrestricted():
    assert _model_route_guard("admin", {"allowed_models": ["x"]}) is None
    assert _model_route_guard("", {"allowed_models": ["x"]}) is None
    assert _model_route_guard("c", {"allowed_models": []}) is None


def test_guard_policy_is_snapshotted():
    rec = {"client_id": "c", "allowed_models": ["test-provider:fake:model-v1"]}
    guard = _model_route_guard("c", rec)
    assert guard is not None
    rec["allowed_models"].append("other-provider:other:model-v2")
    with pytest.raises(HTTPException) as exc:
        guard(_B, "other:model-v2")
    assert exc.value.status_code == 403
    guard(_A, "fake:model-v1")


def test_guard_rejects_cross_provider():
    guard = _model_route_guard(
        "c", {"client_id": "c", "allowed_models": ["test-provider:fake:model-v1"]}
    )
    assert guard is not None
    with pytest.raises(HTTPException) as exc:
        guard(_B, "fake:model-v1")
    assert exc.value.status_code == 403


def test_incompatible_when_unresolvable(monkeypatch):
    def boom(_):
        raise ValueError("no route")

    monkeypatch.setattr("plugboard.app.resolve_provider_and_model", boom)
    rec = {"client_id": "c", "allowed_models": ["test-provider:fake:model-v1"]}
    assert _model_is_compatible("whatever", "c", rec) is False


@pytest.mark.asyncio
async def test_forward_guards_fallback_before_any_call(monkeypatch):
    """A fallback candidate outside the policy is refused before being called."""
    monkeypatch.setattr(
        router, "_resolve_all_providers", lambda _m: [(_B, "other:model-v2")]
    )
    monkeypatch.setattr(
        "plugboard.circuit_breaker.check_circuit",
        lambda _m: {"allowed": True, "fallback": None},
    )
    guard = _model_route_guard(
        "c", {"client_id": "c", "allowed_models": ["test-provider:fake:model-v1"]}
    )
    with pytest.raises(HTTPException) as exc:
        await router.forward(
            {"model": "test-provider:fake:model-v1", "messages": []},
            route_guard=guard,
        )
    assert exc.value.status_code == 403
    assert router.served_by() is None


@pytest.mark.asyncio
async def test_forward_stream_guard_before_call(monkeypatch):
    monkeypatch.setattr(
        router, "resolve_provider_and_model", lambda _m: (_B, "other:model-v2")
    )
    guard = _model_route_guard(
        "c", {"client_id": "c", "allowed_models": ["test-provider:fake:model-v1"]}
    )
    gen = router.forward_stream(
        {"model": "test-provider:fake:model-v1", "messages": []}, route_guard=guard
    )
    with pytest.raises(HTTPException) as exc:
        await gen.__anext__()
    assert exc.value.status_code == 403
