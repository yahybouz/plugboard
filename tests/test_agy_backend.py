"""Antigravity (`agy`) CLI backend: argv, output parsing, model discovery."""

import asyncio

import pytest

from plugboard import cli_subprocess
from plugboard.direct_providers import PRESETS, list_presets


def test_agy_backend_never_skips_permissions():
    backend = cli_subprocess.get_backend("agy")
    assert backend.command == "agy"
    assert "--dangerously-skip-permissions" not in backend.args
    assert "--sandbox" in backend.args
    assert backend.args[backend.args.index("--mode") + 1] == "plan"
    assert backend.resolve_prompt_args("hello")[1] == "hello"


def test_agy_child_env_drops_server_secrets(monkeypatch):
    monkeypatch.setenv("ADMIN_KEY", "secret")
    monkeypatch.setenv("GEMINI_API_KEY", "not-for-agy")
    env = cli_subprocess.build_child_env("agy")
    assert "ADMIN_KEY" not in env
    assert "GEMINI_API_KEY" not in env


def test_parse_agy_models_keeps_ids_only():
    out = (
        "Fetching available models...\n"
        "gemini-4-argon-medium\tGemini 4 Argon (Medium)\n"
        "claude-opus-5-5-high\tClaude Opus 5.5 (High)\n"
        "\n"
        "gemini-4-argon-medium\tduplicate\n"
    )
    assert cli_subprocess.parse_agy_models(out) == [
        "gemini-4-argon-medium",
        "claude-opus-5-5-high",
    ]


def test_agy_json_success_is_parsed():
    backend = cli_subprocess.get_backend("agy")
    stdout = '{"status":"SUCCESS","response":"OK\\n","usage":{}}'
    assert cli_subprocess._try_parse_stdout_text(stdout, backend, "agy") == "OK\n"


def test_agy_json_failure_status_is_rejected():
    backend = cli_subprocess.get_backend("agy")
    stdout = '{"status":"ERROR","response":"partial"}'
    assert cli_subprocess._try_parse_stdout_text(stdout, backend, "agy") is None


def test_agy_denied_tool_gives_no_text():
    backend = cli_subprocess.get_backend("agy")
    stdout = (
        '{"status":"SUCCESS","response":"","denied_actions":[{"action":"read_file"}]}'
    )
    assert cli_subprocess._try_parse_stdout_text(stdout, backend, "agy") is None


def test_list_agy_models_without_binary(monkeypatch):
    monkeypatch.setattr(cli_subprocess, "_binary_path_cache", {})
    monkeypatch.setattr(cli_subprocess.shutil, "which", lambda cmd: None)
    assert asyncio.run(cli_subprocess.list_agy_models()) == []


@pytest.mark.parametrize("ids,trusted", [(["gemini-4-argon-low"], True), ([], False)])
def test_discover_models_uses_agy_list(monkeypatch, ids, trusted):
    from plugboard import router

    async def fake_list(timeout_s=30.0):
        return ids

    monkeypatch.setattr(cli_subprocess, "list_agy_models", fake_list)
    provider = {"id": "antigravity-via-agy-cli", "auth_source": "subprocess:agy"}

    async def run():
        found = await router.discover_models(provider)
        return found, router.last_discovery_trusted()

    models, was_trusted = asyncio.run(run())
    got = [m["id"] for m in models]
    if ids:
        assert got == ids
    else:
        # Falls back to the preset baseline.
        assert got[: len(PRESETS["agy-cli-subprocess"]["known_models"])] == list(
            PRESETS["agy-cli-subprocess"]["known_models"]
        )
    assert was_trusted is trusted


def test_gemini_cli_preset_hidden_but_resolvable():
    keys = {p["key"] for p in list_presets()}
    assert "agy-cli-subprocess" in keys
    assert "gemini-cli-subprocess" not in keys
    assert PRESETS["gemini-cli-subprocess"]["auth_source"] == "subprocess:gemini"


def test_claude_cli_preset_lists_claude_5():
    preset = PRESETS["claude-cli-subprocess"]
    assert preset["default_model"] == "claude-sonnet-5-5"
    for mid in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"):
        assert mid in preset["known_models"]
