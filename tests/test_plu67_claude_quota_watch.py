"""PLU-67: the gateway watches the shared Claude quota from real calls only.

One Claude account serves production, UAT and development (D-59): a
development session can spend the quota production needs. The gateway now
notes, from the calls it makes anyway (no probe, no extra call), the last
"limit reached" of a claude CLI and the last 429 of a Claude provider (CLI or
Anthropic API): ``GET /admin/claude-quota``, a Monitoring block, one audit
entry per episode, and two Prometheus series. The state is kept in
``data/claude_quota.json`` across a restart.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from aioresponses import aioresponses

from plugboard import claude_quota
from plugboard.cli_subprocess import CliSubprocessError
from plugboard.router import ProviderQuotaError
from tests.conftest import _setup_providers
from tests.factories import PROVIDER_A
from tests.test_circuit_breaker_provider_faults import (  # noqa: F401 (fixture)
    _CLI_ID,
    _claude_error,
    cli,
)

ROOT = Path(__file__).resolve().parent.parent
_MESSAGES = [{"role": "user", "content": "hi"}]
_ANTHROPIC = {
    "id": "anthropic-direct",
    "name": "Anthropic Direct",
    "base_url": "https://api.anthropic.com/v1",
    "bearer_token": "synthetic-token",
    "default_model": "claude-sonnet-5-5",
    "preset_key": "anthropic",
    "egress_guard": False,
    "is_active": True,
    "created_at": "2026-01-01T00:00:00+00:00",
}
_ANTHROPIC_URL = "https://api.anthropic.com/v1/chat/completions"


@pytest.fixture
def audit_db(tmp_path, monkeypatch):
    import plugboard.audit as audit_mod

    monkeypatch.setattr(audit_mod, "_DB_PATH", tmp_path / "audit.db")
    return audit_mod


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    path = tmp_path / "claude_quota.json"
    monkeypatch.setattr(claude_quota, "_state_path", lambda: path)
    return path


def _audits(audit_mod):
    return audit_mod.get_audit_log(action="provider.quota_limit")


# ── which providers are watched ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "provider, watched",
    [
        ({"id": "c", "auth_source": "subprocess:claude"}, True),
        ({"id": "a", "base_url": "https://api.anthropic.com/v1"}, True),
        (
            {
                "id": "a2",
                "base_url": "https://gw.example/v1",
                "preset_key": "anthropic",
            },
            True,
        ),
        ({"id": "x", "auth_source": "subprocess:codex"}, False),
        ({"id": "y", "base_url": "https://api.openai.com/v1"}, False),
        ({"id": "z", "base_url": "https://anthropic.com.evil.example/v1"}, False),
    ],
)
def test_only_claude_providers_are_watched(provider, watched):
    assert claude_quota.is_claude_provider(provider) is watched


# ── a real claude CLI call that hits the limit ─────────────────────────────


@pytest.mark.asyncio
async def test_a_claude_cli_limit_is_recorded(cli, audit_db, state_file):  # noqa: F811
    from plugboard.router import forward

    resets = int(time.time()) + 3600
    cli("claude", stdout=_claude_error(f"Claude AI usage limit reached|{resets}"))
    with pytest.raises(Exception):  # noqa: B017, PT011 - the quota error itself
        await forward({"model": f"{_CLI_ID}:pbtest-model", "messages": _MESSAGES})
    await claude_quota.settle()

    state = claude_quota.snapshot()
    assert state["last_limit_at"] is not None
    assert state["provider"] == _CLI_ID
    assert state["model"] == "pbtest-model"
    assert state["source"] == "cli"
    assert "usage limit reached" in state["message"].lower()
    assert state["resumes_at"] is not None
    assert abs(claude_quota.parse_iso(state["resumes_at"]) - resets) <= 1
    assert state["providers"][_CLI_ID]["count"] == 1
    assert len(_audits(audit_db)) == 1
    assert json.loads(state_file.read_text())["last"]["provider"] == _CLI_ID


@pytest.mark.asyncio
async def test_a_claude_cli_limit_in_a_stream_is_recorded(cli, audit_db):  # noqa: F811
    from plugboard.router import forward_stream

    cli("claude", stdout=_claude_error("You've hit your limit"))
    body = {"model": f"{_CLI_ID}:pbtest-model", "messages": _MESSAGES, "stream": True}
    with pytest.raises(Exception):  # noqa: B017, PT011
        async for _chunk in forward_stream(body):
            pass
    await claude_quota.settle()

    assert claude_quota.snapshot()["provider"] == _CLI_ID


@pytest.mark.asyncio
async def test_a_codex_limit_is_not_recorded(cli, audit_db):  # noqa: F811
    from plugboard.router import forward

    cli("codex", stdout=b"", stderr=b"You've hit your usage limit.", code=1)
    with pytest.raises(Exception):  # noqa: B017, PT011
        await forward({"model": f"{_CLI_ID}:pbtest-model", "messages": _MESSAGES})
    await claude_quota.settle()

    assert claude_quota.snapshot()["last_limit_at"] is None
    assert _audits(audit_db) == []


# ── an Anthropic API 429 ────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
async def test_an_anthropic_429_is_recorded(tmp_path, monkeypatch, audit_db, stream):
    from plugboard import route_pauses, router

    _setup_providers(tmp_path, monkeypatch, [_ANTHROPIC])
    route_pauses.reset()
    body = {"model": "anthropic-direct:claude-sonnet-5-5", "messages": _MESSAGES}
    before = time.time()
    with aioresponses() as mock:
        mock.post(
            _ANTHROPIC_URL,
            status=429,
            payload={"error": {"type": "rate_limit_error", "message": "slow down"}},
            headers={"Retry-After": "120"},
            repeat=True,
        )
        with pytest.raises(Exception):  # noqa: B017, PT011
            if stream:
                async for _chunk in router.forward_stream(dict(body, stream=True)):
                    pass
            else:
                await router.forward(body)
    await claude_quota.settle()
    route_pauses.reset()

    state = claude_quota.snapshot()
    assert state["provider"] == "anthropic-direct"
    assert state["source"] == "http"
    assert "429" in state["message"]
    resumes = claude_quota.parse_iso(state["resumes_at"])
    assert before + 119 <= resumes <= time.time() + 121


@pytest.mark.asyncio
async def test_another_providers_429_is_not_recorded(tmp_path, monkeypatch, audit_db):
    from plugboard import route_pauses, router

    _setup_providers(tmp_path, monkeypatch, [PROVIDER_A])
    route_pauses.reset()
    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            status=429,
            payload={"error": "slow down"},
            # Longer than the retry delays: no wait before giving up.
            headers={"Retry-After": "600"},
            repeat=True,
        )
        with pytest.raises(Exception):  # noqa: B017, PT011
            await router.forward({"model": "test-provider:m", "messages": _MESSAGES})
    await claude_quota.settle()
    route_pauses.reset()

    assert claude_quota.snapshot()["last_limit_at"] is None


# ── the record itself: dedup, excerpt, persistence, metrics ────────────────


_CLAUDE_CLI = {"id": "claude-sub", "auth_source": "subprocess:claude"}


def _cli_limit(message: str, retry_after=None) -> CliSubprocessError:
    return CliSubprocessError(
        message, provider_name="claude", quota=True, retry_after=retry_after
    )


@pytest.mark.asyncio
async def test_a_burst_is_one_audit_entry_per_episode(audit_db, monkeypatch):
    clock = [1_900_000_000.0]
    monkeypatch.setattr(claude_quota, "_now", lambda: clock[0])
    for _ in range(5):
        claude_quota.note_limit(_CLAUDE_CLI, "m", _cli_limit("usage limit reached"))
        clock[0] += 60
    await claude_quota.settle()
    assert len(_audits(audit_db)) == 1

    clock[0] += claude_quota.AUDIT_WINDOW_S
    claude_quota.note_limit(_CLAUDE_CLI, "m", _cli_limit("usage limit reached"))
    await claude_quota.settle()

    entries = _audits(audit_db)
    assert len(entries) == 2
    assert entries[0]["actor"] == "system"
    assert entries[0]["target_id"] == "claude-sub"
    assert claude_quota.snapshot()["providers"]["claude-sub"]["count"] == 6


@pytest.mark.asyncio
async def test_the_excerpt_is_short_and_holds_no_secret(audit_db):
    secret = "sk-ant-api03-" + "A" * 40
    message = f"usage limit reached; token {secret} " + "x" * 500
    claude_quota.note_limit(_CLAUDE_CLI, "m", _cli_limit(message))
    await claude_quota.settle()

    excerpt = claude_quota.snapshot()["message"]
    assert len(excerpt) <= claude_quota.EXCERPT_MAX
    assert secret not in excerpt
    assert secret not in json.dumps(_audits(audit_db))


@pytest.mark.asyncio
async def test_retry_after_gives_the_resume_time(audit_db, monkeypatch):
    monkeypatch.setattr(claude_quota, "_now", lambda: 1_900_000_000.0)
    claude_quota.note_limit(_CLAUDE_CLI, "m", _cli_limit("5-hour limit reached", 600))
    await claude_quota.settle()
    assert claude_quota.parse_iso(claude_quota.snapshot()["resumes_at"]) == (
        1_900_000_600.0
    )


@pytest.mark.asyncio
async def test_no_resume_time_when_the_message_gives_none(audit_db):
    claude_quota.note_limit(
        _CLAUDE_CLI, "m", _cli_limit("5-hour limit reached - resets 3pm")
    )
    await claude_quota.settle()
    assert claude_quota.snapshot()["resumes_at"] is None


@pytest.mark.asyncio
async def test_the_state_survives_a_restart(audit_db, state_file, monkeypatch):
    claude_quota.note_limit(_CLAUDE_CLI, "m", _cli_limit("usage limit reached"))
    await claude_quota.settle()
    saved = claude_quota.snapshot()

    # A new process reads the file only.
    assert json.loads(state_file.read_text())
    assert claude_quota.snapshot() == saved
    assert saved["providers"]["claude-sub"]["count"] == 1


def test_an_unreadable_state_file_reads_as_empty(state_file):
    state_file.write_text("{not json")
    assert claude_quota.snapshot()["last_limit_at"] is None


@pytest.mark.asyncio
async def test_prometheus_series(audit_db, monkeypatch):
    from plugboard.metrics import format_prometheus

    monkeypatch.setattr(claude_quota, "_now", lambda: 1_900_000_000.0)
    for _ in range(2):
        claude_quota.note_limit(_CLAUDE_CLI, "m", _cli_limit("usage limit reached"))
    claude_quota.note_limit(
        {"id": "anthropic-direct", "base_url": "https://api.anthropic.com/v1"},
        "claude-sonnet-5-5",
        ProviderQuotaError("[anthropic-direct] HTTP 429: x", provider_id="a"),
    )
    await claude_quota.settle()

    text = format_prometheus()
    assert "# TYPE plugboard_claude_limit_total counter" in text
    assert 'plugboard_claude_limit_total{provider="claude-sub"} 2' in text
    assert 'plugboard_claude_limit_total{provider="anthropic-direct"} 1' in text
    assert "# TYPE plugboard_claude_limit_last_timestamp_seconds gauge" in text
    assert (
        'plugboard_claude_limit_last_timestamp_seconds{provider="claude-sub"} '
        "1900000000.0" in text
    )


def test_prometheus_series_without_any_limit():
    from plugboard.metrics import format_prometheus

    text = format_prometheus()
    assert "# TYPE plugboard_claude_limit_total counter" in text
    assert "plugboard_claude_limit_total{" not in text


# ── admin view ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_admin_endpoint(admin_client, audit_db):
    empty = await admin_client.get(
        "/admin/claude-quota", headers={"Authorization": "Bearer adminkey"}
    )
    assert empty.status_code == 200
    assert empty.json()["last_limit_at"] is None

    claude_quota.note_limit(_CLAUDE_CLI, "m", _cli_limit("usage limit reached", 60))
    await claude_quota.settle()
    resp = await admin_client.get(
        "/admin/claude-quota", headers={"Authorization": "Bearer adminkey"}
    )
    body = resp.json()
    assert body["provider"] == "claude-sub"
    assert body["model"] == "m"
    assert body["resumes_at"] is not None
    assert body["providers"]["claude-sub"]["count"] == 1


@pytest.mark.asyncio
async def test_the_admin_endpoint_needs_the_admin_key(admin_client):
    resp = await admin_client.get(
        "/admin/claude-quota", headers={"Authorization": "Bearer testkey"}
    )
    assert resp.status_code in (401, 403)


def test_the_dashboard_shows_the_block():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert 'id="monitoring-claude-quota"' in html
    assert '"/admin/claude-quota"' in js


def test_the_guide_documents_the_alert():
    guide = (ROOT / "docs" / "guide" / "monitoring.md").read_text(encoding="utf-8")
    assert "/admin/claude-quota" in guide
    assert "plugboard_claude_limit_total" in guide
    assert "plugboard_claude_limit_last_timestamp_seconds" in guide
