"""/v1/embeddings spec-review regressions (F3, F4): extreme engine values.

An integer too large for a float, or a finite value outside float32 range
asked as base64, must end in the bounded 502 used for any invalid engine
response, never an internal error, and an unserved call records no usage.
The engine is mocked with aioresponses (unmatched URLs fail), never called.
"""

import base64
import struct

import pytest
from aioresponses import aioresponses

import tests.test_embeddings_api as base

# Same fixture as the main suite: real ASGI client, disposable key and usage stores.
emb = base.emb

INVALID = {"detail": "Invalid embedding engine response"}


async def _post(client, body):
    try:
        return await client.post("/v1/embeddings", json=body)
    except Exception as exc:  # noqa: BLE001 - an escaped error is the defect
        pytest.fail(f"internal error escaped the route: {type(exc).__name__}")


def _usage_rows():
    import plugboard.usage as usage

    return usage.get_usage(base.MONTH)


async def test_integer_beyond_float_range_is_bounded_502(emb):
    client, _ = emb
    huge = "1" + "0" * 400  # 10**400: valid JSON integer, float() overflows
    with aioresponses() as mock:
        mock.get(base.TAGS, payload=base._tags())
        mock.post(
            base.EMBED,
            body='{"embeddings": [[1, 2, 3, ' + huge + ']], "prompt_eval_count": 7}',
            content_type="application/json",
        )
        r = await _post(client, {"model": base.MODEL, "input": "x"})
    assert r.status_code == 502
    assert r.json() == INVALID
    assert huge not in r.text
    assert _usage_rows() == []


async def test_value_beyond_float32_as_base64_is_bounded_502_without_usage(emb):
    client, _ = emb
    with aioresponses() as mock:
        base._ok(mock, [[1e39, 0.1, 0.2, 0.3]])
        r = await _post(
            client, {"model": base.MODEL, "input": "x", "encoding_format": "base64"}
        )
    assert r.status_code == 502
    assert r.json() == INVALID
    assert _usage_rows() == []


# -- controls: a normal vector is still served and recorded once ---------------

VECTOR = [0.25, -0.5, 0.125, 1.0]  # exact in float32


def _assert_one_usage_row(key_id):
    (entry,) = _usage_rows()
    assert entry["model"] == "embedding:" + base.NATIVE
    assert entry["provider_id"] == "ollama" and entry["key_id"] == key_id
    assert entry["prompt_tokens"] == 7 and entry["completion_tokens"] == 0
    assert entry["cost_usd"] == 0.0 and entry["price_source"] == "local:embeddings"


async def test_normal_vector_as_float_is_served_and_recorded_once(emb):
    client, rec = emb
    with aioresponses() as mock:
        base._ok(mock, [VECTOR])
        r = await _post(client, {"model": base.MODEL, "input": "x"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["data"] == [{"object": "embedding", "index": 0, "embedding": VECTOR}]
    assert data["usage"] == {"prompt_tokens": 7, "total_tokens": 7}
    assert data["plugboard"]["usage_recorded"] is True
    assert data["plugboard"]["dimensions"] == 4
    _assert_one_usage_row(rec["key_id"])


async def test_normal_vector_as_base64_is_served_and_recorded_once(emb):
    client, rec = emb
    with aioresponses() as mock:
        base._ok(mock, [VECTOR])
        r = await _post(
            client, {"model": base.MODEL, "input": "x", "encoding_format": "base64"}
        )
    assert r.status_code == 200, r.text
    data = r.json()
    assert [d["index"] for d in data["data"]] == [0]
    raw = base64.b64decode(data["data"][0]["embedding"])
    assert list(struct.unpack("<4f", raw)) == VECTOR
    assert data["usage"] == {"prompt_tokens": 7, "total_tokens": 7}
    assert data["plugboard"]["usage_recorded"] is True
    _assert_one_usage_row(rec["key_id"])
