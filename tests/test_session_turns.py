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


async def test_streamed_cli_turn_is_recorded(
    client, session_db, _providers_file, monkeypatch
):
    from plugboard import cli_subprocess, router
    from tests.test_router_admission import CLI

    _providers_file.write_text(json.dumps({"providers": [CLI]}))
    router.invalidate_models_cache()

    async def fake_complete(kind, body, *, timeout_s=None):
        return _answer("from the CLI")

    monkeypatch.setattr(cli_subprocess, "complete", fake_complete)
    headers, body = _turn("A", "s4", stream=True, model="pbtest-cli:model-x")
    resp = await client.post("/v1/chat/completions", headers=headers, json=body)
    assert resp.status_code == 200
    assert session_db.load_session("admin", "s4")[-1] == {
        "role": "assistant",
        "content": "from the CLI",
    }
