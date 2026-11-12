"""A CLI subscription route gets the model name whole, and only one it lists.

``cli_subprocess.complete()`` cut everything before the first ":" of the
model, "defensively": the router already hands it the provider's own name,
so the cut only ever damaged a name holding a colon (``qwen3:8b`` reached
the CLI as ``8b``). The name now reaches the CLI, the answer and the error
whole.

With the name whole, a CLI refusing ``qwen3:8b`` would quarantine the bare
name another provider (Ollama) serves. So an unqualified model is no longer
offered to a CLI route that lists models without it; a CLI route without a
model list, or one that knows the name, stays a candidate. A provider-
qualified model is selected as before.

No CLI runs: the process is a fake, or ``cli_subprocess.complete`` is a
counter. The HTTP provider is a real server on 127.0.0.1.
"""

from __future__ import annotations

import json
import uuid

import pytest
from aiohttp import web

from tests.factories import FAKE_COMPLETION, PROVIDER_A

MODEL = "qwen3:8b"

CODEX_CLI = {
    "id": "chatgpt-via-codex-cli",
    "name": "Codex CLI test",
    "base_url": "subprocess://codex",
    "bearer_token": "(via CLI)",
    "default_model": "gpt-5.5",
    "auth_source": "subprocess:codex",
    "provider_type": "cloud",
    "models": [{"id": "gpt-5.5"}],
    "is_active": True,
    "created_at": "2026-01-01T00:00:00+00:00",
}
HTTP_WITH_MODEL = {**PROVIDER_A, "models": [{"id": MODEL}]}


def _chat(model: str = MODEL, stream: bool = False) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": stream,
        # A cached answer from another test would skip the provider call.
        "cache": False,
    }


# ── complete() keeps the name whole ───────────────────────────────────────


@pytest.fixture
def cli_process(monkeypatch):
    """The real CLI wrapper runs on a fake process; returns its spawn list."""
    from unittest.mock import AsyncMock, MagicMock

    from plugboard import cli_subprocess as cs

    spawned: list = []

    def _setup(stdout: bytes, stderr: bytes = b"", returncode: int = 0) -> list:
        proc = MagicMock()
        proc.returncode = returncode
        proc.pid = 4242
        proc.communicate = AsyncMock(return_value=(stdout, stderr))

        async def _spawn(*args, **kwargs):
            spawned.append(list(args))
            return proc

        monkeypatch.setattr(cs, "_resolve_binary", lambda cmd: f"/fake/bin/{cmd}")
        monkeypatch.setattr(cs.asyncio, "create_subprocess_exec", _spawn)
        return spawned

    return _setup


def _has_pair(argv: list, flag: str, value: str) -> bool:
    return any(argv[i : i + 2] == [flag, value] for i in range(len(argv) - 1))


_CODEX_OK = (
    b'{"type":"item.completed","item":{"type":"agent_message","text":"Hello!"}}\n'
    b'{"type":"turn.completed","usage":{"input_tokens":3,"output_tokens":2}}\n'
)


@pytest.mark.asyncio
async def test_complete_passes_a_name_with_a_colon_whole(cli_process):
    from plugboard import cli_subprocess as cs

    spawned = cli_process(_CODEX_OK)

    result = await cs.complete("codex", _chat())

    (argv,) = spawned
    assert _has_pair(argv, "--model", MODEL), argv
    assert not _has_pair(argv, "--model", "8b"), argv
    assert result["model"] == f"codex-cli:{MODEL}"


@pytest.mark.asyncio
async def test_a_cli_failure_names_the_whole_model(cli_process):
    from plugboard import cli_subprocess as cs

    spawned = cli_process(b"", b"model not supported\n", returncode=1)

    with pytest.raises(cs.CliSubprocessError) as info:
        await cs.complete("codex", _chat())

    (argv,) = spawned
    assert _has_pair(argv, "--model", MODEL), argv
    assert info.value.model_id == MODEL


# ── The unqualified fallback skips a CLI that does not list the name ─────


@pytest.fixture
def providers(_providers_file, monkeypatch):
    """Write the given provider records; equal load-balancing scores."""
    import plugboard.metrics as metrics
    from plugboard.router import invalidate_models_cache

    monkeypatch.setattr(metrics, "_stats", {})

    def write(*records: dict) -> None:
        _providers_file.write_text(json.dumps({"providers": list(records)}))
        invalidate_models_cache()

    return write


def _ids(candidates) -> list:
    return [(p["id"], native) for p, native in candidates]


def test_a_cli_listing_other_models_is_not_a_candidate(providers):
    from plugboard.router import _resolve_all_providers

    providers(CODEX_CLI, HTTP_WITH_MODEL)

    assert _ids(_resolve_all_providers(MODEL)) == [(PROVIDER_A["id"], MODEL)]


def test_a_cli_without_a_model_list_stays_a_candidate(providers):
    from plugboard.router import _resolve_all_providers

    unlisted = {k: v for k, v in CODEX_CLI.items() if k != "models"}
    providers(unlisted, HTTP_WITH_MODEL)

    assert _ids(_resolve_all_providers(MODEL)) == [
        (CODEX_CLI["id"], MODEL),
        (PROVIDER_A["id"], MODEL),
    ]


def test_a_cli_listing_the_name_stays_a_candidate(providers):
    from plugboard.router import _resolve_all_providers

    listing = {**CODEX_CLI, "models": [{"id": "gpt-5.5"}, {"id": MODEL}]}
    providers(listing, HTTP_WITH_MODEL)

    assert _ids(_resolve_all_providers(MODEL)) == [
        (CODEX_CLI["id"], MODEL),
        (PROVIDER_A["id"], MODEL),
    ]


def test_a_qualified_cli_model_is_selected_as_before(providers):
    from plugboard.router import _resolve_all_providers

    providers(CODEX_CLI, HTTP_WITH_MODEL)

    assert _ids(_resolve_all_providers(f"{CODEX_CLI['id']}:gpt-5.5")) == [
        (CODEX_CLI["id"], "gpt-5.5")
    ]


# ── Over HTTP: the HTTP provider answers, the CLI is never called ─────────


@pytest.fixture
def isolated(_providers_file, monkeypatch, tmp_path):
    """Fresh breaker, alerts, metrics, admission and quarantine file.

    Returns the list of calls the CLI wrapper received.
    """
    import plugboard.alerts as alerts
    import plugboard.metrics as metrics
    import plugboard.model_quarantine as quarantine
    import plugboard.router as router
    import plugboard.session_store as sessions
    from plugboard import circuit_breaker as cb
    from plugboard import cli_subprocess, provider_admission

    monkeypatch.setattr(router, "_DELAYS", [0, 0, 0])
    monkeypatch.setattr(cb, "_circuits", {})
    monkeypatch.setattr(alerts, "_provider_failures", {})
    monkeypatch.setattr(alerts, "_provider_alerted", {})
    # Equal load-balancing scores keep the candidates in providers.json order.
    monkeypatch.setattr(metrics, "_stats", {})
    monkeypatch.setattr(sessions, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(sessions, "_DB_PATH", tmp_path / "sessions.db")
    monkeypatch.setattr(sessions, "_INITIALISED", False)
    qfile = tmp_path / "quarantined_models.json"
    monkeypatch.setattr(quarantine, "_QUARANTINE_FILE", qfile)
    for fn in (
        quarantine.record_failure,
        quarantine.record_account_unsupported,
        quarantine.record_terminal_unavailable,
    ):
        monkeypatch.setitem(fn.__kwdefaults__, "path", qfile)
    provider_admission.reset()

    cli_calls: list = []

    async def fake_complete(kind, body, *, timeout_s=None):
        cli_calls.append((kind, body))
        return json.loads(json.dumps(FAKE_COMPLETION))

    monkeypatch.setattr(cli_subprocess, "complete", fake_complete)
    yield cli_calls
    provider_admission.reset()


@pytest.fixture
async def http_provider(_providers_file, isolated):
    """The CLI route first, then an HTTP provider listing the model.

    Returns the list of request bodies the HTTP provider received.
    """
    from plugboard.router import invalidate_models_cache

    received: list[dict] = []

    async def handle(request: web.Request) -> web.Response:
        received.append(await request.json())
        return web.json_response(FAKE_COMPLETION)

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    http = {**HTTP_WITH_MODEL, "base_url": f"http://127.0.0.1:{site.port}"}
    _providers_file.write_text(json.dumps({"providers": [CODEX_CLI, http]}))
    invalidate_models_cache()
    try:
        yield received
    finally:
        await runner.cleanup()


@pytest.fixture
def client_key(tmp_path, monkeypatch):
    """Authorization header of a plain client key (not the admin)."""
    import plugboard.apikeys as ak
    from plugboard import app as app_mod

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    monkeypatch.setattr(app_mod, "_check_price_known", lambda *a, **k: None)
    rec = ak.create_key(f"model-name-{uuid.uuid4().hex[:12]}", "test")
    return {"Authorization": f"Bearer {rec['key']}"}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["json", "stream"])
async def test_an_unqualified_name_is_served_by_the_provider_listing_it(
    client, client_key, isolated, http_provider, stream
):
    resp = await client.post(
        "/v1/chat/completions", headers=client_key, json=_chat(stream=stream)
    )

    assert resp.status_code == 200, resp.text
    assert len(http_provider) == 1
    assert http_provider[0]["model"] == MODEL
    assert isolated == []
