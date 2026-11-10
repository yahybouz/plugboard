"""/v1/decisions answers 429 when TypeSafe's quota is exhausted (BUG-36).

Before the fix, a TypeSafe HTTP 429 became the generic 502 "Invalid decision
engine response". The route now answers as /v1/chat/completions does since
BUG-26: 429, a reason composed by the gateway, Retry-After and retry_after
when TypeSafe gave a delay, never the upstream body. Nothing is billed: the
engine did not take the call. Fake HTTP only, never real inference.
"""

from __future__ import annotations

import pytest
from aioresponses import aioresponses

from plugboard.decisions import JEV_URL

REQUEST = {
    "model": "jev",
    "state": "Synthetic state.",
    "questions": {"ok": {"type": "noul", "instructions": "Is it fine?"}},
}
_UPSTREAM_BODY = '{"error": {"message": "upstream-secret org-42 quota"}}'


@pytest.fixture
async def jev_client(client, monkeypatch, tmp_path):
    """(client with a jev-only client key, list of billed calls)."""
    import plugboard.apikeys as keys
    from plugboard import decision_api

    monkeypatch.setattr(keys, "KEYS_FILE", tmp_path / "keys.json")
    monkeypatch.setattr(keys, "KEYS_LOCK", tmp_path / "keys.lock")
    keys._invalidate_cache()
    rec = keys.create_key("synthetic", allowed_decision_models=["jev"])
    client.headers["Authorization"] = f"Bearer {rec['key']}"
    monkeypatch.setenv("PLUGBOARD_JEV_API_KEY", "fake-test-key")
    billed = []

    def record(record, model, raw):
        billed.append(raw)
        return {"status": "billed", "cost_usd": 0.0}

    monkeypatch.setattr(decision_api, "_record_decision", record)
    yield client, billed
    keys._invalidate_cache()


async def test_typesafe_429_with_retry_after_gives_429(jev_client):
    client, billed = jev_client
    with aioresponses() as mock:
        mock.post(
            JEV_URL, status=429, body=_UPSTREAM_BODY, headers={"Retry-After": "120"}
        )
        resp = await client.post("/v1/decisions", json=REQUEST)

    assert resp.status_code == 429, resp.text
    assert resp.headers["Retry-After"] == "120"
    body = resp.json()
    assert body["retry_after"] == 120
    assert "quota" in body["detail"].lower()
    assert "typesafe" in body["detail"]
    assert "120 seconds" in body["detail"]
    # Composed by the gateway: nothing of the upstream body or the server key.
    assert "upstream-secret" not in resp.text and "org-42" not in resp.text
    assert "fake-test-key" not in resp.text
    assert billed == []


async def test_typesafe_429_without_retry_after_has_no_delay(jev_client):
    client, billed = jev_client
    with aioresponses() as mock:
        mock.post(JEV_URL, status=429, body=_UPSTREAM_BODY)
        resp = await client.post("/v1/decisions", json=REQUEST)

    assert resp.status_code == 429, resp.text
    assert "Retry-After" not in resp.headers
    body = resp.json()
    assert "retry_after" not in body
    assert "quota" in body["detail"].lower()
    assert "upstream-secret" not in resp.text
    assert billed == []


async def test_key_can_decide_again_after_a_429(jev_client):
    """The 429 frees the key's in-flight place and the engine slot."""
    client, _ = jev_client
    with aioresponses() as mock:
        mock.post(JEV_URL, status=429, body=_UPSTREAM_BODY)
        mock.post(
            JEV_URL, status=200, body='{"answers":{"ok":{"type":"noul","noul":0.8}}}'
        )
        first = await client.post("/v1/decisions", json=REQUEST)
        second = await client.post("/v1/decisions", json=REQUEST)

    assert first.status_code == 429, first.text
    assert second.status_code == 200, second.text
    assert second.json()["answers"] == {"ok": {"type": "noul", "noul": 0.8}}


@pytest.mark.parametrize("status", [401, 500, 503])
async def test_other_typesafe_errors_stay_502(jev_client, status):
    client, billed = jev_client
    with aioresponses() as mock:
        mock.post(JEV_URL, status=status, body=_UPSTREAM_BODY)
        resp = await client.post("/v1/decisions", json=REQUEST)

    assert resp.status_code == 502, resp.text
    assert resp.json() == {"detail": "Invalid decision engine response"}
    assert billed == []
