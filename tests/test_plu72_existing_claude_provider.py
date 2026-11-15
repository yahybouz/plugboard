"""PLU-72 on a claude provider created before 4.14.3 (4.14.3 review, H1).

Such a provider keeps its saved model list, which has no ``claude-fable-5-2``,
so a call to it gets ``404 model_not_found`` before the CLI starts (BUG-120).
With ``PLUGBOARD_MODEL_REFRESH_PROBE_KINDS`` empty (the production instance),
neither Discover nor the catalogue job adds it: both keep the saved list.

The documented remedy is an admin call, with the admin key and a TOTP code
(MFA enforced, as in production): read the provider's list with
``GET /providers``, then send it back with ``claude-fable-5-2`` added through
``PUT /providers/{id}`` and a new code. These tests replay that remedy and
prove what it gives: the model is listed by ``/v1/models``, the same call
starts the CLI with ``--model claude-fable-5-2`` and the command line of any
other claude model, it is billed at the official 8/40 price, the saved models
stay, a later Discover keeps it, and a key limited by ``allowed_models`` still
needs the model in its list.

No CLI runs: the process creation is a recorder.
"""

from __future__ import annotations

import json
import re
import types
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from plugboard import cli_subprocess as cs
from tests.test_model_not_found import _isolated_providers

ROOT = Path(__file__).resolve().parents[1]
PROVIDER_ID = "claude-via-claude-cli-1"
NEW_MODEL = "claude-fable-5-2"
SAVED_MODELS = [
    {"id": "claude-haiku-4-5", "name": "claude-haiku-4-5"},
    {"id": "claude-sonnet-5-5", "name": "claude-sonnet-5-5"},
    {"id": "claude-fable-5-1", "name": "claude-fable-5-1"},
]
# A claude provider as saved before 4.14.3: no claude-fable-5-2.
SAVED_CLAUDE = {
    "id": PROVIDER_ID,
    "name": "Claude (via claude CLI)",
    "base_url": "subprocess://claude",
    "bearer_token": "(via CLI)",
    "auth_source": "subprocess:claude",
    "preset_key": "claude-cli-subprocess",
    "provider_type": "cloud",
    "default_model": "claude-sonnet-5-5",
    "models": SAVED_MODELS,
    "is_active": True,
    "created_at": "2026-10-01T00:00:00+00:00",
}
ADMIN_KEY = "admin-key-for-tests"
TOTP_HEADER = "X-Plugboard-Admin-TOTP"
MONTH = "2026_11"
_REAL_COMPLETE = cs.complete  # before any fixture wraps it


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.delenv("PLUGBOARD_MODEL_REFRESH_PROBE_KINDS", raising=False)
    monkeypatch.delenv("LLM_ENABLER_MODEL_REFRESH_PROBE_KINDS", raising=False)
    return _isolated_providers(tmp_path, monkeypatch, [dict(SAVED_CLAUDE)])


@pytest.fixture
def spawned(monkeypatch):
    """Every CLI start, recorded: complete() entered, process created."""
    calls: dict = {"complete": [], "spawn": []}

    async def _complete(kind, body, **kwargs):
        calls["complete"].append((kind, body.get("model")))
        return await _REAL_COMPLETE(kind, body, **kwargs)

    async def _spawn(*argv, **kwargs):
        calls["spawn"].append(list(argv))
        raise RuntimeError("recorded: no CLI is started in tests")

    monkeypatch.setattr(cs, "complete", _complete)
    monkeypatch.setattr(cs, "_resolve_binary", lambda cmd: f"/fake/bin/{cmd}")
    monkeypatch.setattr(cs.asyncio, "create_subprocess_exec", _spawn)
    return calls


@pytest.fixture
def quarantined(monkeypatch):
    from plugboard import model_quarantine

    records: list = []
    for name in (
        "record_failure",
        "record_account_unsupported",
        "record_terminal_unavailable",
    ):
        monkeypatch.setattr(
            model_quarantine,
            name,
            lambda *a, _n=name, **k: records.append((_n, a, k)),
        )
    return records


@pytest.fixture
def mfa(tmp_path, monkeypatch):
    """Admin MFA enforced, as in production, on a private file and a clock
    the test moves: one code per 30-second step, each accepted once."""
    from plugboard import admin_mfa, admin_sessions

    clock = types.SimpleNamespace(t=1_800_000_000.0)
    monkeypatch.setattr(admin_mfa, "_MFA_FILE", tmp_path / "admin_mfa.json")
    monkeypatch.setattr(admin_mfa, "time", types.SimpleNamespace(time=lambda: clock.t))
    monkeypatch.setattr(
        admin_sessions, "_path", lambda: tmp_path / "admin_sessions.json"
    )
    secret = admin_mfa.generate_secret()
    admin_mfa.enroll(secret)
    monkeypatch.setenv("PLUGBOARD_ADMIN_MFA_REQUIRED", "1")
    assert admin_mfa.is_required()

    def next_code() -> str:
        clock.t += 30  # the authenticator's next code
        return admin_mfa._generate_code(secret, at=clock.t)

    return types.SimpleNamespace(next_code=next_code)


@pytest.fixture
def audited(monkeypatch):
    import plugboard.app as app_module

    seen: list = []
    monkeypatch.setattr(
        app_module, "_audit", lambda action, **kw: seen.append((action, kw))
    )
    return seen


@pytest.fixture
def official_prices_only(tmp_path, monkeypatch):
    """No pricing grid: the official table prices the call, for November."""
    from plugboard import billing

    pricing = tmp_path / "pricing"
    pricing.mkdir()
    monkeypatch.setattr(billing, "_PRICING_DIR", pricing)
    monkeypatch.setattr(billing, "current_pricing_month", lambda: MONTH)


@pytest.fixture
async def http(saved, spawned, quarantined, mfa, audited, tmp_path, monkeypatch):
    import plugboard.app as app_module
    import plugboard.ratelimit as rl
    from plugboard import apikeys

    monkeypatch.setattr(app_module, "_API_KEY", "instance-key")
    monkeypatch.setattr(app_module, "_ADMIN_KEY", ADMIN_KEY)
    monkeypatch.setattr(rl, "_LIMIT", 0)
    monkeypatch.setattr(apikeys, "KEYS_FILE", tmp_path / "keys.json")
    monkeypatch.setattr(apikeys, "KEYS_LOCK", tmp_path / "keys.lock")
    client_key = apikeys.create_key("acme", "plu-72")["key"]
    async with AsyncClient(
        transport=ASGITransport(app=app_module.app), base_url="http://test"
    ) as client:
        yield client, {"Authorization": f"Bearer {client_key}"}


def _admin(code: str) -> dict:
    return {"Authorization": f"Bearer {ADMIN_KEY}", TOTP_HEADER: code}


def _chat(model: str) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
        "cache": False,
    }


async def _add_fable_5_2(client, mfa) -> dict:
    """The runbook's gesture: read the saved list, send it back with the
    model added, each call with the admin key and its own TOTP code."""
    listed = await client.get("/providers", headers=_admin(mfa.next_code()))
    assert listed.status_code == 200, listed.text
    (current,) = [p for p in listed.json() if p["id"] == PROVIDER_ID]
    models = list(current["models"])
    if not any(m.get("id") == NEW_MODEL for m in models):
        models.append({"id": NEW_MODEL, "name": NEW_MODEL})
    resp = await client.put(
        f"/providers/{PROVIDER_ID}",
        headers=_admin(mfa.next_code()),
        json={"models": models},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _model_value(argv: list) -> str:
    return argv[argv.index("--model") + 1]


# ── Before: the saved list ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_saved_list_without_fable_5_2_answers_404_and_starts_nothing(
    http, spawned
):
    client, client_auth = http
    resp = await client.post(
        "/v1/chat/completions",
        headers=client_auth,
        json=_chat(f"{PROVIDER_ID}:{NEW_MODEL}"),
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "model_not_found"
    assert spawned == {"complete": [], "spawn": []}


@pytest.mark.asyncio
async def test_discover_with_probes_off_does_not_add_fable_5_2(http, mfa, spawned):
    """What the 4.14.3 notes first said: a Discover keeps the saved list."""
    client, _client_auth = http
    resp = await client.post(
        f"/providers/{PROVIDER_ID}/discover", headers=_admin(mfa.next_code())
    )
    assert resp.status_code == 200, resp.text
    from plugboard.config import get_provider

    ids = [m["id"] for m in get_provider(PROVIDER_ID)["models"]]
    assert NEW_MODEL not in ids
    assert spawned["spawn"] == []


# ── The documented gesture ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_update_adds_fable_5_2_and_changes_nothing_else(
    http, mfa, audited, spawned
):
    from plugboard.config import get_provider

    client, _client_auth = http
    before = dict(get_provider(PROVIDER_ID))

    body = await _add_fable_5_2(client, mfa)

    after = get_provider(PROVIDER_ID)
    assert [m["id"] for m in after["models"]] == [
        "claude-haiku-4-5",
        "claude-sonnet-5-5",
        "claude-fable-5-1",
        NEW_MODEL,
    ]
    assert [m["id"] for m in body["models"]] == [m["id"] for m in after["models"]]
    # Every other field as it was: default model, CLI, key, switch.
    assert {k: v for k, v in after.items() if k != "models"} == {
        k: v for k, v in before.items() if k != "models"
    }
    assert "extra_model_ids" not in after
    updates = [kw for action, kw in audited if action == "provider.update"]
    assert [(kw["target_id"], json.loads(kw["details"])) for kw in updates] == [
        (PROVIDER_ID, {"fields": ["models"]})
    ]
    assert spawned["spawn"] == []  # an edit starts no CLI


@pytest.mark.asyncio
async def test_the_update_is_refused_without_a_fresh_code(http, mfa):
    """MFA is required: the admin key alone, or the code of the read
    reused, is refused and nothing is written."""
    from plugboard.config import get_provider

    client, _client_auth = http
    code = mfa.next_code()
    listed = await client.get("/providers", headers=_admin(code))
    assert listed.status_code == 200
    put = {"models": [*SAVED_MODELS, {"id": NEW_MODEL, "name": NEW_MODEL}]}
    alone = await client.put(
        f"/providers/{PROVIDER_ID}",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        json=put,
    )
    reused = await client.put(
        f"/providers/{PROVIDER_ID}", headers=_admin(code), json=put
    )
    assert (alone.status_code, reused.status_code) == (401, 401)
    assert NEW_MODEL not in [m["id"] for m in get_provider(PROVIDER_ID)["models"]]


@pytest.mark.asyncio
async def test_after_the_update_v1_models_lists_fable_5_2(http, mfa):
    client, client_auth = http
    await _add_fable_5_2(client, mfa)

    resp = await client.get("/v1/models", headers=client_auth)
    assert resp.status_code == 200, resp.text
    ids = [m["id"] for m in resp.json()["data"]]
    assert f"{PROVIDER_ID}:{NEW_MODEL}" in ids


@pytest.mark.asyncio
async def test_after_the_update_the_call_starts_the_cli_like_any_claude_model(
    http, mfa, spawned, quarantined
):
    client, client_auth = http
    await _add_fable_5_2(client, mfa)

    await client.post(
        "/v1/chat/completions",
        headers=client_auth,
        json=_chat(f"{PROVIDER_ID}:{NEW_MODEL}"),
    )
    await client.post(
        "/v1/chat/completions",
        headers=client_auth,
        json=_chat(f"{PROVIDER_ID}:claude-sonnet-5-5"),
    )

    assert spawned["complete"] == [
        ("claude", NEW_MODEL),
        ("claude", "claude-sonnet-5-5"),
    ]
    fable, sonnet = spawned["spawn"]
    assert fable[0] == "/fake/bin/claude"
    assert _model_value(fable) == NEW_MODEL
    # The same command line as another claude model, the model value apart.
    i = fable.index("--model") + 1
    assert fable[:i] + fable[i + 1 :] == sonnet[:i] + sonnet[i + 1 :]
    # The very command line complete() builds for that model on its own.
    with pytest.raises(RuntimeError, match="recorded"):
        await _REAL_COMPLETE("claude", _chat(NEW_MODEL))
    assert spawned["spawn"][2] == fable
    assert not any(NEW_MODEL in str(r) for r in quarantined)


@pytest.mark.asyncio
async def test_after_the_update_the_call_is_billed_at_the_official_price(
    http, mfa, official_prices_only, monkeypatch
):
    """1M prompt + 100k completion tokens: 8 + 4 USD (Fable 5.2, 8/40)."""
    from plugboard import chat_billing

    client, client_auth = http
    await _add_fable_5_2(client, mfa)

    async def _answer(kind, body, **kwargs):
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1774000000,
            "model": body["model"],
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 1_000_000,
                "completion_tokens": 100_000,
                "total_tokens": 1_100_000,
            },
        }

    charges: list = []
    real_record = chat_billing.record
    monkeypatch.setattr(cs, "complete", _answer)
    monkeypatch.setattr(
        chat_billing,
        "record",
        lambda client_id, key_id, charge, **kw: (
            charges.append(charge),
            real_record(client_id, key_id, charge, **kw),
        ),
    )

    resp = await client.post(
        "/v1/chat/completions",
        headers=client_auth,
        json=_chat(f"{PROVIDER_ID}:{NEW_MODEL}"),
    )
    assert resp.status_code == 200, resp.text
    (charge,) = charges
    assert (charge.model, charge.provider_id) == (NEW_MODEL, PROVIDER_ID)
    assert charge.price_source == "official", charge
    assert charge.cost_usd == pytest.approx(8.0 + 4.0)


@pytest.mark.asyncio
async def test_a_later_discover_keeps_fable_5_2(http, mfa, spawned):
    """Probes off: Discover keeps the saved list, the added model with it."""
    from plugboard.config import get_provider

    client, _client_auth = http
    await _add_fable_5_2(client, mfa)
    resp = await client.post(
        f"/providers/{PROVIDER_ID}/discover", headers=_admin(mfa.next_code())
    )
    assert resp.status_code == 200, resp.text
    assert NEW_MODEL in [m["id"] for m in get_provider(PROVIDER_ID)["models"]]
    assert spawned["spawn"] == []


@pytest.mark.asyncio
async def test_a_limited_key_needs_fable_5_2_in_its_allowed_models(http, mfa, spawned):
    from plugboard import apikeys

    client, _client_auth = http
    await _add_fable_5_2(client, mfa)
    old = apikeys.create_key(
        "acme", "limited", allowed_models=[f"{PROVIDER_ID}:claude-sonnet-5-5"]
    )["key"]
    new = apikeys.create_key(
        "acme",
        "limited, with Fable 5.2",
        allowed_models=[
            f"{PROVIDER_ID}:claude-sonnet-5-5",
            f"{PROVIDER_ID}:{NEW_MODEL}",
        ],
    )["key"]

    refused = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {old}"},
        json=_chat(f"{PROVIDER_ID}:{NEW_MODEL}"),
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "forbidden_model"
    assert spawned["spawn"] == []

    await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {new}"},
        json=_chat(f"{PROVIDER_ID}:{NEW_MODEL}"),
    )
    assert [_model_value(argv) for argv in spawned["spawn"]] == [NEW_MODEL]


# ── What the operator reads ─────────────────────────────────────────────────


def _section(text: str, title: str) -> str:
    match = re.search(rf"(?ms)^## {re.escape(title)}\n(.*?)(?=^## |\Z)", text)
    assert match, title
    return match.group(1)


def test_the_runbook_gives_the_update_with_the_admin_key_and_a_totp_code():
    runbook = (ROOT / "docs" / "runbooks" / "production-instance.md").read_text(
        encoding="utf-8"
    )
    section = _section(runbook, "Switching to 4.14.3")
    assert "GET" in section and "/providers" in section
    assert "-X PUT" in section and "/providers/<claude-provider-id>" in section
    assert section.count("X-Plugboard-Admin-TOTP") >= 2
    assert NEW_MODEL in section
    assert "allowed_models" in section
    # Why a Discover is not the way: the probes are off in production.
    assert "PLUGBOARD_MODEL_REFRESH_PROBE_KINDS" in section


def test_the_changelog_no_longer_says_discover_adds_fable_5_2():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    release = _section(changelog, "[4.14.3] — 2026-11-15")
    (entry,) = [line for line in release.splitlines() if "(PLU-72" in line]
    assert "**Discover** on the provider, or the catalogue pass" not in entry
    assert "PUT /providers/" in entry
    assert "production-instance.md#switching-to-4143" in entry
