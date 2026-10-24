"""Parent RED counter-proofs for four inherited runtime flaws (v6a).

Each defect test fails by assertion on the unfixed code; the witnesses pass
before and after the fix. No network, no real subprocess, synthetic values.

#1 POST /v1/preflight bypasses per-key policy, is unbounded and leaks str(exc).
#2 An unpriced model is free (calculate_cost(..., None) == 0.0) and slips past
   monthly_limit_usd.
#3 cli_subprocess.complete()/list_agy_models(): no process group, timeout and
   cancellation do not kill+reap the child before releasing the semaphore and
   the private gemini home.
#4 The INFO 'spawn CLI' log line carries argv[1:4], i.e. the agy prompt.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import os
import signal

import pytest

from plugboard import cli_subprocess
from plugboard.cli_subprocess import CliSubprocessError
from tests.factories import FAKE_COMPLETION

_MODEL = "test-provider:fake:model-v1"
_MONTH = "2026_10"
_KNOWN_PRICE = {"input_price": 1.0, "output_price": 2.0, "source": "synthetic"}


# ── shared app harness ───────────────────────────────────────────────────────


@pytest.fixture
def keys_env(tmp_path, monkeypatch):
    import plugboard.apikeys as ak
    import plugboard.billing as b
    import plugboard.usage as u

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    monkeypatch.setattr(u, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(u, "_DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(b, "current_pricing_month", lambda: _MONTH)
    ak._invalidate_cache()
    yield ak
    ak._invalidate_cache()


def _new_key(ak, client_id, **kw):
    rec = ak.create_key(client_id, "synthetic", **kw)
    ak._invalidate_cache()
    return rec


@pytest.fixture
def fake_forward(monkeypatch):
    """Replace the provider call point; records each call, never networks."""
    state = {"calls": [], "exc": None}

    async def _forward(body, *args, **kwargs):
        state["calls"].append(copy.deepcopy(body))
        if state["exc"] is not None:
            raise state["exc"]
        return copy.deepcopy(FAKE_COMPLETION)

    monkeypatch.setattr("plugboard.app.forward", _forward)
    monkeypatch.setattr("plugboard.router.forward", _forward)
    return state


def _set_price(monkeypatch, entry):
    import plugboard.billing as b

    if entry is None:
        monkeypatch.setattr(b, "resolve_price", lambda *a, **k: None)
    else:
        match = b.PriceMatch(dict(entry), "fake:model-v1", "models")
        monkeypatch.setattr(b, "resolve_price", lambda *a, **k: match)


def _auth(rec):
    return {"Authorization": f"Bearer {rec['key']}"}


def _items(n=1):
    return {"items": [{"provider": "test-provider", "model": "fake:model-v1"}] * n}


def _all_refused(resp):
    if resp.status_code in (402, 403, 429):
        return True
    if resp.status_code != 200:
        return False
    results = resp.json().get("results") or []
    return bool(results) and all(r.get("ok") is False for r in results)


# ── #1 /v1/preflight ─────────────────────────────────────────────────────────


async def test_preflight_disallowed_model_not_forwarded(
    client, keys_env, fake_forward, monkeypatch
):
    _set_price(monkeypatch, _KNOWN_PRICE)
    rec = _new_key(keys_env, "pf-acl", allowed_models=["other-provider"])
    resp = await client.post("/v1/preflight", headers=_auth(rec), json=_items(1))
    assert fake_forward["calls"] == [], (
        "preflight forwarded a model excluded by allowed_models "
        f"(status={resp.status_code}, body={resp.text[:200]})"
    )
    assert _all_refused(resp), f"disallowed model not refused: {resp.text[:200]}"


async def test_preflight_exhausted_budget_not_forwarded(
    client, keys_env, fake_forward, monkeypatch
):
    import plugboard.usage as u

    _set_price(monkeypatch, _KNOWN_PRICE)
    rec = _new_key(keys_env, "pf-budget", monthly_limit_usd=1.0)
    u.append_usage(
        client_id="pf-budget",
        model="fake:model-v1",
        provider_id="test-provider",
        prompt_tokens=100,
        completion_tokens=50,
        pricing_month=_MONTH,
        cost_usd=1.5,
        key_id=rec["key_id"],
    )
    resp = await client.post("/v1/preflight", headers=_auth(rec), json=_items(1))
    assert fake_forward["calls"] == [], (
        "preflight forwarded with an exhausted monthly budget "
        f"(status={resp.status_code}, body={resp.text[:200]})"
    )
    assert _all_refused(resp), f"exhausted budget not refused: {resp.text[:200]}"


async def test_preflight_more_than_ten_items_rejected(
    client, keys_env, fake_forward, monkeypatch
):
    _set_price(monkeypatch, _KNOWN_PRICE)
    rec = _new_key(keys_env, "pf-bound")
    resp = await client.post("/v1/preflight", headers=_auth(rec), json=_items(11))
    assert resp.status_code == 422, (
        f"11 preflight items accepted: status={resp.status_code}, "
        f"forward calls={len(fake_forward['calls'])}"
    )
    assert fake_forward["calls"] == []


async def test_preflight_upstream_error_not_echoed(
    client, keys_env, fake_forward, monkeypatch
):
    secret = "SYNTH-UPSTREAM-SECRET-9c1e5b"
    _set_price(monkeypatch, _KNOWN_PRICE)
    fake_forward["exc"] = RuntimeError(f"upstream 500 body: token={secret}")
    rec = _new_key(keys_env, "pf-leak")
    resp = await client.post("/v1/preflight", headers=_auth(rec), json=_items(1))
    assert secret not in resp.text, (
        f"upstream exception text echoed by preflight: {resp.text[:200]}"
    )
    if resp.status_code == 200:
        assert resp.json()["results"][0]["ok"] is False


async def test_preflight_witness_allowed_key_ok(
    client, keys_env, fake_forward, monkeypatch
):
    _set_price(monkeypatch, _KNOWN_PRICE)
    rec = _new_key(
        keys_env, "pf-ok", allowed_models=["test-provider"], monthly_limit_usd=5.0
    )
    resp = await client.post("/v1/preflight", headers=_auth(rec), json=_items(1))
    assert resp.status_code == 200, resp.text[:200]
    results = resp.json()["results"]
    assert len(results) == 1 and results[0]["ok"] is True, results
    assert len(fake_forward["calls"]) == 1


# ── #2 unpriced model vs monthly budget ─────────────────────────────────────


def _chat_body():
    return {
        "model": _MODEL,
        "messages": [{"role": "user", "content": "Hi"}],
        "cache": False,
    }


async def test_unpriced_model_refused_for_budgeted_key(
    client, keys_env, fake_forward, monkeypatch
):
    _set_price(monkeypatch, None)
    rec = _new_key(keys_env, "bill-capped", monthly_limit_usd=5.0)
    resp = await client.post(
        "/v1/chat/completions", headers=_auth(rec), json=_chat_body()
    )
    assert fake_forward["calls"] == [], (
        "unpriced model forwarded for a key with monthly_limit_usd>0 "
        f"(status={resp.status_code}, billing={resp.json().get('billing') if resp.status_code == 200 else None})"
    )
    assert resp.status_code in (402, 429), resp.status_code


async def test_unpriced_model_witness_unbudgeted_key_allowed(
    client, keys_env, fake_forward, monkeypatch
):
    _set_price(monkeypatch, None)
    rec = _new_key(keys_env, "bill-free", monthly_limit_usd=0.0)
    resp = await client.post(
        "/v1/chat/completions", headers=_auth(rec), json=_chat_body()
    )
    assert resp.status_code == 200, resp.text[:200]
    assert len(fake_forward["calls"]) == 1


async def test_priced_model_witness_billed(client, keys_env, fake_forward, monkeypatch):
    _set_price(monkeypatch, _KNOWN_PRICE)
    rec = _new_key(keys_env, "bill-priced", monthly_limit_usd=5.0)
    resp = await client.post(
        "/v1/chat/completions", headers=_auth(rec), json=_chat_body()
    )
    assert resp.status_code == 200, resp.text[:200]
    assert len(fake_forward["calls"]) == 1
    # 5 prompt tokens * $1/M + 2 completion tokens * $2/M
    assert resp.json()["billing"]["cost_usd"] == pytest.approx(9e-06)


# ── #3/#4 CLI subprocess harness ─────────────────────────────────────────────

_PID = 987654
_OK_STDOUT = {
    "claude": b'{"type":"result","result":"OK"}\n',
    "codex": b'{"type":"item.completed","item":{"type":"agent_message","text":"OK"}}\n',
    "gemini": b'{"response":"OK"}',
    "agy": b'{"status":"SUCCESS","response":"OK","usage":{}}',
}


class _FakeProc:
    def __init__(self, events, *, stdout=b"", hang=False):
        self.events = events
        self.pid = _PID
        self.returncode = None
        self._stdout = stdout
        self._hang = hang
        self.entered = asyncio.Event()
        self.stdin_input = None
        self.on_wait = None

    async def communicate(self, input=None):
        self.stdin_input = input
        self.entered.set()
        if self._hang:
            await asyncio.Event().wait()
        self.returncode = 0
        return self._stdout, b""

    def kill(self):
        self.events.append("proc.kill")

    def terminate(self):
        self.events.append("proc.terminate")

    def send_signal(self, sig):
        self.events.append(("proc.send_signal", sig))

    async def wait(self):
        if self.on_wait is not None:
            self.on_wait()
        await asyncio.sleep(0)
        self.events.append("wait")
        if self.returncode is None:
            self.returncode = -signal.SIGKILL
        return self.returncode


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    """Fake binaries, fake exec, fake signals, recording semaphore/home."""
    events: list = []
    state = {"events": events, "exec": [], "proc": None, "homes": []}

    # Synthetic operator home: never look at a real ~/.gemini.
    operator_home = tmp_path / "operator-home"
    operator_home.mkdir()
    monkeypatch.setenv("HOME", str(operator_home))
    monkeypatch.setenv("GEMINI_CLI_HOME", str(operator_home))
    # Harness only: the strict runner's audit hook resolves dir_fd-relative
    # opens (shutil's fd-based rmtree) against the cwd and flags them as a
    # private read of ".gemini". The path-based rmtree still deletes for real.
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "_use_fd_functions", False)

    monkeypatch.setattr(cli_subprocess, "_binary_path_cache", {})
    monkeypatch.setattr(cli_subprocess.shutil, "which", lambda cmd: f"/fake/bin/{cmd}")
    monkeypatch.delenv("PLUGBOARD_SUBPROCESS_CWD", raising=False)

    async def fake_exec(*argv, **kwargs):
        state["exec"].append((list(argv), dict(kwargs)))
        return state["proc"]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    def fake_killpg(pgid, sig):
        events.append(("killpg", pgid, sig))
        proc = state["proc"]
        if proc is not None and proc.returncode is None:
            proc.returncode = -sig

    def fake_kill(pid, sig):
        events.append(("os.kill", pid, sig))

    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)

    class _RecSem(asyncio.Semaphore):
        def release(self):
            events.append("sem_release")
            super().release()

    sems: dict = {}

    def fake_get_semaphore(kind):
        return sems.setdefault(kind, _RecSem(4))

    monkeypatch.setattr(cli_subprocess, "_get_semaphore", fake_get_semaphore)

    real_home = cli_subprocess.gemini_private_home

    @contextlib.contextmanager
    def rec_home():
        with real_home() as home:
            state["homes"].append(home)
            try:
                yield home
            finally:
                events.append("home_release")

    monkeypatch.setattr(cli_subprocess, "gemini_private_home", rec_home)
    return state


def _body(kind, text="hello"):
    return {
        "model": f"{kind}-cli:x-model",
        "messages": [{"role": "user", "content": text}],
    }


def _attach_wait_probe(state, proc):
    """At proc.wait() time, record whether the private gemini home exists."""

    def probe():
        env = state["exec"][-1][1].get("env") or {}
        home = env.get("GEMINI_CLI_HOME")
        state["home_alive_at_wait"] = bool(home) and os.path.isdir(home)

    proc.on_wait = probe


def _assert_killed_and_reaped(state, kind):
    events = state["events"]
    kills = [e for e in events if isinstance(e, tuple) and e[0] == "killpg"]
    assert kills and kills[0][1:] == (_PID, signal.SIGKILL), (
        f"os.killpg(pid, SIGKILL) not called; events={events}"
    )
    assert "wait" in events, f"proc.wait() never awaited after kill; events={events}"
    w = events.index("wait")
    assert events.index(kills[0]) < w, events
    assert "sem_release" in events and w < events.index("sem_release"), (
        f"semaphore released before the child was reaped; events={events}"
    )
    if kind == "gemini":
        assert state.get("home_alive_at_wait") is True, (
            "private gemini home removed before proc.wait()"
        )
        assert w < events.index("home_release"), events
        assert not os.path.isdir(state["homes"][0])


@pytest.mark.parametrize("kind", ["claude", "codex", "gemini", "agy"])
async def test_complete_spawns_new_session(cli_env, kind):
    cli_env["proc"] = _FakeProc(cli_env["events"], stdout=_OK_STDOUT[kind])
    await cli_subprocess.complete(kind, _body(kind))
    kwargs = cli_env["exec"][0][1]
    assert kwargs.get("start_new_session") is True, (
        f"{kind}: create_subprocess_exec without start_new_session=True"
    )


@pytest.mark.parametrize("kind", ["claude", "gemini"])
async def test_complete_timeout_kills_group_and_reaps(cli_env, kind):
    proc = _FakeProc(cli_env["events"], hang=True)
    cli_env["proc"] = proc
    _attach_wait_probe(cli_env, proc)
    with pytest.raises(CliSubprocessError):
        await cli_subprocess.complete(kind, _body(kind), timeout_s=0.05)
    _assert_killed_and_reaped(cli_env, kind)


@pytest.mark.parametrize("kind", ["claude", "gemini"])
async def test_complete_cancel_kills_group_and_reaps(cli_env, kind):
    proc = _FakeProc(cli_env["events"], hang=True)
    cli_env["proc"] = proc
    _attach_wait_probe(cli_env, proc)
    task = asyncio.ensure_future(
        cli_subprocess.complete(kind, _body(kind), timeout_s=30)
    )
    await asyncio.wait_for(proc.entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    _assert_killed_and_reaped(cli_env, kind)


@pytest.mark.parametrize("kind", ["claude", "gemini"])
async def test_complete_witness_normal_run_no_kill(cli_env, kind):
    cli_env["proc"] = _FakeProc(cli_env["events"], stdout=_OK_STDOUT[kind])
    result = await cli_subprocess.complete(kind, _body(kind))
    assert result["choices"][0]["message"]["content"] == "OK"
    events = cli_env["events"]
    assert not any(
        isinstance(e, tuple) and e[0] in ("killpg", "os.kill") for e in events
    )
    assert "proc.kill" not in events
    assert "sem_release" in events
    if kind == "gemini":
        assert not os.path.isdir(cli_env["homes"][0])


async def test_list_agy_models_spawns_new_session(cli_env):
    cli_env["proc"] = _FakeProc(cli_env["events"], stdout=b"m-1\tModel one\n")
    assert await cli_subprocess.list_agy_models() == ["m-1"]
    kwargs = cli_env["exec"][0][1]
    assert kwargs.get("start_new_session") is True, (
        "list_agy_models: create_subprocess_exec without start_new_session=True"
    )


async def test_list_agy_models_timeout_kills_group_and_reaps(cli_env):
    cli_env["proc"] = _FakeProc(cli_env["events"], hang=True)
    assert await cli_subprocess.list_agy_models(timeout_s=0.05) == []
    events = cli_env["events"]
    kills = [e for e in events if isinstance(e, tuple) and e[0] == "killpg"]
    assert kills and kills[0][1:] == (_PID, signal.SIGKILL), (
        f"list_agy_models timeout: os.killpg(pid, SIGKILL) not called; events={events}"
    )
    assert "wait" in events and events.index(kills[0]) < events.index("wait"), (
        f"list_agy_models timeout: proc.wait() not awaited after kill; events={events}"
    )


# ── #4 prompt never logged ───────────────────────────────────────────────────

_CANARY = "SYNTH-PROMPT-CANARY-4d2f81"


@pytest.mark.parametrize("kind", ["claude", "codex", "gemini", "agy"])
async def test_spawn_log_does_not_contain_prompt(cli_env, caplog, kind):
    cli_env["proc"] = _FakeProc(cli_env["events"], stdout=_OK_STDOUT[kind])
    module_logger = logging.getLogger(cli_subprocess.__name__)
    # Capture even if the plugboard logger tree does not propagate to root.
    module_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.DEBUG):
            module_logger.setLevel(logging.DEBUG)
            result = await cli_subprocess.complete(kind, _body(kind, _CANARY))
    finally:
        module_logger.removeHandler(caplog.handler)
    assert result["choices"][0]["message"]["content"] == "OK"
    leaked = [r.getMessage() for r in caplog.records if _CANARY in r.getMessage()]
    assert not leaked, f"{kind}: prompt written to logs: {leaked[0][:160]}"


async def test_claude_stdin_mode_keeps_prompt_out_of_argv(cli_env):
    """Witness: claude declares input_mode='stdin' in CLI_BACKENDS."""
    assert cli_subprocess.get_backend("claude").input_mode == "stdin"
    proc = _FakeProc(cli_env["events"], stdout=_OK_STDOUT["claude"])
    cli_env["proc"] = proc
    await cli_subprocess.complete("claude", _body("claude", _CANARY))
    argv = cli_env["exec"][0][0]
    assert not any(_CANARY in str(a) for a in argv), argv
    assert proc.stdin_input is not None and _CANARY.encode() in proc.stdin_input
