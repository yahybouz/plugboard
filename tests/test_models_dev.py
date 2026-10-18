"""models.dev catalog: validation, fetch fallbacks and generated presets."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aioresponses import aioresponses

from plugboard import models_dev
from plugboard.app import _require_public_host as REAL_REQUIRE_PUBLIC_HOST
from plugboard.endpoints import openai_url

FIXTURE = Path(__file__).parent / "fixtures" / "models_dev_small.json"
URL = "https://catalog.test/api.json"


@pytest.fixture(autouse=True)
def _public_catalog_hosts(monkeypatch):
    """Catalog hosts in the fixture do not resolve; treat them as public."""

    async def _ok(base_url):
        return []

    monkeypatch.setattr("plugboard.app._require_public_host", _ok)


@pytest.fixture
def raw() -> dict:
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def catalog_env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    monkeypatch.setenv("PLUGBOARD_DATA", str(data))
    monkeypatch.setenv("PLUGBOARD_CATALOG_URL", URL)
    monkeypatch.delenv("PLUGBOARD_CATALOG", raising=False)
    monkeypatch.delenv("PLUGBOARD_CATALOG_EXCLUDE", raising=False)
    models_dev._cache.update(mtime=None, providers={})
    return data


def _store(data: Path, raw: dict) -> None:
    (data / "catalog").mkdir(exist_ok=True)
    (data / "catalog" / "models_dev.json").write_text(json.dumps(raw))


# -- validation ----------------------------------------------------------------


def test_parse_catalog_skips_invalid_entries(raw):
    raw["broken"] = "not an object"
    providers, invalid = models_dev.parse_catalog(raw)
    assert "broken" not in providers
    assert "neg" not in providers["badprice"].models  # negative price rejected
    assert "ok-1" in providers["badprice"].models
    assert invalid == 2


def test_parse_catalog_rejects_non_object():
    with pytest.raises(ValueError):
        models_dev.parse_catalog([1, 2])


def test_shrink_guard(raw):
    full, _ = models_dev.parse_catalog(raw)
    assert models_dev.shrank_too_much(full, {}) is False  # first fetch
    small = {k: full[k] for k in list(full)[:3]}
    assert models_dev.shrank_too_much(small, full) is True
    assert models_dev.shrank_too_much(full, small) is False


# -- fetch ---------------------------------------------------------------------


async def test_refresh_stores_catalog_and_etag(catalog_env, raw):
    with aioresponses() as m:
        m.get(URL, status=200, body=json.dumps(raw), headers={"ETag": '"v1"'})
        result = await models_dev.refresh_catalog()
    assert result["state"] == "updated"
    assert "acme" in models_dev.load_catalog()
    assert models_dev.status()["state"] == "ok"
    assert json.loads((catalog_env / "catalog" / "models_dev.meta.json").read_text())["etag"] == '"v1"'


async def test_refresh_304_keeps_copy(catalog_env, raw):
    _store(catalog_env, raw)
    (catalog_env / "catalog" / "models_dev.meta.json").write_text(json.dumps({"etag": '"v1"'}))
    with aioresponses() as m:
        m.get(URL, status=304)
        result = await models_dev.refresh_catalog()
    assert result["state"] == "unchanged"
    assert "acme" in models_dev.load_catalog()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"exception": asyncio.TimeoutError()},
        {"status": 200, "body": "{not json"},
        {"status": 500, "body": "oops"},
    ],
    ids=["timeout", "invalid-json", "http-500"],
)
async def test_refresh_failure_keeps_last_good_copy(catalog_env, raw, kwargs):
    _store(catalog_env, raw)
    with aioresponses() as m:
        m.get(URL, **kwargs)
        result = await models_dev.refresh_catalog()
    assert result["state"] == "error"
    assert "acme" in models_dev.load_catalog()
    status = models_dev.status()
    assert status["state"] == "stale" and status["last_error"]


async def test_refresh_rejects_oversized_catalog(catalog_env, raw, monkeypatch):
    monkeypatch.setenv("PLUGBOARD_CATALOG_MAX_MB", "0.0001")  # ~100 bytes
    with aioresponses() as m:
        m.get(URL, status=200, body=json.dumps(raw))
        result = await models_dev.refresh_catalog()
    assert result["state"] == "error" and "larger" in result["error"]
    assert models_dev.status()["state"] == "unavailable"


async def test_refresh_rejects_shrunk_catalog(catalog_env, raw):
    _store(catalog_env, raw)
    with aioresponses() as m:
        m.get(URL, status=200, body=json.dumps({"acme": raw["acme"]}))
        result = await models_dev.refresh_catalog()
    assert result["state"] == "error" and "shrank" in result["error"]
    assert len(models_dev.load_catalog()) == len(raw)


async def test_catalog_off_makes_no_request(catalog_env, monkeypatch):
    monkeypatch.setenv("PLUGBOARD_CATALOG", "off")
    with aioresponses():  # any request would raise ClientConnectionError
        assert await models_dev.refresh_catalog() == {"state": "off"}
    assert models_dev.status()["state"] == "off"


def test_no_catalog_means_no_presets(catalog_env):
    assert models_dev.generated_presets() == []
    assert models_dev.status()["state"] == "unavailable"


# -- presets -------------------------------------------------------------------


def test_generated_presets(catalog_env, raw):
    _store(catalog_env, raw)
    presets = {p["key"]: p for p in models_dev.generated_presets()}
    # OpenRouter is in the coded table: its coded base_url beats the catalog's.
    assert presets["catalog-openrouter"]["base_url"] == "https://openrouter.ai/api/v1"
    assert presets["catalog-acme"]["base_url"] == "https://api.acme.test/v1"
    # A model routed through another SDK is excluded.
    assert presets["catalog-acme"]["known_models"] == ["acme-1"]
    for key in ("catalog-anthropic", "catalog-templated", "catalog-private", "catalog-plainhttp", "catalog-onlyanthropic"):
        assert key not in presets
    assert all(":" not in key for key in presets)
    assert presets["catalog-acme"]["verified"] is False


def test_catalog_exclude(catalog_env, raw, monkeypatch):
    _store(catalog_env, raw)
    monkeypatch.setenv("PLUGBOARD_CATALOG_EXCLUDE", "acme, openrouter")
    keys = {p["key"] for p in models_dev.generated_presets()}
    assert "catalog-acme" not in keys and "catalog-openrouter" not in keys


@pytest.mark.parametrize(
    "url, ok",
    [
        ("https://api.example.com/v1", True),
        ("http://api.example.com/v1", False),
        ("https://user:pw@api.example.com/v1", False),
        ("https://api.example.com:8443/v1", False),
        ("https://127.0.0.1/v1", False),
        ("https://[::1]/v1", False),
        ("https://localhost/v1", False),
        ("https://${X}.example.com/v1", False),
    ],
)
def test_is_public_https(url, ok):
    assert models_dev.is_public_https(url) is ok


def test_list_presets_hides_catalog_duplicate_of_builtin(catalog_env, raw):
    from plugboard.direct_providers import list_presets

    _store(catalog_env, raw)
    keys = [p["key"] for p in list_presets()]
    assert "openai" in keys  # built-in
    assert "catalog-openai" not in keys  # same base_url as the built-in preset
    assert "catalog-acme" in keys
    assert keys.index("anthropic") < keys.index("catalog-acme")


def test_create_from_catalog_preset_freezes_base_url(catalog_env, raw, tmp_path, monkeypatch):
    from tests.conftest import _setup_providers
    from plugboard.config import get_provider
    from plugboard.direct_providers import create_from_preset

    _store(catalog_env, raw)
    (tmp_path / "p").mkdir()
    _setup_providers(tmp_path / "p", monkeypatch, [])
    monkeypatch.setenv("PLUGBOARD_DATA", str(catalog_env))
    record = create_from_preset("catalog-acme", "test-key-123456")
    assert record["preset_key"] == "catalog-acme"
    assert record["auto_discover"] is True
    assert [m["id"] for m in record["models"]] == ["acme-1"]
    # The catalog later points acme somewhere else: the provider keeps its URL.
    raw["acme"]["api"] = "https://attacker.test/v1"
    _store(catalog_env, raw)
    assert get_provider(record["id"])["base_url"] == "https://api.acme.test/v1"


@pytest.mark.parametrize(
    "base, url",
    [
        ("https://api.openai.com/v1", "https://api.openai.com/v1/chat/completions"),
        ("https://generativelanguage.googleapis.com/v1beta/openai", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"),
        ("https://api.groq.com/openai/v1/", "https://api.groq.com/openai/v1/chat/completions"),
        ("https://x.openai.azure.com/openai", "https://x.openai.azure.com/openai/v1/chat/completions"),
        ("https://gw.example/v2proxy", "https://gw.example/v2proxy/v1/chat/completions"),
        ("https://api.upstage.ai/v1/solar", "https://api.upstage.ai/v1/solar/chat/completions"),
        ("https://api.z.ai/api/paas/v4", "https://api.z.ai/api/paas/v4/chat/completions"),
        ("http://localhost:18789", "http://localhost:18789/v1/chat/completions"),
    ],
)
def test_openai_url(base, url):
    assert openai_url(base, "chat/completions") == url


# -- admin test endpoint ---------------------------------------------------------


@pytest.mark.parametrize(
    "mock, status",
    [
        ({"status": 200, "payload": {"data": [{"id": "m1"}, {"id": "m2"}]}}, "ok"),
        ({"status": 200, "payload": {"data": []}}, "ok_no_models"),
        ({"status": 401, "payload": {"error": "bad key"}}, "invalid_key"),
        ({"status": 200, "body": "<html>hello</html>"}, "not_openai_compatible"),
        ({"exception": asyncio.TimeoutError()}, "unreachable"),
    ],
    ids=["ok", "no-models", "401", "html", "timeout"],
)
async def test_admin_test_endpoint(admin_client, catalog_env, raw, mock, status):
    _store(catalog_env, raw)
    with aioresponses(passthrough=["http://test"]) as m:
        m.get("https://api.acme.test/v1/models", **mock)
        resp = await admin_client.post(
            "/admin/direct-providers/test",
            json={"preset_key": "catalog-acme", "api_key": "k"},
            headers={"Authorization": "Bearer adminkey"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == status
    if status != "ok":
        assert body["message"]
    if status == "ok":
        assert body["models_count"] == 2


async def test_admin_test_endpoint_needs_target(admin_client):
    resp = await admin_client.post(
        "/admin/direct-providers/test",
        json={"api_key": "k"},
        headers={"Authorization": "Bearer adminkey"},
    )
    assert resp.status_code == 422


# -- scheduler -----------------------------------------------------------------


async def test_catalog_failure_does_not_open_pricing_breaker(tmp_path):
    from plugboard.pricing_scheduler import PricingScheduler

    async def update():
        return {"added": 0}

    async def catalog():
        raise RuntimeError("catalog down")

    sched = PricingScheduler(update_fn=update, catalog_fn=catalog, cb_state_file=tmp_path / "cb.json")
    await sched._invoke_update("daily")
    assert sched.status_dict["cb_consecutive_failures"] == 0


@pytest.mark.parametrize(
    "base, expected",
    [
        ("https://versioned.test/v1", "https://versioned.test/v1/chat/completions"),
        ("http://bare.test", "http://bare.test/v1/chat/completions"),
    ],
)
async def test_forward_does_not_double_the_version(tmp_path, monkeypatch, base, expected):
    # Value: protects=requests to presets whose base_url ends in /v1 (OpenAI,
    # Groq, catalog presets) reach .../v1/chat/completions; fails_when=the
    # router concatenates "/v1/chat/completions" again; why_new=the old router
    # built .../v1/v1/...; seam=none
    from tests.conftest import _setup_providers
    from tests.factories import FAKE_COMPLETION
    from plugboard.router import forward

    _setup_providers(
        tmp_path,
        monkeypatch,
        [{"id": "p", "name": "P", "base_url": base, "bearer_token": "k", "default_model": "m",
          "is_active": True, "provider_type": "cloud", "timeout": 30.0, "created_at": "2026-01-01T00:00:00+00:00"}],
    )
    with aioresponses() as m:
        m.post(expected, payload=FAKE_COMPLETION)
        result = await forward({"model": "p:m", "messages": [{"role": "user", "content": "hi"}]})
    assert result


@pytest.mark.asyncio
async def test_catalog_preset_on_a_private_address_is_refused(monkeypatch):
    from fastapi import HTTPException

    from plugboard import llm_pricing

    async def private(host, port):
        raise llm_pricing.UnsafeURL(f"{host} resolves to a non-public address (10.0.0.5)")

    monkeypatch.setattr(llm_pricing, "_resolve_public", private)
    with pytest.raises(HTTPException) as exc:
        await REAL_REQUIRE_PUBLIC_HOST("https://api.evil.example/v1")
    assert exc.value.status_code == 422 and "non-public" in exc.value.detail
