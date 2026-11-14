"""BUG-102: one provider's refusal never removes a model from another provider.

Replays the production switch to 4.14.0 (14/11/2026) with its data. On 01/11 a
codex refusal ("not supported when using Codex with a ChatGPT account")
quarantined three model names; the store kept the name only, not the provider.
A catalogue pass of 02/11 counted one absence for them on the claude provider,
and the first pass of 4.14.0 was the second: claude lost three models it
serves. Ten names that agy serves sat in the same store.
"""

from __future__ import annotations

import json

import pytest

import plugboard.model_availability as ma
from plugboard import cli_subprocess
from plugboard import model_quarantine as mq
from tests.conftest import _setup_providers

CLAUDE = "claude-via-claude-cli"
CODEX = "chatgpt-via-codex-cli"
AGY = "antigravity-via-agy-cli"
LOST = ["claude-sonnet-5-5", "claude-haiku-5-5", "claude-haiku-4-5"]
CLAUDE_MODELS = [
    "claude-opus-5-5",
    "claude-opus-4-5-20251101",
    "claude-sonnet-4-6",
    "claude-fable-5-1",
    "claude-fable-5-2",
    *LOST,
]
AGY_QUARANTINED = [
    "claude-opus-5-5-high",
    "claude-opus-5-5-medium",
    "claude-sonnet-5-5-high",
    "claude-sonnet-5-5-low",
    "claude-sonnet-5-5-medium",
    "gemini-3.8-flash-high",
    "gemini-4-argon-high",
    "gemini-4-argon-low",
    "gemini-4-argon-medium",
    "gpt-oss-120b-medium",
]
AGY_MODELS = [
    *AGY_QUARANTINED,
    "gemini-3.8-flash-low",
    "gemini-3.8-flash-medium",
    "gemini-3-flash",
]
CODEX_MODELS = ["gpt-5.6-luna", "gpt-6-luna", "gpt-5.6-mini"]
_CODEX_REFUSAL = (
    "CLI 'codex' exited 1: The 'claude-sonnet-5-5' model is not supported when "
    "using Codex with a ChatGPT account"
)


def _cli(provider_id: str, kind: str, models: list) -> dict:
    return {
        "id": provider_id,
        "name": provider_id,
        "base_url": "",
        "bearer_token": "",
        "auth_source": f"subprocess:{kind}",
        "default_model": models[0],
        "models": [{"id": m} for m in models],
        "is_active": True,
        "created_at": "2026-10-20T00:00:00+00:00",
    }


def _saved(pfile, provider_id: str) -> list:
    providers = json.loads(pfile.read_text(encoding="utf-8"))["providers"]
    record = next(p for p in providers if p["id"] == provider_id)
    return [m["id"] for m in record["models"]]


@pytest.fixture
def prod(tmp_path, monkeypatch):
    """The data directory of production just before the switch."""
    monkeypatch.setattr("plugboard.usage.get_all_used_models", lambda: set())
    monkeypatch.delenv("PLUGBOARD_MODEL_REFRESH_PROBE_KINDS", raising=False)
    monkeypatch.delenv("PLUGBOARD_MODEL_REMOVAL_PASSES", raising=False)

    async def no_webhook(event, data):
        return 0

    monkeypatch.setattr("plugboard.webhooks.emit", no_webhook)
    pfile = _setup_providers(
        tmp_path,
        monkeypatch,
        [
            _cli(CLAUDE, "claude", CLAUDE_MODELS),
            _cli(CODEX, "codex", CODEX_MODELS),
            _cli(AGY, "agy", AGY_MODELS),
        ],
    )
    catalog = pfile.parent / "catalog"
    catalog.mkdir()
    (catalog / "model_absences.json").write_text(
        json.dumps({CLAUDE: {m: 1 for m in LOST}}), encoding="utf-8"
    )
    (catalog / "refresh_state.json").write_text(
        json.dumps({p: "2026-11-02T02:29:07+00:00" for p in (CLAUDE, CODEX, AGY)}),
        encoding="utf-8",
    )
    refused = {
        "consecutive_fails": 3,
        "last_failure_at": "2026-11-01T18:58:00+00:00",
        "last_failure_reason": _CODEX_REFUSAL,
        "terminal": True,
        "terminal_reason": "account_unsupported",
    }
    failed = {"consecutive_fails": 3, "last_failure_at": "2026-11-01T19:10:00+00:00"}
    qfile = pfile.parent / "quarantined_models.json"
    qfile.write_text(
        json.dumps(
            {
                "models": {
                    **{m: dict(refused) for m in LOST},
                    **{m: dict(failed) for m in AGY_QUARANTINED},
                }
            }
        ),
        encoding="utf-8",
    )

    async def agy_account(timeout_s: float = 30.0):
        return list(AGY_MODELS)

    monkeypatch.setattr(cli_subprocess, "list_agy_models", agy_account)
    return pfile, qfile


async def test_the_first_pass_after_the_switch_removes_no_model_claude_serves(prod):
    pfile, qfile = prod

    report = await ma.refresh_all_model_catalogs(
        pass_kind="bootstrap", quarantine_path=qfile
    )

    assert set(LOST) <= set(_saved(pfile, CLAUDE))
    assert _saved(pfile, CODEX) == CODEX_MODELS
    assert "removed" not in json.dumps(report.get("changes", {}).get(CLAUDE, {}))
    absences = json.loads(
        (pfile.parent / "catalog" / "model_absences.json").read_text(encoding="utf-8")
    )
    assert not absences.get(CLAUDE)


async def test_the_models_agy_serves_are_no_longer_held_by_the_anomaly_guard(prod):
    """In production ten of agy's thirteen models were quarantined too. They
    stayed only because a pass that drops more than half of a list is refused
    as an anomaly: the guard hid the same defect, it did not prevent it."""
    pfile, qfile = prod

    report = await ma.refresh_all_model_catalogs(
        pass_kind="bootstrap", quarantine_path=qfile
    )

    assert set(AGY_QUARANTINED) <= set(_saved(pfile, AGY))
    assert "not applied" not in json.dumps(report)


def test_names_quarantined_without_a_provider_apply_to_no_provider(prod):
    """The entries written before the provider was recorded cannot say whose
    refusal they are: they are kept in the file and applied to nobody."""
    _, qfile = prod

    for provider_id in (CLAUDE, CODEX, AGY):
        assert mq.get_quarantined_suffixes(qfile, provider_id=provider_id) == set()
    assert set(LOST) <= set(mq.get_state(qfile)["models"])


def test_a_refusal_is_recorded_for_its_own_provider_only(tmp_path):
    qfile = tmp_path / "quarantine.json"

    mq.record_account_unsupported(
        "claude-sonnet-5-5", reason=_CODEX_REFUSAL, provider_id=CODEX, path=qfile
    )

    assert mq.get_quarantined_suffixes(qfile, provider_id=CODEX) == {
        "claude-sonnet-5-5"
    }
    assert mq.get_quarantined_suffixes(qfile, provider_id=CLAUDE) == set()


def test_failures_are_counted_per_provider(tmp_path):
    qfile = tmp_path / "quarantine.json"
    for _ in range(mq.QUARANTINE_THRESHOLD - 1):
        mq.record_failure("shared-name", provider_id="a", path=qfile)
    mq.record_failure("shared-name", provider_id="b", path=qfile)
    assert mq.get_quarantined_suffixes(qfile, provider_id="a") == set()

    mq.record_failure("shared-name", provider_id="a", path=qfile)

    assert mq.get_quarantined_suffixes(qfile, provider_id="a") == {"shared-name"}
    assert mq.get_quarantined_suffixes(qfile, provider_id="b") == set()


def test_a_success_lifts_the_entry_of_its_own_provider_only(tmp_path):
    qfile = tmp_path / "quarantine.json"
    for provider_id in ("a", "b"):
        mq.record_terminal_unavailable("gone", provider_id=provider_id, path=qfile)

    mq.record_success("gone", provider_id="a", path=qfile)

    assert mq.get_quarantined_suffixes(qfile, provider_id="a") == set()
    assert mq.get_quarantined_suffixes(qfile, provider_id="b") == {"gone"}


async def test_a_provider_s_own_refusal_still_removes_its_model(prod):
    """The rule itself stays: a model its own provider refuses leaves that
    provider's list at the second absence, and only that model."""
    pfile, qfile = prod
    mq.record_terminal_unavailable(
        "claude-haiku-4-5", reason="model_not_found", provider_id=CLAUDE, path=qfile
    )

    await ma.refresh_all_model_catalogs(pass_kind="bootstrap", quarantine_path=qfile)

    saved = _saved(pfile, CLAUDE)
    assert "claude-haiku-4-5" not in saved
    assert {"claude-sonnet-5-5", "claude-haiku-5-5"} <= set(saved)


# ── Every writer names its provider ─────────────────────────────────────────


@pytest.fixture
def gateway_quarantine(tmp_path, monkeypatch):
    """The gateway's own quarantine calls, sent to a file of this test."""
    qfile = tmp_path / "gateway-quarantine.json"
    for name in (
        "record_account_unsupported",
        "record_terminal_unavailable",
        "get_quarantined_by_provider",
    ):
        real = getattr(mq, name)
        monkeypatch.setattr(
            mq, name, lambda *a, _real=real, **k: _real(*a, **{**k, "path": qfile})
        )
    return qfile


def _two_providers_of_one_name(tmp_path, monkeypatch) -> None:
    _setup_providers(
        tmp_path,
        monkeypatch,
        [
            _cli(CODEX, "codex", ["shared-model"]),
            _cli(CLAUDE, "claude", ["shared-model"]),
        ],
    )


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
async def test_a_refusal_met_by_a_chat_is_recorded_for_the_provider_called(
    tmp_path, monkeypatch, gateway_quarantine, stream
):
    from plugboard import router

    _two_providers_of_one_name(tmp_path, monkeypatch)

    async def refuse(kind, body, **kwargs):
        raise cli_subprocess.CliSubprocessError(
            f"CLI '{kind}' exited 1: this model is not available for your account",
            provider_name=kind,
            model_id=str(body.get("model") or ""),
        )

    monkeypatch.setattr(cli_subprocess, "complete", refuse)
    body = {
        "model": f"{CODEX}:shared-model",
        "messages": [{"role": "user", "content": "hi"}],
    }

    with pytest.raises(Exception):  # noqa: B017, PT011 - the refusal itself
        if stream:
            async for _chunk in router.forward_stream(dict(body, stream=True)):
                pass
        else:
            await router.forward(body)

    assert mq.get_quarantined_suffixes(gateway_quarantine, provider_id=CODEX) == {
        "shared-model"
    }
    assert mq.get_quarantined_suffixes(gateway_quarantine, provider_id=CLAUDE) == set()
    assert mq.get_quarantined_suffixes(gateway_quarantine) == set()


async def test_a_failed_internet_check_is_recorded_for_the_provider_checked(
    monkeypatch, gateway_quarantine
):
    import plugboard.app as app_module

    async def gone(body, timeout=20.0):
        raise RuntimeError("HTTP 404: model_not_found")

    monkeypatch.setattr(app_module, "forward", gone)
    monkeypatch.setattr(
        app_module,
        "resolve_provider_and_model",
        lambda mid: ({"id": CODEX, "models": []}, "shared-model"),
    )

    await app_module.check_model_internet_access(
        {"model_id": f"{CODEX}:shared-model", "persist": False}, _=None
    )

    assert mq.get_quarantined_suffixes(gateway_quarantine, provider_id=CODEX) == {
        "shared-model"
    }
    assert mq.get_quarantined_suffixes(gateway_quarantine) == set()


async def test_the_models_list_labels_the_name_at_the_refusing_provider_only(
    tmp_path, monkeypatch, gateway_quarantine
):
    from plugboard import router

    _two_providers_of_one_name(tmp_path, monkeypatch)
    monkeypatch.setenv("PLUGBOARD_MODEL_EXPOSE", "all")
    mq.record_terminal_unavailable("shared-model", provider_id=CODEX)
    router.invalidate_models_cache()

    rows = {m["owned_by"]: m for m in await router.list_models()}

    assert rows[CODEX].get("reason") == "quarantined"
    assert "reason" not in rows[CLAUDE]
