import asyncio
import json

import pytest
from aioresponses import CallbackResult, aioresponses

from plugboard import session_turns
from tests.factories import FAKE_COMPLETION, FAKE_STREAM_BODY

AUTH = {"Authorization": "Bearer testkey"}  # admin: client_id "admin"
URL_A = "http://fake-llm.test/v1/chat/completions"
MODEL = "test-provider:fake:model-v1"


# ── session_turns ───────────────────────────────────────────────────────────


async def test_turns_of_one_session_run_one_after_the_other():
    release = await session_turns.acquire("c", "s")
    second = asyncio.create_task(session_turns.acquire("c", "s"))
    await asyncio.sleep(0)
    assert not second.done() and session_turns.waiting("c", "s") == 1
    release()
    (await second)()
    assert session_turns._turns == {}


async def test_waiting_turns_keep_their_order():
    release = await session_turns.acquire("c", "s")
    second = asyncio.create_task(session_turns.acquire("c", "s"))
    await asyncio.sleep(0)
    third = asyncio.create_task(session_turns.acquire("c", "s"))
    await asyncio.sleep(0)
    release()
    release_second = await second
    assert not third.done()
    release_second()
    (await third)()


async def test_other_sessions_and_other_clients_do_not_wait():
    release = await session_turns.acquire("c", "s")
    other_session = await asyncio.wait_for(session_turns.acquire("c", "t"), 0.1)
    other_client = await asyncio.wait_for(session_turns.acquire("d", "s"), 0.1)
    for done in (release, other_session, other_client):
        done()
    assert session_turns._turns == {}


async def test_cancelled_wait_leaves_the_queue():
    release = await session_turns.acquire("c", "s")
    second = asyncio.create_task(session_turns.acquire("c", "s"))
    await asyncio.sleep(0)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    release()
    release()  # idempotent
    assert session_turns._turns == {}


async def test_wait_with_a_timeout_leaves_the_queue():
    """PLU-15: a turn waits at most what is left of its request's budget."""
    release = await session_turns.acquire("c", "s")
    with pytest.raises(TimeoutError):
        await session_turns.acquire("c", "s", timeout=0.05)
    assert session_turns.waiting("c", "s") == 0
    release()
    assert session_turns._turns == {}
    (await session_turns.acquire("c", "s", timeout=0))()  # free: taken at once
    assert session_turns._turns == {}


# ── chat route ──────────────────────────────────────────────────────────────


@pytest.fixture
def session_db(tmp_path, monkeypatch):
    from plugboard import session_store

    monkeypatch.setattr(session_store, "_DB_PATH", tmp_path / "sessions.db")
    monkeypatch.setattr(session_store, "_INITIALISED", False)
    return session_store


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


class _Upstream:
    """Fake provider: records each prompt; the first answer waits for `gate`.

    aioresponses awaits only a callback that ``inspect.iscoroutinefunction``
    recognises: a bound async method, not an instance with an async
    ``__call__``. Pass ``callback=upstream.reply``.
    """

    def __init__(self):
        self.prompts, self.gate = [], asyncio.Event()

    async def reply(self, url, **kwargs):
        self.prompts.append(kwargs["json"]["messages"])
        n = len(self.prompts)
        if n == 1:
            await self.gate.wait()
        return CallbackResult(payload=_answer(f"answer {n}"))


def _turn(text, sid=None, how="header", **extra):
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": text}],
        "cache": False,
        **extra,
    }
    headers = dict(AUTH)
    if sid and how == "header":
        headers["X-LLM-Session-ID"] = sid
    if sid and how == "body":
        body["session_id"] = sid
    return headers, body


async def _until(predicate, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never met")


async def _two_turns(client, upstream, path, first, second, *, waits):
    one = asyncio.create_task(client.post(path, headers=first[0], json=first[1]))
    await _until(lambda: len(upstream.prompts) == 1)
    two = asyncio.create_task(client.post(path, headers=second[0], json=second[1]))
    if waits:
        await asyncio.sleep(0.2)
        assert len(upstream.prompts) == 1  # the second turn waits
    else:
        await _until(lambda: len(upstream.prompts) == 2)
    upstream.gate.set()
    return await asyncio.gather(one, two)


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v2/chat/completions"])
@pytest.mark.parametrize("how", ["header", "body"])
async def test_second_turn_waits_for_the_first_and_sees_it(
    client, session_db, path, how
):
    upstream = _Upstream()
    with aioresponses() as mock:
        mock.post(URL_A, callback=upstream.reply, repeat=True)
        r1, r2 = await _two_turns(
            client,
            upstream,
            path,
            _turn("A", "s1", how),
            _turn("B", "s1", how),
            waits=True,
        )
    assert r1.status_code == r2.status_code == 200
    assert upstream.prompts[1] == [
        {"role": "user", "content": "A"},
        {"role": "assistant", "content": "answer 1"},
        {"role": "user", "content": "B"},
    ]
    assert session_db.load_session("admin", "s1")[-1] == {
        "role": "assistant",
        "content": "answer 2",
    }
    assert session_turns._turns == {}


async def test_requests_without_session_are_not_serialised(client, session_db):
    upstream = _Upstream()
    with aioresponses() as mock:
        mock.post(URL_A, callback=upstream.reply, repeat=True)
        await _two_turns(
            client,
            upstream,
            "/v1/chat/completions",
            _turn("A"),
            _turn("B"),
            waits=False,
        )
    assert upstream.prompts[1] == [{"role": "user", "content": "B"}]


async def test_concurrent_turns_on_a_full_history_keep_both(
    client, session_db, monkeypatch
):
    monkeypatch.setattr(session_db, "_MAX_TURNS", 6)
    history = [
        {"role": r, "content": f"{r}{i}"}
        for i in range(3)
        for r in ("user", "assistant")
    ]
    session_db.save_session("admin", "full", history)
    upstream = _Upstream()
    with aioresponses() as mock:
        mock.post(URL_A, callback=upstream.reply, repeat=True)
        await _two_turns(
            client,
            upstream,
            "/v1/chat/completions",
            _turn("A", "full"),
            _turn("B", "full"),
            waits=True,
        )
    assert [m["content"] for m in session_db.load_session("admin", "full")[-4:]] == [
        "A",
        "answer 1",
        "B",
        "answer 2",
    ]


async def test_client_leaving_while_waiting_for_its_turn_leaves_the_queue(
    client, session_db
):
    from tests.test_inflight_disconnect import _Abandoned

    upstream = _Upstream()
    with aioresponses() as mock:
        mock.post(URL_A, callback=upstream.reply, repeat=True)
        headers, body = _turn("A", "s2")
        first = asyncio.create_task(
            client.post("/v1/chat/completions", headers=headers, json=body)
        )
        await _until(lambda: len(upstream.prompts) == 1)
        gone = _Abandoned(
            "/v1/chat/completions", _turn("B")[1], {"X-LLM-Session-ID": "s2"}
        )
        await _until(lambda: session_turns.waiting("admin", "s2") == 1)
        await gone.hang_up()
        assert gone.status() == 499
        upstream.gate.set()
        assert (await first).status_code == 200
        await gone.close()
    assert len(upstream.prompts) == 1
    assert [m["content"] for m in session_db.load_session("admin", "s2")] == [
        "A",
        "answer 1",
    ]
    assert session_turns._turns == {}


async def test_turn_refused_after_taking_its_turn_gives_it_back(client, session_db):
    """A 400 raised once the turn is held must not lock the session for good."""
    headers, body = _turn("A", "s5")
    body["messages"] = []  # no history yet: still empty after the merge
    resp = await client.post("/v1/chat/completions", headers=headers, json=body)
    assert resp.status_code == 400
    assert session_turns._turns == {}


def _key(tmp_path, monkeypatch, **limits):
    import plugboard.apikeys as ak

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    return ak.create_key("turns-client", "turns", **limits)["key"]


@pytest.mark.parametrize(
    "limits, second, status",
    [
        ({}, {"model": "nope:model-x"}, 404),
        ({"allowed_models": [MODEL]}, {"model": "other-provider:other:model-v2"}, 403),
        ({"rpm_limit": 1}, {}, 429),
        ({}, {"messages": ["hi"]}, 400),
    ],
    ids=["unknown-model", "model-not-allowed", "rpm", "bad-messages"],
)
async def test_refused_turn_does_not_wait_for_the_running_one(
    client, session_db, _providers_file, tmp_path, monkeypatch, limits, second, status
):
    """A request its admission refuses is answered at once, not after the
    turn its session is running; it never takes the turn."""
    from plugboard import router
    from tests.factories import PROVIDER_A, PROVIDER_B

    # Every provider lists its models: an unlisted name is then unknown (404).
    listed = {**PROVIDER_A, "models": [{"id": "fake:model-v1", "name": "V1"}]}
    _providers_file.write_text(json.dumps({"providers": [listed, PROVIDER_B]}))
    router.invalidate_models_cache()
    headers = {
        "Authorization": f"Bearer {_key(tmp_path, monkeypatch, **limits)}",
        "X-LLM-Session-ID": "s7",
    }
    upstream = _Upstream()
    with aioresponses() as mock:
        mock.post(URL_A, callback=upstream.reply, repeat=True)
        first = asyncio.create_task(
            client.post("/v1/chat/completions", headers=headers, json=_turn("A")[1])
        )
        await _until(lambda: len(upstream.prompts) == 1)
        try:
            refused = await asyncio.wait_for(
                client.post(
                    "/v1/chat/completions",
                    headers=headers,
                    json={**_turn("B")[1], **second},
                ),
                1.0,
            )
            first_still_running = not first.done()
        finally:
            upstream.gate.set()
        assert (await first).status_code == 200
    assert refused.status_code == status, refused.text
    assert first_still_running
    assert len(upstream.prompts) == 1
    assert session_turns._turns == {}


async def test_streamed_http_turn_is_recorded(client, session_db):
    with aioresponses() as mock:
        mock.post(URL_A, body=FAKE_STREAM_BODY, status=200)
        headers, body = _turn("A", "s3", stream=True)
        resp = await client.post("/v1/chat/completions", headers=headers, json=body)
    assert resp.status_code == 200
    assert session_db.load_session("admin", "s3") == [
        {"role": "user", "content": "A"},
        {"role": "assistant", "content": "Hello!"},
    ]
    assert session_turns._turns == {}


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v2/chat/completions"])
async def test_next_turn_waits_until_the_streamed_answer_is_sent(
    client, session_db, monkeypatch, path
):
    """The turn goes back after the last streamed byte, not when the handler returns.

    ``_session_turn_dep`` must stay a yield dependency of scope "request":
    with scope "function" it ends before the body is sent, and the next turn
    reads the session before the streamed answer is recorded.
    """
    import contextlib

    import plugboard.app

    real_stream = plugboard.app.forward_stream
    sending = asyncio.Event()

    async def slow_stream(body, **options):
        async with contextlib.aclosing(real_stream(body, **options)) as chunks:
            yield await chunks.__anext__()
            sending.set()  # the handler has returned: the body is on its way
            await asyncio.sleep(0.3)
            async for chunk in chunks:
                yield chunk

    monkeypatch.setattr("plugboard.app.forward_stream", slow_stream)
    upstream = _Upstream()
    upstream.gate.set()  # only the second, non-streamed turn calls it
    with aioresponses() as mock:
        mock.post(URL_A, body=FAKE_STREAM_BODY, status=200)
        mock.post(URL_A, callback=upstream.reply, repeat=True)
        headers, body = _turn("A", "s6", stream=True)
        first = asyncio.create_task(client.post(path, headers=headers, json=body))
        await asyncio.wait_for(sending.wait(), 2)
        headers, body = _turn("B", "s6")
        second = await client.post(path, headers=headers, json=body)
        assert (await first).status_code == second.status_code == 200
    assert upstream.prompts == [
        [
            {"role": "user", "content": "A"},
            {"role": "assistant", "content": "Hello!"},
            {"role": "user", "content": "B"},
        ]
    ]
    assert session_turns._turns == {}


async def test_streamed_cli_turn_is_recorded(
    client, session_db, _providers_file, monkeypatch
):
    from plugboard import cli_subprocess, router
    from tests.test_router_admission import CLI

    _providers_file.write_text(json.dumps({"providers": [CLI]}))
    router.invalidate_models_cache()

    async def fake_complete(kind, body, *, timeout_s=None, on_delta=None):
        return _answer("from the CLI")

    monkeypatch.setattr(cli_subprocess, "complete", fake_complete)
    headers, body = _turn("A", "s4", stream=True, model="pbtest-cli:model-x")
    resp = await client.post("/v1/chat/completions", headers=headers, json=body)
    assert resp.status_code == 200
    assert session_db.load_session("admin", "s4")[-1] == {
        "role": "assistant",
        "content": "from the CLI",
    }


async def test_a_session_queue_is_bounded(monkeypatch):
    """PLU-50: past PLUGBOARD_SESSION_MAX_WAITING turns waiting in one
    session, the next turn does not wait (TimeoutError at once)."""
    monkeypatch.setattr(session_turns, "MAX_WAITING", 1)
    release = await session_turns.acquire("c", "s")
    second = asyncio.create_task(session_turns.acquire("c", "s", timeout=5.0))
    await asyncio.sleep(0)
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(TimeoutError):
        await session_turns.acquire("c", "s", timeout=5.0)
    assert loop.time() - started < 0.5
    assert session_turns.waiting("c", "s") == 1
    other = await session_turns.acquire("c", "t", timeout=5.0)  # another session
    other()
    release()
    (await second)()
    assert session_turns._turns == {}


async def test_waiting_count_is_exact_while_the_turn_is_handed_over(monkeypatch):
    monkeypatch.setattr(session_turns, "MAX_WAITING", 2)
    release = await session_turns.acquire("c", "h")
    second = asyncio.create_task(session_turns.acquire("c", "h", timeout=5.0))
    await asyncio.sleep(0)
    release()  # handed to ``second``, which has not resumed yet
    assert session_turns.waiting("c", "h") == 0
    third = asyncio.create_task(session_turns.acquire("c", "h", timeout=5.0))
    (await second)()
    (await third)()
    assert session_turns._turns == {}


async def test_no_session_bound_when_max_waiting_is_zero(monkeypatch):
    monkeypatch.setattr(session_turns, "MAX_WAITING", 0)
    release = await session_turns.acquire("c", "z")
    waits = [
        asyncio.create_task(session_turns.acquire("c", "z", timeout=5.0))
        for _ in range(12)
    ]
    await asyncio.sleep(0)
    assert session_turns.waiting("c", "z") == 12
    release()
    for task in waits:
        (await task)()
    assert session_turns._turns == {}
