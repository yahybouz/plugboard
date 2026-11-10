"""An exhausted provider quota answers 429 with a readable reason (BUG-26).

Before the fix, an upstream HTTP 429 (after retries and fallback) and a
subscription CLI that failed on its quota both reached the client as a
generic 502. A client key now gets 429, a reason composed by the gateway
(provider + quota/limit exhausted + delay when the upstream gives one) and
never the upstream body or the CLI's stderr, pid or paths; the admin keeps
the details. A fallback provider that answers still wins (200).
"""

from __future__ import annotations

import pytest
from aioresponses import aioresponses
from yarl import URL

from tests.factories import FAKE_COMPLETION

_A = "http://fake-llm.test/v1/chat/completions"
_B = "http://other-llm.test/v1/chat/completions"
# Unqualified on purpose: both providers are candidates, so the fallback runs.
_MODEL = "fake:model-v1"
_UPSTREAM_BODY = '{"error": {"message": "upstream-secret org-42 quota"}}'
ADMIN = {"Authorization": "Bearer testkey"}  # the admin key in the fixture


@pytest.fixture
def no_retry_delay(monkeypatch):
    import plugboard.router as router

    monkeypatch.setattr(router, "_DELAYS", [0, 0, 0])


@pytest.fixture
def a_first(monkeypatch):
    """test-provider is the preferred candidate, other-provider the fallback."""
    import plugboard.router as router

    monkeypatch.setattr(
        router.metrics,
        "get_stats",
        lambda: {
            "test-provider": {"latency_avg": 0.1, "requests": 100, "errors": 0},
            "other-provider": {"latency_avg": 2.0, "requests": 100, "errors": 0},
        },
    )
    monkeypatch.setattr(router, "_IN_FLIGHT_BY_PROVIDER", {})


@pytest.fixture
def client_key(tmp_path, monkeypatch):
    """Authorization header of a plain client key (not the admin)."""
    import plugboard.apikeys as ak
    from plugboard import app as app_mod

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    monkeypatch.setattr(app_mod, "_check_price_known", lambda *a, **k: None)
    key = ak.create_key("quota-client", "test")["key"]
    return {"Authorization": f"Bearer {key}"}


def _chat(stream: bool = False) -> dict:
    return {
        "model": _MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": stream,
        # A cached answer from another test would skip the provider call.
        "cache": False,
    }


def _calls(mock, url: str) -> int:
    return len(mock.requests.get(("POST", URL(url)), []))


# ── HTTP provider answers 429 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upstream_429_with_retry_after_gives_429_to_a_client_key(
    client, client_key, a_first, no_retry_delay
):
    with aioresponses() as mock:
        for url in (_A, _B):
            mock.post(
                url,
                status=429,
                body=_UPSTREAM_BODY,
                headers={"Retry-After": "120"},
                repeat=True,
            )
        resp = await client.post(
            "/v1/chat/completions", headers=client_key, json=_chat()
        )
        # The fallback provider was tried before giving up.
        assert _calls(mock, _A) >= 1 and _calls(mock, _B) >= 1

    assert resp.status_code == 429, resp.text
    assert resp.headers["Retry-After"] == "120"
    body = resp.json()
    assert body["retry_after"] == 120
    assert "quota" in body["detail"].lower()
    assert "other-provider" in body["detail"]
    assert "120 seconds" in body["detail"]
    # Composed by the gateway: nothing of the upstream body.
    assert "upstream-secret" not in resp.text and "org-42" not in resp.text


@pytest.mark.asyncio
async def test_upstream_429_without_retry_after_has_no_delay(
    client, client_key, a_first, no_retry_delay
):
    with aioresponses() as mock:
        for url in (_A, _B):
            mock.post(url, status=429, body=_UPSTREAM_BODY, repeat=True)
        resp = await client.post(
            "/v1/chat/completions", headers=client_key, json=_chat()
        )

    assert resp.status_code == 429, resp.text
    assert "Retry-After" not in resp.headers
    body = resp.json()
    assert "retry_after" not in body
    assert "quota" in body["detail"].lower()
    assert "upstream-secret" not in resp.text


@pytest.mark.asyncio
async def test_upstream_429_http_date_retry_after_becomes_seconds(
    client, client_key, a_first, no_retry_delay
):
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone

    when = format_datetime(
        datetime.now(timezone.utc) + timedelta(seconds=600), usegmt=True
    )
    with aioresponses() as mock:
        for url in (_A, _B):
            mock.post(
                url,
                status=429,
                body=_UPSTREAM_BODY,
                headers={"Retry-After": when},
                repeat=True,
            )
        resp = await client.post(
            "/v1/chat/completions", headers=client_key, json=_chat()
        )

    assert resp.status_code == 429, resp.text
    assert 590 <= resp.json()["retry_after"] <= 600
    assert resp.headers["Retry-After"] == str(resp.json()["retry_after"])


@pytest.mark.asyncio
async def test_upstream_429_keeps_the_upstream_detail_for_the_admin(
    client, a_first, no_retry_delay
):
    with aioresponses() as mock:
        for url in (_A, _B):
            mock.post(
                url,
                status=429,
                body=_UPSTREAM_BODY,
                headers={"Retry-After": "30"},
                repeat=True,
            )
        resp = await client.post("/v1/chat/completions", headers=ADMIN, json=_chat())

    assert resp.status_code == 429, resp.text
    assert resp.headers["Retry-After"] == "30"
    assert "upstream-secret" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_streamed_request_upstream_429_gives_429(client, client_key):
    with aioresponses() as mock:
        mock.post(
            _A,
            status=429,
            body=_UPSTREAM_BODY,
            headers={"Retry-After": "45"},
        )
        resp = await client.post(
            "/v1/chat/completions",
            headers=client_key,
            json={**_chat(stream=True), "model": "test-provider:fake:model-v1"},
        )

    assert resp.status_code == 429, resp.text
    assert resp.headers["Retry-After"] == "45"
    assert "test-provider" in resp.json()["detail"]
    assert "upstream-secret" not in resp.text


@pytest.mark.asyncio
async def test_fallback_provider_answers_after_a_429(
    client, client_key, a_first, no_retry_delay
):
    with aioresponses() as mock:
        mock.post(_A, status=429, body=_UPSTREAM_BODY, repeat=True)
        mock.post(_B, payload=FAKE_COMPLETION, status=200)
        resp = await client.post(
            "/v1/chat/completions", headers=client_key, json=_chat()
        )
        assert _calls(mock, _A) >= 1 and _calls(mock, _B) == 1

    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["message"]["content"] == "Hello!"


# ── Subscription CLI fails on its quota ───────────────────────────────────

# Formats observed in the repository (tests/test_cli_subprocess.py and
# tests/test_gemini_nonzero_exit_recovery.py), not guessed.
_CLI_QUOTA_OUTPUTS = [
    pytest.param(
        "claude",
        b'{"type":"error","message":"rate limit exceeded"}\n',
        id="claude-rate-limit",
    ),
    pytest.param(
        "agy",
        b'{"status":"ERROR","response":"","error":"quota exceeded"}',
        id="agy-quota",
    ),
    pytest.param(
        "gemini",
        b'{"error": {"code": 429, "message": "Resource exhausted"}}',
        id="gemini-resource-exhausted",
    ),
]
_STDERR = b"429 Resource exhausted /home/op/.config token=abc123\n"


@pytest.fixture
def cli_fails(monkeypatch):
    """forward() runs the real CLI wrapper on a fake process that fails."""
    from unittest.mock import AsyncMock, MagicMock

    from plugboard import app as app_mod
    from plugboard import cli_subprocess as cs

    state: dict = {}

    def _setup(kind: str, stdout: bytes) -> None:
        proc = MagicMock()
        proc.returncode = 1
        proc.pid = 4242
        proc.communicate = AsyncMock(return_value=(stdout, _STDERR))
        monkeypatch.setattr(cs, "_resolve_binary", lambda cmd: f"/fake/bin/{cmd}")
        monkeypatch.setattr(
            cs.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
        )
        state["kind"] = kind

    async def _forward(body, **_kwargs):
        return await cs.complete(state["kind"], {**body, "model": "some-model"})

    monkeypatch.setattr(app_mod, "forward", _forward)
    return _setup


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "stdout"), _CLI_QUOTA_OUTPUTS)
async def test_cli_quota_error_gives_429_without_process_details(
    client, client_key, cli_fails, kind, stdout
):
    cli_fails(kind, stdout)
    resp = await client.post("/v1/chat/completions", headers=client_key, json=_chat())

    assert resp.status_code == 429, resp.text
    body = resp.json()
    assert "quota" in body["detail"].lower()
    assert kind in body["detail"]
    for leak in ("/home/op", "token=abc123", "4242", "stderr", "exit_code"):
        assert leak not in resp.text


@pytest.mark.asyncio
async def test_cli_quota_error_keeps_details_for_the_admin(client, cli_fails):
    cli_fails("claude", b'{"type":"error","message":"rate limit exceeded"}\n')
    resp = await client.post("/v1/chat/completions", headers=ADMIN, json=_chat())

    assert resp.status_code == 429, resp.text
    body = resp.json()
    assert body["pid"] == 4242
    assert "rate limit exceeded" in body["detail"]


@pytest.mark.asyncio
async def test_cli_error_other_than_quota_stays_502(client, client_key, cli_fails):
    cli_fails("claude", b'{"type":"error","message":"model overloaded"}\n')
    resp = await client.post("/v1/chat/completions", headers=client_key, json=_chat())

    assert resp.status_code == 502, resp.text
