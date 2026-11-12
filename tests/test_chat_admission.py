"""Chat admission under load (PLU-14, PLU-15).

PLU-14 (D-32): the global chat cap is 8 requests at once with a 10 s wait, in
the code and in the OVH deployment. With one chat at a time and no wait, the
cap came before the provider queue: a second application got 503 at once
while a provider capped at one request (Ollama) could have queued it.

Providers are faked with aioresponses: no network, no inference.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aioresponses import CallbackResult, aioresponses

from tests.factories import FAKE_COMPLETION, PROVIDER_A
from tests.test_ovh_catalog_jobs import _gateway

AUTH = {"Authorization": "Bearer testkey"}
URL_A = f"{PROVIDER_A['base_url']}/v1/chat/completions"
ONLY_A = f"{PROVIDER_A['id']}:fake:model-v1"


@pytest.fixture
def app_mod():
    """The app; conftest gives each test a fresh global semaphore."""
    import plugboard.app as app_mod

    return app_mod


@pytest.fixture
def one_at_a_time(_providers_file):
    """A provider that takes one request at a time, like Ollama in production."""
    from plugboard import router

    capped = {**PROVIDER_A, "max_concurrency": 1}
    _providers_file.write_text(json.dumps({"providers": [capped]}))
    router.invalidate_models_cache()
    return capped


class _Upstream:
    """Fake provider whose first answer waits for ``gate``.

    Pass ``callback=upstream.reply`` (aioresponses awaits a bound async method).
    """

    def __init__(self):
        self.calls, self.gate = 0, asyncio.Event()

    async def reply(self, url, **kwargs):
        self.calls += 1
        if self.calls == 1:
            await self.gate.wait()
        return CallbackResult(payload=FAKE_COMPLETION)


def _chat(model=ONLY_A, **extra):
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "cache": False,
        **extra,
    }


async def _until(predicate, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never met")


# ── PLU-14: cap of 8 chats, 10 s wait ───────────────────────────────────────


def test_code_defaults_cap_eight_chats_with_a_ten_second_wait(app_mod):
    assert app_mod._INFLIGHT_CHAT_MAX == 8
    assert app_mod._INFLIGHT_WAIT_S == 10.0


def test_ovh_deployment_caps_eight_chats_with_a_ten_second_wait():
    env = _gateway()["environment"]
    assert env["PLUGBOARD_MAX_INFLIGHT_CHAT"] == "8"
    assert env["PLUGBOARD_INFLIGHT_WAIT_S"] == "10"


@pytest.fixture
def ovh_settings(app_mod, monkeypatch):
    """The global cap and wait of the OVH deployment (code default when unset)."""
    env = _gateway()["environment"]
    monkeypatch.setattr(
        app_mod, "_INFLIGHT_CHAT_MAX", int(env["PLUGBOARD_MAX_INFLIGHT_CHAT"])
    )
    wait = env.get("PLUGBOARD_INFLIGHT_WAIT_S")
    if wait is not None:
        monkeypatch.setattr(app_mod, "_INFLIGHT_WAIT_S", float(wait))
    return app_mod


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v2/chat/completions"])
async def test_second_chat_waits_for_a_provider_at_one_instead_of_503(
    client, one_at_a_time, ovh_settings, path
):
    """PLU-14 criterion, with the OVH settings: the second request passes the
    global cap, queues behind the first one at the provider and is served
    when it ends, no 503 at once."""
    from plugboard import provider_admission

    upstream = _Upstream()
    with aioresponses() as mock:
        mock.post(URL_A, callback=upstream.reply, repeat=True)
        first = asyncio.create_task(client.post(path, headers=AUTH, json=_chat()))
        await _until(lambda: upstream.calls == 1)
        second = asyncio.create_task(client.post(path, headers=AUTH, json=_chat()))
        await _until(lambda: provider_admission.waiting(PROVIDER_A["id"]) == 1)
        sem = ovh_settings._get_inflight_sem()
        both_hold_a_slot = sem._value == ovh_settings._INFLIGHT_CHAT_MAX - 2
        await asyncio.sleep(0.3)
        waited = not second.done()
        upstream.gate.set()
        r1, r2 = await asyncio.gather(first, second)
    assert waited, f"answered at once: {r2.status_code} {r2.text}"
    assert both_hold_a_slot
    assert r1.status_code == r2.status_code == 200, r2.text
    assert upstream.calls == 2
    assert ovh_settings._get_inflight_sem()._value == ovh_settings._INFLIGHT_CHAT_MAX
