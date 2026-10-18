"""Phase 8 tests -- Enterprise features (audit trail, per-client rate limit)."""

from __future__ import annotations

import pytest

AUTH = {"Authorization": "Bearer testkey"}


# ── Audit trail ──────────────────────────────────────────────────────────────


def test_audit_log_action(tmp_path, monkeypatch):
    """log_action writes and get_audit_log retrieves entries."""
    from plugboard.audit import log_action, get_audit_log, count_audit_log
    import plugboard.audit as audit_mod

    monkeypatch.setattr(audit_mod, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(audit_mod, "_DB_PATH", tmp_path / "audit.db")

    log_action("key.create", target_type="key", target_id="abc", details="test-client")
    log_action("provider.delete", target_type="provider", target_id="xyz")

    entries = get_audit_log()
    assert len(entries) == 2
    # Newest first
    assert entries[0]["action"] == "provider.delete"
    assert entries[1]["action"] == "key.create"
    assert entries[1]["target_id"] == "abc"

    assert count_audit_log() == 2
    assert count_audit_log(action="key.create") == 1


def test_audit_log_filter_by_action(tmp_path, monkeypatch):
    """get_audit_log with action filter returns only matching entries."""
    from plugboard.audit import log_action, get_audit_log
    import plugboard.audit as audit_mod

    monkeypatch.setattr(audit_mod, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(audit_mod, "_DB_PATH", tmp_path / "audit.db")

    log_action("key.create")
    log_action("key.delete")
    log_action("provider.update")

    entries = get_audit_log(action="key.create")
    assert len(entries) == 1
    assert entries[0]["action"] == "key.create"


@pytest.mark.asyncio
async def test_audit_endpoint(client, tmp_path, monkeypatch):
    """GET /admin/audit returns audit log entries."""
    import plugboard.audit as audit_mod

    monkeypatch.setattr(audit_mod, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(audit_mod, "_DB_PATH", tmp_path / "audit.db")

    audit_mod.log_action("test.action", details="hello")

    resp = await client.get("/admin/audit", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    assert body["entries"][0]["action"] == "test.action"


@pytest.mark.asyncio
async def test_audit_endpoint_requires_admin(client):
    """GET /admin/audit without admin key returns 401."""
    resp = await client.get("/admin/audit")
    assert resp.status_code == 401


# ── Per-client rate limit ────────────────────────────────────────────────────


def test_client_rate_limit_allows_under():
    """check_client_rate_limit allows requests under the limit."""
    from plugboard.ratelimit import check_client_rate_limit, _client_buckets

    _client_buckets.pop("test-rpm", None)

    for _ in range(5):
        assert check_client_rate_limit("test-rpm", 10) is True


def test_client_rate_limit_blocks_over():
    """check_client_rate_limit blocks when limit exceeded."""
    from plugboard.ratelimit import check_client_rate_limit, _client_buckets

    _client_buckets.pop("test-rpm-block", None)

    for _ in range(3):
        check_client_rate_limit("test-rpm-block", 3)

    assert check_client_rate_limit("test-rpm-block", 3) is False


def test_client_rate_limit_unlimited():
    """check_client_rate_limit with rpm_limit=0 always allows."""
    from plugboard.ratelimit import check_client_rate_limit

    for _ in range(100):
        assert check_client_rate_limit("unlimited-client", 0) is True
