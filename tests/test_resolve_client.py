"""Branch coverage tests for `plugboard.app._resolve_client`.

Closes ARCH-DEBT-032 — the auth resolution function has 7 distinct
branches that decide which client_id (or 401) a caller gets:

  1. Admin cookie session (modern)
  2. Admin cookie session (legacy llm_admin_key cookie)
  3. Dev mode (API_KEY="") + admin token
  4. Dev mode + valid lle_* bearer
  5. Dev mode + invalid/revoked lle_* → 401 (v2.8.4 MF2)
  6. Dev mode + no/non-lle bearer → None (anonymous)
  7. API_KEY enforced + admin token
  8. API_KEY enforced + valid lle_* bearer
  9. API_KEY enforced + invalid bearer → 401

Pre-v2.9.1 these were exercised only indirectly through E2E tests
(test_audit_e2e.py). This file pins each branch in isolation so a
regression on one path can't hide behind a green E2E suite.
"""

from __future__ import annotations

from typing import Dict, Optional
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException


# -- helpers ------------------------------------------------------------------


def _make_request(
    *,
    bearer: str = "",
    cookies: Optional[Dict[str, str]] = None,
) -> MagicMock:
    """Mock fastapi.Request just enough for _resolve_client."""
    req = MagicMock()
    req.headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    req.cookies = cookies or {}

    # request.state needs attribute assignment; use a real namespace.
    class _State:
        pass

    req.state = _State()
    return req


@pytest.fixture()
def isolated_app(monkeypatch, tmp_path):
    """Isolate the apikeys store + admin/api keys for each test.

    We import _resolve_client AFTER the env is patched so module-level
    constants (_API_KEY, _ADMIN_KEY) read the test values.
    """
    import plugboard.apikeys as ak

    keys_file = tmp_path / "api_keys.json"
    keys_file.write_text('{"keys": []}')
    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", keys_file)
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    # Force the cache to reload from this empty file.
    ak._invalidate_cache()
    return ak


def _create_lle_key(ak, client_id: str = "alice") -> str:
    """Create a fresh lle_* key in the isolated store, return its raw value."""
    rec = ak.create_key(client_id=client_id, label="resolve-client-test")
    return rec["key"]


# -- branch 1 + 2 : admin cookie ----------------------------------------------


def test_branch_admin_cookie_modern_session(isolated_app, monkeypatch):
    """Admin session cookie → client_id == 'admin', state.key_id == 'admin'."""
    monkeypatch.setattr("plugboard.app._API_KEY", "prod-key")
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "adminkey")
    from plugboard.app import _create_admin_session_token, _resolve_client

    session = _create_admin_session_token()
    req = _make_request(cookies={"plugboard_admin_session": session})
    assert _resolve_client(req) == "admin"
    assert req.state.key_id == "admin"


def test_branch_admin_cookie_legacy_llm_admin_key_rejected_in_v3(
    isolated_app, monkeypatch
):
    """v3.0.0 BREAKING (ADR-033): the legacy `llm_admin_key` cookie
    carrying the raw admin key in cleartext is no longer accepted.

    Pre-v3 behaviour: cookie present with the right value → resolved as
    'admin'. v3.0.0 onwards: the cookie is ignored, no other auth → 401.
    """
    monkeypatch.setattr("plugboard.app._API_KEY", "prod-key")
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "adminkey")
    from plugboard.app import _resolve_client

    req = _make_request(cookies={"llm_admin_key": "adminkey"})
    with pytest.raises(HTTPException) as ei:
        _resolve_client(req)
    assert ei.value.status_code == 401


def test_dashboard_auth_rejects_legacy_llm_admin_key_cookie(isolated_app, monkeypatch):
    """The dashboard guard ignores the cleartext `llm_admin_key` cookie too."""
    monkeypatch.setattr("plugboard.app._API_KEY", "prod-key")
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "adminkey")
    from plugboard.app import _check_dashboard_auth

    req = _make_request(cookies={"llm_admin_key": "adminkey"})
    assert _check_dashboard_auth(req) is False


# -- branches 3-6 : API_KEY="" (dev mode) -------------------------------------


def test_branch_dev_mode_admin_token(isolated_app, monkeypatch):
    """Dev mode + admin bearer → 'admin'."""
    monkeypatch.setattr("plugboard.app._API_KEY", "")
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "adminkey")
    from plugboard.app import _resolve_client

    req = _make_request(bearer="adminkey")
    assert _resolve_client(req) == "admin"
    assert req.state.key_id == "admin"


def test_branch_dev_mode_valid_lle(isolated_app, monkeypatch):
    """Dev mode + valid lle_* (v2.8.3 fix) → caller's client_id."""
    monkeypatch.setattr("plugboard.app._API_KEY", "")
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "adminkey")
    raw = _create_lle_key(isolated_app, client_id="bob")
    from plugboard.app import _resolve_client

    req = _make_request(bearer=raw)
    assert _resolve_client(req) == "bob"
    assert req.state.key_id != ""
    assert req.state.key_record is not None


def test_branch_dev_mode_invalid_lle_raises_401(isolated_app, monkeypatch):
    """Dev mode + lle_* prefix but unknown key → 401 (v2.8.4 MF2).

    Critical regression: pre-v2.8.4 this silently returned None and the
    caller landed in the empty-tenant scope.
    """
    monkeypatch.setattr("plugboard.app._API_KEY", "")
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "adminkey")
    from plugboard.app import _resolve_client

    req = _make_request(bearer="lle_definitely_not_registered_xxx")
    with pytest.raises(HTTPException) as ei:
        _resolve_client(req)
    assert ei.value.status_code == 401


def test_branch_dev_mode_revoked_lle_raises_401(isolated_app, monkeypatch):
    """Dev mode + revoked lle_* → 401 (v2.8.4 MF2 covers this too)."""
    monkeypatch.setattr("plugboard.app._API_KEY", "")
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "adminkey")
    raw = _create_lle_key(isolated_app, client_id="charlie")
    rec = isolated_app.get_key_record(raw)
    isolated_app.revoke_key(rec["key_id"])
    from plugboard.app import _resolve_client

    req = _make_request(bearer=raw)
    with pytest.raises(HTTPException) as ei:
        _resolve_client(req)
    assert ei.value.status_code == 401


def test_branch_dev_mode_no_bearer_returns_none(isolated_app, monkeypatch):
    """Dev mode + no bearer at all → None (anonymous, auth disabled)."""
    monkeypatch.setattr("plugboard.app._API_KEY", "")
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "adminkey")
    from plugboard.app import _resolve_client

    req = _make_request()
    assert _resolve_client(req) is None
    assert req.state.key_id == ""


def test_branch_dev_mode_non_lle_bearer_returns_none(isolated_app, monkeypatch):
    """Dev mode + bearer that isn't admin and doesn't start with lle_ → None.

    Same anonymous fallback as no bearer at all (e.g. legacy app sending a
    placeholder string).
    """
    monkeypatch.setattr("plugboard.app._API_KEY", "")
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "adminkey")
    from plugboard.app import _resolve_client

    req = _make_request(bearer="some-non-lle-token")
    assert _resolve_client(req) is None


# -- branches 7-9 : API_KEY enforced ------------------------------------------


def test_branch_prod_admin_token(isolated_app, monkeypatch):
    """API_KEY enforced + admin bearer → 'admin'."""
    monkeypatch.setattr("plugboard.app._API_KEY", "prod-key")
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "adminkey")
    from plugboard.app import _resolve_client

    req = _make_request(bearer="adminkey")
    assert _resolve_client(req) == "admin"


def test_branch_prod_valid_lle(isolated_app, monkeypatch):
    """API_KEY enforced + valid lle_* → caller's client_id."""
    monkeypatch.setattr("plugboard.app._API_KEY", "prod-key")
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "adminkey")
    raw = _create_lle_key(isolated_app, client_id="diana")
    from plugboard.app import _resolve_client

    req = _make_request(bearer=raw)
    assert _resolve_client(req) == "diana"


def test_branch_prod_invalid_bearer_raises_401(isolated_app, monkeypatch):
    """API_KEY enforced + unknown bearer → 401 (the standard auth gate)."""
    monkeypatch.setattr("plugboard.app._API_KEY", "prod-key")
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "adminkey")
    from plugboard.app import _resolve_client

    req = _make_request(bearer="not-a-real-key")
    with pytest.raises(HTTPException) as ei:
        _resolve_client(req)
    assert ei.value.status_code == 401


# -- v2.8.4 MF3 cross-check (cookie + bearer priority) ------------------------


def _as_pre_rename_key(ak, raw: str) -> str:
    """Store the record under an lle_* value, as a pre-rename install has it."""
    legacy = "lle_" + raw.split("_", 1)[1]
    data = ak._read()
    for rec in data["keys"]:
        if rec["key_hash"] == ak._hash_key(raw):
            rec["key_hash"] = ak._hash_key(legacy)
    ak._write(data)
    return legacy


@pytest.mark.parametrize("issued", ["current", "pre_rename"])
def test_branch_cookie_plus_lle_bearer_priority_lle(isolated_app, monkeypatch, issued):
    """Admin cookie + Bearer lle_* present → bearer scope wins (MF3)."""
    # Extended by the /ship coverage audit (pre_rename row).
    # Value: protects=a client key issued before the rename (lle_) keeps its
    # tenant scope when an admin cookie is also sent; fails_when=the cookie
    # precedence check recognises only the pb_ prefix; why_new=only freshly
    # issued pb_ keys were exercised here; seam=none
    monkeypatch.setattr("plugboard.app._API_KEY", "prod-key")
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "adminkey")
    raw = _create_lle_key(isolated_app, client_id="eve")
    if issued == "pre_rename":
        raw = _as_pre_rename_key(isolated_app, raw)
    from plugboard.app import _create_admin_session_token, _resolve_client

    session = _create_admin_session_token()
    req = _make_request(bearer=raw, cookies={"plugboard_admin_session": session})
    # MF3: bearer lle_* takes precedence over admin cookie.
    assert _resolve_client(req) == "eve"
