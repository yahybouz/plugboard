"""Lot B: the catalogue a client key sees holds only models it can call.

A model whose last probe failed or that sits in quarantine is not callable
now; an inactive provider has no models; the admin keeps the full view. The
MCP ``list_models`` tool reads the same route, so both surfaces agree.
"""

from __future__ import annotations

import pytest

import plugboard.router as router
from tests.conftest import _setup_providers


def _cli(provider_id, kind, *models, active=True):
    return {
        "id": provider_id,
        "name": provider_id,
        "base_url": "",
        "bearer_token": "",
        "auth_source": f"subprocess:{kind}",
        "default_model": models[0],
        "models": [{"id": m} for m in models],
        "is_active": active,
        "created_at": "2026-01-01T00:00:00+00:00",
    }


_AGY = _cli("agy-sub", "agy", "agy-alpha", "agy-beta", "agy-gamma")
_CLAUDE = _cli("claude-sub", "claude", "claude-sonnet-4-6")
_OFF = _cli("codex-off", "codex", "gpt-off-1", active=False)


@pytest.fixture
def keys(tmp_path, monkeypatch):
    import plugboard.apikeys as ak
    import plugboard.usage as u

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    monkeypatch.setattr(u, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(u, "_DB_PATH", tmp_path / "usage.db")
    ak._invalidate_cache()
    yield ak
    ak._invalidate_cache()


def _prepare(tmp_path, monkeypatch, status=None, quarantined=()):
    (tmp_path / "reg").mkdir(exist_ok=True)
    _setup_providers(tmp_path / "reg", monkeypatch, [_AGY, _CLAUDE, _OFF])
    monkeypatch.setenv("PLUGBOARD_MODEL_EXPOSE", "all")
    import plugboard.model_availability as ma
    import plugboard.model_quarantine as mq

    monkeypatch.setattr(ma, "load_model_status", lambda: {"models": dict(status or {})})
    # The quarantine is per provider (BUG-102): these names are agy-sub's.
    monkeypatch.setattr(
        mq, "get_quarantined_by_provider", lambda *a, **k: {"agy-sub": set(quarantined)}
    )
    router.invalidate_models_cache()


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


async def _ids(client, key):
    resp = await client.get("/v1/models", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    return {m["id"] for m in resp.json()["data"]}


def _ago(hours):
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


_FAILED = {"connected": False, "last_probed_at": _ago(1)}
_STALE = {"connected": False, "last_probed_at": "2025-01-01T00:00:00+00:00"}
_OK = {"connected": True, "last_probed_at": _ago(1)}


async def test_client_key_does_not_see_a_model_whose_last_probe_failed(
    client, keys, tmp_path, monkeypatch
):
    _prepare(
        tmp_path,
        monkeypatch,
        status={"agy-sub:agy-beta": _FAILED, "agy-sub:agy-alpha": _OK},
    )
    rec = keys.create_key("cust", "synthetic")
    keys._invalidate_cache()
    ids = await _ids(client, rec["key"])
    assert "agy-sub:agy-alpha" in ids
    assert "agy-sub:agy-beta" not in ids  # probed and failed: not callable
    assert "agy-sub:agy-gamma" in ids  # never probed: still offered


async def test_stale_failure_and_quarantine_do_not_hide_a_model(
    client, keys, tmp_path, monkeypatch
):
    """A failure older than a probe cycle, or a bare-name quarantine (shared by
    every provider serving the name), says nothing sure about this route."""
    _prepare(
        tmp_path,
        monkeypatch,
        status={"agy-sub:agy-beta": _STALE},
        quarantined={"agy-gamma"},
    )
    rec = keys.create_key("cust", "synthetic")
    keys._invalidate_cache()
    ids = await _ids(client, rec["key"])
    assert {"agy-sub:agy-beta", "agy-sub:agy-gamma"} <= ids


async def test_filter_applies_to_v2_and_leaves_the_admin_view_whole(
    client, keys, tmp_path, monkeypatch
):
    _prepare(tmp_path, monkeypatch, status={"agy-sub:agy-beta": _FAILED})
    rec = keys.create_key("cust", "synthetic")
    keys._invalidate_cache()
    resp = await client.get("/v2/models", headers=_auth(rec["key"]))
    assert resp.status_code == 200, resp.text
    assert "agy-sub:agy-beta" not in resp.text
    # Same cache, admin afterwards: nothing was removed from it.
    assert "agy-sub:agy-beta" in await _ids(client, "testkey")


async def test_limited_key_gets_the_intersection(client, keys, tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch, status={"agy-sub:agy-beta": _FAILED})
    rec = keys.create_key(
        "cust",
        "synthetic",
        allowed_models=["agy-sub:agy-alpha", "agy-sub:agy-beta"],
    )
    keys._invalidate_cache()
    assert await _ids(client, rec["key"]) == {"agy-sub:agy-alpha"}


async def test_inactive_provider_has_no_model_for_a_client_key(
    client, keys, tmp_path, monkeypatch
):
    _prepare(tmp_path, monkeypatch)
    rec = keys.create_key("cust", "synthetic")
    keys._invalidate_cache()
    ids = await _ids(client, rec["key"])
    assert not any(i.startswith("codex-off:") for i in ids)
    assert "claude-sub:claude-sonnet-4-6" in ids


async def test_admin_keeps_the_full_catalogue(client, keys, tmp_path, monkeypatch):
    _prepare(
        tmp_path,
        monkeypatch,
        status={"agy-sub:agy-beta": _FAILED},
    )
    ids = await _ids(client, "testkey")  # the fixture's admin key
    assert {"agy-sub:agy-beta", "agy-sub:agy-gamma"} <= ids


async def _info(client, key, model_id):
    resp = await client.get(f"/v1/models/{model_id}/info", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_model_info_follows_the_client_catalogue(
    client, keys, tmp_path, monkeypatch
):
    """A model the client's catalogue leaves out answers as an unknown model
    on ``/v1/models/{id}/info`` too; the admin keeps its profile."""
    from plugboard.benchmarks import derived_model_info

    hidden = "claude-sub:claude-sonnet-4-6"  # has a curated profile
    _prepare(tmp_path, monkeypatch, status={hidden: _FAILED})
    rec = keys.create_key("cust", "synthetic")
    keys._invalidate_cache()
    assert hidden not in await _ids(client, rec["key"])
    assert hidden in await _ids(client, "testkey")

    assert (await _info(client, "testkey", hidden))["info_source"] == "benchmark_db"
    client_view = await _info(client, rec["key"], hidden)
    assert client_view["info_source"] == "derived"
    assert client_view == derived_model_info(hidden)


async def test_model_info_hides_every_spelling_of_a_hidden_model(
    client, keys, tmp_path, monkeypatch
):
    """PLU-60: chat sends the bare name to the same route as the published ID,
    so it is hidden too; the check follows chat's routes, not the spelling."""
    from plugboard.benchmarks import derived_model_info

    hidden = "claude-sub:claude-sonnet-4-6"
    bare = "claude-sonnet-4-6"
    _prepare(tmp_path, monkeypatch, status={hidden: _FAILED})
    rec = keys.create_key("cust", "synthetic")
    keys._invalidate_cache()
    routes = router._resolve_all_providers(bare)
    assert [(p["id"], m) for p, m in routes] == [("claude-sub", bare)]

    assert (await _info(client, "testkey", bare))["info_source"] == "benchmark_db"
    assert await _info(client, rec["key"], bare) == derived_model_info(bare)


@pytest.mark.parametrize("status", [_OK, _STALE, None])
async def test_model_info_keeps_the_profile_of_a_listed_model(
    client, keys, tmp_path, monkeypatch, status
):
    listed = "claude-sub:claude-sonnet-4-6"
    _prepare(tmp_path, monkeypatch, status={listed: status} if status else {})
    rec = keys.create_key("cust", "synthetic")
    keys._invalidate_cache()
    assert listed in await _ids(client, rec["key"])
    assert await _info(client, rec["key"], listed) == await _info(
        client, "testkey", listed
    )
    assert (await _info(client, rec["key"], listed))["info_source"] == "benchmark_db"
