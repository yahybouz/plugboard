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
    monkeypatch.setattr(mq, "get_quarantined_suffixes", lambda: set(quarantined))
    router.invalidate_models_cache()


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


async def _ids(client, key):
    resp = await client.get("/v1/models", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    return {m["id"] for m in resp.json()["data"]}


_FAILED = {"connected": False, "last_probed_at": "2026-11-14T08:00:00+00:00"}
_OK = {"connected": True, "last_probed_at": "2026-11-14T08:00:00+00:00"}


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


async def test_client_key_does_not_see_a_quarantined_model(
    client, keys, tmp_path, monkeypatch
):
    _prepare(tmp_path, monkeypatch, quarantined={"agy-gamma"})
    rec = keys.create_key("cust", "synthetic")
    keys._invalidate_cache()
    ids = await _ids(client, rec["key"])
    assert "agy-sub:agy-gamma" not in ids
    assert "agy-sub:agy-alpha" in ids


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
        quarantined={"agy-gamma"},
    )
    ids = await _ids(client, "testkey")  # the fixture's admin key
    assert {"agy-sub:agy-beta", "agy-sub:agy-gamma"} <= ids
