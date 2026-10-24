"""Parent counterexamples from the static security review of the decision lot.

Engine and transport ports are synthetic; no inference, sockets or providers.
"""

import asyncio
import email.message
import io
import re
import urllib.request
import urllib.response
from pathlib import Path

import pytest

from tests.test_decision_api import ANSWERS, REQUEST

ROOT = Path(__file__).resolve().parents[1]


async def _key(client, monkeypatch, tmp_path, **kwargs):
    import plugboard.apikeys as keys

    monkeypatch.setattr(keys, "KEYS_FILE", tmp_path / "keys.json")
    monkeypatch.setattr(keys, "KEYS_LOCK", tmp_path / "keys.lock")
    keys._invalidate_cache()
    rec = keys.create_key("synthetic-client", **kwargs)
    return {"Authorization": f"Bearer {rec['key']}"}, rec


def _jev_ready(monkeypatch, calls):
    from plugboard import decision_api

    monkeypatch.setenv("PLUGBOARD_JEV_API_KEY", "synthetic-not-a-real-key")

    async def engine(model, payload):
        calls.append(model)
        return ANSWERS

    monkeypatch.setattr(decision_api, "dispatch", engine)


def _month_cost(key_id):
    from plugboard import billing, usage

    return usage.get_total_cost_by_key(billing.current_pricing_month(), key_id)


def _rows(key_id):
    from plugboard import billing, usage

    return usage.get_usage_by_key(billing.current_pricing_month(), key_id)


# --- NEW medium: billed Jev must never run without a budget-compatible price ---
# Per-call price oracles superseded by the user pricing decision (registre #111):
# Jev is billed at the official per-input-token price, see test_pricing_decision_parent.py.


async def test_failed_vendor_call_is_not_billed(client, monkeypatch, tmp_path):
    from plugboard import decision_api
    from plugboard.decisions import DecisionUnavailable

    headers, rec = await _key(
        client,
        monkeypatch,
        tmp_path,
        monthly_limit_usd=10.0,
        allowed_decision_models=["jev"],
    )
    monkeypatch.setenv("PLUGBOARD_JEV_API_KEY", "synthetic-not-a-real-key")
    monkeypatch.setenv("PLUGBOARD_JEV_PRICE_PER_CALL_USD", "0.5")

    async def down(model, payload):
        raise DecisionUnavailable()

    monkeypatch.setattr(decision_api, "dispatch", down)
    response = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert response.status_code == 503
    assert _month_cost(rec["key_id"]) == 0


# --- NEW low: one key must not monopolize the single decision slot ---


async def test_second_key_waits_fairly_instead_of_immediate_rejection(
    client, monkeypatch, tmp_path
):
    from plugboard import decision_api

    headers_a, _ = await _key(
        client, monkeypatch, tmp_path, allowed_decision_models=["jev"]
    )
    import plugboard.apikeys as keys

    rec_b = keys.create_key("other-client", allowed_decision_models=["jev"])
    headers_b = {"Authorization": f"Bearer {rec_b['key']}"}
    monkeypatch.setenv("PLUGBOARD_JEV_API_KEY", "synthetic-not-a-real-key")
    release = asyncio.Event()
    started = []

    async def engine(model, payload):
        started.append(payload["state"])
        if len(started) == 1:
            await release.wait()
        return ANSWERS

    monkeypatch.setattr(decision_api, "dispatch", engine)
    first = asyncio.create_task(
        client.post("/v1/decisions", headers=headers_a, json=REQUEST)
    )
    while not started:
        await asyncio.sleep(0)
    same_key = await client.post("/v1/decisions", headers=headers_a, json=REQUEST)
    assert same_key.status_code == 429  # per-key concurrency stays bounded
    second = asyncio.create_task(
        client.post("/v1/decisions", headers=headers_b, json=REQUEST)
    )
    await asyncio.sleep(0.05)
    assert not second.done()  # queued, not rejected while the slot is busy
    release.set()
    assert (await first).status_code == 200
    assert (await second).status_code == 200
    assert len(started) == 2


# --- NEW low: sync SDK must not follow redirects carrying the bearer ---


@pytest.mark.parametrize("operation", ["list_decision_models", "decide"])
def test_sync_sdk_refuses_redirect_and_keeps_bearer_on_origin(monkeypatch, operation):
    import plugboard_sdk.client as sdk

    seen = []

    def fake_open(self, req):
        seen.append((req.host, req.get_header("Authorization")))
        headers = email.message.Message()
        headers["Location"] = "http://third-party.invalid/steal"
        response = urllib.response.addinfourl(
            io.BytesIO(b""), headers, req.full_url, 302
        )
        response.msg = "Found"
        return response

    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", fake_open)
    client = sdk.PlugboardClient(
        "pb_synthetic_secret", base_url="http://origin.invalid"
    )
    with pytest.raises(sdk.PlugboardError) as error:
        if operation == "decide":
            client.decide(
                "jev", "state", {"q": {"type": "noul", "instructions": "synthetic"}}
            )
        else:
            client.list_decision_models()
    assert [host for host, _ in seen] == ["origin.invalid"]
    assert "pb_synthetic_secret" not in str(error.value)


# --- NEW deploy defect: image must contain the decision manifests it loads ---


def test_image_ships_decision_manifests():
    dockerfile = (ROOT / "Dockerfile").read_text()
    stages = dockerfile.split("\nFROM ")
    code_stage = next(
        stage
        for stage in stages
        if re.search(r"^COPY\b.*\bplugboard/\s+plugboard/", stage, re.M)
    )
    assert re.search(r"^COPY\b.*\bmanifests/?\s+manifests/?\s*$", code_stage, re.M), (
        "manifests/ missing beside runtime code"
    )
    ignored = [
        line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()
    ]
    assert not any(
        line.rstrip("/") in {"manifests", "manifests/*", "manifests/decision-configs"}
        for line in ignored
    )
