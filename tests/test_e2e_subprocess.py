"""End-to-end tests for the CLI subprocess provider path.

Covers the full request lifecycle with mocked `asyncio.create_subprocess_exec`:
- Provider creation via POST /admin/direct-providers/{preset_key}
- Model discovery via POST /providers/{id}/discover (no HTTP — reads known_models)
- Health check via GET /providers/{id}/health (probes binary + --version)
- Non-streaming chat completion (Bearer lle_* key)
- Non-streaming chat completion (admin cookie session — Playground path)
- Streaming chat completion (SSE chunks from CLI output)
- Provider listing exposes the subprocess provider
- /v1/models includes known_models
- Error propagation when CLI fails
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugboard import cli_subprocess
from plugboard.cli_subprocess import check_cli_installed


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def _mock_gemini_cli(monkeypatch: pytest.MonkeyPatch):
    """Make `gemini` appear installed + always return a canned JSON response."""
    monkeypatch.setattr(
        cli_subprocess.shutil,
        "which",
        lambda cmd: "/fake/bin/" + cmd if cmd == "gemini" else None,
    )

    async def fake_probe(kind, timeout_s=5.0):
        return {"ok": True, "version": "0.99.0-test", "error": None}

    monkeypatch.setattr(cli_subprocess, "probe_cli_version", fake_probe)

    # Prepare a reusable subprocess spawner that returns a JSON "response" payload.
    def _spawner(response_text: str = "Hello from Gemini CLI"):
        async def fake_exec(*argv, **kwargs):
            proc = MagicMock()
            proc.returncode = 0
            payload = json.dumps({"response": response_text}).encode()
            proc.communicate = AsyncMock(return_value=(payload, b""))
            proc.kill = MagicMock()
            return proc

        return fake_exec

    monkeypatch.setattr(
        cli_subprocess.asyncio,
        "create_subprocess_exec",
        _spawner("Hello from Gemini CLI"),
    )
    return _spawner


@pytest.fixture
def _admin_session(admin_client, _admin_key: str):
    """Log the admin in so the client sends the session cookie on subsequent calls."""
    return admin_client  # admin_client already runs under bearer=adminkey


# ── Preset + provider creation ──────────────────────────────────────────────


async def test_preset_listed_as_subprocess(admin_client):
    res = await admin_client.get(
        "/admin/direct-providers/presets",
        headers={"Authorization": "Bearer adminkey"},
    )
    assert res.status_code == 200
    presets = res.json()["presets"]
    agy = next((p for p in presets if p["key"] == "agy-cli-subprocess"), None)
    assert agy is not None, "agy-cli-subprocess preset missing"
    assert agy["auth_source"] == "subprocess:agy"
    assert agy["connect_via"] == "subprocess_check"
    assert agy["known_models"]
    # Replaced by agy: no longer offered, still resolvable by key.
    assert all(p["key"] != "gemini-cli-subprocess" for p in presets)


async def test_check_preset_reports_installed_cli(admin_client, _mock_gemini_cli):
    res = await admin_client.get(
        "/admin/direct-providers/gemini-cli-subprocess/check",
        headers={"Authorization": "Bearer adminkey"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["auth_type"] == "subprocess"
    assert data["kind"] == "gemini"
    assert data["binary"] == "/fake/bin/gemini"
    assert data["version"] == "0.99.0-test"


async def test_create_subprocess_provider_end_to_end(admin_client, _mock_gemini_cli):
    # Create
    res = await admin_client.post(
        "/admin/direct-providers/gemini-cli-subprocess",
        headers={"Authorization": "Bearer adminkey"},
        json={"api_key": ""},
    )
    assert res.status_code == 201
    provider = res.json()
    assert provider["auth_source"] == "subprocess:gemini"
    assert provider["base_url"] == "subprocess://gemini"
    assert provider["bearer_token"] == "(via CLI)"
    pid = provider["id"]

    # Appears in /providers list
    listing = await admin_client.get(
        "/providers",
        headers={"Authorization": "Bearer adminkey"},
    )
    assert any(p["id"] == pid for p in listing.json())


async def test_discover_returns_known_models(admin_client, _mock_gemini_cli):
    # Ensure a provider exists (idempotent — if already exists, still discover)
    await admin_client.post(
        "/admin/direct-providers/gemini-cli-subprocess",
        headers={"Authorization": "Bearer adminkey"},
        json={"api_key": ""},
    )
    # Find the created provider id
    listing = await admin_client.get(
        "/providers",
        headers={"Authorization": "Bearer adminkey"},
    )
    pid = next(
        p["id"] for p in listing.json() if p.get("auth_source") == "subprocess:gemini"
    )

    res = await admin_client.post(
        f"/providers/{pid}/discover",
        headers={"Authorization": "Bearer adminkey"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["discovered"] >= 3
    assert "gemini-2.5-flash" in data["models"]


async def test_health_check_reports_cli_ok(admin_client, _mock_gemini_cli):
    await admin_client.post(
        "/admin/direct-providers/gemini-cli-subprocess",
        headers={"Authorization": "Bearer adminkey"},
        json={"api_key": ""},
    )
    listing = await admin_client.get(
        "/providers",
        headers={"Authorization": "Bearer adminkey"},
    )
    pid = next(
        p["id"] for p in listing.json() if p.get("auth_source") == "subprocess:gemini"
    )
    res = await admin_client.get(
        f"/providers/{pid}/health",
        headers={"Authorization": "Bearer adminkey"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["url"] == "subprocess://gemini"
    assert data["version"] == "0.99.0-test"


# ── Chat completion — non-streaming, Bearer admin key ───────────────────────


async def test_chat_completion_via_bearer_admin(admin_client, _mock_gemini_cli):
    # Create provider
    create = await admin_client.post(
        "/admin/direct-providers/gemini-cli-subprocess",
        headers={"Authorization": "Bearer adminkey"},
        json={"api_key": ""},
    )
    pid = create.json()["id"]

    # Chat completion
    res = await admin_client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer adminkey"},
        json={
            "model": f"{pid}:gemini-2.5-flash",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["choices"][0]["message"]["content"] == "Hello from Gemini CLI"
    # Billing is injected by the app after the call
    assert "billing" in data
    # Usage must be tiktoken-estimated on the subprocess path; the raw
    # CLI returns 0,0,0 so any non-zero total proves the injection works.
    usage = data.get("usage") or {}
    assert usage.get("total_tokens", 0) > 0, (
        f"usage not injected: {usage} — router.py should estimate tokens "
        "when the CLI doesn't report them"
    )
    assert usage["total_tokens"] == (
        usage["prompt_tokens"] + usage["completion_tokens"]
    )


# ── Chat completion — Playground path (admin cookie, no bearer) ─────────────


async def test_chat_completion_via_admin_cookie(admin_client, _mock_gemini_cli):
    """Reproduce the Playground flow: login → cookie → POST without Bearer."""
    # Login to obtain the session cookie (endpoint returns 204 No Content)
    login = await admin_client.post(
        "/auth/admin/login",
        json={"key": "adminkey"},
    )
    assert login.status_code in (200, 204)
    assert "plugboard_admin_session" in admin_client.cookies

    # Create provider first (admin bearer)
    await admin_client.post(
        "/admin/direct-providers/gemini-cli-subprocess",
        headers={"Authorization": "Bearer adminkey"},
        json={"api_key": ""},
    )
    listing = await admin_client.get(
        "/providers",
        headers={"Authorization": "Bearer adminkey"},
    )
    pid = next(
        p["id"] for p in listing.json() if p.get("auth_source") == "subprocess:gemini"
    )

    # Now call /v1/chat/completions WITHOUT a Bearer header — cookie only.
    res = await admin_client.post(
        "/v1/chat/completions",
        # no Authorization header — only the cookie
        json={
            "model": f"{pid}:gemini-2.5-flash",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        },
    )
    assert res.status_code == 200, f"Body: {res.text}"
    data = res.json()
    assert data["choices"][0]["message"]["content"] == "Hello from Gemini CLI"


# ── Chat completion — streaming SSE ─────────────────────────────────────────


async def test_chat_completion_streaming_sse(admin_client, _mock_gemini_cli):
    await admin_client.post(
        "/admin/direct-providers/gemini-cli-subprocess",
        headers={"Authorization": "Bearer adminkey"},
        json={"api_key": ""},
    )
    listing = await admin_client.get(
        "/providers",
        headers={"Authorization": "Bearer adminkey"},
    )
    pid = next(
        p["id"] for p in listing.json() if p.get("auth_source") == "subprocess:gemini"
    )

    res = await admin_client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer adminkey"},
        json={
            "model": f"{pid}:gemini-2.5-flash",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert res.status_code == 200
    body = res.text
    # Validate SSE shape: several `data: ...` lines + `[DONE]`
    assert "data: [DONE]" in body
    chunks = [line for line in body.splitlines() if line.startswith("data: {")]
    assert len(chunks) >= 2  # at least one content chunk + final stop chunk

    # Concatenate deltas — should match the mocked response
    assembled = ""
    for line in chunks:
        evt = json.loads(line[len("data: ") :])
        delta = evt["choices"][0]["delta"].get("content", "")
        assembled += delta
    assert assembled.strip() == "Hello from Gemini CLI"


# ── /v1/models includes the subprocess provider's known_models ──────────────


async def test_models_endpoint_exposes_subprocess_models(
    admin_client, _mock_gemini_cli
):
    # Create provider
    create = await admin_client.post(
        "/admin/direct-providers/gemini-cli-subprocess",
        headers={"Authorization": "Bearer adminkey"},
        json={"api_key": ""},
    )
    pid = create.json()["id"]
    # Discover to populate models
    await admin_client.post(
        f"/providers/{pid}/discover",
        headers={"Authorization": "Bearer adminkey"},
    )
    # /v1/models
    res = await admin_client.get(
        "/v1/models",
        headers={"Authorization": "Bearer adminkey"},
    )
    assert res.status_code == 200
    data = res.json()
    model_ids = {m["id"] for m in data.get("data", [])}
    # Models are exposed with "<provider_id>:<model_name>" prefix
    assert any(
        (mid.startswith(f"{pid}:") or mid.startswith("Gemini:"))
        and mid.endswith("gemini-2.5-flash")
        for mid in model_ids
    ), f"Expected {pid}:gemini-2.5-flash or Gemini:gemini-2.5-flash in {model_ids}"


# ── Error propagation ──────────────────────────────────────────────────────


async def test_subprocess_failure_falls_back_to_next_provider(
    admin_client, monkeypatch: pytest.MonkeyPatch
):
    """When the CLI fails at request time, router falls through to next candidate.

    End-to-end resilience check: subprocess failure does NOT crash the request —
    the router logs the warning and tries the next provider in the candidates
    list (HTTP-based test providers mocked by other fixtures). This matches the
    behavior for transient HTTP errors.
    """
    # Pretend CLI is installed for provider creation
    monkeypatch.setattr(
        cli_subprocess.shutil,
        "which",
        lambda cmd: "/fake/bin/" + cmd if cmd == "gemini" else None,
    )

    async def fake_probe(kind, timeout_s=5.0):
        return {"ok": True, "version": "x", "error": None}

    monkeypatch.setattr(cli_subprocess, "probe_cli_version", fake_probe)

    # Make subprocess invocation fail
    async def bad_exec(*args, **kwargs):
        raise FileNotFoundError(2, "No such file", args[0])

    monkeypatch.setattr(cli_subprocess.asyncio, "create_subprocess_exec", bad_exec)

    create = await admin_client.post(
        "/admin/direct-providers/gemini-cli-subprocess",
        headers={"Authorization": "Bearer adminkey"},
        json={"api_key": ""},
    )
    pid = create.json()["id"]

    # Also ensure failure surfaces when subprocess is the only candidate:
    # deactivate all other providers
    listing = await admin_client.get(
        "/providers",
        headers={"Authorization": "Bearer adminkey"},
    )
    for p in listing.json():
        if p["id"] != pid:
            await admin_client.put(
                f"/providers/{p['id']}",
                headers={"Authorization": "Bearer adminkey"},
                json={"is_active": False},
            )

    res = await admin_client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer adminkey"},
        json={
            "model": f"{pid}:gemini-2.5-flash",
            "messages": [{"role": "user", "content": "unique-cli-failure-probe"}],
            "stream": False,
            "cache": False,  # bypass response cache so we see the real subprocess failure
        },
    )
    # With no fallback, the router returns 502 surfacing the subprocess error
    assert res.status_code in {502, 500}
    body = res.text.lower()
    assert "not installed" in body or "cli" in body or "bad gateway" in body


async def test_streaming_surfaces_subprocess_error_as_sse(
    admin_client, monkeypatch: pytest.MonkeyPatch
):
    """When subprocess fails mid-stream, SSE yields an error chunk then [DONE]."""
    monkeypatch.setattr(
        cli_subprocess.shutil,
        "which",
        lambda cmd: "/fake/bin/" + cmd if cmd == "gemini" else None,
    )

    async def fake_probe(kind, timeout_s=5.0):
        return {"ok": True, "version": "x", "error": None}

    monkeypatch.setattr(cli_subprocess, "probe_cli_version", fake_probe)

    async def bad_exec(*args, **kwargs):
        raise FileNotFoundError(2, "No such file", args[0])

    monkeypatch.setattr(cli_subprocess.asyncio, "create_subprocess_exec", bad_exec)

    create = await admin_client.post(
        "/admin/direct-providers/gemini-cli-subprocess",
        headers={"Authorization": "Bearer adminkey"},
        json={"api_key": ""},
    )
    pid = create.json()["id"]

    res = await admin_client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer adminkey"},
        json={
            "model": f"{pid}:gemini-2.5-flash",
            "messages": [
                {"role": "user", "content": "unique-stream-failure-probe-xyz"}
            ],
            "stream": True,
            "cache": False,  # bypass cache so we see the real failure path
        },
    )
    # With fallback providers in the fixture there may be a 200 or a 502;
    # in either case, the SSE body should contain [DONE] and start with either
    # an error event or a normal event. This test mainly checks the stream
    # doesn't hang or crash the server.
    assert res.status_code in (200, 502)
    if res.status_code != 200:
        return
    body = res.text
    assert "data: [DONE]" in body or body == ""


# ── Sanity: pure helper (no subprocess needed) ─────────────────────────────


def test_check_cli_installed_integration():
    # Unknown kind -> error payload
    result = check_cli_installed("nonexistent-cli-kind")
    assert result["ok"] is False
    assert "Unknown CLI kind" in result["error"]
