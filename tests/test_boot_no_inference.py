"""BUG-45 (audit K3-02, B-03): with the code's defaults, startup calls no model.

The real `_lifespan` runs (provider watchdog, Ollama bootstrap, model
availability scheduler, internet-access probe) against a CLI provider, the
local Ollama provider and an HTTP provider, under a simulated clock: each
task's ``asyncio.sleep`` advances its own virtual time, and a sleep that would
reach 60 s never returns. Every call that could run a model is recorded:

* a CLI completion (``cli_subprocess.complete``);
* an HTTP request to a generation endpoint (``/api/generate``,
  ``/chat/completions``...), seen through aioresponses, which also answers the
  calls that run no model (``/api/tags``, ``/health``) and keeps the suite off
  the network.

No CLI process is started: ``asyncio.create_subprocess_exec`` fails. The
pricing scheduler is replaced, since it only downloads prices (reading prices
with a model is off by default: ``test_llm_pricing.test_off_by_default``).
"""

from __future__ import annotations

import asyncio
import contextvars
import re
from typing import Any, Dict, List

import pytest
from aioresponses import CallbackResult, aioresponses

from plugboard import app as app_module
from plugboard import cli_subprocess
from plugboard import model_availability as ma
from plugboard import ollama as ollama_mod
from plugboard import provider_watchdog
from tests.conftest import _setup_providers

WINDOW_S = 60.0

# Paths that make a provider run a model.
_INFERENCE_PATH = re.compile(
    r"(/api/generate|/api/chat|/chat/completions|/completions|/messages|"
    r"/responses|:generateContent|:streamGenerateContent)$"
)

_virtual_now: contextvars.ContextVar[float] = contextvars.ContextVar(
    "virtual_now", default=0.0
)


def _providers() -> List[Dict[str, Any]]:
    return [
        {
            "id": "boot-claude-cli",
            "name": "Claude (via claude CLI)",
            "is_active": True,
            "base_url": "subprocess://claude",
            "auth_source": "subprocess:claude",
            "provider_type": "cloud",
            "default_model": "claude-sonnet-4-5",
            "models": [{"id": "claude-sonnet-4-5", "name": "claude-sonnet-4-5"}],
        },
        {
            "id": ollama_mod._PROVIDER_ID,
            "name": "Ollama",
            "is_active": True,
            "base_url": ollama_mod._OLLAMA_URL,
            "provider_type": "sovereign",
            "default_model": "llama3:latest",
            "models": [{"id": "llama3:latest", "name": "llama3:latest"}],
        },
        {
            "id": "boot-http",
            "name": "Boot HTTP",
            "is_active": True,
            "base_url": "https://api.boot-http.test/v1",
            "bearer_token": "test-token",
            "provider_type": "cloud",
            "default_model": "boot-large",
            "models": [{"id": "boot-large", "name": "boot-large"}],
        },
    ]


class _StubScheduler:
    disabled = True
    daily_hour_utc = 0

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class _Boot:
    """The simulated clock and what the boot called."""

    def __init__(self) -> None:
        self.inference: List[str] = []
        self.parked: set[asyncio.Task] = set()
        self.real_sleep = asyncio.sleep

    async def sleep(self, delay: float, result: Any = None) -> Any:
        now = _virtual_now.get() + max(0.0, float(delay))
        if now >= WINDOW_S:
            task = asyncio.current_task()
            if task is not None:
                self.parked.add(task)
            await asyncio.get_running_loop().create_future()  # past the window
        _virtual_now.set(now)
        await self.real_sleep(0)
        return result

    def http(self, url, **kwargs) -> CallbackResult:
        method = str(kwargs.get("method") or "")
        path = url.path
        if _INFERENCE_PATH.search(path):
            self.inference.append(f"http {url}")
        if path.endswith("/api/tags"):
            return CallbackResult(
                payload={
                    "models": [{"name": "llama3:latest", "model": "llama3:latest"}]
                }
            )
        if path.endswith("/api/generate"):
            return CallbackResult(payload={"response": "OK", "done": True})
        if path.endswith("/chat/completions"):
            return CallbackResult(
                payload={
                    "id": "chatcmpl-boot",
                    "object": "chat.completion",
                    "model": "boot-large",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "OK"},
                            "finish_reason": "stop",
                        }
                    ],
                }
            )
        if path.endswith("/health") or path in ("", "/"):
            return CallbackResult(payload={"status": "ok"})
        return CallbackResult(status=404, payload={"error": f"{method} not found"})

    async def complete(self, kind: str, body: Dict[str, Any], **_kwargs) -> Dict:
        last = (body.get("messages") or [{}])[-1].get("content")
        what = "probe" if last == ma._AVAILABILITY_PROMPT else "chat"
        self.inference.append(f"cli {kind} {body.get('model')} {what}")
        return {
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}}]
        }

    async def settled(self) -> None:
        """Wait until every boot task has finished or is parked past 60 s."""
        names = {
            "model-availability-scheduler",
            "ollama-late-start-watcher",
            "internet-boot-probe",
            "subprocess-warm-up",
        }

        def pending() -> List[asyncio.Task]:
            watched = [
                t
                for t in asyncio.all_tasks()
                if t.get_name() in names or t is provider_watchdog._watchdog_task
            ]
            return [t for t in watched if not t.done() and t not in self.parked]

        for _ in range(1000):
            if not pending():
                return
            await self.real_sleep(0.01)
        raise AssertionError(f"boot tasks still running: {pending()}")


@pytest.fixture
def boot(tmp_path, monkeypatch):
    """A fresh install with three providers, its clock and its spies."""
    for name in (
        "MODEL_AVAILABILITY_SCHEDULER_DISABLED",
        "MODEL_AVAILABILITY_BOOTSTRAP_DELAY_S",
        "OLLAMA_URL",
        "OLLAMA_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    _setup_providers(tmp_path, monkeypatch, _providers())
    data = tmp_path / "data"
    monkeypatch.setattr(ma, "_MODEL_STATUS_PATH", data / "model_status.json")
    monkeypatch.setattr(ma, "_MARKER_FILE", data / "last_model_availability_run.txt")
    monkeypatch.setattr(ma, "_DATA_DIR", data)
    from plugboard import model_quarantine

    monkeypatch.setattr(
        model_quarantine, "_QUARANTINE_FILE", data / "quarantined_models.json"
    )
    monkeypatch.setattr(provider_watchdog, "_states", {})
    monkeypatch.setattr(provider_watchdog, "_check_counter", {})
    monkeypatch.setattr(provider_watchdog, "_log_event", lambda *a, **k: None)

    state = _Boot()
    monkeypatch.setattr(asyncio, "sleep", state.sleep)
    monkeypatch.setattr(cli_subprocess, "complete", state.complete)

    async def _no_process(*args, **kwargs):
        raise FileNotFoundError(args[0] if args else "cli")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _no_process)
    monkeypatch.setattr("plugboard.pricing_scheduler.PricingScheduler", _StubScheduler)
    # Loading the tiktoken encoding may download it: never from the suite.
    import importlib

    monkeypatch.setattr(
        importlib.import_module("plugboard.tokenizer"), "_get_encoding", lambda: None
    )

    async def _noop():
        return None

    monkeypatch.setattr("plugboard.change_events.drain", _noop)
    monkeypatch.setattr("plugboard.http_pool.close_session", _noop)
    return state


async def _run_boot(state: _Boot) -> None:
    with aioresponses() as mocked:
        for method in ("GET", "POST", "HEAD"):
            mocked.add(
                re.compile(r".*"),
                method=method,
                callback=lambda url, _m=method, **kw: state.http(url, method=_m, **kw),
                repeat=True,
            )
        async with app_module._lifespan(app_module.app):
            await state.settled()


@pytest.mark.asyncio
async def test_defaults_call_no_model_in_the_first_60_seconds(boot, monkeypatch):
    for name in (
        "PLUGBOARD_MODEL_REFRESH_PROBE_KINDS",
        "PLUGBOARD_INTERNET_CHECK_ON_BOOT",
        "PLUGBOARD_PROBE_ON_BOOT",
    ):
        monkeypatch.delenv(name, raising=False)

    await _run_boot(boot)

    assert boot.inference == []


@pytest.mark.asyncio
async def test_probe_variables_turn_the_boot_probes_back_on(boot, monkeypatch):
    # The harness sees a boot that does call the models: the two documented
    # variables bring back the CLI and Ollama probes, and the internet-access
    # check (a chat request to each model).
    monkeypatch.setenv("PLUGBOARD_MODEL_REFRESH_PROBE_KINDS", "claude,ollama")
    monkeypatch.setenv("PLUGBOARD_INTERNET_CHECK_ON_BOOT", "1")

    await _run_boot(boot)

    assert "cli claude claude-sonnet-4-5 probe" in boot.inference
    assert f"http {ollama_mod._OLLAMA_URL}/api/generate" in boot.inference
    assert any(call.endswith(" chat") for call in boot.inference)


@pytest.mark.asyncio
async def test_ollama_watcher_probes_only_when_ollama_probes_are_on(
    tmp_path, monkeypatch
):
    # The late-start watcher re-probes offline Ollama models with a generation
    # request each: like the startup probe, only with ollama in the kinds.
    status_file = tmp_path / "model_status.json"
    ma.save_model_status(
        {
            "models": {
                "ollama:llama3:latest": {
                    "connected": False,
                    "provider": "ollama",
                    "reason": "ollama_daemon_unreachable",
                }
            }
        },
        status_file,
    )
    monkeypatch.setattr(ma, "_MODEL_STATUS_PATH", status_file)
    calls: List[set] = []

    async def _probes(*, probe_kinds=None, **_kwargs):
        calls.append(probe_kinds)
        return {"models": {}}

    monkeypatch.setattr(ma, "run_connectivity_probes", _probes)
    cycles = 0

    async def _cycle(_delay):
        nonlocal cycles
        cycles += 1
        if cycles > 3:
            raise asyncio.CancelledError

    scheduler = ma.ModelAvailabilityScheduler(sleep_fn=_cycle)

    monkeypatch.delenv("PLUGBOARD_MODEL_REFRESH_PROBE_KINDS", raising=False)
    with pytest.raises(asyncio.CancelledError):
        await scheduler._watch_ollama_late_start()
    assert calls == []

    cycles = 0
    monkeypatch.setenv("PLUGBOARD_MODEL_REFRESH_PROBE_KINDS", "ollama")
    with pytest.raises(asyncio.CancelledError):
        await scheduler._watch_ollama_late_start()
    assert calls and all(kinds == {"ollama"} for kinds in calls)
