"""agy account catalog: a trusted `agy models` list is published without inference.

The signed-in account's `agy models` answer is the discovery authority for the
Antigravity provider. Publishing it must not spend subscription quota (no
completion, no probe) and must not touch the quarantine state; fallback lists,
failed discoveries and other CLI providers keep their existing rules.
"""

from __future__ import annotations

import json
import uuid

import pytest

from plugboard import cli_subprocess, config, model_diff
from plugboard import model_availability as ma
from plugboard import router as router_mod
from plugboard.model_quarantine import QUARANTINE_THRESHOLD
from tests.conftest import _setup_providers

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
QUOTA_ERROR = (
    "CLI 'agy' exited 3: Individual quota reached. Please upgrade your "
    "subscription to increase your limits. Resets in 122h59m46s."
)


def _entries(ids):
    return [{"id": mid, "name": mid} for mid in ids]


def _ids(report_or_provider):
    return [m["id"] for m in report_or_provider["models"]]


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


@pytest.fixture
def no_inference(monkeypatch):
    """Any completion or probe fails the test, as the real quota would."""
    calls = []

    async def quota_complete(*args, **_kwargs):
        calls.append(("complete", args))
        raise AssertionError(f"inference must not run: {QUOTA_ERROR}")

    async def quota_probe(*args, **_kwargs):
        calls.append(("probe", args))
        raise AssertionError("availability probe must not run for a trusted list")

    monkeypatch.setattr(cli_subprocess, "complete", quota_complete)
    monkeypatch.setattr(ma, "probe_subprocess_model", quota_probe)
    return calls


@pytest.fixture
def pricing_offers_argon(monkeypatch):
    """The preset/pricing fallback would list Argon: it must not be trusted."""
    monkeypatch.setattr(
        "plugboard.model_catalog.discover_subprocess_models_from_pricing",
        lambda _kind: list(ARGON_IDS),
    )


def _agy_lists(monkeypatch, ids):
    calls = []

    async def fake_list_agy_models(timeout_s: float = 30.0):
        calls.append(timeout_s)
        return list(ids)

    monkeypatch.setattr(cli_subprocess, "list_agy_models", fake_list_agy_models)
    return calls


async def _refresh(tmp_path, probe_kinds):
    return await ma.refresh_subprocess_provider_models(
        config.get_provider(AGY_PROVIDER_ID),
        probe_kinds=probe_kinds,
        quarantine_path=tmp_path / "quarantine.json",
    )


# -- trusted account list ---------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("probe_kinds", [set(), {"agy"}, {"all"}])
async def test_trusted_agy_list_publishes_argon_without_inference(
    agy_provider, no_inference, tmp_path, monkeypatch, probe_kinds
):
    listed = _agy_lists(monkeypatch, SAVED_IDS + ARGON_IDS)

    report = await _refresh(tmp_path, probe_kinds)

    assert listed, "`agy models` is read even when probes are disabled"
    assert no_inference == []
    assert _ids(report) == SAVED_IDS + ARGON_IDS
    assert report["trusted_account_catalog"] is True
    assert report["verified_count"] == 0
    assert report["default_model"] == DEFAULT_MODEL
    assert report["dropped"] == []
    # Catalogue presence is not a successful completion: no quarantine write.
    assert not (tmp_path / "quarantine.json").exists()


@pytest.mark.asyncio
async def test_trusted_agy_list_persists_through_model_diff(
    agy_provider, no_inference, tmp_path, monkeypatch
):
    _agy_lists(monkeypatch, SAVED_IDS + ARGON_IDS)

    report = await _refresh(tmp_path, set())
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
    assert set(_ids(stored)) == set(SAVED_IDS + ARGON_IDS)
    assert stored["default_model"] == DEFAULT_MODEL

    # A later trusted pass without one old model only marks it pending:
    # model_diff keeps owning the removal protection.
    _agy_lists(monkeypatch, [m for m in SAVED_IDS if m != "gemini-3.8-flash-lite"])
    report = await _refresh(tmp_path, set())
    diff = model_diff.apply(
        AGY_PROVIDER_ID,
        report["models"],
        "scheduler",
        default_model=report["default_model"],
    )
    assert diff.removed == []
    assert "gemini-3.8-flash-lite" in _ids(config.get_provider(AGY_PROVIDER_ID))
    assert no_inference == []


@pytest.mark.asyncio
async def test_trusted_agy_list_keeps_arbitrary_future_ids(
    agy_provider, no_inference, tmp_path, monkeypatch
):
    future = [f"gemini-{uuid.uuid4().hex[:8]}-preview" for _ in range(2)]
    _agy_lists(monkeypatch, SAVED_IDS + future)

    report = await _refresh(tmp_path, set())

    assert _ids(report) == SAVED_IDS + future
    assert no_inference == []


# -- untrusted, empty or failed discovery -----------------------------------------


@pytest.mark.asyncio
async def test_fallback_list_does_not_add_argon_without_probes(
    agy_provider, no_inference, pricing_offers_argon, tmp_path, monkeypatch
):
    _agy_lists(monkeypatch, [])  # signed out / CLI error -> preset fallback

    report = await _refresh(tmp_path, set())

    assert _ids(report) == SAVED_IDS
    assert not set(ARGON_IDS) & set(_ids(report))
    assert report["trusted_account_catalog"] is False
    assert report["default_model"] == DEFAULT_MODEL
    assert no_inference == []


@pytest.mark.asyncio
async def test_trust_does_not_leak_from_a_previous_discovery(
    agy_provider, no_inference, pricing_offers_argon, tmp_path, monkeypatch
):
    _agy_lists(monkeypatch, SAVED_IDS)
    first = await _refresh(tmp_path, set())
    assert first["trusted_account_catalog"] is True

    _agy_lists(monkeypatch, [])
    second = await _refresh(tmp_path, set())

    assert second["trusted_account_catalog"] is False
    assert _ids(second) == SAVED_IDS


@pytest.mark.asyncio
async def test_external_trust_flag_is_ignored(
    agy_provider, no_inference, tmp_path, monkeypatch
):
    async def flagged_discover(_provider):
        router_mod._discovery_trusted.set(False)
        return [{"id": m, "name": m, "trusted": True} for m in ARGON_IDS]

    monkeypatch.setattr("plugboard.router.discover_models", flagged_discover)

    report = await _refresh(tmp_path, set())

    assert _ids(report) == SAVED_IDS
    assert report["trusted_account_catalog"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["empty", "error"])
async def test_failed_discovery_preserves_previous_and_default(
    agy_provider, no_inference, tmp_path, monkeypatch, outcome
):
    async def failing_discover(_provider):
        if outcome == "error":
            raise RuntimeError("agy unavailable")
        router_mod._discovery_trusted.set(True)
        return []

    monkeypatch.setattr("plugboard.router.discover_models", failing_discover)

    report = await _refresh(tmp_path, set())
    diff = model_diff.apply(
        AGY_PROVIDER_ID,
        report["models"],
        "scheduler",
        default_model=report["default_model"],
    )

    assert _ids(report) == SAVED_IDS
    assert report["default_model"] == DEFAULT_MODEL
    assert report["trusted_account_catalog"] is False
    assert diff.removed == [] and diff.added == []
    stored = config.get_provider(AGY_PROVIDER_ID)
    assert _ids(stored) == SAVED_IDS
    assert stored["default_model"] == DEFAULT_MODEL


# -- quarantine -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quarantine_stays_hidden_and_untouched(
    agy_provider, no_inference, tmp_path, monkeypatch
):
    qpath = tmp_path / "quarantine.json"
    qpath.write_text(
        json.dumps(
            {
                "models": {
                    "gemini-4-argon-high": {
                        "consecutive_fails": QUARANTINE_THRESHOLD,
                        "terminal": True,
                        "terminal_reason": "account_unsupported",
                    },
                    "gemini-3.8-pro-low": {
                        "consecutive_fails": QUARANTINE_THRESHOLD,
                        "terminal": True,
                        "terminal_reason": "model_unavailable",
                    },
                    "gemini-4-argon-low": {"consecutive_fails": 1},
                }
            }
        )
    )
    before = qpath.read_bytes()
    _agy_lists(monkeypatch, SAVED_IDS + ARGON_IDS)

    report = await _refresh(tmp_path, set())

    ids = _ids(report)
    assert "gemini-4-argon-high" not in ids
    assert "gemini-3.8-pro-low" not in ids
    assert "gemini-4-argon-medium" in ids and "gemini-4-argon-low" in ids
    assert {d["id"] for d in report["dropped"]} == {
        "gemini-4-argon-high",
        "gemini-3.8-pro-low",
    }
    assert qpath.read_bytes() == before, "a catalogue never clears quarantine"
    assert no_inference == []


# -- other CLI providers keep their probe rules -----------------------------------


@pytest.fixture
def claude_provider(tmp_path, monkeypatch):
    provider = {
        "id": "claude-via-claude-cli",
        "is_active": True,
        "auth_source": "subprocess:claude",
        "default_model": "claude-sonnet-5-5",
        "models": _entries(["claude-sonnet-5-5", "claude-opus-5-5"]),
    }
    _setup_providers(tmp_path, monkeypatch, [provider])
    return provider


@pytest.mark.asyncio
async def test_non_agy_empty_probe_kinds_keeps_previous(
    claude_provider, no_inference, tmp_path, monkeypatch
):
    async def no_discover(_provider):
        raise AssertionError("claude must not be expanded without probes")

    monkeypatch.setattr("plugboard.router.discover_models", no_discover)

    report = await ma.refresh_subprocess_provider_models(
        config.get_provider("claude-via-claude-cli"),
        probe_kinds=set(),
        quarantine_path=tmp_path / "quarantine.json",
    )

    assert _ids(report) == ["claude-sonnet-5-5", "claude-opus-5-5"]
    assert report["verified_count"] == 0
    assert report["trusted_account_catalog"] is False


@pytest.mark.asyncio
async def test_non_agy_new_candidates_still_need_a_probe(
    claude_provider, tmp_path, monkeypatch
):
    async def fake_discover(_provider):
        router_mod._discovery_trusted.set(False)
        return _entries(["claude-sonnet-5-5", "claude-opus-5-5", "claude-new-6"])

    probed = []

    async def quota_probe(kind, model_id, *, timeout_s):
        probed.append((kind, model_id))
        if model_id == "claude-new-6":
            return {"ok": False, "terminal": False, "reason": QUOTA_ERROR}
        return {"ok": True, "reason": ""}

    monkeypatch.setattr("plugboard.router.discover_models", fake_discover)
    monkeypatch.setattr(ma, "probe_subprocess_model", quota_probe)

    report = await ma.refresh_subprocess_provider_models(
        config.get_provider("claude-via-claude-cli"),
        probe_kinds={"claude"},
        quarantine_path=tmp_path / "quarantine.json",
    )

    assert ("claude", "claude-new-6") in probed
    assert _ids(report) == ["claude-sonnet-5-5", "claude-opus-5-5"]
    assert report["verified_count"] == 3
    assert report["trusted_account_catalog"] is False
    assert {d["id"] for d in report["dropped"]} == {"claude-new-6"}
