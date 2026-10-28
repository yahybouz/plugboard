"""OVH deployment: model/pricing jobs enabled, no probes, agy account catalog sync."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from plugboard import config, model_diff
from plugboard import model_availability as ma
from tests.conftest import _setup_providers

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "ovh" / "compose.yml"
OVH_README = ROOT / "deploy" / "ovh" / "README.md"

AGY_PROVIDER_ID = "antigravity-via-agy-cli"
DEFAULT_MODEL = "gemini-3.8-flash-medium"
SAVED_IDS = [
    DEFAULT_MODEL,
    "gemini-3.8-flash-high",
    "gemini-3.8-flash-low",
    "gemini-3.8-pro-medium",
    "gemini-3.8-pro-high",
    "gemini-3.8-pro-low",
    "claude-sonnet-5-5",
    "claude-opus-5-5",
    "gpt-oss-120b-medium",
    "gemini-3.8-flash-lite",
]
ARGON_IDS = [
    "gemini-4-argon-medium",
    "gemini-4-argon-high",
    "gemini-4-argon-low",
]


def _gateway():
    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return doc["services"]["gateway"]


def _entries(ids):
    return [{"id": mid, "name": mid} for mid in ids]


# -- compose ---------------------------------------------------------------------


def test_compose_enables_model_and_pricing_schedulers():
    env = _gateway()["environment"]
    assert env["MODEL_AVAILABILITY_SCHEDULER_DISABLED"] == "0"
    assert env["PRICING_SCHEDULER_DISABLED"] == "0"
    assert ma._parse_bool(env["MODEL_AVAILABILITY_SCHEDULER_DISABLED"]) is False
    assert ma._parse_bool(env["PRICING_SCHEDULER_DISABLED"]) is False


def test_compose_disables_inference_probes(monkeypatch):
    env = _gateway()["environment"]
    assert env["PLUGBOARD_MODEL_REFRESH_PROBE_KINDS"] == ""
    monkeypatch.setenv(
        "PLUGBOARD_MODEL_REFRESH_PROBE_KINDS",
        env["PLUGBOARD_MODEL_REFRESH_PROBE_KINDS"],
    )
    assert ma._probe_kinds_from_env() == set()


def test_compose_keeps_default_cadence():
    env = _gateway()["environment"]
    for name in (
        "PLUGBOARD_PRICING_REFRESH_INTERVAL_H",
        "PRICING_REFRESH_INTERVAL_H",
        "PLUGBOARD_MODEL_AVAILABILITY_INTERVAL_H",
        "MODEL_AVAILABILITY_INTERVAL_H",
        "PLUGBOARD_MODEL_AVAILABILITY_CLI_INTERVAL_H",
    ):
        assert name not in env


def test_compose_protected_restrictions_unchanged():
    gateway = _gateway()
    env = gateway["environment"]
    assert gateway["image"] == "plugboard:private-full-v4.3.3"
    assert gateway["build"]["target"] == "full"
    assert gateway["build"]["args"] == {
        "GEMINI_CLI_VERSION": "0.65.0",
        "CLAUDE_CODE_VERSION": "2.1.306",
        "CODEX_VERSION": "0.163.0",
    }
    assert env["PLUGBOARD_LOCAL_AUTO_LOGIN"] == "0"
    assert env["COOKIE_SECURE"] == "true"
    assert env["CORS_ORIGINS"] == ""
    assert env["PLUGBOARD_CODEX_SANDBOX"] == "read-only"
    assert env["PLUGBOARD_MAX_INFLIGHT_CHAT"] == "1"
    assert env["PLUGBOARD_SUBPROCESS_LIMIT"] == "1"
    assert env["PLUGBOARD_INTERNET_CHECK_ON_BOOT"] == "0"
    assert env["PLUGBOARD_LLM_PRICING"] == "0"
    assert env["PLUGBOARD_OLLAMA_CLOUD_AUTOSYNC"] == "0"
    assert not any("GEMINI_API_KEY" in key or "GOOGLE_API_KEY" in key for key in env)
    assert gateway["ports"] == ["127.0.0.1:8765:8765"]
    assert gateway["networks"] == ["default", "ollama-private"]
    assert gateway["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in gateway["security_opt"]
    assert "apparmor=plugboard-codex" in gateway["security_opt"]
    assert any(opt.startswith("seccomp=") for opt in gateway["security_opt"])
    assert (gateway["pids_limit"], gateway["mem_limit"], gateway["cpus"]) == (
        256,
        "3g",
        1.75,
    )


def test_readme_documents_jobs_without_free_claim():
    text = OVH_README.read_text(encoding="utf-8")
    assert "## Model and pricing jobs" in text
    section = text.split("## Model and pricing jobs", 1)[1].split("\n## ", 1)[0]
    assert "`PLUGBOARD_MODEL_REFRESH_PROBE_KINDS` is empty" in section
    assert "agy models" in section
    assert "24 hours" in section and "6 hours" in section
    assert "does not consume the subscription's quota" in section
    assert "Individual quota reached" in section
    assert "never clears a quarantine" in section
    assert "no Gemini API key" in section
    assert "plugboard:private-full-v4.3.3" in section
    assert "not the subscription's invoice" in section


# -- agy catalog refresh ----------------------------------------------------------


@pytest.fixture
def agy_provider(tmp_path, monkeypatch):
    provider = {
        "id": AGY_PROVIDER_ID,
        "name": "Antigravity (agy CLI)",
        "is_active": True,
        "auth_source": "subprocess:agy",
        "provider_type": "cloud",
        "default_model": DEFAULT_MODEL,
        "models": _entries(SAVED_IDS),
    }
    _setup_providers(tmp_path, monkeypatch, [provider])
    return provider


@pytest.mark.asyncio
async def test_agy_refresh_persists_new_argon_models_without_inference(
    agy_provider, monkeypatch, tmp_path
):
    from plugboard import cli_subprocess

    listed = []

    async def fake_list_agy_models(timeout_s: float = 30.0):
        listed.append(timeout_s)
        return SAVED_IDS + ARGON_IDS

    async def no_probe(*_args, **_kwargs):
        raise AssertionError("listing must not spend quota on a probe")

    async def no_complete(*_args, **_kwargs):
        raise AssertionError("listing must not spend quota on a completion")

    monkeypatch.setattr(cli_subprocess, "list_agy_models", fake_list_agy_models)
    monkeypatch.setattr(cli_subprocess, "complete", no_complete)
    monkeypatch.setattr(ma, "probe_subprocess_model", no_probe)
    env = _gateway()["environment"]
    monkeypatch.setenv(
        "PLUGBOARD_MODEL_REFRESH_PROBE_KINDS",
        env["PLUGBOARD_MODEL_REFRESH_PROBE_KINDS"],
    )

    report = await ma.refresh_subprocess_provider_models(
        config.get_provider(AGY_PROVIDER_ID),
        quarantine_path=tmp_path / "quarantine.json",
    )

    assert listed, "`agy models` is the discovery source"
    assert report["trusted_account_catalog"] is True
    assert report["verified_count"] == 0
    assert not (tmp_path / "quarantine.json").exists()
    assert [m["id"] for m in report["models"]] == SAVED_IDS + ARGON_IDS
    assert report["default_model"] == DEFAULT_MODEL
    assert report["dropped"] == []

    diff = model_diff.apply(
        AGY_PROVIDER_ID,
        report["models"],
        "scheduler",
        default_model=report["default_model"],
    )

    assert diff.error == ""
    assert diff.applied is True
    assert diff.added == ARGON_IDS
    assert diff.removed == []
    stored = config.get_provider(AGY_PROVIDER_ID)
    stored_ids = [m["id"] for m in stored["models"]]
    assert set(stored_ids) == set(SAVED_IDS + ARGON_IDS)
    assert len(stored_ids) == 13
    assert stored["default_model"] == DEFAULT_MODEL


@pytest.mark.asyncio
async def test_empty_probe_kinds_leave_other_cli_lists_unprobed(tmp_path, monkeypatch):
    claude = {
        "id": "claude-via-claude-cli",
        "is_active": True,
        "auth_source": "subprocess:claude",
        "default_model": "claude-sonnet-5-5",
        "models": _entries(["claude-sonnet-5-5", "claude-opus-5-5"]),
    }
    _setup_providers(tmp_path, monkeypatch, [claude])

    async def no_discover(_provider):
        raise AssertionError("claude must not be expanded from discovery")

    async def no_probe(*_args, **_kwargs):
        raise AssertionError("claude quota must not be used")

    monkeypatch.setattr("plugboard.router.discover_models", no_discover)
    monkeypatch.setattr(ma, "probe_subprocess_model", no_probe)
    monkeypatch.setenv(
        "PLUGBOARD_MODEL_REFRESH_PROBE_KINDS",
        _gateway()["environment"]["PLUGBOARD_MODEL_REFRESH_PROBE_KINDS"],
    )

    report = await ma.refresh_subprocess_provider_models(
        config.get_provider("claude-via-claude-cli"),
        quarantine_path=tmp_path / "quarantine.json",
    )

    assert [m["id"] for m in report["models"]] == [
        "claude-sonnet-5-5",
        "claude-opus-5-5",
    ]
    assert report["verified_count"] == 0
