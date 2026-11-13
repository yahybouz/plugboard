"""Chat admission under load (PLU-14, PLU-15).

PLU-14 (D-32): the global chat cap is 8 requests at once with a 10 s wait, in
the code and in the OVH deployment. With one chat at a time and no wait, the
cap came before the provider queue: a second application got 503 at once
while a provider capped at one request (Ollama) could have queued it.

D-43: a CLI provider takes one request at a time by default; the next ones
wait in its bounded queue (within the request's wait budget), not in the
CLI's spawn queue, which has no limit.

PLU-15: the key is checked before a global slot is taken, and a request waits
once: its waits for a global slot, for its session's turn and for a provider
share one budget (10 s in all by default).

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
        return _reply_for(kwargs)


def _reply_for(kwargs):
    """A JSON answer, or an SSE stream when the request asked for one."""
    from tests.factories import FAKE_STREAM_BODY

    if (kwargs.get("json") or {}).get("stream"):
        return CallbackResult(body=FAKE_STREAM_BODY, content_type="text/event-stream")
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
    global cap, queues behind the first one at the provider (without a global
    slot, BUG-77) and is served when it ends, no 503 at once."""
    from plugboard import provider_admission

    upstream = _Upstream()
    with aioresponses() as mock:
        mock.post(URL_A, callback=upstream.reply, repeat=True)
        first = asyncio.create_task(client.post(path, headers=AUTH, json=_chat()))
        await _until(lambda: upstream.calls == 1)
        second = asyncio.create_task(client.post(path, headers=AUTH, json=_chat()))
        await _until(lambda: provider_admission.waiting(PROVIDER_A["id"]) == 1)
        sem = ovh_settings._get_inflight_sem()
        # BUG-77: only the running request holds a global slot; the waiting
        # one gave its slot back for the requests to other providers.
        only_the_running_one = sem._value == ovh_settings._INFLIGHT_CHAT_MAX - 1
        await asyncio.sleep(0.3)
        waited = not second.done()
        upstream.gate.set()
        r1, r2 = await asyncio.gather(first, second)
    assert waited, f"answered at once: {r2.status_code} {r2.text}"
    assert only_the_running_one
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
async def test_anonymous_chat_in_strict_mode_gets_401_even_when_the_cap_is_full(
    client, app_mod, full_cap, monkeypatch, path
):
    """With an empty API_KEY the key check lets an anonymous call through; strict
    tenant mode (the default) still refuses it before it waits for a slot."""
    monkeypatch.setattr(app_mod, "_API_KEY", "")
    monkeypatch.delenv("PLUGBOARD_ALLOW_PERMISSIVE", raising=False)
    started = time.monotonic()
    resp = await client.post(path, json=_chat())
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


# ── PLU-15: one wait budget per request ─────────────────────────────────────


@pytest.fixture
def busy_provider(one_at_a_time):
    """The provider's only slot, held for the whole test."""
    from plugboard import provider_admission

    lease = provider_admission.try_acquire(one_at_a_time)
    yield one_at_a_time
    lease.release()


@pytest.fixture
def provider_waits(monkeypatch):
    """The timeout given to each wait for a provider, in order."""
    from plugboard import provider_admission

    seen = []
    real_wait = provider_admission.wait_acquire

    async def spy(providers, timeout):
        seen.append(timeout)
        return await real_wait(providers, timeout)

    monkeypatch.setattr(provider_admission, "wait_acquire", spy)
    return seen


def _waits(app_mod, monkeypatch, *, cap=None, slot_s, provider_s):
    from plugboard import provider_admission

    if cap is not None:
        monkeypatch.setattr(app_mod, "_INFLIGHT_CHAT_MAX", cap)
    monkeypatch.setattr(app_mod, "_INFLIGHT_WAIT_S", slot_s)
    monkeypatch.setattr(provider_admission, "WAIT_S", provider_s)


async def _release_once_waiting(is_waiting, delay, release):
    """Give the slot or turn back ``delay`` s after the request started
    waiting for it: the request has then waited at least that long."""
    await _until(is_waiting)
    await asyncio.sleep(delay)
    release()


@pytest.mark.parametrize("stream", [False, True], ids=["json", "stream"])
async def test_slot_wait_and_provider_wait_share_one_budget(
    client, app_mod, busy_provider, provider_waits, monkeypatch, stream
):
    """0.6 s waiting for a global slot leaves 0.4 s for the provider, not 1 s."""
    _waits(app_mod, monkeypatch, cap=1, slot_s=1.0, provider_s=1.0)
    sem = app_mod._get_inflight_sem()
    await sem.acquire()
    freeing = asyncio.create_task(
        _release_once_waiting(lambda: sem._waiters, 0.6, sem.release)
    )
    started = time.monotonic()
    resp = await client.post(
        "/v1/chat/completions", headers=AUTH, json=_chat(stream=stream)
    )
    elapsed = time.monotonic() - started
    await freeing
    assert resp.status_code == 503, resp.text
    assert resp.json()["code"] == "providers_busy"
    assert len(provider_waits) == 1 and provider_waits[0] <= 0.4, provider_waits
    assert elapsed >= 0.9, f"waited {elapsed:.2f} s in all"


@pytest.fixture
def session_db(tmp_path, monkeypatch):
    from plugboard import session_store

    monkeypatch.setattr(session_store, "_DB_PATH", tmp_path / "sessions.db")
    monkeypatch.setattr(session_store, "_INITIALISED", False)
    return session_store


async def test_turn_wait_and_provider_wait_share_one_budget(
    client, app_mod, busy_provider, provider_waits, session_db, monkeypatch
):
    """0.6 s waiting for the session's turn leaves 0.4 s for the provider."""
    from plugboard import session_turns

    _waits(app_mod, monkeypatch, slot_s=1.0, provider_s=1.0)
    release_turn = await session_turns.acquire("admin", "s1")
    freeing = asyncio.create_task(
        _release_once_waiting(
            lambda: session_turns.waiting("admin", "s1") == 1, 0.6, release_turn
        )
    )
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
    assert len(provider_waits) == 1 and provider_waits[0] <= 0.4, provider_waits
    assert elapsed >= 0.9, f"waited {elapsed:.2f} s in all"
    assert session_turns._turns == {}


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v2/chat/completions"])
async def test_turn_wait_ends_with_the_budget_with_503(
    client, app_mod, session_db, monkeypatch, path
):
    """A turn no longer waits without end for the previous one: when the
    request's budget is spent it gets 503 with Retry-After, and leaves the
    session's queue."""
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
    assert 0.45 <= elapsed < 2.5, f"waited {elapsed:.2f} s"
    assert session_turns._turns == {}


async def test_preflight_pings_share_one_budget(
    client, app_mod, busy_provider, provider_waits, monkeypatch
):
    """Two pings of a busy provider wait 0.5 s in all, not 0.5 s each."""
    _waits(app_mod, monkeypatch, slot_s=0.5, provider_s=0.5)
    resp = await client.post(
        "/v1/preflight", headers=AUTH, json={"items": _PREFLIGHT["items"] * 2}
    )
    assert resp.status_code == 200, resp.text
    assert [r.get("error") for r in resp.json()["results"]] == ["providers_busy"] * 2
    assert provider_waits[0] == pytest.approx(0.5, abs=0.05)
    assert provider_waits[1] == 0.0


async def test_only_waiting_time_counts(busy_provider, provider_waits, monkeypatch):
    """Time spent working (the checks, a provider that failed slowly, an
    earlier preflight ping) leaves the budget whole; an earlier wait uses it."""
    from plugboard import provider_admission, request_wait, router

    monkeypatch.setattr(provider_admission, "WAIT_S", 10.0)

    async def request(earlier_wait: bool):
        request_wait.start(0.4)
        if earlier_wait:
            with request_wait.waiting():
                await asyncio.sleep(0.3)
        else:
            await asyncio.sleep(0.3)  # work, not a wait
        with pytest.raises(router.ProvidersBusyError):
            await router.forward({"model": ONLY_A, "messages": _chat()["messages"]})

    # Own task each: the budget lives in that task's context only.
    await asyncio.create_task(request(earlier_wait=False))
    await asyncio.create_task(request(earlier_wait=True))
    assert provider_waits[0] == pytest.approx(0.4, abs=0.01)
    assert provider_waits[1] <= 0.1


def test_wait_budget_outside_a_request_keeps_each_ceiling():
    from plugboard import request_wait

    async def outside():
        with request_wait.waiting(2.5) as wait_s:
            assert wait_s == 2.5
        with request_wait.waiting() as wait_s:
            assert wait_s is None

    asyncio.run(outside())


def test_wait_budget_adds_up_the_waits_only():
    from plugboard import request_wait

    async def inside():
        budget = request_wait.start(1.0)
        with request_wait.waiting(0.2) as wait_s:
            assert wait_s == 0.2  # its own ceiling
        with request_wait.waiting() as wait_s:
            assert wait_s == pytest.approx(1.0, abs=0.05)
            await asyncio.sleep(0.3)
        await asyncio.sleep(0.2)  # work, not counted
        with request_wait.waiting(5.0) as wait_s:
            assert 0.6 < wait_s <= 0.7
        with pytest.raises(TimeoutError):  # counted however the wait ends
            with request_wait.waiting():
                await asyncio.sleep(0.4)
                raise TimeoutError
        assert 0.7 <= budget.spent < 0.9
        with request_wait.waiting(5.0) as wait_s:
            assert wait_s < 0.3
        request_wait.start(0.0)
        with request_wait.waiting(5.0) as wait_s:
            assert wait_s == 0.0

    asyncio.run(inside())


# ── D-43: one request at a time per CLI provider ────────────────────────────


def _cli_provider(kind):
    return {
        "id": f"pbtest-{kind}",
        "name": f"{kind} CLI test",
        "base_url": f"subprocess://{kind}",
        "bearer_token": "(via CLI)",
        "default_model": "model-x",
        "auth_source": f"subprocess:{kind}",
        "is_active": True,
        "created_at": "2026-01-01T00:00:00+00:00",
    }


@pytest.fixture
def two_clis(_providers_file, monkeypatch):
    """Two CLI providers, no cap set on them; every CLI run waits for the
    gate of its kind (no real CLI)."""
    from plugboard import cli_subprocess, router

    providers = [_cli_provider("claude"), _cli_provider("codex")]
    _providers_file.write_text(json.dumps({"providers": providers}))
    router.invalidate_models_cache()
    runs = {"claude": 0, "codex": 0}
    gates = {"claude": asyncio.Event(), "codex": asyncio.Event()}

    async def fake_complete(kind, body, *, timeout_s=None):
        runs[kind] += 1
        await gates[kind].wait()
        return {**FAKE_COMPLETION}

    monkeypatch.setattr(cli_subprocess, "complete", fake_complete)
    return runs, gates


def test_ovh_deployment_runs_one_request_at_a_time_per_cli():
    assert _gateway()["environment"]["PLUGBOARD_CLI_PROVIDER_MAX_INFLIGHT"] == "1"


async def test_second_request_to_a_cli_waits_then_is_served(client, two_clis):
    from plugboard import provider_admission

    runs, gates = two_clis
    body = _chat("pbtest-claude:model-x")
    first = asyncio.create_task(
        client.post("/v1/chat/completions", headers=AUTH, json=body)
    )
    await _until(lambda: runs["claude"] == 1)
    second = asyncio.create_task(
        client.post("/v1/chat/completions", headers=AUTH, json=body)
    )
    await _until(lambda: provider_admission.waiting("pbtest-claude") == 1)
    queued_outside_the_cli = runs["claude"] == 1  # not in the spawn queue
    gates["claude"].set()
    r1, r2 = await asyncio.gather(first, second)
    assert queued_outside_the_cli
    assert r1.status_code == r2.status_code == 200, r2.text
    assert runs["claude"] == 2


@pytest.mark.parametrize("stream", [False, True], ids=["json", "stream"])
async def test_second_request_to_a_busy_cli_gets_503_after_the_budget(
    client, app_mod, two_clis, monkeypatch, stream
):
    runs, gates = two_clis
    _waits(app_mod, monkeypatch, slot_s=0.4, provider_s=0.4)
    body = _chat("pbtest-claude:model-x", stream=stream)
    first = asyncio.create_task(
        client.post("/v1/chat/completions", headers=AUTH, json=body)
    )
    await _until(lambda: runs["claude"] == 1)
    started = time.monotonic()
    try:
        second = await asyncio.wait_for(
            client.post("/v1/chat/completions", headers=AUTH, json=body), 3.0
        )
    finally:
        gates["claude"].set()
    elapsed = time.monotonic() - started
    assert (await first).status_code == 200
    assert second.status_code == 503, second.text
    assert second.json()["code"] == "providers_busy"
    assert second.headers["Retry-After"] == "5"
    assert elapsed >= 0.35, f"answered after {elapsed:.2f} s"
    assert runs["claude"] == 1  # the CLI never ran it


async def test_another_cli_is_not_blocked(client, two_clis):
    runs, gates = two_clis
    first = asyncio.create_task(
        client.post(
            "/v1/chat/completions", headers=AUTH, json=_chat("pbtest-claude:model-x")
        )
    )
    await _until(lambda: runs["claude"] == 1)
    gates["codex"].set()
    try:
        other = await asyncio.wait_for(
            client.post(
                "/v1/chat/completions",
                headers=AUTH,
                json=_chat("pbtest-codex:model-x"),
            ),
            2.0,
        )
    finally:
        gates["claude"].set()
    assert other.status_code == 200, other.text
    assert (await first).status_code == 200


def _answer(text):
    return {
        **FAKE_COMPLETION,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": text},
            }
        ],
    }


async def test_internet_check_calls_only_the_provider_it_checks(
    client, _providers_file, monkeypatch
):
    """The probe names its provider: checking an HTTP model never runs a CLI
    (which, capped at one request, would also answer the bulk check "busy"),
    and with persist the result recorded is the checked provider's answer."""
    from plugboard import cli_subprocess, router
    from plugboard.config import get_provider
    from plugboard.probes.internet import (
        build_yesterday_variants,
        today_iso,
        today_weekdays,
    )

    listed = {**PROVIDER_A, "models": [{"id": "fake:model-v1", "name": "V1"}]}
    cli = {**_cli_provider("claude"), "models": [{"id": "fake:model-v1"}]}
    _providers_file.write_text(json.dumps({"providers": [cli, listed]}))
    router.invalidate_models_cache()
    cli_runs = []

    async def fake_complete(kind, body, *, timeout_s=None):
        cli_runs.append(body["model"])
        return _answer("I cannot browse the web.")  # offline, if ever asked

    monkeypatch.setattr(cli_subprocess, "complete", fake_complete)
    online = (
        f"Today is {today_iso()}, {today_weekdays()[0]}; "
        f"yesterday was {build_yesterday_variants()[0]}."
    )
    with aioresponses() as mock:
        mock.post(URL_A, payload=_answer(online), repeat=True)
        resp = await client.post(
            "/admin/models/check-internet",
            headers=AUTH,
            json={"model_id": ONLY_A, "persist": True},
        )
        http_calls = sum(len(v) for k, v in mock.requests.items() if k[0] == "POST")
    assert resp.status_code == 200, resp.text
    assert cli_runs == []
    assert http_calls == 1
    assert resp.json()["internet_access"] is True
    [checked] = get_provider(PROVIDER_A["id"])["models"]
    assert checked["internet_access"] is True
    [untouched] = get_provider("pbtest-claude")["models"]
    assert "internet_access" not in untouched


# ── BUG-77: a request waiting for a provider holds no global slot ───────────

URL_B = "http://other-llm.test/v1/chat/completions"
ONLY_B = "other-provider:other:model-v2"


@pytest.fixture
def a_at_one_b_free(_providers_file):
    """Provider A takes one request at a time; provider B has no cap."""
    from plugboard import router
    from tests.factories import PROVIDER_B

    capped = {**PROVIDER_A, "max_concurrency": 1}
    _providers_file.write_text(json.dumps({"providers": [capped, PROVIDER_B]}))
    router.invalidate_models_cache()


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v2/chat/completions"])
@pytest.mark.parametrize("stream", [False, True], ids=["json", "stream"])
async def test_requests_waiting_for_a_full_provider_leave_the_others_served(
    client, app_mod, a_at_one_b_free, session_db, monkeypatch, path, stream
):
    """BUG-77, the review's probe: with a global cap of 2, one request runs on
    A and two wait for it (for A's slot, and for a session's turn); a request
    for B, which is free, is served at once instead of 503 after the wait. It
    needs the slots of both waiting requests: each wait must give its own back
    (streamed requests take forward_stream's wait)."""
    from plugboard import provider_admission, session_turns

    _waits(app_mod, monkeypatch, cap=2, slot_s=0.5, provider_s=5.0)
    upstream = _Upstream()
    in_session = {**AUTH, "X-LLM-Session-ID": "s"}

    def post(headers, model=ONLY_A):
        return asyncio.create_task(
            client.post(path, headers=headers, json=_chat(model, stream=stream))
        )

    with aioresponses() as mock:
        mock.post(URL_A, callback=upstream.reply, repeat=True)
        mock.post(URL_B, callback=lambda url, **kw: _reply_for(kw), repeat=True)
        on_a = [post(in_session)]
        await _until(lambda: upstream.calls == 1)
        on_a.append(post(AUTH))  # waits for A's slot
        await _until(lambda: provider_admission.waiting(PROVIDER_A["id"]) == 1)
        on_a.append(post(in_session))  # waits for its session's turn
        await _until(lambda: session_turns.waiting("admin", "s") == 1)
        try:
            on_b = await asyncio.wait_for(post(AUTH, ONLY_B), 3.0)
            a_still_waiting = not any(t.done() for t in on_a)
        finally:
            upstream.gate.set()
        answers = await asyncio.gather(*on_a)
    # Before the fix: 503 "Server overloaded" once the 0.5 s slot wait ended.
    assert on_b.status_code == 200, on_b.text
    assert a_still_waiting
    assert [r.status_code for r in answers] == [200, 200, 200]
    sem = app_mod._get_inflight_sem()
    assert sem._value == 2 and app_mod._inflight_chat_active == 0


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v2/chat/completions"])
async def test_full_global_cap_answers_503_server_busy(
    client, full_cap, app_mod, monkeypatch, path
):
    """The global cap's 503 carries a code for programs, like providers_busy."""
    monkeypatch.setattr(app_mod, "_INFLIGHT_WAIT_S", 0.0)
    resp = await client.post(path, headers=AUTH, json=_chat())
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["code"] == "server_busy"
    assert body["retry_after"] == 5 and resp.headers["Retry-After"] == "5"
    assert "limit of concurrent chat requests" in body["detail"]


class _AllGated:
    """Fake provider whose every answer waits for ``gate``."""

    def __init__(self):
        self.calls, self.gate = 0, asyncio.Event()

    async def reply(self, url, **kwargs):
        self.calls += 1
        await self.gate.wait()
        return CallbackResult(payload=FAKE_COMPLETION)


@pytest.mark.parametrize("stream", [False, True], ids=["json", "stream"])
async def test_slot_not_back_after_the_provider_wait_gives_the_provider_back(
    client, app_mod, a_at_one_b_free, monkeypatch, stream
):
    """A request granted A's slot while the global cap is full of running
    requests gets 503 server_busy when its slot wait ends, and A's slot goes
    back at once: it is not held idle by a request that will not run."""
    from plugboard import provider_admission

    _waits(app_mod, monkeypatch, cap=2, slot_s=0.5, provider_s=5.0)
    a, b = _Upstream(), _AllGated()
    with aioresponses() as mock:
        mock.post(URL_A, callback=a.reply, repeat=True)
        mock.post(URL_B, callback=b.reply, repeat=True)

        def post(model, **extra):
            return asyncio.create_task(
                client.post(
                    "/v1/chat/completions", headers=AUTH, json=_chat(model, **extra)
                )
            )

        first_a = post(ONLY_A)
        await _until(lambda: a.calls == 1)
        second_a = post(ONLY_A, stream=stream)  # waits for A, without a global slot
        await _until(lambda: provider_admission.waiting(PROVIDER_A["id"]) == 1)
        on_b = [post(ONLY_B)]  # takes the free global slot
        await _until(lambda: b.calls == 1)
        on_b.append(post(ONLY_B))  # waits for a global slot
        sem = app_mod._get_inflight_sem()
        await _until(lambda: bool(sem._waiters))
        try:
            a.gate.set()  # first_a ends; its slot goes to the queued B request
            refused = await asyncio.wait_for(second_a, 3.0)
            a_slots_after = provider_admission.in_flight(PROVIDER_A["id"])
        finally:
            b.gate.set()
        assert (await first_a).status_code == 200
        assert [r.status_code for r in await asyncio.gather(*on_b)] == [200, 200]
    assert refused.status_code == 503, refused.text
    assert refused.json()["code"] == "server_busy"
    assert a_slots_after == 0
    assert sem._value == 2 and app_mod._inflight_chat_active == 0


async def test_slot_not_back_after_the_turn_gives_the_turn_back(
    client, app_mod, a_at_one_b_free, session_db, monkeypatch
):
    """A turn obtained while the global cap is full of running requests gets
    503 server_busy when its slot wait ends; the session's turn goes back, and
    the session's next turn is served."""
    from plugboard import session_turns

    _waits(app_mod, monkeypatch, cap=2, slot_s=0.5, provider_s=5.0)
    a, b = _Upstream(), _AllGated()
    in_session = {**AUTH, "X-LLM-Session-ID": "t"}
    with aioresponses() as mock:
        mock.post(URL_A, callback=a.reply, repeat=True)
        mock.post(URL_B, callback=b.reply, repeat=True)

        def post(headers, model):
            return asyncio.create_task(
                client.post("/v1/chat/completions", headers=headers, json=_chat(model))
            )

        first_turn = post(in_session, ONLY_A)
        await _until(lambda: a.calls == 1)
        second_turn = post(in_session, ONLY_A)  # waits for the turn, no slot
        await _until(lambda: session_turns.waiting("admin", "t") == 1)
        on_b = [post(AUTH, ONLY_B)]
        await _until(lambda: b.calls == 1)
        on_b.append(post(AUTH, ONLY_B))  # waits for a global slot
        sem = app_mod._get_inflight_sem()
        await _until(lambda: bool(sem._waiters))
        try:
            a.gate.set()
            refused = await asyncio.wait_for(second_turn, 3.0)
            turns_after = dict(session_turns._turns)
        finally:
            b.gate.set()
        assert (await first_turn).status_code == 200
        assert [r.status_code for r in await asyncio.gather(*on_b)] == [200, 200]
        third_turn = await post(in_session, ONLY_A)
    assert refused.status_code == 503, refused.text
    assert refused.json()["code"] == "server_busy"
    assert turns_after == {}
    assert third_turn.status_code == 200, third_turn.text
    assert sem._value == 2 and app_mod._inflight_chat_active == 0


async def test_preflight_reports_server_busy_for_a_pair_it_could_not_ping(
    client, app_mod, a_at_one_b_free, monkeypatch
):
    """A ping refused after waiting for A leaves the batch without its global
    slot; when the cap stays full, the next pair answers server_busy (not
    pinged) instead of the whole batch losing its results to a 503."""
    from plugboard import provider_admission

    _waits(app_mod, monkeypatch, cap=2, slot_s=1.0, provider_s=0.4)
    from plugboard.config import get_provider

    held = provider_admission.try_acquire(get_provider(PROVIDER_A["id"]))
    b = _AllGated()
    with aioresponses() as mock:
        mock.post(URL_B, callback=b.reply, repeat=True)
        sem = app_mod._get_inflight_sem()
        items = [
            {"provider": PROVIDER_A["id"], "model": "fake:model-v1"},
            {"provider": "other-provider", "model": "other:model-v2"},
        ]
        batch = asyncio.create_task(
            client.post("/v1/preflight", headers=AUTH, json={"items": items})
        )
        await _until(lambda: provider_admission.waiting(PROVIDER_A["id"]) == 1)
        on_b = [
            asyncio.create_task(
                client.post("/v1/chat/completions", headers=AUTH, json=_chat(ONLY_B))
            )
            for _ in range(2)
        ]  # they fill the cap while the batch waits for A
        await _until(lambda: b.calls == 2)
        try:
            resp = await asyncio.wait_for(batch, 3.0)
        finally:
            b.gate.set()
            held.release()
        assert [r.status_code for r in await asyncio.gather(*on_b)] == [200, 200]
    assert resp.status_code == 200, resp.text
    errors = [r.get("error") for r in resp.json()["results"]]
    assert errors == ["providers_busy", "server_busy"]
    assert b.calls == 2  # the second pair was not pinged
    assert sem._value == 2 and app_mod._inflight_chat_active == 0
