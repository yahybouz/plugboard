"""MR-B: model-list refresh, change journal, webhooks and auto-update status."""

from __future__ import annotations

import json

import pytest
from aioresponses import aioresponses

from plugboard import change_events, config, model_diff
from plugboard import model_availability as ma
from tests.conftest import _setup_providers

AUTH = {"Authorization": "Bearer testkey"}
BASE = "https://api.reseller.test/v1"


NAMES = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"]


def ids(n):
    """Distinct model families, so that family pruning leaves them alone."""
    return [{"id": NAMES[i], "name": NAMES[i]} for i in range(n)]


@pytest.fixture
def sent(monkeypatch):
    events = []

    async def fake_emit(event, data):
        events.append((event, data))
        return 1

    monkeypatch.setattr("plugboard.webhooks.emit", fake_emit)
    return events


@pytest.fixture
def http_provider(tmp_path, monkeypatch, sent):
    monkeypatch.setattr("plugboard.usage.get_all_used_models", lambda: set())
    provider = {
        "id": "reseller",
        "is_active": True,
        "base_url": BASE,
        "bearer_token": "",
        "auto_discover": True,
        "default_model": "alpha",
        "models": ids(5),
    }
    _setup_providers(tmp_path, monkeypatch, [provider])
    return provider


def stored_ids(pid="reseller"):
    return [m["id"] for m in config.get_provider(pid)["models"]]


async def refresh(payload=None, status=200):
    with aioresponses() as mock:
        if payload is None:
            mock.get(f"{BASE}/models", status=status, body="nope")
        else:
            mock.get(f"{BASE}/models", status=status, payload=payload)
        report = await ma.refresh_all_model_catalogs()
    await change_events.drain()  # webhooks are sent in the background
    return report


def listing(models):
    return {"object": "list", "data": [{"id": m["id"], "object": "model"} for m in models]}


# -- generic HTTP discovery and the diff ------------------------------------------------


@pytest.mark.asyncio
async def test_new_model_is_added_and_announced(http_provider, sent):
    report = await refresh(listing(ids(6)))
    assert stored_ids() == NAMES[:6]
    assert report["last_changes"]["providers"]["reseller"]["added"] == ["foxtrot"]
    assert sent == [("models.changed", {"provider": "reseller", "added": ["foxtrot"], "removed": []})]
    [line] = change_events.read_journal()
    assert line["event"] == "models.changed" and line["added"] == ["foxtrot"]


@pytest.mark.asyncio
async def test_empty_list_changes_nothing(http_provider, sent):
    report = await refresh({"object": "list", "data": []})
    assert stored_ids() == NAMES[:5]
    assert "empty" in report["last_changes"]["errors"][0]["error"]
    assert sent == []


@pytest.mark.asyncio
async def test_unauthorized_changes_nothing(http_provider, sent):
    report = await refresh(status=401)
    assert stored_ids() == NAMES[:5]
    assert "401" in report["last_changes"]["errors"][0]["error"]
    assert sent == []


@pytest.mark.asyncio
async def test_drop_of_sixty_percent_is_an_anomaly(http_provider, sent):
    report = await refresh(listing(ids(2)))
    assert stored_ids() == NAMES[:5]
    assert "dropped 3 of 5" in report["last_changes"]["errors"][0]["error"]
    assert sent == []


@pytest.mark.asyncio
async def test_removed_after_two_absences_even_across_restarts(http_provider, sent):
    without_m4 = listing(ids(4))
    first = await refresh(without_m4)
    assert "echo" in stored_ids()
    assert first["last_changes"]["providers"]["reseller"]["pending_removal"] == {"echo": 1}
    assert sent == []
    # The absence count is on disk: a restart between passes does not reset it.
    second = await refresh(without_m4)
    assert "echo" not in stored_ids()
    assert second["last_changes"]["providers"]["reseller"]["removed"] == ["echo"]
    assert sent == [("models.changed", {"provider": "reseller", "added": [], "removed": ["echo"]})]


@pytest.mark.asyncio
async def test_reappearing_model_resets_its_count(http_provider):
    await refresh(listing(ids(4)))
    await refresh(listing(ids(5)))
    await refresh(listing(ids(4)))
    assert "echo" in stored_ids()


@pytest.mark.asyncio
async def test_default_and_pinned_are_never_removed(tmp_path, monkeypatch, sent):
    monkeypatch.setattr("plugboard.usage.get_all_used_models", lambda: set())
    provider = {
        "id": "reseller", "is_active": True, "base_url": BASE, "auto_discover": True,
        "default_model": "alpha", "pinned_models": ["bravo"], "models": ids(4),
    }
    _setup_providers(tmp_path, monkeypatch, [provider])
    keep = listing([{"id": "charlie"}, {"id": "delta"}, {"id": "hotel"}])
    for _ in range(3):
        await refresh(keep)
    assert set(stored_ids()) == {"alpha", "bravo", "charlie", "delta", "hotel"}


@pytest.mark.asyncio
async def test_auto_discover_false_is_left_alone(tmp_path, monkeypatch, sent):
    monkeypatch.setattr("plugboard.usage.get_all_used_models", lambda: set())
    _setup_providers(tmp_path, monkeypatch, [
        {"id": "reseller", "is_active": True, "base_url": BASE, "models": ids(2)},
    ])
    with aioresponses():  # any request would fail: none is registered
        report = await ma.refresh_all_model_catalogs()
    assert report["providers"] == [] and stored_ids() == ["alpha", "bravo"]


@pytest.mark.asyncio
async def test_first_population_is_silent(tmp_path, monkeypatch, sent):
    monkeypatch.setattr("plugboard.usage.get_all_used_models", lambda: set())
    _setup_providers(tmp_path, monkeypatch, [
        {"id": "reseller", "is_active": True, "base_url": BASE, "auto_discover": True},
    ])
    await refresh(listing(ids(3)))
    assert stored_ids() == ["alpha", "bravo", "charlie"]
    assert sent == [] and change_events.read_journal() == []


@pytest.mark.asyncio
async def test_admin_edit_during_a_pass_is_kept(http_provider, monkeypatch):
    real = ma.discover_openai_models

    async def slow_discovery(provider):
        result = await real(provider)
        config.update_provider("reseller", {"name": "Renamed while refreshing"})
        return result

    monkeypatch.setattr(ma, "discover_openai_models", slow_discovery)
    await refresh(listing(ids(6)))
    record = config.get_provider("reseller")
    assert record["name"] == "Renamed while refreshing"
    assert len(record["models"]) == 6


def test_manual_mode_removes_at_once(http_provider):
    result = model_diff.apply("reseller", ids(4), "manual")
    assert result.removed == ["echo"] and "echo" not in stored_ids()


def test_dry_run_writes_nothing(http_provider):
    result = model_diff.apply("reseller", ids(4) + [{"id": "new"}], "manual", dry_run=True)
    assert result.removed == ["echo"] and result.added == ["new"]
    assert stored_ids() == NAMES[:5]


# -- webhooks cap ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_cap_sends_a_summary(monkeypatch, sent, tmp_path):
    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path))
    monkeypatch.setenv("PLUGBOARD_WEBHOOK_MAX_PER_RUN", "2")
    batch = change_events.ChangeBatch()
    for i in range(5):
        batch.add("pricing.changed", {"model": f"m{i}"})
    stats = await batch.flush()
    assert stats == {"journaled": 5, "sent": 2, "truncated": 3}
    assert sent[-1] == ("pricing.changed", {"truncated": 3})
    assert len(change_events.read_journal()) == 5


def test_journal_is_bounded(monkeypatch, tmp_path):
    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path))
    monkeypatch.setattr(change_events, "JOURNAL_MAX_LINES", 10)
    change_events.append_journal([{"n": i} for i in range(25)])
    lines = (tmp_path / "catalog" / "changes.jsonl").read_text().splitlines()
    assert [json.loads(x)["n"] for x in lines] == list(range(15, 25))


# -- pricing.changed --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pricing_changed_pending_once_and_applied(tmp_path, monkeypatch, sent):
    from plugboard import billing, pricing_scraper, pricing_suggestions

    d = tmp_path / "pricing"
    d.mkdir()
    monkeypatch.setattr(billing, "_PRICING_DIR", d)
    monkeypatch.setattr(pricing_scraper, "_PRICING_SOURCE_URL", "")
    monkeypatch.setattr(pricing_scraper, "_openrouter_configured", lambda: False)

    async def _none():
        return {}

    monkeypatch.setattr(pricing_scraper, "_fetch_litellm_pricing", _none)
    month = billing.current_pricing_month()
    (d / f"{month}.json").write_text(json.dumps({"models": {
        "m": {"input_price": 2, "output_price": 8, "source": "models_dev"},
        "small": {"input_price": 1, "output_price": 4, "source": "models_dev"},
    }}))
    monkeypatch.setattr(pricing_scraper, "_models_dev_pricing", lambda: ({
        "m": {"input_price": 4, "output_price": 16},        # +100%: held
        "small": {"input_price": 1.1, "output_price": 4},   # +10%: applied silently
        "fresh": {"input_price": 1, "output_price": 1},     # first price: silent
    }, {}))
    await pricing_scraper.update_current_pricing()
    await pricing_scraper.update_current_pricing()  # same proposal: not announced again
    await change_events.drain()
    events = [data for event, data in sent if event == "pricing.changed"]
    assert len(events) == 1
    assert events[0]["model"] == "m" and events[0]["status"] == "pending" and events[0]["pct"] == 100.0

    [item] = pricing_suggestions.list_suggestions()
    from plugboard import app as app_mod

    await app_mod._suggestion_action(item["id"], "apply")
    await change_events.drain()
    assert [d["status"] for e, d in sent if e == "pricing.changed"] == ["pending", "applied"]


# -- scheduler status and manual run --------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_update_run_refuses_while_running(client):
    from plugboard.app import app

    class Busy:
        running_since = "2026-10-18T08:00:00+00:00"
        status_dict = {"running_since": running_since}

    previous = getattr(app.state, "pricing_scheduler", None)
    app.state.pricing_scheduler = Busy()
    try:
        resp = await client.post("/admin/auto-update/run", headers=AUTH)
        assert resp.status_code == 409
        assert resp.json()["running_since"] == Busy.running_since
        status = await client.get("/admin/auto-update/status", headers=AUTH)
        assert status.status_code == 200
        assert {"pricing", "models", "catalog", "pending_suggestions", "recent_changes"} <= set(status.json())
    finally:
        app.state.pricing_scheduler = previous


def test_scheduler_staleness(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from plugboard.pricing_scheduler import PricingScheduler

    now = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
    monkeypatch.setenv("PLUGBOARD_PRICING_REFRESH_INTERVAL_H", "6")
    sched = PricingScheduler(update_fn=None, now_fn=lambda: now)
    assert sched.is_stale() is False  # never ran yet: not stale, just unknown
    sched.last_success_at = (now - timedelta(hours=11)).isoformat()
    assert sched.is_stale() is False
    sched.last_success_at = (now - timedelta(hours=13)).isoformat()
    assert sched.is_stale() is True
    monkeypatch.setenv("PLUGBOARD_PRICING_REFRESH_INTERVAL_H", "0")
    daily = PricingScheduler(update_fn=None, now_fn=lambda: now)
    daily.last_success_at = (now - timedelta(hours=47)).isoformat()
    assert daily.is_stale() is False
    daily.last_success_at = (now - timedelta(hours=49)).isoformat()
    assert daily.is_stale() is True


@pytest.mark.asyncio
async def test_cli_provider_refreshed_once_per_cli_interval(tmp_path, monkeypatch, sent):
    monkeypatch.setattr("plugboard.usage.get_all_used_models", lambda: set())
    _setup_providers(tmp_path, monkeypatch, [
        {"id": "cli", "is_active": True, "auth_source": "subprocess:gemini", "models": ids(1)},
    ])
    calls = []

    async def fake_subprocess(provider, **kwargs):
        calls.append(provider["id"])
        return {"provider": provider["id"], "models": ids(1)}

    monkeypatch.setattr(ma, "refresh_subprocess_provider_models", fake_subprocess)
    await ma.refresh_all_model_catalogs()
    await ma.refresh_all_model_catalogs()
    assert calls == ["cli"]
    await ma.refresh_all_model_catalogs(pass_kind="manual")
    assert calls == ["cli", "cli"]


@pytest.mark.asyncio
async def test_provider_form_fields_are_saved(client, _providers_file):
    resp = await client.put(
        "/providers/test-provider", headers=AUTH,
        json={"auto_discover": True, "pinned_models": ["fake:model-v1"]},
    )
    assert resp.status_code == 200, resp.text
    stored = config.get_provider("test-provider")
    assert stored["auto_discover"] is True and stored["pinned_models"] == ["fake:model-v1"]
    bad = await client.put("/providers/test-provider", headers=AUTH, json={"pinned_models": [""]})
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_manual_discover_preview_then_apply(client, _providers_file, sent):
    payload = {"object": "list", "data": [{"id": "fake:model-v1"}, {"id": "fake:new"}]}
    config.update_provider("test-provider", {"models": [{"id": "fake:model-v1"}, {"id": "fake:old"}]})
    with aioresponses() as mock:
        mock.get("http://fake-llm.test/v1/models", payload=payload, repeat=True)
        preview = await client.post("/providers/test-provider/discover?dry_run=true", headers=AUTH)
        assert preview.json()["removed"] == ["fake:old"] and preview.json()["added"] == ["fake:new"]
        assert stored_ids("test-provider") == ["fake:model-v1", "fake:old"]
        applied = await client.post("/providers/test-provider/discover", headers=AUTH)
    await change_events.drain()
    assert applied.json()["removed"] == ["fake:old"]
    assert stored_ids("test-provider") == ["fake:model-v1", "fake:new"]
    assert ("models.changed", {"provider": "test-provider", "added": ["fake:new"], "removed": ["fake:old"]}) in sent
