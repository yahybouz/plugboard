"""A caller who hangs up frees the chat slot at once (BUG-30).

With PLUGBOARD_MAX_INFLIGHT_CHAT=1 and PLUGBOARD_INFLIGHT_WAIT_S=0, a request
the client abandoned kept the only slot until its upstream answered (up to
900 s for a CLI): every other model got 503 meanwhile. Nothing watched the
client while the gateway waited for the upstream, so the CLI kept running,
and its late answer was billed and cached.

The abandoned request is driven like uvicorn does (ASGI spec 2.3): the body,
then `http.disconnect` once the upstream has started. The CLI is a real
shell script that sleeps (and starts a child), so its death is observed for
real; HTTP providers are faked with aioresponses, no network, no inference.
"""

from __future__ import annotations

import asyncio
import collections
import json
import os
import signal
from pathlib import Path

import pytest
from aioresponses import CallbackResult, aioresponses

from plugboard import cli_subprocess, provider_admission
from tests.factories import (
    FAKE_COMPLETION,
    FAKE_STREAM_BODY,
    PROVIDER_A,
    PROVIDER_B,
)

AUTH = {"Authorization": "Bearer testkey"}

CLI_ID = "pbtest-claude-cli"
CLI_MODEL = "pbtest-claude-model"
CLI_PROVIDER = {
    "id": CLI_ID,
    "name": "Claude CLI test",
    "base_url": "subprocess://claude",
    "bearer_token": "(via CLI)",
    "default_model": CLI_MODEL,
    "auth_source": "subprocess:claude",
    "provider_type": "cloud",
    "is_active": True,
    "created_at": "2026-01-01T00:00:00+00:00",
}
SLOW_CLI = f"{CLI_ID}:{CLI_MODEL}"
SLOW_HTTP = f"{PROVIDER_A['id']}:fake:model-v1"
SLOW_HTTP_URL = f"{PROVIDER_A['base_url']}/v1/chat/completions"
OTHER = f"{PROVIDER_B['id']}:other:model-v2"
OTHER_URL = f"{PROVIDER_B['base_url']}/v1/chat/completions"

# Generous for a loaded CI host; the fix frees the slot in well under 1 s.
SETTLE_S = 5.0


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def _providers_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Overrides the conftest fixture: a CLI provider plus two HTTP ones."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    pfile = data_dir / "providers.json"
    pfile.write_text(json.dumps({"providers": [CLI_PROVIDER, PROVIDER_A, PROVIDER_B]}))
    monkeypatch.setenv("PLUGBOARD_DATA", str(data_dir))
    import plugboard.config as cfg

    cfg.PROVIDERS_FILE = pfile
    cfg.PROVIDERS_LOCK = data_dir / "providers.json.lock"
    from plugboard.router import invalidate_models_cache

    invalidate_models_cache()
    return pfile


@pytest.fixture(autouse=True)
def usage_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import plugboard.usage as usage

    store = tmp_path / "usage-store"
    monkeypatch.setattr(usage, "_DATA_DIR", store)
    monkeypatch.setattr(usage, "_DB_PATH", store / "usage.db")
    return usage


@pytest.fixture(autouse=True)
def inflight_cap(monkeypatch: pytest.MonkeyPatch):
    """The UAT setting: one chat at a time, no waiting."""
    import plugboard.app as app_mod

    monkeypatch.setattr(app_mod, "_INFLIGHT_CHAT_MAX", 1)
    monkeypatch.setattr(app_mod, "_INFLIGHT_WAIT_S", 0.0)
    monkeypatch.setattr(app_mod, "_inflight_chat_sem", None)
    monkeypatch.setattr(app_mod, "_inflight_chat_active", 0)
    return app_mod


@pytest.fixture(autouse=True)
def side_effects(monkeypatch: pytest.MonkeyPatch):
    """Record the cache writes and session turns of every request."""
    import plugboard.cache as cache
    import plugboard.session_store as session_store

    seen = {"cached": [], "sessions": []}
    real_set_cached = cache.set_cached

    def set_cached(key, model, *args, **kwargs):
        seen["cached"].append(model)
        return real_set_cached(key, model, *args, **kwargs)

    def record_exchange(client_id, session_id, *args, **kwargs):
        seen["sessions"].append(session_id)

    monkeypatch.setattr(cache, "set_cached", set_cached)
    monkeypatch.setattr(session_store, "record_exchange", record_exchange)
    return seen


class _SlowCli:
    """A `claude` binary that never answers: it starts a child and waits."""

    def __init__(self, root: Path):
        self.ready = root / "ready"
        self.runs = root / "runs"
        self.script = root / "bin" / "claude"
        self.script.parent.mkdir(parents=True)
        self.script.write_text(
            "#!/bin/sh\n"
            "sleep 30 &\n"
            f'echo "$$ $!" >> "{self.runs}"\n'
            f'echo "$$ $!" > "{root}/ready.tmp"\n'
            f'mv "{root}/ready.tmp" "{self.ready}"\n'
            "wait\n"
        )
        self.script.chmod(0o755)

    def pids(self) -> list[int]:
        """The CLI and its child, once the last run has started."""
        if not self.ready.exists():
            return []
        return [int(p) for p in self.ready.read_text().split()]

    def run_count(self) -> int:
        return len(self.runs.read_text().splitlines()) if self.runs.exists() else 0

    def every_pid(self) -> list[int]:
        if not self.runs.exists():
            return []
        return [int(p) for p in self.runs.read_text().split()]

    async def started(self) -> list[int]:
        for _ in range(int(SETTLE_S / 0.02)):
            if self.pids():
                return self.pids()
            await asyncio.sleep(0.02)
        raise AssertionError("the fake CLI never started")


@pytest.fixture
def slow_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cli = _SlowCli(tmp_path / "cli")
    monkeypatch.setattr(
        cli_subprocess, "_binary_path_cache", {"claude": str(cli.script)}
    )
    spawn = tmp_path / "spawn"
    spawn.mkdir()
    monkeypatch.setenv("PLUGBOARD_SUBPROCESS_CWD", str(spawn))
    yield cli
    for pid in cli.every_pid():  # never leave a sleeper behind, even on failure
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)


class _SlowHttp:
    """An HTTP upstream that never answers; records its cancellation."""

    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def callback(self, url, **kwargs):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


@pytest.fixture
async def upstreams():
    """HTTP providers: the slow one hangs, the other answers at once."""
    slow = _SlowHttp()
    with aioresponses() as mock:
        mock.post(SLOW_HTTP_URL, callback=slow.callback, repeat=True)
        mock.post(OTHER_URL, payload=FAKE_COMPLETION, repeat=True)
        yield slow


def _alive(pid: int) -> bool:
    """True while `pid` runs (a zombie counts as dead)."""
    if Path("/proc/self/stat").exists():
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except (FileNotFoundError, ProcessLookupError):
            return False
        return stat.rsplit(")", 1)[1].split()[0] not in {"Z", "X"}
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _all_dead(pids: list[int]) -> bool:
    for _ in range(int(SETTLE_S / 0.02)):
        if not any(_alive(pid) for pid in pids):
            return True
        await asyncio.sleep(0.02)
    return False


class _Abandoned:
    """A request whose client hangs up when told to, driven like uvicorn."""

    def __init__(self, path: str, body: dict, headers: dict | None = None):
        from plugboard.app import app

        raw = json.dumps(body).encode()
        self.sent: list = []
        self._hang_up = asyncio.Event()
        delivered = False

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": raw, "more_body": False}
            await self._hang_up.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            self.sent.append(message)

        extra = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"test"),
                (b"content-type", b"application/json"),
                (b"authorization", b"Bearer testkey"),
                (b"content-length", str(len(raw)).encode()),
                *extra,
            ],
            "client": ("127.0.0.1", 50000),
            "server": ("test", 80),
        }
        self.task = asyncio.ensure_future(app(scope, receive, send))

    def disconnect(self) -> None:
        """Disconnect, without waiting for the gateway."""
        self._hang_up.set()

    async def hang_up(self) -> None:
        """Disconnect, then give the gateway time to notice."""
        self.disconnect()
        await asyncio.wait({self.task}, timeout=SETTLE_S)

    def status(self):
        starts = [m for m in self.sent if m["type"] == "http.response.start"]
        return starts[0]["status"] if starts else None

    async def close(self) -> None:
        if not self.task.done():
            self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


@pytest.fixture
async def abandon():
    started: list[_Abandoned] = []

    def _start(path: str, body: dict, headers: dict | None = None) -> _Abandoned:
        req = _Abandoned(path, body, headers)
        started.append(req)
        return req

    yield _start
    for req in started:
        await req.close()


def _chat(model: str, *, stream: bool = False) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "a long question"}],
        "stream": stream,
    }


def _rows_for(usage, provider_id: str) -> list:
    from plugboard.billing import current_pricing_month

    return [
        row
        for row in usage.get_usage(current_pricing_month())
        if row["provider_id"] == provider_id
    ]


async def _other_model_answers(client, usage_store) -> None:
    """The symptom of the ticket: another model must not get 503."""
    body = {**_chat(OTHER), "cache": False}  # the response cache outlives a test
    resp = await client.post("/v1/chat/completions", headers=AUTH, json=body)
    assert resp.status_code == 200, resp.text
    # Still billed to the provider that served it.
    assert len(_rows_for(usage_store, PROVIDER_B["id"])) == 1


# ── The slot comes back as soon as the client hangs up ──────────────────────


@pytest.mark.parametrize(
    "path",
    ["/v1/chat/completions", "/v2/chat/completions"],
)
async def test_hang_up_during_cli_call_frees_slot_and_kills_cli(
    path, client, slow_cli, upstreams, abandon, inflight_cap, usage_store, side_effects
):
    req = abandon(path, _chat(SLOW_CLI), {"X-LLM-Session-ID": "bug30-session"})
    pids = await slow_cli.started()

    await req.hang_up()

    await _other_model_answers(client, usage_store)
    assert req.task.done(), "the abandoned request still holds its slot"
    assert req.status() == 499
    assert await _all_dead(pids), "the CLI (or its child) survived the hang-up"
    assert _rows_for(usage_store, CLI_ID) == []
    assert SLOW_CLI not in side_effects["cached"]
    assert side_effects["sessions"] == []
    assert inflight_cap._inflight_chat_active == 0


async def test_hang_up_during_streamed_cli_call_frees_slot_and_kills_cli(
    client, slow_cli, upstreams, abandon, inflight_cap, usage_store, side_effects
):
    req = abandon("/v1/chat/completions", _chat(SLOW_CLI, stream=True))
    pids = await slow_cli.started()

    await req.hang_up()

    await _other_model_answers(client, usage_store)
    assert req.task.done(), "the abandoned stream still holds its slot"
    assert await _all_dead(pids), "the CLI (or its child) survived the hang-up"
    assert _rows_for(usage_store, CLI_ID) == []
    assert SLOW_CLI not in side_effects["cached"]
    assert inflight_cap._inflight_chat_active == 0


@pytest.mark.parametrize("stream", [False, True], ids=["non-stream", "stream"])
async def test_hang_up_before_http_answer_frees_slot_and_cancels_upstream(
    stream, client, upstreams, abandon, inflight_cap, usage_store, side_effects
):
    req = abandon("/v1/chat/completions", _chat(SLOW_HTTP, stream=stream))
    await asyncio.wait_for(upstreams.started.wait(), SETTLE_S)

    await req.hang_up()

    await _other_model_answers(client, usage_store)
    assert req.task.done(), "the abandoned request still holds its slot"
    assert upstreams.cancelled.is_set(), "the upstream request was not cancelled"
    assert _rows_for(usage_store, PROVIDER_A["id"]) == []
    assert SLOW_HTTP not in side_effects["cached"]
    assert inflight_cap._inflight_chat_active == 0
    assert provider_admission._active == {}


async def test_hang_up_during_preflight_frees_slot_and_kills_cli(
    client, slow_cli, upstreams, abandon, inflight_cap, usage_store
):
    items = {"items": [{"provider": CLI_ID, "model": CLI_MODEL}] * 3}
    req = abandon("/v1/preflight", items)
    pids = await slow_cli.started()

    await req.hang_up()

    await _other_model_answers(client, usage_store)
    assert req.task.done(), "the abandoned preflight still holds its slot"
    assert await _all_dead(pids), "the CLI (or its child) survived the hang-up"
    # The batch stops: no other ping is started after the hang-up.
    assert slow_cli.run_count() == 1
    assert _rows_for(usage_store, CLI_ID) == []
    assert inflight_cap._inflight_chat_active == 0


@pytest.mark.parametrize("stream", [False, True], ids=["non-stream", "stream"])
async def test_hang_up_as_the_answer_arrives_gives_200_or_499_never_500(
    stream, client, abandon, inflight_cap
):
    """The answer and the hang-up race: the request ends 200 or 499, never 500.

    The client hangs up as the upstream answers, the answer then comes `lag`
    loop steps later: the disconnect reaches the gateway at every point around
    the end of the upstream call, before and after it.
    """
    current: list = []

    async def answer(url, **kwargs):
        req, lag = current[-1]
        req.disconnect()
        for _ in range(lag):
            await asyncio.sleep(0)
        if stream:
            return CallbackResult(
                body=FAKE_STREAM_BODY, content_type="text/event-stream"
            )
        return CallbackResult(payload=FAKE_COMPLETION)

    outcomes: dict[int, object] = {}
    with aioresponses() as mock:
        mock.post(OTHER_URL, callback=answer, repeat=True)
        for lag in range(40):
            body = {**_chat(OTHER, stream=stream), "cache": False}
            req = abandon("/v1/chat/completions", body)
            current.append((req, lag))
            await asyncio.wait({req.task}, timeout=SETTLE_S)
            assert req.task.done(), f"lag {lag}: the request never ended"
            error = None if req.task.cancelled() else req.task.exception()
            outcomes[lag] = repr(error) if error else req.status()

    seen = collections.Counter(outcomes.values())
    assert set(seen) <= {200, 499}, outcomes
    assert seen[200] and seen[499], "the sweep missed the race window"
    assert inflight_cap._get_inflight_sem()._value == inflight_cap._INFLIGHT_CHAT_MAX
    assert inflight_cap._inflight_chat_active == 0


# ── Non-regressions ─────────────────────────────────────────────────────────


async def test_finished_requests_free_their_slot(
    client, upstreams, inflight_cap, usage_store
):
    for _ in range(3):
        resp = await client.post(
            "/v1/chat/completions", headers=AUTH, json=_chat(OTHER)
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["X-Cache"] in {"MISS", "HIT"}
    assert inflight_cap._inflight_chat_active == 0


async def test_saturated_cap_still_answers_503_with_retry_after(
    client, upstreams, inflight_cap
):
    sem = inflight_cap._get_inflight_sem()
    await sem.acquire()
    try:
        resp = await client.post(
            "/v1/chat/completions", headers=AUTH, json=_chat(OTHER)
        )
    finally:
        sem.release()

    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "5"
    detail = resp.json()["detail"]
    assert "concurrent chat" in detail and "limit" in detail
    assert "retry" in detail.lower()


async def test_client_that_stays_gets_the_cli_answer(
    client, tmp_path, monkeypatch, inflight_cap, usage_store
):
    """Watching the client does not disturb a CLI call that completes."""
    script = tmp_path / "claude"
    answer = {"type": "result", "subtype": "success", "result": "late answer"}
    script.write_text(f"#!/bin/sh\nsleep 0.3\necho '{json.dumps(answer)}'\n")
    script.chmod(0o755)
    monkeypatch.setattr(cli_subprocess, "_binary_path_cache", {"claude": str(script)})
    spawn = tmp_path / "spawn"
    spawn.mkdir()
    monkeypatch.setenv("PLUGBOARD_SUBPROCESS_CWD", str(spawn))

    body = {**_chat(SLOW_CLI), "cache": False}
    resp = await client.post("/v1/chat/completions", headers=AUTH, json=body)

    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["message"]["content"] == "late answer"
    assert len(_rows_for(usage_store, CLI_ID)) == 1
    assert inflight_cap._inflight_chat_active == 0


# ── The watch itself ────────────────────────────────────────────────────────


class _Caller:
    """A request whose receive() answers `message`, or never answers."""

    def __init__(self, message: dict | None = None):
        self._message = message

    async def receive(self) -> dict:
        if self._message is None:
            await asyncio.Event().wait()
        return self._message


async def test_hang_up_leaves_no_pending_cancellation(inflight_cap):
    with pytest.raises(inflight_cap._ClientGone):
        await inflight_cap._unless_client_gone(
            _Caller({"type": "http.disconnect"}), asyncio.sleep(10)
        )
    assert asyncio.current_task().cancelling() == 0
    await asyncio.sleep(0)  # no stray CancelledError on the next await


async def test_other_cancellations_still_propagate(inflight_cap):
    started = asyncio.Event()

    async def work():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.ensure_future(inflight_cap._unless_client_gone(_Caller(), work()))
    await started.wait()
    task.cancel()  # e.g. the server shuts down

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_unexpected_message_stops_the_watch_not_the_work(inflight_cap):
    """A server that answers anything but http.disconnect is not watched."""
    caller = _Caller({"type": "http.request", "body": b"", "more_body": False})
    result = await asyncio.wait_for(
        inflight_cap._unless_client_gone(caller, asyncio.sleep(0.05, result="ok")),
        SETTLE_S,
    )
    assert result == "ok"


class _LateDisconnect:
    """receive() as the middleware stack may give it to the watcher.

    BaseHTTPMiddleware reads the client through an anyio task group: when the
    disconnect is already there, the watcher's own cancellation is swallowed
    and receive() still returns http.disconnect.
    """

    async def receive(self) -> dict:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        return {"type": "http.disconnect"}


async def test_disconnect_seen_after_the_answer_keeps_the_answer(inflight_cap):
    work = inflight_cap._unless_client_gone(
        _LateDisconnect(), asyncio.sleep(0.01, result="ok")
    )
    task = asyncio.ensure_future(work)
    await asyncio.wait({task}, timeout=SETTLE_S)

    assert task.done()
    assert not task.cancelled(), "a disconnect after the answer cancelled it"
    assert task.result() == "ok"
