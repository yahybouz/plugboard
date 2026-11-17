"""BUG-143: the Claude quota excerpt never keeps a secret.

PLU-67 shows a short excerpt of the error met by a Claude provider in
``GET /admin/claude-quota`` and in the ``provider.quota_limit`` audit entry.
The excerpt goes through ``log_redact.redact``, which masked ``sk-ant-…``,
``Bearer …`` and ``"api_key": "…"`` only: an ``x-api-key`` header, a
``token=`` query parameter, a bare ``Authorization`` value or a Google
``AIza…`` key stayed in clear.

Every fake secret below is built at run time from a short repeated pattern:
obviously fake, and invisible to the secret scanner.
"""

from __future__ import annotations

import json

import pytest

from plugboard import claude_quota
from plugboard.cli_subprocess import CliSubprocessError
from plugboard.log_redact import redact


def _fake(tag: str, n: int) -> str:
    """A fake secret body of ``n`` characters, repeating ``tag``."""
    return (tag * n)[:n]


HEX = _fake("0f", 32)  # 32 hex characters, like the review's sample
TOK = _fake("Tq7", 28)  # a bearer-ish token
B64 = _fake("dXNlcjpw", 24) + "=="  # a Basic credential
ANT = "sk-" + "ant-" + "api03-" + _fake("Kv4", 40)
ANT_BODY = ANT[len("sk-ant-") :]
GOOG = "AI" + "za" + "Sy" + _fake("Gm2", 33)
GOOG_BODY = GOOG[len("AIza") :]


def _leaks(text: str, secret: str, window: int = 8) -> bool:
    """Whether ``text`` still holds ``secret`` or any ``window``-long slice
    of it: a fragment cut by the truncation counts as a leak."""
    if secret in text:
        return True
    return any(secret[i : i + window] in text for i in range(len(secret) - window + 1))


# ── redact(): the forms the review found, and their usual variants ──────────


CASES = [
    # x-api-key: header and JSON forms, any case, escaped JSON, line break.
    ("header x-api-key", f"401 x-api-key: {HEX} rejected", HEX),
    ("header X-Api-Key", f"X-Api-Key:{HEX}", HEX),
    ("header X-API-KEY", f"X-API-KEY = {HEX}", HEX),
    ("json x-api-key", json.dumps({"x-api-key": HEX, "n": 1}), HEX),
    ("json X-API-Key", '{"X-API-Key":"' + HEX + '"}', HEX),
    ("escaped json", json.dumps(json.dumps({"x-api-key": HEX})), HEX),
    ("single quotes", f"{{'x-api-key': '{HEX}'}}", HEX),
    ("line break", f"x-api-key:\n    {HEX}\nend", HEX),
    ("goog header", f"x-goog-api-key: {HEX}", HEX),
    # Authorization: Bearer, Basic, bare; header and JSON.
    ("bearer header", f"Authorization: Bearer {TOK}", TOK),
    ("bearer short", "authorization: bearer " + _fake("Sb", 10), _fake("Sb", 10)),
    ("basic header", f"authorization: Basic {B64}", B64[:-2]),
    ("bare header", f"Authorization: {TOK}", TOK),
    ("bearer json", json.dumps({"Authorization": f"Bearer {TOK}"}), TOK),
    ("basic json", json.dumps({"authorization": f"Basic {B64}"}), B64[:-2]),
    ("bare json", json.dumps({"Authorization": TOK}), TOK),
    ("proxy auth", f"Proxy-Authorization: Basic {B64}", B64[:-2]),
    # token= and the usual query and form parameters.
    ("token=", f"GET /v1/x?token={HEX} 401", HEX),
    ("token= alone", f"token={HEX}", HEX),
    ("access_token=", f"https://h.example/cb?a=1&access_token={TOK}#f", TOK),
    ("api_key=", f"form: api_key={HEX}&x=1", HEX),
    ("apikey=", f"?apikey={HEX}", HEX),
    ("key=", f"https://h.example/v1?key={HEX}", HEX),
    ("TOKEN= upper", f"TOKEN={HEX};", HEX),
    # sk-ant-: in the middle of a text, quoted, in a URL, glued, cut short.
    ("sk-ant mid text", f"invalid key {ANT} for org", ANT_BODY),
    ("sk-ant quoted", f"key '{ANT}' rejected", ANT_BODY),
    ("sk-ant in url", f"https://h.example/v1/keys/{ANT}/usage", ANT_BODY),
    ("sk-ant in query", f"https://h.example/v1?k={ANT}&x=1", ANT_BODY),
    ("sk-ant glued", f"id_{ANT}", ANT_BODY),
    ("sk-ant cut short", "key sk-ant-api03-" + _fake("Kv4", 12), _fake("Kv4", 12)),
    # Google API keys.
    ("AIza alone", f"API key not valid: {GOOG}", GOOG_BODY),
    ("AIza in url", f"https://h.example/v1beta/models?key={GOOG}", GOOG_BODY),
    ("AIza in json", json.dumps({"error": {"key": GOOG}}), GOOG_BODY),
    ("AIza glued", f"k_{GOOG}", GOOG_BODY),
]


@pytest.mark.parametrize(
    "text, secret", [c[1:] for c in CASES], ids=[c[0] for c in CASES]
)
def test_redact_masks_the_secret(text, secret):
    out = redact(text)
    assert not _leaks(out, secret), out
    assert "REDACTED" in out


@pytest.mark.parametrize(
    "text",
    [
        "Claude AI usage limit reached|1900000000",
        "5-hour limit reached - resets 3pm",
        "Authorization failed for this request",
        "max_tokens=1024 exceeds the limit",
        "monkey=1 turkey=2",
        "HTTP 429: rate_limit_error",
    ],
)
def test_redact_keeps_harmless_text(text):
    assert redact(text) == text


def test_the_provider_hint_survives():
    # Operators still tell providers apart: the known prefixes stay visible.
    assert "sk-ant-***REDACTED***" in redact(f"key {ANT}")
    assert "AIza***REDACTED***" in redact(f"key {GOOG}")
    assert "Bearer ***REDACTED***" in redact(f"Authorization: Bearer {TOK}")


# ── the excerpt: masked before the cut ──────────────────────────────────────


@pytest.mark.parametrize(
    "secret_text, secret",
    [
        (f"x-api-key: {HEX}", HEX),
        (f"token={HEX}", HEX),
        (f"Authorization: Bearer {TOK}", TOK),
        (GOOG, GOOG_BODY),
        (ANT, ANT_BODY),
    ],
    ids=["x-api-key", "token", "bearer", "AIza", "sk-ant"],
)
@pytest.mark.parametrize("shift", [-20, -12, -6, -2])
def test_the_cut_never_leaves_a_fragment(secret_text, secret, shift):
    # The secret straddles the excerpt limit: whatever part of it falls
    # before the cut must be masked, not a recognisable slice.
    pad = "z" * (claude_quota.EXCERPT_MAX + shift - len(secret_text) // 2)
    excerpt = claude_quota._excerpt(f"{pad} {secret_text} tail")
    assert len(excerpt) <= claude_quota.EXCERPT_MAX
    assert not _leaks(excerpt, secret), excerpt


# ── the three surfaces ──────────────────────────────────────────────────────


_CLAUDE_CLI = {"id": "claude-sub", "auth_source": "subprocess:claude"}
SURFACE_CASES = [
    (f"usage limit reached; x-api-key: {HEX}", HEX),
    (f'usage limit reached {{"X-Api-Key": "{HEX}"}}', HEX),
    (f"usage limit reached GET /v1?token={HEX}", HEX),
    (f"usage limit reached authorization: {TOK}", TOK),
    (f"usage limit reached key={GOOG}", GOOG_BODY),
    (f"usage limit reached for {ANT}", ANT_BODY),
]
SURFACE_IDS = ["x-api-key", "json", "token", "bare-auth", "AIza", "sk-ant"]


@pytest.fixture
def audit_db(tmp_path, monkeypatch):
    import plugboard.audit as audit_mod

    monkeypatch.setattr(audit_mod, "_DB_PATH", tmp_path / "audit.db")
    return audit_mod


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    path = tmp_path / "claude_quota.json"
    monkeypatch.setattr(claude_quota, "_state_path", lambda: path)
    return path


async def _limit(message: str) -> None:
    exc = CliSubprocessError(message, provider_name="claude", quota=True)
    claude_quota.note_limit(_CLAUDE_CLI, "m", exc)
    await claude_quota.settle()


@pytest.mark.asyncio
@pytest.mark.parametrize("message, secret", SURFACE_CASES, ids=SURFACE_IDS)
async def test_the_admin_view_holds_no_secret(admin_client, audit_db, message, secret):
    await _limit(message)
    resp = await admin_client.get(
        "/admin/claude-quota", headers={"Authorization": "Bearer adminkey"}
    )
    assert resp.status_code == 200
    assert resp.json()["provider"] == "claude-sub"
    assert "REDACTED" in resp.json()["message"]
    assert not _leaks(resp.text, secret), resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("message, secret", SURFACE_CASES, ids=SURFACE_IDS)
async def test_the_audit_entry_holds_no_secret(audit_db, message, secret):
    await _limit(message)
    entries = audit_db.get_audit_log(action="provider.quota_limit")
    assert len(entries) == 1
    assert "REDACTED" in json.loads(entries[0]["details"])["message"]
    assert not _leaks(json.dumps(entries), secret)


@pytest.mark.asyncio
@pytest.mark.parametrize("message, secret", SURFACE_CASES, ids=SURFACE_IDS)
async def test_the_metrics_hold_no_secret(audit_db, message, secret):
    # /metrics carries counts and timestamps only; this guards it stays so.
    from plugboard.metrics import format_prometheus

    await _limit(message)
    text = format_prometheus()
    assert 'plugboard_claude_limit_total{provider="claude-sub"} 1' in text
    assert not _leaks(text, secret)


def test_an_excerpt_saved_before_the_fix_is_masked_on_read(state_file):
    # A state file written by 4.14.5 may hold a secret in clear: the admin
    # view masks it again when it reads it.
    state_file.write_text(
        json.dumps(
            {
                "last": {
                    "at": "2026-11-16T00:00:00Z",
                    "provider": "claude-sub",
                    "model": "m",
                    "source": "cli",
                    "message": f"x-api-key: {HEX} token={TOK}",
                    "resumes_at": None,
                },
                "providers": {},
            }
        )
    )
    shown = claude_quota.snapshot()["message"]
    assert not _leaks(shown, HEX), shown
    assert not _leaks(shown, TOK), shown
