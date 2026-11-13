"""PLU-27: a chat reads and decrypts providers.json at most once (none while
the file is unchanged), runs its SQLite reads and writes (usage, cache) off
the event loop, never waits for an alert webhook, and ``gh auth token``
runs off the loop too."""

import asyncio
import json
import sqlite3
import subprocess
import threading
import time

import pytest
from aioresponses import CallbackResult, aioresponses

from plugboard import alerts, cli_auth
from plugboard import config as cfg
from tests.factories import FAKE_COMPLETION, FAKE_STREAM_BODY, PROVIDER_A, PROVIDER_B

URL_A = "http://fake-llm.test/v1/chat/completions"
MODEL = "test-provider:fake:model-v1"


def _body(**extra):
    return {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], **extra}


@pytest.fixture
def budget_key(client, tmp_path, monkeypatch):
    import plugboard.apikeys as ak
    import plugboard.billing as b
    import plugboard.cache as c
    import plugboard.usage as u

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    monkeypatch.setattr(u, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(u, "_DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(c, "_DB_PATH", tmp_path / "cache.db")
    match = b.PriceMatch({"input_price": 1.0, "output_price": 2.0}, "m", "models")
    monkeypatch.setattr(b, "resolve_price", lambda *a, **k: match)
    rec = ak.create_key("acme", "m", monthly_limit_usd=100.0)
    return {"Authorization": f"Bearer {rec['key']}"}


def _count(monkeypatch, module, name):
    calls = []
    real = getattr(module, name)

    def wrapper(*a, **k):
        calls.append(threading.current_thread() is threading.main_thread())
        return real(*a, **k)

    monkeypatch.setattr(module, name, wrapper)
    return calls


# ── providers.json ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
async def test_a_chat_reads_providers_json_at_most_once(
    client, budget_key, monkeypatch, stream
):
    reads = _count(monkeypatch, cfg, "_read")
    with aioresponses() as mock:
        if stream:
            mock.post(URL_A, body=FAKE_STREAM_BODY, repeat=True)
        else:
            mock.post(URL_A, payload=FAKE_COMPLETION, repeat=True)
        for _ in range(2):
            r = await client.post(
                "/v1/chat/completions",
                headers=budget_key,
                json=_body(stream=stream, cache=False),
            )
            assert r.status_code == 200, r.text
    assert len(reads) <= 1  # 11 or 12 per chat before PLU-27


def test_the_cache_follows_the_file(_providers_file):
    first = cfg.load_providers()
    assert [p["id"] for p in first] == ["test-provider", "other-provider"]
    first[0]["name"] = "changed by a caller"  # callers get their own copy
    assert cfg.load_providers()[0]["name"] == PROVIDER_A["name"]
    # Rewritten in place, same size, same instant: the content decides.
    data = json.loads(_providers_file.read_text())
    data["providers"][0]["name"] = PROVIDER_A["name"].upper()
    _providers_file.write_text(json.dumps(data))
    assert cfg.load_providers()[0]["name"] == PROVIDER_A["name"].upper()


def test_tokens_come_back_decrypted_from_the_cache(_providers_file):
    record = {**PROVIDER_B, "bearer_token": cfg._encrypt_token("sk-test-only")}
    _providers_file.write_text(json.dumps({"providers": [record]}))
    assert cfg.load_providers()[0]["bearer_token"] == "sk-test-only"
    assert cfg.load_providers()[0]["bearer_token"] == "sk-test-only"


def test_a_write_through_config_is_seen_at_once(_providers_file):
    cfg.load_providers()
    created = cfg.create_provider("Cached", "http://cached.test", "")
    assert created["id"] in [p["id"] for p in cfg.load_providers()]


# ── SQLite off the event loop ────────────────────────────────────────────────


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
async def test_usage_sqlite_runs_off_the_event_loop(
    client, budget_key, monkeypatch, stream
):
    connects = _count(monkeypatch, sqlite3, "connect")
    with aioresponses() as mock:
        if stream:
            mock.post(URL_A, body=FAKE_STREAM_BODY)
        else:
            mock.post(URL_A, payload=FAKE_COMPLETION)
        r = await client.post(
            "/v1/chat/completions", headers=budget_key, json=_body(stream=stream)
        )
    assert r.status_code == 200, r.text
    assert connects and not any(connects)  # every connection in a worker thread


async def test_cache_sqlite_runs_off_the_event_loop(client, tmp_path, monkeypatch):
    import plugboard.cache as c

    monkeypatch.setattr(c, "_DB_PATH", tmp_path / "cache.db")
    connects = _count(monkeypatch, sqlite3, "connect")
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION)
        first = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer testkey"},
            json=_body(messages=[{"role": "user", "content": "cache me off the loop"}]),
        )
        hit = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer testkey"},
            json=_body(messages=[{"role": "user", "content": "cache me off the loop"}]),
        )
    assert first.status_code == hit.status_code == 200
    assert hit.headers["X-Cache"] == "HIT"
    assert connects and not any(connects)


async def test_a_slow_disk_no_longer_stalls_the_loop(client, budget_key, monkeypatch):
    real_connect = sqlite3.connect

    def slow_connect(*a, **k):
        time.sleep(0.2)  # disk contention
        return real_connect(*a, **k)

    monkeypatch.setattr(sqlite3, "connect", slow_connect)
    gaps = []

    async def ticker():
        last = time.perf_counter()
        while True:
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    tick = asyncio.create_task(ticker())
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION)
        r = await client.post("/v1/chat/completions", headers=budget_key, json=_body())
    tick.cancel()
    assert r.status_code == 200
    assert max(gaps) < 0.15  # was about 0.2 per connection, on the loop


# ── Alerts and gh ────────────────────────────────────────────────────────────


async def test_a_slow_alert_webhook_does_not_hold_the_request(client, monkeypatch):
    monkeypatch.setattr(alerts, "_WEBHOOK_URL", "http://hook.test/alert")
    monkeypatch.setattr(alerts, "_ALERT_THRESHOLD", 1)
    monkeypatch.setattr(alerts, "_provider_failures", {})
    monkeypatch.setattr(alerts, "_provider_alerted", {})
    monkeypatch.setattr("plugboard.router._DELAYS", [])
    sent = asyncio.Event()

    async def slow_hook(url, **kwargs):
        await asyncio.sleep(1.0)
        sent.set()
        return CallbackResult(status=200, payload={})

    with aioresponses() as mock:
        mock.post(URL_A, status=500, body="down")
        mock.post("http://hook.test/alert", callback=slow_hook)
        t0 = time.perf_counter()
        r = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer testkey"},
            json=_body(cache=False),
        )
        elapsed = time.perf_counter() - t0
        assert r.status_code == 502
        assert elapsed < 0.8  # was 1.0 s: the request waited for the webhook
        await asyncio.wait_for(sent.wait(), 3)  # the alert still goes out


async def test_gh_auth_token_runs_off_the_loop(monkeypatch):
    threads = []

    def run(*a, **k):
        threads.append(threading.current_thread() is threading.main_thread())
        return subprocess.CompletedProcess(a[0], 0, stdout="gho_test_only\n", stderr="")

    monkeypatch.setattr(cli_auth.subprocess, "run", run)
    bundle = await cli_auth.GHAuthLoader().get_token()
    assert bundle is not None and bundle.access_token == "gho_test_only"
    assert threads == [False]


# ── Worker threads open the databases at once ────────────────────────────────


@pytest.mark.parametrize("store", ["usage", "cache"])
def test_a_fresh_database_set_up_from_many_threads(tmp_path, monkeypatch, store):
    """The first connections of a process come from several threads at once:
    the schema is set up once, nobody gets "database is locked"."""
    import plugboard.cache as c
    import plugboard.usage as u

    module = u if store == "usage" else c
    monkeypatch.setattr(module, "_DB_PATH", tmp_path / f"{store}.db")
    monkeypatch.setattr(module, "_schema_ready", {})
    errors = []
    start = threading.Barrier(12)

    def use():
        start.wait()
        try:
            if store == "usage":
                u.get_total_cost_by_key("2026_11", "k")
                u.append_usage("c", "m", "p", 1, 1, "2026_11", 0.1, key_id="k")
            else:
                c.get_cached("k" * 64, client_id="c")
        except Exception as exc:  # noqa: BLE001 - collected for the assertion
            errors.append(repr(exc))

    threads = [threading.Thread(target=use) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
