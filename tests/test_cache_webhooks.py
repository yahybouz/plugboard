"""Tests for cache, webhooks, and pricing_scraper modules."""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest
from aioresponses import aioresponses
from pathlib import Path

AUTH = {"Authorization": "Bearer testkey"}


# ---------------------------------------------------------------------------
# cache.py tests
# ---------------------------------------------------------------------------

# v3.0.0 flipped PLUGBOARD_STRICT_TENANT default 0→1. Most legacy tests
# in this file predate multi-tenant scoping and exercise the cache without
# a client_id — they would now raise ValueError. We opt them out per-class
# via this fixture, isolating the v3.0.0 default-flip behaviour to the
# dedicated TestComputeCacheKeyMultiTenant / TestCacheStorageMultiTenant
# suites at the bottom of the file.


@pytest.fixture(autouse=True)
def _permissive_for_legacy_cache_tests(monkeypatch: pytest.MonkeyPatch):
    """Force permissive mode for legacy cache tests (pre-v3.0.0 behaviour)."""
    monkeypatch.setenv("PLUGBOARD_STRICT_TENANT", "0")


class TestComputeCacheKey:
    """compute_cache_key determinism and sensitivity."""

    def test_deterministic(self):
        from plugboard.cache import compute_cache_key

        msgs = [{"role": "user", "content": "hello"}]
        k1 = compute_cache_key("gpt-4o", msgs, temperature=0.7)
        k2 = compute_cache_key("gpt-4o", msgs, temperature=0.7)
        assert k1 == k2

    def test_changes_with_model(self):
        from plugboard.cache import compute_cache_key

        msgs = [{"role": "user", "content": "hello"}]
        k1 = compute_cache_key("gpt-4o", msgs)
        k2 = compute_cache_key("claude-sonnet-4-5", msgs)
        assert k1 != k2

    def test_changes_with_messages(self):
        from plugboard.cache import compute_cache_key

        m1 = [{"role": "user", "content": "hello"}]
        m2 = [{"role": "user", "content": "bye"}]
        k1 = compute_cache_key("gpt-4o", m1)
        k2 = compute_cache_key("gpt-4o", m2)
        assert k1 != k2

    def test_changes_with_params(self):
        from plugboard.cache import compute_cache_key

        msgs = [{"role": "user", "content": "hello"}]
        k1 = compute_cache_key("gpt-4o", msgs, temperature=0.0)
        k2 = compute_cache_key("gpt-4o", msgs, temperature=1.0)
        assert k1 != k2


class TestCacheStorage:
    """set_cached, get_cached, and cache management."""

    @pytest.fixture(autouse=True)
    def _isolate_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Point the cache module at a temporary directory."""
        import plugboard.cache as cache_mod

        monkeypatch.setattr(cache_mod, "_DATA_DIR", tmp_path)
        monkeypatch.setattr(cache_mod, "_DB_PATH", tmp_path / "cache.db")

    def test_set_and_get(self):
        from plugboard.cache import get_cached, set_cached

        resp = {"id": "chatcmpl-1", "choices": [{"text": "hi"}]}
        set_cached("key1", "gpt-4o", "ph1", resp, ttl=60)
        result = get_cached("key1")
        assert result is not None
        assert result["id"] == "chatcmpl-1"
        assert result["_cache"]["hit"] is True

    def test_get_returns_none_on_miss(self):
        from plugboard.cache import get_cached

        assert get_cached("nonexistent") is None

    def test_get_returns_none_after_ttl(self):
        from plugboard.cache import get_cached, set_cached

        resp = {"id": "chatcmpl-2"}
        set_cached("key-ttl", "gpt-4o", "ph", resp, ttl=1)
        time.sleep(1.1)
        assert get_cached("key-ttl") is None

    def test_get_increments_hit_count(self):
        from plugboard.cache import get_cached, set_cached

        resp = {"id": "chatcmpl-3"}
        set_cached("key-hit", "gpt-4o", "ph", resp, ttl=60)
        r1 = get_cached("key-hit")
        assert r1["_cache"]["hit_count"] == 1
        r2 = get_cached("key-hit")
        assert r2["_cache"]["hit_count"] == 2

    def test_eviction_on_max_size(self, monkeypatch: pytest.MonkeyPatch):
        import plugboard.cache as cache_mod
        from plugboard.cache import get_cached, set_cached

        monkeypatch.setattr(cache_mod, "_MAX_CACHE_SIZE", 5)
        for i in range(6):
            set_cached(f"evict-{i}", "gpt-4o", "ph", {"id": str(i)}, ttl=300)
        # Oldest entry should have been evicted
        assert get_cached("evict-0") is None
        # Newest should still be present
        assert get_cached("evict-5") is not None

    def test_clear_cache(self):
        from plugboard.cache import clear_cache, get_cached, set_cached

        set_cached("clear1", "gpt-4o", "ph", {"id": "1"}, ttl=60)
        set_cached("clear2", "gpt-4o", "ph", {"id": "2"}, ttl=60)
        deleted = clear_cache()
        assert deleted == 2
        assert get_cached("clear1") is None
        assert get_cached("clear2") is None

    def test_purge_expired(self):
        from plugboard.cache import get_cached, purge_expired, set_cached

        set_cached("alive", "gpt-4o", "ph", {"id": "a"}, ttl=300)
        set_cached("dead", "gpt-4o", "ph", {"id": "d"}, ttl=1)
        time.sleep(1.1)
        deleted = purge_expired()
        assert deleted == 1
        assert get_cached("alive") is not None
        assert get_cached("dead") is None

    def test_get_cache_stats(self):
        from plugboard.cache import get_cache_stats, get_cached, set_cached

        set_cached("stat1", "gpt-4o", "ph", {"id": "1"}, ttl=60, cost_usd=0.001)
        set_cached("stat2", "gpt-4o", "ph", {"id": "2"}, ttl=60, cost_usd=0.002)
        get_cached("stat1")  # 1 hit on stat1
        stats = get_cache_stats()
        assert stats["entries"] == 2
        assert stats["total_hits"] == 1
        assert stats["total_cost_saved_usd"] == pytest.approx(0.001, abs=1e-4)
        assert "max_size" in stats
        assert "default_ttl" in stats


class TestComputeCacheKeyMultiTenant:
    """SEC-001 v2.8.2 — compute_cache_key includes client_id in the hash."""

    def test_changes_with_client_id(self):
        """Two tenants asking the exact same question must get distinct keys."""
        from plugboard.cache import compute_cache_key

        msgs = [{"role": "user", "content": "What is the password reset URL ?"}]
        k_alice = compute_cache_key("gpt-4o", msgs, client_id="alice")
        k_bob = compute_cache_key("gpt-4o", msgs, client_id="bob")
        assert k_alice != k_bob, "client_id must scope the cache_key (SEC-001)"

    def test_none_and_empty_client_id_equivalent(self):
        """``None`` and ``''`` collapse to the same key (legacy callers)."""
        from plugboard.cache import compute_cache_key

        msgs = [{"role": "user", "content": "hello world"}]
        k_none = compute_cache_key("gpt-4o", msgs, client_id=None)
        k_empty = compute_cache_key("gpt-4o", msgs, client_id="")
        k_default = compute_cache_key("gpt-4o", msgs)
        assert k_none == k_empty == k_default

    def test_legacy_keys_invalidated(self):
        """Pre-v2.8.2 keys (no client_id in hash) differ from post-v2.8.2 keys.

        Documents the implicit cache invalidation: rolling out v2.8.2
        produces fresh keys for all tenants — the old shared rows become
        unreachable (which is the correct security outcome).
        """
        import hashlib
        import json

        from plugboard.cache import compute_cache_key

        msgs = [{"role": "user", "content": "same prompt as before"}]
        # Reproduce the pre-v2.8.2 key shape (no client_id field at all).
        legacy_payload = {"model": "gpt-4o", "messages": msgs}
        legacy_key = hashlib.sha256(
            json.dumps(legacy_payload, sort_keys=True, ensure_ascii=True).encode()
        ).hexdigest()
        new_key = compute_cache_key("gpt-4o", msgs, client_id="alice")
        assert legacy_key != new_key, (
            "rollout invalidates shared pre-v2.8.2 cache rows by construction"
        )


class TestCacheStorageMultiTenant:
    """SEC-001 v2.8.2 — get_cached/set_cached scoped by client_id."""

    @pytest.fixture(autouse=True)
    def _isolate_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import plugboard.cache as cache_mod

        monkeypatch.setattr(cache_mod, "_DATA_DIR", tmp_path)
        monkeypatch.setattr(cache_mod, "_DB_PATH", tmp_path / "cache.db")

    def test_isolation_two_tenants_same_prompt(self):
        """Same prompt + different client_id → distinct keys, strict isolation.

        Matches the production code path: compute_cache_key folds client_id
        into the SHA-256 hash, so alice and bob get distinct cache_keys for
        the same prompt and their rows coexist without PRIMARY KEY collision.
        """
        from plugboard.cache import compute_cache_key, get_cached, set_cached

        msgs = [{"role": "user", "content": "What is the secret URL ?"}]
        ka = compute_cache_key("gpt-4o", msgs, client_id="alice")
        kb = compute_cache_key("gpt-4o", msgs, client_id="bob")
        assert ka != kb  # SEC-001 — invariant of the fix

        set_cached(ka, "gpt-4o", "ph", {"id": "alice-resp"}, client_id="alice")
        set_cached(kb, "gpt-4o", "ph", {"id": "bob-resp"}, client_id="bob")

        ra = get_cached(ka, client_id="alice")
        rb = get_cached(kb, client_id="bob")
        assert ra is not None and ra["id"] == "alice-resp"
        assert rb is not None and rb["id"] == "bob-resp"

        # Cross-tenant lookup with the OTHER tenant's key sees nothing —
        # double safety: hash mismatch AND WHERE filter mismatch.
        assert get_cached(ka, client_id="bob") is None
        assert get_cached(kb, client_id="alice") is None

    def test_legacy_row_accessible_only_with_empty_client(self):
        """Backward compat: rows written with client_id='' (default) stay
        readable by callers that haven't been wired up yet."""
        from plugboard.cache import get_cached, set_cached

        set_cached("legacy-key", "gpt-4o", "ph", {"id": "legacy"})  # default ''
        # Reachable with empty client_id (legacy path).
        assert get_cached("legacy-key") is not None
        assert get_cached("legacy-key", client_id="") is not None
        # NOT reachable from any real tenant.
        assert get_cached("legacy-key", client_id="alice") is None

    def test_migrate_schema_idempotent(self):
        """Running _migrate_schema twice on the same DB must not crash.

        Without the OperationalError catch, the second ALTER TABLE would
        fail on "duplicate column name: client_id".
        """
        from plugboard.cache import _get_conn, _migrate_schema

        conn1 = _get_conn()  # creates DB + applies migration once
        conn1.close()
        conn2 = _get_conn()  # would re-run migration; must not raise
        try:
            _migrate_schema(conn2)  # explicit double-call
            _migrate_schema(conn2)  # triple, just to be sure
        finally:
            conn2.close()

    def test_ttl_delete_is_tenant_scoped(self):
        """Expired entry deletion must not affect another tenant's row."""
        from plugboard.cache import get_cached, set_cached

        set_cached("k", "gpt-4o", "ph", {"id": "a"}, ttl=1, client_id="alice")
        set_cached("k", "gpt-4o", "ph", {"id": "b"}, ttl=300, client_id="bob")
        time.sleep(1.1)

        # Alice's call expires alice's row (and only that row).
        assert get_cached("k", client_id="alice") is None
        # Bob's row still alive.
        rb = get_cached("k", client_id="bob")
        assert rb is not None and rb["id"] == "b"

    def test_strict_tenant_mode_rejects_empty_client_id(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """PLUGBOARD_STRICT_TENANT=1 → cache calls without client_id raise."""
        monkeypatch.setenv("PLUGBOARD_STRICT_TENANT", "1")
        from plugboard.cache import compute_cache_key, get_cached, set_cached

        with pytest.raises(ValueError, match="client_id"):
            compute_cache_key("gpt-4o", [{"role": "user", "content": "x" * 30}])
        with pytest.raises(ValueError, match="client_id"):
            get_cached("any-key")
        with pytest.raises(ValueError, match="client_id"):
            set_cached("any-key", "gpt-4o", "ph", {"id": "x"})

    def test_permissive_mode_warns_but_accepts_empty_client_id(
        self, monkeypatch: pytest.MonkeyPatch, caplog
    ):
        """Opt-out mode (STRICT_TENANT=0) logs WARNING on empty client_id
        but does not raise.

        v3.0.0 BREAKING: the default flipped to strict, so this test
        explicitly opts back into permissive — the rollback path operators
        keep available via env var (manifesto §15.10).
        """
        monkeypatch.setenv("PLUGBOARD_STRICT_TENANT", "0")
        import logging as _log

        from plugboard.cache import compute_cache_key

        with caplog.at_level(_log.WARNING, logger="plugboard.cache"):
            key = compute_cache_key("gpt-4o", [{"role": "user", "content": "hello"}])
        assert isinstance(key, str) and len(key) == 64
        assert any("client_id" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# webhooks.py tests
# ---------------------------------------------------------------------------


class TestSignPayload:
    """HMAC-SHA256 signature generation."""

    def test_sign_payload_produces_hmac_sha256(self, monkeypatch: pytest.MonkeyPatch):
        import plugboard.webhooks as wh_mod

        monkeypatch.setattr(wh_mod, "_WEBHOOK_SECRET", "mysecret")
        payload = b'{"event":"test"}'
        sig = wh_mod._sign_payload(payload)
        expected = hmac.new(b"mysecret", payload, hashlib.sha256).hexdigest()
        assert sig == expected

    def test_sign_payload_returns_empty_without_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        import plugboard.webhooks as wh_mod

        monkeypatch.setattr(wh_mod, "_WEBHOOK_SECRET", "")
        assert wh_mod._sign_payload(b"data") == ""


class TestEmit:
    """Webhook emit behavior."""

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_urls(self, monkeypatch: pytest.MonkeyPatch):
        import plugboard.webhooks as wh_mod

        monkeypatch.setattr(wh_mod, "_WEBHOOK_URLS", [])
        result = await wh_mod.emit("key.created", {"key": "abc"})
        assert result == 0

    @pytest.mark.asyncio
    async def test_sends_to_configured_urls(self, monkeypatch: pytest.MonkeyPatch):
        import plugboard.webhooks as wh_mod

        url1 = "https://hook.example.com/a"
        url2 = "https://hook.example.com/b"
        monkeypatch.setattr(wh_mod, "_WEBHOOK_URLS", [url1, url2])
        monkeypatch.setattr(wh_mod, "_ALLOWED_EVENTS", None)
        monkeypatch.setattr(wh_mod, "_WEBHOOK_SECRET", "")

        with aioresponses() as m:
            m.post(url1, status=200)
            m.post(url2, status=200)
            result = await wh_mod.emit("key.created", {"key": "abc"})
        assert result == 2

    @pytest.mark.asyncio
    async def test_skips_events_not_in_filter(self, monkeypatch: pytest.MonkeyPatch):
        import plugboard.webhooks as wh_mod

        monkeypatch.setattr(wh_mod, "_WEBHOOK_URLS", ["https://hook.example.com/a"])
        monkeypatch.setattr(wh_mod, "_ALLOWED_EVENTS", {"key.created"})

        result = await wh_mod.emit("provider.down", {"provider": "x"})
        assert result == 0

    @pytest.mark.asyncio
    async def test_emit_fire_and_forget_does_not_block(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        import plugboard.webhooks as wh_mod

        monkeypatch.setattr(wh_mod, "_WEBHOOK_URLS", [])
        # Should return immediately without error
        await wh_mod.emit_fire_and_forget("key.created", {"key": "abc"})


# ---------------------------------------------------------------------------
# pricing_scraper.py tests
# ---------------------------------------------------------------------------


class TestMatchKnownPrice:
    """_match_known_price exact, normalized, and suffix matching."""

    def test_exact_match(self):
        from plugboard.pricing_scraper import _match_known_price

        result = _match_known_price("claude-opus-4-5")
        assert result is not None
        assert result["input_price"] == 5.0
        assert result["output_price"] == 25.0

    def test_normalized_match_dots_to_dashes(self):
        from plugboard.pricing_scraper import _match_known_price

        # "gemini-2.5-pro" is in _KNOWN_PRICES; query with dots should still match
        result = _match_known_price("gemini-2.5-pro")
        assert result is not None
        assert result["input_price"] == 1.25

    def test_suffix_match(self):
        from plugboard.pricing_scraper import _match_known_price

        # A long prefix followed by a known suffix should trigger suffix matching
        result = _match_known_price("openai-gpt-4o")
        assert result is not None
        assert result["input_price"] == 2.50

    def test_returns_none_for_unknown(self):
        from plugboard.pricing_scraper import _match_known_price

        assert _match_known_price("totally-unknown-model-xyz") is None


class TestDiscoverNewModels:
    """_discover_new_models finds models missing from existing grid."""

    def test_finds_missing_models(self):
        from plugboard.pricing_scraper import _discover_new_models

        existing = {"gpt-4o": {"input_price": 2.5, "output_price": 10.0}}
        provider_models = ["openai:gpt-4o", "openai:gpt-5-mini"]
        missing = _discover_new_models(existing, provider_models)
        assert "gpt-5-mini" in missing
        assert "gpt-4o" not in missing

    def test_handles_double_dash_separator(self):
        from plugboard.pricing_scraper import _discover_new_models

        existing = {}
        provider_models = ["provider--claude-opus-4-5"]
        missing = _discover_new_models(existing, provider_models)
        assert "claude-opus-4-5" in missing


class TestUpdateCurrentPricing:
    """update_current_pricing integration with no external source."""

    @pytest.fixture(autouse=True)
    def _isolate_billing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        pricing_dir = data_dir / "pricing"
        pricing_dir.mkdir()
        import plugboard.billing as billing_mod

        monkeypatch.setattr(billing_mod, "_DATA_DIR", data_dir)
        monkeypatch.setattr(billing_mod, "_PRICING_DIR", pricing_dir)
        import plugboard.pricing_scraper as ps_mod

        monkeypatch.setattr(ps_mod, "_PRICING_SOURCE_URL", "")

        # Mute the LiteLLM live fetcher so the test doesn't depend on
        # network reachability and can assert source="known_prices".
        async def _no_litellm():
            return {}

        monkeypatch.setattr(ps_mod, "_fetch_litellm_pricing", _no_litellm)

    @pytest.mark.asyncio
    async def test_update_with_no_external_source(self):
        from plugboard.pricing_scraper import update_current_pricing

        result = await update_current_pricing(
            provider_models=["openai:gpt-5-mini"],
        )
        assert result["source"] == "known_prices"
        assert result["added"] >= 1
        assert result["total"] >= 1
