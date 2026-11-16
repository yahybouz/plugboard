"""BUG-90: a provider's own 403 is never answered as the key's refusal.

A key allowed one provider's route asks for a bare name two providers
serve. Its route is tried; the other one is refused by the key's policy and
never called. When the allowed route fails (here with a 403 of the
provider), the client gets that provider's failure, not the key's
``forbidden_model``: a stable code a program reads as final, which would
stop it from retrying a model it may use. Only the gateway's own refusal of
the key is ``forbidden_model``.
"""

import pytest
from aioresponses import aioresponses
from fastapi import HTTPException

from plugboard import router
from plugboard.app import _model_route_guard
from plugboard.router import ProviderHTTPError
from tests.factories import FAKE_COMPLETION
from tests.test_route_pauses import two_providers  # noqa: F401 (fixture)
from tests.test_router_admission import BARE, ONLY_A, URL_A, URL_B, _body, _calls

FORBIDDEN = {"error": {"message": "Your organisation may not use this model"}}


def _guard():
    return _model_route_guard("c", {"client_id": "c", "allowed_models": [ONLY_A]})


async def test_a_provider_403_is_its_failure_not_the_keys_refusal(
    two_providers,  # noqa: F811
):
    with aioresponses() as mock:
        mock.post(URL_A, status=403, payload=FORBIDDEN)
        mock.post(URL_B, payload=FAKE_COMPLETION, repeat=True)
        with pytest.raises(Exception) as caught:
            await router.forward(_body(BARE), route_guard=_guard())
        assert _calls(mock, URL_A) == 1 and _calls(mock, URL_B) == 0
    assert not isinstance(caught.value, HTTPException), caught.value
    assert isinstance(caught.value, ProviderHTTPError)
    assert caught.value.status == 403
    assert caught.value.provider_id == "test-provider"


async def test_a_provider_500_is_its_failure_not_the_keys_refusal(
    two_providers,  # noqa: F811
):
    with aioresponses() as mock:
        mock.post(URL_A, status=500, payload={"error": "boom"}, repeat=True)
        with pytest.raises(Exception) as caught:
            await router.forward(_body(BARE), route_guard=_guard())
        assert _calls(mock, URL_B) == 0
    assert isinstance(caught.value, ProviderHTTPError), caught.value
    assert caught.value.status == 500


async def test_the_stream_answers_the_providers_403_too(two_providers):  # noqa: F811
    with aioresponses() as mock:
        mock.post(URL_A, status=403, payload=FORBIDDEN)
        mock.post(URL_B, payload=FAKE_COMPLETION, repeat=True)
        chunks = router.forward_stream(_body(BARE, stream=True), route_guard=_guard())
        with pytest.raises(Exception) as caught:
            await chunks.__anext__()
        assert _calls(mock, URL_B) == 0
    assert isinstance(caught.value, ProviderHTTPError), caught.value
    assert caught.value.status == 403


async def test_the_next_call_is_served_after_a_provider_403(
    two_providers,  # noqa: F811
):
    """Nothing lasting: the same key's next call reaches its route again."""
    with aioresponses() as mock:
        mock.post(URL_A, status=403, payload=FORBIDDEN)
        mock.post(URL_A, payload=FAKE_COMPLETION)
        with pytest.raises(ProviderHTTPError):
            await router.forward(_body(BARE), route_guard=_guard())
        await router.forward(_body(BARE), route_guard=_guard())
        assert _calls(mock, URL_A) == 2 and _calls(mock, URL_B) == 0
    assert router.served_by()[0]["id"] == "test-provider"


async def test_a_route_only_the_key_refuses_is_still_forbidden_model(
    two_providers,  # noqa: F811
):
    """The key's own refusal keeps its 403 when nothing it may use is left."""
    guard = _model_route_guard(
        "c", {"client_id": "c", "allowed_models": ["other-provider:fake:model-v1"]}
    )
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION, repeat=True)
        mock.post(URL_B, payload=FAKE_COMPLETION, repeat=True)
        with pytest.raises(HTTPException) as caught:
            await router.forward(_body(ONLY_A), route_guard=guard)
        assert _calls(mock, URL_A) == 0 and _calls(mock, URL_B) == 0
    assert caught.value.status_code == 403
