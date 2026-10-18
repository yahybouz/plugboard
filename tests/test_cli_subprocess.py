"""CLI subprocess provider tests — fork of OpenClaw's cli-backend pattern."""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugboard import cli_subprocess
from plugboard.cli_subprocess import (
    check_cli_installed,
    get_backend,
    messages_to_prompt,
    _parse_gemini_json_stdout,
    _extract_cli_error,
    _extract_text_from_gemini_json,
    _extract_text_from_codex_jsonl,
    _extract_text_from_claude_jsonl,
)


@pytest.fixture(autouse=True)
def _fake_cli_binaries(monkeypatch):
    """Tests spawn fake processes: pretend every CLI is on PATH.

    CI runners have no gemini, codex or claude binary. Tests that check the
    "not installed" path set shutil.which to return None themselves.
    """
    monkeypatch.setattr(cli_subprocess, "_binary_path_cache", {})
    monkeypatch.setattr(
        cli_subprocess.shutil, "which", lambda cmd: f"/usr/local/bin/{cmd}"
    )


# ── Pure helpers (no subprocess) ─────────────────────────────────────────────


def test_get_backend_known_kinds():
    for kind in ("gemini", "codex", "claude"):
        backend = get_backend(kind)
        assert backend.kind == kind
        assert backend.command


def test_get_backend_unknown_raises():
    with pytest.raises(ValueError, match="Unknown CLI subprocess kind"):
        get_backend("openrouter")


def test_extract_cli_error_codex_unwraps_nested_message():
    """Codex emits `{"type":"error","message":"<stringified JSON with nested error.message>"}`.
    The extractor must peel the outer layer so users see the useful
    business error instead of the noisy stderr warning."""
    stream = (
        '{"type":"thread.started","thread_id":"abc"}\n'
        '{"type":"turn.started"}\n'
        '{"type":"error","message":"{\\"type\\":\\"error\\",\\"status\\":400,'
        '\\"error\\":{\\"type\\":\\"invalid_request_error\\",'
        '\\"message\\":\\"The gpt-5.2-codex model is not supported for ChatGPT accounts.\\"}}"}\n'
        '{"type":"turn.failed"}'
    )
    err = _extract_cli_error(stream, "codex")
    assert "gpt-5.2-codex model is not supported" in err


def test_extract_cli_error_claude_flat_message():
    """Claude uses a flatter shape — `{"type":"error","message":"..."}`."""
    stream = '{"type":"error","message":"rate limit exceeded"}'
    assert _extract_cli_error(stream, "claude") == "rate limit exceeded"


def test_extract_cli_error_no_error_events_returns_empty():
    stream = '{"type":"turn.started"}\n{"type":"item.completed","item":{"text":"hi"}}'
    assert _extract_cli_error(stream, "codex") == ""


def test_extract_cli_error_unknown_kind_returns_empty():
    """Gemini has a different error surface — we don't parse it here."""
    stream = '{"type":"error","message":"boom"}'
    assert _extract_cli_error(stream, "gemini") == ""


def test_parse_gemini_json_stdout_accepts_noise_around_json():
    raw = 'warning line\n```json\n{"response":"OK"}\n```\n'
    payload = _parse_gemini_json_stdout(raw)
    assert payload["response"] == "OK"


def test_messages_to_prompt_flattens_system_user_assistant():
    msgs = [
        {"role": "system", "content": "Be concise."},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
        {"role": "user", "content": "How are you?"},
    ]
    prompt = messages_to_prompt(msgs)
    assert "[System]\nBe concise." in prompt
    assert "[User]\nHello" in prompt
    assert "[Assistant]\nHi there" in prompt
    assert "[User]\nHow are you?" in prompt


def test_messages_to_prompt_ignores_empty_content():
    msgs = [
        {"role": "user", "content": ""},
        {"role": "user", "content": "   "},
        {"role": "user", "content": "real"},
    ]
    prompt = messages_to_prompt(msgs)
    assert prompt == "[User]\nreal"


def test_messages_to_prompt_handles_content_blocks():
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "part1"},
                {"type": "image", "image_url": "http://x"},
                {"type": "text", "text": "part2"},
            ],
        }
    ]
    prompt = messages_to_prompt(msgs)
    assert "part1" in prompt
    assert "part2" in prompt
    assert "http://x" not in prompt


# ── Output extractors ───────────────────────────────────────────────────────


def test_extract_gemini_json_response_key():
    assert _extract_text_from_gemini_json({"response": "hello"}) == "hello"


def test_extract_gemini_json_candidates_path():
    payload = {
        "candidates": [{"content": {"parts": [{"text": "hi "}, {"text": "there"}]}}]
    }
    assert _extract_text_from_gemini_json(payload) == "hi there"


def test_extract_gemini_json_fallback_to_dumps():
    payload = {"unknown_shape": 42}
    result = _extract_text_from_gemini_json(payload)
    assert "unknown_shape" in result


def test_clean_spawn_cwd_returns_dedicated_subdir():
    """TNR — bug fix 2026-05-04: macOS tempfile.gettempdir() contains a
    Spotlight-owned TemporaryItems folder that triggers EPERM warnings in
    gemini-cli's recursive cwd scan, causing exit 1 on long outputs.
    _clean_spawn_cwd() must return a dedicated subdir we own (no
    TemporaryItems sibling)."""
    import os
    from plugboard.cli_subprocess import _clean_spawn_cwd

    cwd = _clean_spawn_cwd()
    assert os.path.isdir(cwd)
    assert "plugboard-spawn" in cwd
    # The subdir we own must NOT contain TemporaryItems
    assert "TemporaryItems" not in os.listdir(cwd)
    # Idempotent: second call returns the same path (cached)
    assert _clean_spawn_cwd() == cwd


def test_extract_gemini_json_prefers_candidates_over_flat_key():
    """TNR — bug fix 2026-05-04: long gemini-2.5-pro responses include both
    a flat "response" key (truncated/partial) AND the full candidates path.
    The extractor must prefer candidates to avoid silent truncation."""
    payload = {
        "response": "partial summary",
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": "full long response with "
                            "much more content than the flat key"
                        }
                    ]
                }
            }
        ],
    }
    result = _extract_text_from_gemini_json(payload)
    assert "full long response" in result
    assert result != "partial summary"


def test_extract_codex_jsonl_assistant_message():
    lines = [
        '{"type":"session_created","session_id":"abc"}',
        '{"type":"assistant_message","text":"Answer"}',
        '{"type":"turn_end"}',
    ]
    assert _extract_text_from_codex_jsonl(lines) == "Answer"


def test_extract_codex_jsonl_item_completed_agent_message():
    """Codex CLI >=0.121 shape: {type: item.completed, item: {type: agent_message, text: ...}}."""
    lines = [
        '{"type":"thread.started","thread_id":"abc"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"id":"i0","type":"agent_message","text":"Hi there friend"}}',
        '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":3}}',
    ]
    assert _extract_text_from_codex_jsonl(lines) == "Hi there friend"


def test_extract_codex_jsonl_deltas():
    lines = [
        '{"type":"response.output_text.delta","delta":"He"}',
        '{"type":"response.output_text.delta","delta":"llo"}',
    ]
    assert _extract_text_from_codex_jsonl(lines) == "Hello"


def test_extract_codex_jsonl_ignores_invalid_lines():
    lines = ["not json", "", '{"type":"assistant_message","text":"ok"}']
    assert _extract_text_from_codex_jsonl(lines) == "ok"


def test_extract_claude_jsonl_text_blocks():
    lines = [
        '{"type":"system","subtype":"init"}',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"Hi"}]}}',
        '{"type":"result","subtype":"success"}',
    ]
    assert _extract_text_from_claude_jsonl(lines) == "Hi"


def test_extract_claude_jsonl_content_block_deltas():
    lines = [
        '{"type":"content_block_delta","delta":{"type":"text_delta","text":"a"}}',
        '{"type":"content_block_delta","delta":{"type":"text_delta","text":"b"}}',
    ]
    assert _extract_text_from_claude_jsonl(lines) == "ab"


def test_extract_claude_jsonl_thinking_blocks():
    """Extended-thinking mode emits ``thinking`` blocks (BENCH-001)."""
    lines = [
        '{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"Deep thought"}]}}',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"Answer"}]}}',
    ]
    assert _extract_text_from_claude_jsonl(lines) == "Deep thoughtAnswer"


def test_extract_claude_jsonl_thinking_delta():
    """Thinking deltas must be collected just like text deltas."""
    lines = [
        '{"type":"content_block_delta","delta":{"type":"thinking_delta","text":"t1"}}',
        '{"type":"content_block_delta","delta":{"type":"text_delta","text":"t2"}}',
    ]
    assert _extract_text_from_claude_jsonl(lines) == "t1t2"


def test_extract_claude_jsonl_result_fallback():
    """Some CLI versions surface the final text in the ``result`` event."""
    lines = [
        '{"type":"result","subtype":"success","result":"Final answer"}',
    ]
    assert _extract_text_from_claude_jsonl(lines) == "Final answer"


# ── check_cli_installed (no subprocess — shutil.which mock) ─────────────────


def test_check_cli_installed_ok(monkeypatch):
    monkeypatch.setattr(
        cli_subprocess.shutil,
        "which",
        lambda cmd: "/opt/bin/" + cmd if cmd == "gemini" else None,
    )
    result = check_cli_installed("gemini")
    assert result["ok"] is True
    assert result["binary"] == "/opt/bin/gemini"
    assert result["kind"] == "gemini"


def test_check_cli_installed_not_found(monkeypatch):
    monkeypatch.setattr(cli_subprocess.shutil, "which", lambda cmd: None)
    result = check_cli_installed("gemini")
    assert result["ok"] is False
    assert "not found on PATH" in result["error"]


def test_check_cli_installed_unknown_kind():
    result = check_cli_installed("perplexity")
    assert result["ok"] is False
    assert "Unknown CLI kind" in result["error"]


# ── complete() — subprocess mocked ──────────────────────────────────────────


def _make_proc_mock(*, returncode=0, stdout=b"", stderr=b""):
    """Return a mock for asyncio subprocess Process."""
    proc = MagicMock()
    proc.returncode = returncode
    # communicate is awaited
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    return proc


async def test_complete_spawns_from_neutral_cwd(monkeypatch, tmp_path):
    """PERF: the CLI must not be spawned from the plugboard project
    directory, otherwise Claude Code loads the project's MCP servers,
    memory, CLAUDE.md, slash-commands, etc. — which inflates the prompt
    from ~30k to ~145k tokens and triples wall-clock latency.
    """
    captured_kwargs: dict = {}
    stdout = json.dumps({"response": "ok"}).encode()
    proc = _make_proc_mock(stdout=stdout)

    async def fake_exec(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    # Default case: cwd should fall back to the system temp dir, NOT the
    # current working directory (which might be the plugboard repo
    # when the server is launched from there).
    monkeypatch.delenv("PLUGBOARD_SUBPROCESS_CWD", raising=False)
    await cli_subprocess.complete(
        "gemini",
        {
            "model": "gemini-cli:gemini-2.5-flash",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    cwd = captured_kwargs.get("cwd", "")
    assert cwd, "cwd not passed to create_subprocess_exec"
    # Must not be the plugboard project directory.
    # _clean_spawn_cwd() returns a dedicated temp subdir; verify we are
    # isolated from the project root (which would contain pyproject.toml).
    assert not os.path.isfile(os.path.join(str(cwd), "pyproject.toml")), (
        f"CLI spawned in project dir: {cwd}"
    )
    assert str(cwd) == cli_subprocess._clean_spawn_cwd()

    # Explicit override: PLUGBOARD_SUBPROCESS_CWD wins.
    override_cwd = str(tmp_path)
    monkeypatch.setenv("PLUGBOARD_SUBPROCESS_CWD", override_cwd)
    captured_kwargs.clear()
    await cli_subprocess.complete(
        "gemini",
        {
            "model": "gemini-cli:gemini-2.5-flash",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert str(captured_kwargs.get("cwd")) == override_cwd


async def test_complete_gemini_sets_trust_workspace_env(monkeypatch):
    captured_kwargs: dict = {}
    stdout = json.dumps({"response": "ok"}).encode()
    proc = _make_proc_mock(stdout=stdout)

    async def fake_exec(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    body = {
        "model": "gemini-cli:gemini-2.5-flash",
        "messages": [{"role": "user", "content": "hi"}],
    }
    await cli_subprocess.complete("gemini", body)
    env = captured_kwargs.get("env", {})
    assert env.get("GEMINI_CLI_TRUST_WORKSPACE") == "true"


async def test_complete_gemini_parses_response_key(monkeypatch):
    stdout = json.dumps({"response": "Bonjour, comment puis-je aider ?"}).encode()
    proc = _make_proc_mock(stdout=stdout)

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    body = {
        "model": "gemini-cli:gemini-2.5-flash",
        "messages": [{"role": "user", "content": "hi"}],
    }
    result = await cli_subprocess.complete("gemini", body)
    assert result["object"] == "chat.completion"
    assert (
        result["choices"][0]["message"]["content"] == "Bonjour, comment puis-je aider ?"
    )
    assert result["model"] == "gemini-cli:gemini-2.5-flash"


async def test_complete_codex_parses_jsonl(monkeypatch):
    jsonl = (
        b'{"type":"session_created","session_id":"x"}\n'
        b'{"type":"assistant_message","text":"42"}\n'
    )
    proc = _make_proc_mock(stdout=jsonl)

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    body = {"model": "gpt-5.4", "messages": [{"role": "user", "content": "x"}]}
    result = await cli_subprocess.complete("codex", body)
    assert result["choices"][0]["message"]["content"] == "42"


async def test_complete_raises_when_cli_not_installed(monkeypatch):
    async def fake_exec(*args, **kwargs):
        raise FileNotFoundError(2, "No such file", args[0])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    body = {
        "model": "gemini-2.5-flash",
        "messages": [{"role": "user", "content": "hi"}],
    }
    with pytest.raises(RuntimeError, match="not installed"):
        await cli_subprocess.complete("gemini", body)


async def test_complete_raises_when_cli_fails(monkeypatch):
    proc = _make_proc_mock(returncode=1, stdout=b"", stderr=b"Auth expired")

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    body = {
        "model": "gemini-2.5-flash",
        "messages": [{"role": "user", "content": "hi"}],
    }
    with pytest.raises(RuntimeError, match="exited 1"):
        await cli_subprocess.complete("gemini", body)


async def test_complete_includes_dyld_hint_on_library_error(monkeypatch):
    """Homebrew upgrades can remove old .dylib versions; stderr surfaces a dyld
    error that is cryptic without a remediation hint."""
    stderr = (
        b"dyld: Library not loaded: /opt/homebrew/opt/simdutf/lib/libsimdutf.33.dylib"
    )
    proc = _make_proc_mock(returncode=1, stdout=b"", stderr=stderr)

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    body = {
        "model": "claude-sonnet-4-5",
        "messages": [{"role": "user", "content": "hi"}],
    }
    with pytest.raises(cli_subprocess.CliSubprocessError) as exc_info:
        await cli_subprocess.complete("claude", body)
    err = str(exc_info.value)
    assert "exited 1" in err
    assert "dyld library error" in err
    assert "brew reinstall claude" in err


async def test_probe_cli_version_includes_dyld_hint(monkeypatch):
    """probe_cli_version must surface a remediation hint when the CLI fails
    due to a missing native library (common after Homebrew upgrades)."""
    proc = _make_proc_mock(
        returncode=1,
        stdout=b"",
        stderr=b"dyld: Library not loaded: libsimdutf.33.dylib",
    )

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = await cli_subprocess.probe_cli_version("gemini")
    assert result["ok"] is False
    assert "dyld library error" in result["error"]
    assert "brew reinstall gemini" in result["error"]


async def test_complete_raises_on_invalid_json(monkeypatch):
    proc = _make_proc_mock(stdout=b"not-json-at-all")

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    body = {
        "model": "gemini-2.5-flash",
        "messages": [{"role": "user", "content": "hi"}],
    }
    with pytest.raises(RuntimeError, match="invalid JSON"):
        await cli_subprocess.complete("gemini", body)


async def test_complete_raises_on_empty_prompt(monkeypatch):
    body = {"model": "gemini-2.5-flash", "messages": []}
    with pytest.raises(RuntimeError, match="Empty prompt"):
        await cli_subprocess.complete("gemini", body)


async def test_complete_strips_model_prefix(monkeypatch):
    """Ensure `provider_id:real_model` prefix is stripped before passing to CLI."""
    captured_argv = []

    async def fake_exec(*argv, **kwargs):
        captured_argv.extend(argv)
        return _make_proc_mock(stdout=b'{"response":"ok"}')

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    body = {
        "model": "gemini-cli-subprocess:gemini-2.5-pro",
        "messages": [{"role": "user", "content": "hi"}],
    }
    await cli_subprocess.complete("gemini", body)
    # `--model gemini-2.5-pro` should be in the argv, not `gemini-cli-subprocess:gemini-2.5-pro`
    assert "gemini-2.5-pro" in captured_argv
    assert "gemini-cli-subprocess:gemini-2.5-pro" not in captured_argv


# ── Router integration ─────────────────────────────────────────────────────


async def test_router_dispatches_to_subprocess(monkeypatch, tmp_path):
    """auth_source=subprocess:gemini short-circuits HTTP forward."""
    import json as _json

    # Isolate providers.json via PLUGBOARD_DATA
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "providers.json").write_text(
        _json.dumps(
            {
                "providers": [
                    {
                        "id": "gem-sub",
                        "name": "Gemini Subprocess",
                        "base_url": "subprocess://gemini",
                        "bearer_token": "",
                        "default_model": "gemini-2.5-flash",
                        "is_active": True,
                        "auth_source": "subprocess:gemini",
                    }
                ]
            }
        )
    )
    monkeypatch.setenv("PLUGBOARD_DATA", str(data_dir))
    import plugboard.config as cfg

    cfg.PROVIDERS_FILE = data_dir / "providers.json"
    cfg.PROVIDERS_LOCK = data_dir / "providers.json.lock"
    from plugboard.router import forward, invalidate_models_cache

    invalidate_models_cache()

    # Mock the subprocess invocation
    called = {}

    async def fake_complete(kind, body, timeout_s=None):
        called["kind"] = kind
        called["model"] = body["model"]
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": f"{kind}-cli:x",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "OK"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    monkeypatch.setattr(cli_subprocess, "complete", fake_complete)
    body = {
        "model": "gem-sub:gemini-2.5-flash",
        "messages": [{"role": "user", "content": "hi"}],
    }
    result = await forward(body)
    assert called["kind"] == "gemini"
    assert called["model"] == "gemini-2.5-flash"  # provider prefix stripped by router
    assert result["choices"][0]["message"]["content"] == "OK"


# ── Admin endpoint ─────────────────────────────────────────────────────────


async def test_check_direct_preset_subprocess_not_installed(admin_client, monkeypatch):
    """GET /admin/direct-providers/gemini-cli-subprocess/check reports CLI absence."""
    monkeypatch.setattr(cli_subprocess.shutil, "which", lambda cmd: None)
    res = await admin_client.get(
        "/admin/direct-providers/gemini-cli-subprocess/check",
        headers={"Authorization": "Bearer adminkey"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is False
    assert data["auth_type"] == "subprocess"
    assert data["kind"] == "gemini"


async def test_check_direct_preset_subprocess_installed(admin_client, monkeypatch):
    """GET /admin/direct-providers/gemini-cli-subprocess/check reports CLI when found."""
    monkeypatch.setattr(
        cli_subprocess.shutil,
        "which",
        lambda cmd: "/usr/local/bin/" + cmd if cmd == "gemini" else None,
    )

    async def fake_probe(kind, timeout_s=5.0):
        return {"ok": True, "version": "0.34.0", "error": None}

    monkeypatch.setattr(cli_subprocess, "probe_cli_version", fake_probe)
    res = await admin_client.get(
        "/admin/direct-providers/gemini-cli-subprocess/check",
        headers={"Authorization": "Bearer adminkey"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["binary"] == "/usr/local/bin/gemini"
    assert data["version"] == "0.34.0"


# ── SEC-3: _resolve_binary path cache (anti PATH hijack) ─────────────────────


def test_resolve_binary_caches_path(monkeypatch, tmp_path):
    """_resolve_binary calls shutil.which ONCE then hits the cache."""
    # Clear global cache to avoid pollution from other tests.
    cli_subprocess._binary_path_cache.clear()

    fake_bin = tmp_path / "fake-gemini"
    fake_bin.write_text("#!/bin/sh\nexit 0\n")
    fake_bin.chmod(0o755)

    call_count = {"n": 0}

    def fake_which(cmd):
        call_count["n"] += 1
        return str(fake_bin) if cmd == "fake-gemini" else None

    monkeypatch.setattr(cli_subprocess.shutil, "which", fake_which)

    first = cli_subprocess._resolve_binary("fake-gemini")
    second = cli_subprocess._resolve_binary("fake-gemini")
    third = cli_subprocess._resolve_binary("fake-gemini")

    assert first == str(fake_bin)
    assert second == str(fake_bin)
    assert third == str(fake_bin)
    # which() was called exactly once — subsequent calls are cached.
    assert call_count["n"] == 1


def test_resolve_binary_returns_none_when_missing(monkeypatch):
    cli_subprocess._binary_path_cache.clear()
    monkeypatch.setattr(cli_subprocess.shutil, "which", lambda cmd: None)
    assert cli_subprocess._resolve_binary("never-installed-cli") is None


def test_resolve_binary_drops_cache_when_path_vanishes(monkeypatch, tmp_path):
    """If the cached binary was removed, the cache is refreshed via which()."""
    cli_subprocess._binary_path_cache.clear()

    fake_bin = tmp_path / "vanishing"
    fake_bin.write_text("")
    fake_bin.chmod(0o755)

    def which_first(cmd):
        return str(fake_bin) if cmd == "vanishing" else None

    monkeypatch.setattr(cli_subprocess.shutil, "which", which_first)
    assert cli_subprocess._resolve_binary("vanishing") == str(fake_bin)

    # Remove the binary — the cached entry is now stale.
    fake_bin.unlink()
    new_bin = tmp_path / "new-location"
    new_bin.write_text("")
    new_bin.chmod(0o755)
    monkeypatch.setattr(
        cli_subprocess.shutil,
        "which",
        lambda cmd: str(new_bin) if cmd == "vanishing" else None,
    )

    resolved = cli_subprocess._resolve_binary("vanishing")
    assert resolved == str(new_bin)


# ── SEC-2: per-kind semaphore ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_semaphore_is_per_kind():
    """Each backend kind has its own semaphore (no cross-kind contention)."""
    sem_gemini = cli_subprocess._get_semaphore("gemini")
    sem_codex = cli_subprocess._get_semaphore("codex")
    assert sem_gemini is not sem_codex
    # Same kind must return the same instance.
    assert cli_subprocess._get_semaphore("gemini") is sem_gemini


@pytest.mark.asyncio
async def test_semaphore_limits_concurrent_subprocesses(monkeypatch):
    """Semaphore limits concurrent spawns to PLUGBOARD_SUBPROCESS_LIMIT."""
    # Force a tiny semaphore for this test by rebuilding the per-kind one.
    kind = "gemini"
    cli_subprocess._semaphores[kind] = asyncio.Semaphore(2)

    in_flight = {"count": 0, "peak": 0}
    started = asyncio.Event()

    async def acquire_and_hold():
        async with cli_subprocess._get_semaphore(kind):
            in_flight["count"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["count"])
            started.set()
            await asyncio.sleep(0.05)
            in_flight["count"] -= 1

    # Launch 6 concurrent holders — peak concurrency must be ≤ 2.
    await asyncio.gather(*[acquire_and_hold() for _ in range(6)])
    assert in_flight["peak"] <= 2
    assert started.is_set()

    # Restore: drop the shrunk semaphore so other tests get the real limit.
    cli_subprocess._semaphores.pop(kind, None)
