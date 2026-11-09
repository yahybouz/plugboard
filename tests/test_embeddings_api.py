"""/v1/embeddings: client-key admission, exact routing, engine checks.

The engine is mocked with aioresponses (unmatched URLs fail), never called.
"""

import asyncio
import base64
import json
import logging
import struct

import aiohttp
import pytest
from aioresponses import aioresponses

from tests.conftest import _setup_providers

BASE = "http://127.0.0.1:11434"
TAGS = f"{BASE}/api/tags"
EMBED = f"{BASE}/api/embed"
MODEL = "ollama:qwen3-embedding:0.6b"
NATIVE = "qwen3-embedding:0.6b"
DIGEST = "0" * 63 + "1"
SECRET_TEXT = "synthetic-confidential-passage-7f3a"
MONTH = "2099-01"

OLLAMA = {
    "id": "ollama",
    "name": "Ollama",
    "base_url": BASE,
    "bearer_token": "",
    "default_model": "",
    "is_active": True,
    "provider_type": "sovereign",
}
CLOUDY = {
    "id": "cloudy",
    "name": "Cloudy",
    "base_url": "https://api.example.test/v1",
    "bearer_token": "synthetic-provider-token",
    "default_model": NATIVE,
    "is_active": True,
}
REMOTE = {**OLLAMA, "id": "ollama-lan", "base_url": "http://192.0.2.10:11434"}
SLEEPING = {**OLLAMA, "id": "ollama-off", "is_active": False}


def _tags(native=NATIVE, digest=DIGEST):
    return {
        "models": [
            {"name": "qwen3:4b-instruct", "digest": "f" * 64},
            {"name": native, "digest": digest},
        ]
    }


def _vec(seed: float, dims: int = 4):
    return [seed + i / 10 for i in range(dims)]


@pytest.fixture
async def emb(client, monkeypatch, tmp_path):
    import plugboard.apikeys as keys
    import plugboard.billing as billing
    import plugboard.ratelimit as rl
    import plugboard.usage as usage

    (tmp_path / "p").mkdir()
    _setup_providers(tmp_path / "p", monkeypatch, [OLLAMA, CLOUDY, REMOTE, SLEEPING])
    monkeypatch.setenv(
        "PLUGBOARD_EMBEDDING_MODELS",
        ",".join(
            [
                MODEL,
                "cloudy:" + NATIVE,
                "ollama-lan:" + NATIVE,
                "ollama-off:" + NATIVE,
                "ollama:qwen3-embedding:cloud",
                "ghost:" + NATIVE,
            ]
        ),
    )
    monkeypatch.setattr(keys, "KEYS_FILE", tmp_path / "keys.json")
    monkeypatch.setattr(keys, "KEYS_LOCK", tmp_path / "keys.lock")
    keys._invalidate_cache()
    store = tmp_path / "usage-store"
    monkeypatch.setattr(usage, "_DATA_DIR", store)
    monkeypatch.setattr(usage, "_DB_PATH", store / "usage.db")
    monkeypatch.setattr(billing, "current_pricing_month", lambda: MONTH)
    rl._client_buckets.clear()
    rec = keys.create_key("argos-synthetic", allowed_models=[MODEL])
    client.headers["Authorization"] = f"Bearer {rec['key']}"
    yield client, rec
    keys._invalidate_cache()


def _ok(mock, vectors, tokens=7, digest=DIGEST):
    mock.get(TAGS, payload=_tags(digest=digest))
    mock.post(
        EMBED,
        payload={"model": NATIVE, "embeddings": vectors, "prompt_eval_count": tokens},
    )


def _embed_calls(mock):
    return [
        c
        for (method, url), calls in mock.requests.items()
        if method == "POST"
        for c in calls
    ]


# -- authentication -----------------------------------------------------------


@pytest.mark.parametrize(
    "auth",
    [None, "Bearer testkey", "Bearer pb_invalid", "Bearer lle_invalid", "Basic x"],
)
@pytest.mark.parametrize("dev_mode", [False, True])
async def test_requires_real_client_key(emb, auth, dev_mode, monkeypatch):
    client, _ = emb
    if dev_mode:
        monkeypatch.setattr("plugboard.app._API_KEY", "")
    headers = {"Authorization": auth} if auth else {}
    client.headers.pop("Authorization")
    with aioresponses() as mock:
        r = await client.post(
            "/v1/embeddings", headers=headers, json={"model": MODEL, "input": "x"}
        )
        assert not mock.requests
    assert r.status_code == 401
    assert r.json()["detail"] == "Client API key required for embeddings"


async def test_revoked_key_is_refused(emb):
    import plugboard.apikeys as keys

    client, rec = emb
    keys.revoke_key(rec["key_id"])
    keys._invalidate_cache()
    with aioresponses() as mock:
        r = await client.post("/v1/embeddings", json={"model": MODEL, "input": "x"})
        assert not mock.requests
    assert r.status_code == 401


# -- input bounds -------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"model": MODEL, "input": []},
        {"model": MODEL, "input": ["ok"] * 257},
        {"model": MODEL, "input": ["ok", "   "]},
        {"model": MODEL, "input": SECRET_TEXT + "x" * 16_384},
        {"model": MODEL, "input": [SECRET_TEXT, 3]},
        {"model": MODEL, "input": [[1, 2, 3]]},
        {"model": MODEL, "input": SECRET_TEXT, "encoding_format": "int8"},
        {"model": MODEL, "input": SECRET_TEXT, "dimensions": 256},
        {"model": MODEL, "input": SECRET_TEXT, "base_url": "http://evil.test"},
        {"input": SECRET_TEXT},
        {"model": "", "input": SECRET_TEXT},
    ],
)
async def test_input_is_bounded_and_never_echoed(emb, body):
    client, _ = emb
    with aioresponses() as mock:
        r = await client.post("/v1/embeddings", json=body)
        assert not mock.requests
    assert r.status_code == 422
    assert SECRET_TEXT not in r.text


async def test_body_size_cap(emb):
    client, _ = emb
    with aioresponses() as mock:
        r = await client.post(
            "/v1/embeddings", json={"model": MODEL, "input": ["a" * 16_000] * 140}
        )
        assert not mock.requests
    assert r.status_code == 413


async def test_invalid_json_is_sanitized(emb):
    client, _ = emb
    r = await client.post(
        "/v1/embeddings",
        content=b'{"model": "' + SECRET_TEXT.encode() + b'", ',
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422
    assert SECRET_TEXT not in r.text


# -- routing ------------------------------------------------------------------


@pytest.mark.parametrize(
    "model,status",
    [
        (NATIVE, 404),  # unqualified: no owner guessing
        ("ollama:qwen3-embedding", 404),  # prefix of an allowed identity
        ("ollama:qwen3:4b-instruct", 404),  # generation model, not listed
        ("ghost:" + NATIVE, 404),  # listed but owner not registered
        ("ollama:qwen3-embedding:cloud", 404),  # remote-run tag
        ("cloudy:" + NATIVE, 503),  # registered owner, not a local engine
        ("ollama-lan:" + NATIVE, 503),  # sovereign, but not loopback
        ("ollama-off:" + NATIVE, 503),  # inactive provider
    ],
)
async def test_routing_is_exact_and_local(emb, model, status):
    import plugboard.apikeys as keys

    client, _ = emb
    rec = keys.create_key(
        "argos-synthetic"
    )  # no model restriction: routing alone decides
    with aioresponses() as mock:
        r = await client.post(
            "/v1/embeddings",
            headers={"Authorization": f"Bearer {rec['key']}"},
            json={"model": model, "input": "x"},
        )
        assert not mock.requests
    assert r.status_code == status


async def test_route_closed_without_explicit_identities(emb, monkeypatch):
    client, _ = emb
    monkeypatch.setenv("PLUGBOARD_EMBEDDING_MODELS", "")
    with aioresponses() as mock:
        r = await client.post("/v1/embeddings", json={"model": MODEL, "input": "x"})
        assert not mock.requests
    assert r.status_code == 404


async def test_key_model_rights_apply(emb):
    import plugboard.apikeys as keys

    client, _ = emb
    rec = keys.create_key(
        "argos-synthetic", allowed_models=["ollama:qwen3:4b-instruct"]
    )
    with aioresponses() as mock:
        r = await client.post(
            "/v1/embeddings",
            headers={"Authorization": f"Bearer {rec['key']}"},
            json={"model": MODEL, "input": "x"},
        )
        assert not mock.requests
    assert r.status_code == 403


@pytest.mark.parametrize(
    "base",
    [
        "http://user:pw@127.0.0.1:11434",
        "http://127.0.0.1:11434/proxy",
        "http://127.0.0.1.example.test:11434",
        "http://127.0.0.1:99999",
        "file:///tmp/x",
        "http://[::1]:11434?x=1",
    ],
)
async def test_provider_base_url_must_be_plain_loopback(
    emb, monkeypatch, tmp_path, base
):
    client, _ = emb
    (tmp_path / "q").mkdir()
    _setup_providers(tmp_path / "q", monkeypatch, [{**OLLAMA, "base_url": base}])
    with aioresponses() as mock:
        r = await client.post("/v1/embeddings", json={"model": MODEL, "input": "x"})
        assert not mock.requests
    assert r.status_code == 503


async def test_rpm_limit(emb):
    import plugboard.apikeys as keys

    client, _ = emb
    rec = keys.create_key("argos-synthetic", allowed_models=[MODEL], rpm_limit=1)
    headers = {"Authorization": f"Bearer {rec['key']}"}
    with aioresponses() as mock:
        _ok(mock, [_vec(1)])
        first = await client.post(
            "/v1/embeddings", headers=headers, json={"model": MODEL, "input": "x"}
        )
        second = await client.post(
            "/v1/embeddings", headers=headers, json={"model": MODEL, "input": "x"}
        )
    assert first.status_code == 200
    assert second.status_code == 429


# -- engine errors ------------------------------------------------------------


@pytest.mark.parametrize(
    "setup,status",
    [
        (
            lambda m: (
                m.get(TAGS, payload=_tags()),
                m.post(EMBED, status=500, body="SECRET-UPSTREAM trace"),
            ),
            502,
        ),
        (
            lambda m: (
                m.get(TAGS, payload=_tags()),
                m.post(EMBED, status=400, body='{"error":"SECRET-UPSTREAM ctx"}'),
            ),
            422,
        ),
        (
            lambda m: (
                m.get(TAGS, payload=_tags()),
                m.post(EMBED, status=302, headers={"Location": "http://evil.test/"}),
            ),
            502,
        ),
        (
            lambda m: (
                m.get(TAGS, payload=_tags()),
                m.post(EMBED, exception=asyncio.TimeoutError()),
            ),
            504,
        ),
        (
            lambda m: m.get(
                TAGS, exception=aiohttp.ClientConnectionError("SECRET-UPSTREAM refused")
            ),
            503,
        ),
        (lambda m: m.get(TAGS, status=500, body="SECRET-UPSTREAM"), 502),
        (lambda m: m.get(TAGS, payload=_tags(native="other-embedding:1b")), 404),
        (lambda m: m.get(TAGS, payload={"models": [{"name": NATIVE}]}), 502),
    ],
)
async def test_engine_errors_are_bounded(emb, setup, status, caplog):
    client, _ = emb
    caplog.set_level(logging.INFO, logger="plugboard.embeddings_api")
    with aioresponses() as mock:
        setup(mock)
        r = await client.post(
            "/v1/embeddings", json={"model": MODEL, "input": [SECRET_TEXT]}
        )
    assert r.status_code == status
    assert "SECRET-UPSTREAM" not in r.text and SECRET_TEXT not in r.text
    assert SECRET_TEXT not in caplog.text and "SECRET-UPSTREAM" not in caplog.text


@pytest.mark.parametrize(
    "body",
    [
        json.dumps({"embeddings": [_vec(1)]}),  # 1 vector for 2 inputs
        json.dumps({"embeddings": [_vec(1), _vec(1), _vec(1)]}),
        json.dumps({"embeddings": [_vec(1), _vec(1, dims=5)]}),
        json.dumps({"embeddings": [_vec(1), []]}),
        '{"embeddings": [[0.1, NaN, 0.3, 0.4], [0.1, 0.2, 0.3, 0.4]]}',
        '{"embeddings": [[0.1, Infinity, 0.3, 0.4], [0.1, 0.2, 0.3, 0.4]]}',
        json.dumps({"embeddings": [_vec(1), ["0.1", "0.2", "0.3", "0.4"]]}),
        json.dumps({"embeddings": [_vec(1), [True, False, True, True]]}),
        json.dumps({"embeddings": [_vec(1), [0.0, 0.0, 0.0, 0.0]]}),
        json.dumps({"embeddings": [_vec(1), [[0.1]] * 4]}),
        json.dumps({"embedding": _vec(1)}),
        json.dumps([_vec(1), _vec(2)]),
        "not json",
    ],
)
async def test_malformed_vectors_are_refused(emb, body):
    client, _ = emb
    with aioresponses() as mock:
        mock.get(TAGS, payload=_tags())
        mock.post(EMBED, body=body, content_type="application/json")
        r = await client.post(
            "/v1/embeddings", json={"model": MODEL, "input": ["a", "b"]}
        )
    assert r.status_code == 502
    assert r.json() == {"detail": "Invalid embedding engine response"}


# -- success ------------------------------------------------------------------


async def test_batch_vectors_order_usage_and_logs(emb, caplog):
    import plugboard.usage as usage

    client, rec = emb
    caplog.set_level(logging.INFO, logger="plugboard.embeddings_api")
    vectors = [_vec(1), _vec(2), _vec(3)]
    inputs = [SECRET_TEXT, "deuxième passage", "third"]
    with aioresponses() as mock:
        _ok(mock, vectors, tokens=11)
        r = await client.post("/v1/embeddings", json={"model": MODEL, "input": inputs})
        calls = _embed_calls(mock)
    assert r.status_code == 200, r.text
    data = r.json()
    assert [d["index"] for d in data["data"]] == [0, 1, 2]
    assert [d["embedding"] for d in data["data"]] == vectors
    assert data["object"] == "list" and data["model"] == MODEL
    assert data["usage"] == {"prompt_tokens": 11, "total_tokens": 11}
    assert data["plugboard"] == {
        "provider_id": "ollama",
        "native_model": NATIVE,
        "digest": DIGEST,
        "dimensions": 4,
        "cost_usd": 0.0,
        "usage_recorded": True,
    }
    assert len(calls) == 1
    assert calls[0].kwargs["json"] == {
        "model": NATIVE,
        "input": inputs,
        "truncate": False,
    }
    assert calls[0].kwargs["allow_redirects"] is False
    assert len(r.headers["X-Request-ID"]) == 16

    (entry,) = usage.get_usage(MONTH)
    assert entry["model"] == "embedding:" + NATIVE
    assert entry["provider_id"] == "ollama" and entry["key_id"] == rec["key_id"]
    assert entry["prompt_tokens"] == 11 and entry["completion_tokens"] == 0
    assert entry["cost_usd"] == 0.0 and entry["price_source"] == "local:embeddings"

    assert "embeddings served provider=ollama" in caplog.text
    assert rec["key_id"] in caplog.text
    assert SECRET_TEXT not in caplog.text and rec["key"] not in caplog.text


async def test_single_string_input_and_unrestricted_key(emb):
    import plugboard.apikeys as keys

    client, _ = emb
    rec = keys.create_key("argos-synthetic")
    with aioresponses() as mock:
        _ok(mock, [_vec(5)])
        r = await client.post(
            "/v1/embeddings",
            headers={"Authorization": f"Bearer {rec['key']}"},
            json={"model": MODEL, "input": "seul"},
        )
        calls = _embed_calls(mock)
    assert r.status_code == 200
    assert len(r.json()["data"]) == 1
    assert calls[0].kwargs["json"]["input"] == ["seul"]


async def test_base64_encoding_matches_floats(emb):
    client, _ = emb
    vector = [0.25, -0.5, 0.125, 1.0]
    with aioresponses() as mock:
        _ok(mock, [vector])
        r = await client.post(
            "/v1/embeddings",
            json={"model": MODEL, "input": "x", "encoding_format": "base64"},
        )
    raw = base64.b64decode(r.json()["data"][0]["embedding"])
    assert list(struct.unpack("<4f", raw)) == vector


async def test_missing_token_count_is_estimated(emb):
    client, _ = emb
    with aioresponses() as mock:
        mock.get(TAGS, payload=_tags())
        mock.post(EMBED, payload={"embeddings": [_vec(1)], "prompt_eval_count": -3})
        r = await client.post(
            "/v1/embeddings", json={"model": MODEL, "input": "un deux trois"}
        )
    assert r.status_code == 200
    assert r.json()["usage"]["prompt_tokens"] > 0


# -- GET /v1/embeddings/models ------------------------------------------------


@pytest.mark.parametrize("auth", [None, "Bearer testkey", "Bearer pb_invalid"])
async def test_embedding_models_need_a_client_key(emb, auth):
    client, _ = emb
    del client.headers["Authorization"]
    headers = {"Authorization": auth} if auth else {}
    resp = await client.get("/v1/embeddings/models", headers=headers)
    assert resp.status_code == 401


async def test_embedding_models_lists_only_what_the_key_may_use(emb, monkeypatch):
    import plugboard.embeddings_api as embeddings_api

    monkeypatch.setattr(embeddings_api, "_KNOWN_DIMENSIONS", {})
    client, _ = emb
    resp = await client.get("/v1/embeddings/models")
    assert resp.status_code == 200
    assert resp.json() == {
        "object": "list",
        "data": [
            {
                "id": MODEL,
                "provider_id": "ollama",
                "available": True,
                "dimensions": None,
            }
        ],
    }
    with aioresponses() as mock:
        _ok(mock, [_vec(0.1, 6)])
        assert (
            await client.post("/v1/embeddings", json={"model": MODEL, "input": "x"})
        ).status_code == 200
    resp = await client.get("/v1/embeddings/models")
    assert resp.json()["data"][0]["dimensions"] == 6


async def test_embedding_models_flags_unavailable_routes(emb, monkeypatch):
    import plugboard.apikeys as keys

    client, _ = emb
    rec = keys.create_key("all-models")
    client.headers["Authorization"] = f"Bearer {rec['key']}"
    data = {
        m["id"]: m["available"]
        for m in (await client.get("/v1/embeddings/models")).json()["data"]
    }
    assert data[MODEL] is True
    assert data["ollama-lan:" + NATIVE] is False  # not loopback
    assert data["ollama-off:" + NATIVE] is False  # inactive
    assert "ghost:" + NATIVE not in data  # unregistered owner


async def test_embedding_models_closed_route_is_empty(emb, monkeypatch):
    client, _ = emb
    monkeypatch.setenv("PLUGBOARD_EMBEDDING_MODELS", "")
    resp = await client.get("/v1/embeddings/models")
    assert resp.status_code == 200 and resp.json()["data"] == []


# -- Operator OLLAMA_URL on a private bridge (container deployments) ----------


def test_operator_private_ollama_url_is_a_local_engine(monkeypatch):
    from plugboard import embeddings_api as ea

    monkeypatch.setenv("OLLAMA_URL", "http://172.30.114.1:11434/")
    assert ea._is_loopback_base("http://172.30.114.1:11434")
    # Any other private address, port or scheme stays refused.
    assert not ea._is_loopback_base("http://172.30.114.2:11434")
    assert not ea._is_loopback_base("http://172.30.114.1:11435")
    assert not ea._is_loopback_base("https://172.30.114.1:11434")
    assert not ea._is_loopback_base("http://172.30.114.1:11434/v1")


def test_operator_public_ollama_url_is_not_trusted(monkeypatch):
    from plugboard import embeddings_api as ea

    monkeypatch.setenv("OLLAMA_URL", "http://8.8.8.8:11434")
    assert not ea._is_loopback_base("http://8.8.8.8:11434")
    monkeypatch.setenv("OLLAMA_URL", "http://ollama.example:11434")
    assert not ea._is_loopback_base("http://ollama.example:11434")
    monkeypatch.delenv("OLLAMA_URL")
    assert not ea._is_loopback_base("http://172.30.114.1:11434")


# -- Global concurrency cap ----------------------------------------------------


def test_concurrency_cap_is_bounded_and_released(monkeypatch):
    from plugboard import embeddings_api as ea

    monkeypatch.setenv("PLUGBOARD_EMBEDDING_CONCURRENCY", "2")
    monkeypatch.setattr(ea, "_SLOTS", {})
    assert ea._try_acquire_slot()
    assert ea._try_acquire_slot()
    assert not ea._try_acquire_slot()
    ea._release_slot()
    assert ea._try_acquire_slot()
    ea._release_slot()
    ea._release_slot()
    ea._release_slot()
    assert ea._SLOTS["state"]["busy"] == 0


def test_concurrency_env_is_clamped(monkeypatch):
    from plugboard import embeddings_api as ea

    for raw in ("0", "-3", "999", "abc"):
        monkeypatch.setenv("PLUGBOARD_EMBEDDING_CONCURRENCY", raw)
        assert ea._concurrency() == ea.DEFAULT_CONCURRENCY
    monkeypatch.setenv("PLUGBOARD_EMBEDDING_CONCURRENCY", "4")
    assert ea._concurrency() == 4
