"""Chat admission under load (PLU-14, PLU-15).

PLU-14 (D-32): the global chat cap is 8 requests at once with a 10 s wait, in
the code and in the OVH deployment. With one chat at a time and no wait, the
cap came before the provider queue: a second application got 503 at once
while a provider capped at one request (Ollama) could have queued it.

PLU-15: the key is checked before a global slot is taken, and a request waits
once: one deadline, set by its first wait, ends its waits for a global slot,
for its session's turn and for a provider.

Providers are faked with aioresponses: no network, no inference.
"""

from __future__ import annotations

import asyncio
import json
import time

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


# ── PLU-15: the key first ───────────────────────────────────────────────────


@pytest.fixture
async def full_cap(app_mod, monkeypatch):
    """One global slot, held by another request; a wait would last 2 s."""
    monkeypatch.setattr(app_mod, "_INFLIGHT_CHAT_MAX", 1)
    monkeypatch.setattr(app_mod, "_INFLIGHT_WAIT_S", 2.0)
    sem = app_mod._get_inflight_sem()
    await sem.acquire()
    yield sem
    sem.release()


_PREFLIGHT = {"items": [{"provider": PROVIDER_A["id"], "model": "fake:model-v1"}]}


@pytest.mark.parametrize(
    "path, body",
    [
        ("/v1/chat/completions", _chat()),
        ("/v2/chat/completions", _chat()),
        ("/v1/preflight", _PREFLIGHT),
    ],
    ids=["v1", "v2", "preflight"],
)
@pytest.mark.parametrize(
    "headers",
    [{"Authorization": "Bearer not-a-key"}, {}],
    ids=["invalid-key", "no-key"],
)
async def test_call_without_a_valid_key_gets_401_even_when_the_cap_is_full(
    client, full_cap, path, body, headers
):
    """PLU-15 criterion: no valid key and a full cap give 401 at once, not a
    wait then 503; such a call never takes nor waits for a slot."""
    started = time.monotonic()
    resp = await client.post(path, headers=headers, json=body)
    elapsed = time.monotonic() - started
    assert resp.status_code == 401, resp.text
    assert elapsed < 1.0, f"waited {elapsed:.2f} s for a slot"
    assert full_cap._value == 0 and not full_cap._waiters


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v2/chat/completions"])
async def test_key_is_resolved_once_per_request(
    client, app_mod, tmp_path, monkeypatch, path
):
    """The route's ``Depends(_resolve_client)`` and the handler's share one
    resolution (FastAPI's per-request cache): the key is not read twice."""
    import plugboard.apikeys as ak

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    key = ak.create_key("admission-client", "admission")["key"]
    applied = []
    real_apply = app_mod._apply_key_record

    def counting_apply(request, rec, **kwargs):
        applied.append(rec["client_id"])
        return real_apply(request, rec, **kwargs)

    monkeypatch.setattr(app_mod, "_apply_key_record", counting_apply)
    monkeypatch.setattr(app_mod, "_check_price_known", lambda *a, **k: None)
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION)
        resp = await client.post(
            path, headers={"Authorization": f"Bearer {key}"}, json=_chat()
        )
    assert resp.status_code == 200, resp.text
    assert applied == ["admission-client"]


def test_every_slot_route_checks_the_key_first(app_mod):
    """A route that takes a global slot resolves the key before it (PLU-15)."""
    from fastapi.routing import APIRoute

    slot_routes = 0
    for route in app_mod.app.routes:
        if not isinstance(route, APIRoute):
            continue
        calls = [d.call for d in route.dependant.dependencies]
        if app_mod._inflight_chat_dep in calls:
            slot_routes += 1
            assert app_mod._resolve_client in calls, route.path
            first_key = calls.index(app_mod._resolve_client)
            assert first_key < calls.index(app_mod._inflight_chat_dep), route.path
    assert slot_routes == 3


# ── PLU-15: one deadline per request ────────────────────────────────────────


@pytest.fixture
def busy_provider(one_at_a_time):
    """The provider's only slot, held for the whole test."""
    from plugboard import provider_admission

    lease = provider_admission.try_acquire(one_at_a_time)
    yield one_at_a_time
    lease.release()


def _waits(app_mod, monkeypatch, *, cap=None, slot_s, provider_s):
    from plugboard import provider_admission

    if cap is not None:
        monkeypatch.setattr(app_mod, "_INFLIGHT_CHAT_MAX", cap)
    monkeypatch.setattr(app_mod, "_INFLIGHT_WAIT_S", slot_s)
    monkeypatch.setattr(provider_admission, "WAIT_S", provider_s)


async def _release_after(delay, release):
    await asyncio.sleep(delay)
    release()


@pytest.mark.parametrize("stream", [False, True], ids=["json", "stream"])
async def test_slot_wait_and_provider_wait_share_one_deadline(
    client, app_mod, busy_provider, monkeypatch, stream
):
    """0.6 s waiting for a global slot leaves 0.4 s for the provider, not 1 s."""
    _waits(app_mod, monkeypatch, cap=1, slot_s=1.0, provider_s=1.0)
    sem = app_mod._get_inflight_sem()
    await sem.acquire()
    freeing = asyncio.create_task(_release_after(0.6, sem.release))
    started = time.monotonic()
    resp = await client.post(
        "/v1/chat/completions", headers=AUTH, json=_chat(stream=stream)
    )
    elapsed = time.monotonic() - started
    await freeing
    assert resp.status_code == 503, resp.text
    assert resp.json()["code"] == "providers_busy"
    assert 0.9 <= elapsed < 1.4, f"waited {elapsed:.2f} s in all"


@pytest.fixture
def session_db(tmp_path, monkeypatch):
    from plugboard import session_store

    monkeypatch.setattr(session_store, "_DB_PATH", tmp_path / "sessions.db")
    monkeypatch.setattr(session_store, "_INITIALISED", False)
    return session_store


async def test_turn_wait_and_provider_wait_share_one_deadline(
    client, app_mod, busy_provider, session_db, monkeypatch
):
    """0.6 s waiting for the session's turn leaves 0.4 s for the provider."""
    from plugboard import session_turns

    _waits(app_mod, monkeypatch, slot_s=1.0, provider_s=1.0)
    release_turn = await session_turns.acquire("admin", "s1")
    freeing = asyncio.create_task(_release_after(0.6, release_turn))
    started = time.monotonic()
    resp = await client.post(
        "/v1/chat/completions",
        headers={**AUTH, "X-LLM-Session-ID": "s1"},
        json=_chat(),
    )
    elapsed = time.monotonic() - started
    await freeing
    assert resp.status_code == 503, resp.text
    assert resp.json()["code"] == "providers_busy"
    assert 0.9 <= elapsed < 1.4, f"waited {elapsed:.2f} s in all"
    assert session_turns._turns == {}


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v2/chat/completions"])
async def test_turn_wait_ends_at_the_deadline_with_503(
    client, app_mod, session_db, monkeypatch, path
):
    """A turn no longer waits without end for the previous one: at the
    deadline it gets 503 with Retry-After, and leaves the session's queue."""
    from plugboard import session_turns

    _waits(app_mod, monkeypatch, slot_s=0.5, provider_s=0.5)
    release_turn = await session_turns.acquire("admin", "s2")
    started = time.monotonic()
    try:
        resp = await asyncio.wait_for(
            client.post(path, headers={**AUTH, "X-LLM-Session-ID": "s2"}, json=_chat()),
            3.0,
        )
    finally:
        release_turn()
    elapsed = time.monotonic() - started
    assert resp.status_code == 503, resp.text
    assert resp.headers["Retry-After"] == "5"
    assert "session" in resp.json()["detail"]
    assert 0.45 <= elapsed < 1.0, f"waited {elapsed:.2f} s"
    assert session_turns._turns == {}


async def test_preflight_pings_share_one_deadline(
    client, app_mod, busy_provider, monkeypatch
):
    """Two pings of a busy provider wait 0.5 s in all, not 0.5 s each."""
    _waits(app_mod, monkeypatch, slot_s=0.5, provider_s=0.5)
    started = time.monotonic()
    resp = await client.post(
        "/v1/preflight", headers=AUTH, json={"items": _PREFLIGHT["items"] * 2}
    )
    elapsed = time.monotonic() - started
    assert resp.status_code == 200, resp.text
    assert [r.get("error") for r in resp.json()["results"]] == ["providers_busy"] * 2
    assert 0.45 <= elapsed < 0.85, f"waited {elapsed:.2f} s in all"


async def test_deadline_starts_at_the_first_wait(busy_provider, monkeypatch):
    """Time spent before any wait (a provider that failed slowly, an earlier
    preflight ping) does not shorten the wait; once a wait has started, the
    next one only gets what is left."""
    from plugboard import provider_admission, request_wait, router

    monkeypatch.setattr(provider_admission, "WAIT_S", 10.0)

    async def provider_wait(first_wait_s):
        request_wait.start(0.4)
        if first_wait_s is not None:
            request_wait.remaining()  # a first wait starts the clock
            await asyncio.sleep(first_wait_s)
        else:
            await asyncio.sleep(0.3)  # work, no wait
        started = time.monotonic()
        with pytest.raises(router.ProvidersBusyError):
            await router.forward({"model": ONLY_A, "messages": _chat()["messages"]})
        return time.monotonic() - started

    # Own task each: the deadline lives in that task's context only.
    assert 0.35 <= await asyncio.create_task(provider_wait(None)) < 0.7
    assert await asyncio.create_task(provider_wait(0.3)) < 0.2


def test_request_wait_outside_a_request_keeps_each_ceiling():
    from plugboard import request_wait

    async def outside():
        assert request_wait.remaining(2.5) == 2.5
        assert request_wait.remaining() is None

    asyncio.run(outside())


def test_request_wait_is_bounded_by_the_ceiling_and_the_deadline():
    from plugboard import request_wait

    async def inside():
        request_wait.start(1.0)
        assert request_wait.remaining(0.2) == pytest.approx(0.2)
        assert 0.9 < request_wait.remaining() <= 1.0
        await asyncio.sleep(0.3)
        assert 0.6 < request_wait.remaining(5.0) <= 0.7
        request_wait.start(0.0)
        assert request_wait.remaining() == 0.0

    asyncio.run(inside())
