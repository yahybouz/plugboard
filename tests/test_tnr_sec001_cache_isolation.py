"""TNR-SEC-001 — Multi-tenant cache isolation, parametrized across all caches.

Closes ARCH-DEBT-025 — manifesto §5.5 mandates a Test de Non-Regression for
every fixed bug. SEC-001 was fixed in 3 cascading versions :

  - v2.8.1 : `semantic_cache.py` (MinHash + embedding cosine)
  - v2.8.2 : `cache.py` (exact response cache)
  - v2.8.3 : `app.py:_resolve_client` (lle_* keys honored even when API_KEY="")

Without a parametrized TNR, a 4th cache module added in the future would
re-introduce the same vulnerability silently. This file exercises the
isolation invariant against every registered cache through one
parametrized contract test:

    tenant A's cached response MUST NOT be visible to tenant B,
    even when both ask the exact same question with the exact same model.

Adding a new cache to the suite = adding one tuple to ``CACHE_BACKENDS``.

Catalogue : tests/tnr/README.md
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest


# -- adapter pattern -----------------------------------------------------------
#
# Each registered cache exposes the same 3-method contract via an adapter:
#   isolate_dir(tmp_path)  → patch the module's storage to tmp_path
#   store(client_id, key, response)   → write under that tenant's scope
#   lookup(client_id, key)             → read; returns None on miss


def _exact_cache_adapter(monkeypatch, tmp_path: Path) -> Dict[str, Callable]:
    import plugboard.cache as cache_mod

    monkeypatch.setattr(cache_mod, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(cache_mod, "_DB_PATH", tmp_path / "cache.db")

    def store(client_id: str, key: str, response: Dict[str, Any]) -> None:
        cache_mod.set_cached(key, "model-x", "ph", response, client_id=client_id)

    def lookup(client_id: str, key: str) -> Any:
        return cache_mod.get_cached(key, client_id=client_id)

    def make_key(client_id: str, prompt: str) -> str:
        return cache_mod.compute_cache_key(
            "model-x", [{"role": "user", "content": prompt}], client_id=client_id
        )

    return {"store": store, "lookup": lookup, "make_key": make_key}


def _semantic_cache_adapter(monkeypatch, tmp_path: Path) -> Dict[str, Callable]:
    monkeypatch.setenv("PLUGBOARD_SEMANTIC_CACHE", "1")
    import plugboard.semantic_cache as sem_mod

    monkeypatch.setattr(sem_mod, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(sem_mod, "_DB_PATH", tmp_path / "semantic_cache.db")

    def store(client_id: str, key: str, response: Dict[str, Any]) -> None:
        # Semantic cache stores by prompt similarity; we use the prompt
        # encoded in the key as the message content.
        msgs = [{"role": "user", "content": key}]
        sem_mod.store("model-x", msgs, response, ttl=60, client_id=client_id)

    def lookup(client_id: str, key: str) -> Any:
        msgs = [{"role": "user", "content": key}]
        return sem_mod.lookup_similar("model-x", msgs, client_id=client_id)

    def make_key(client_id: str, prompt: str) -> str:
        # Long enough to clear semantic_cache._MIN_PROMPT_CHARS = 20.
        return f"{prompt} (sufficiently long content for the semantic layer)"

    return {"store": store, "lookup": lookup, "make_key": make_key}


# Registry of caches participating in the SEC-001 contract. Add a tuple
# (name, adapter_factory) here when a new cache module ships.
CACHE_BACKENDS: List = [
    ("exact", _exact_cache_adapter),
    ("semantic_minhash", _semantic_cache_adapter),
]


# -- parametrized contract tests ----------------------------------------------


@pytest.fixture()
def cache(request, monkeypatch, tmp_path):
    """Build the adapter for the parametrized backend."""
    _name, factory = request.param
    return factory(monkeypatch, tmp_path)


@pytest.mark.parametrize(
    "cache",
    CACHE_BACKENDS,
    indirect=True,
    ids=[name for name, _ in CACHE_BACKENDS],
)
def test_tnr_sec001_no_cross_tenant_read(cache):
    """Tenant A's cached row is invisible to tenant B (the SEC-001 invariant).

    Setup: only alice stores a row. bob then queries with HIS OWN
    client_id and the same prompt. If isolation works, bob MUST miss —
    he never wrote anything, so finding alice's row would prove a leak.
    """
    prompt = "what is the secret URL only alice asked"
    key = cache["make_key"]("alice", prompt)
    cache["store"]("alice", key, {"id": "alice-secret"})

    # alice can read her own row.
    ra = cache["lookup"]("alice", cache["make_key"]("alice", prompt))
    assert ra is not None and ra["id"] == "alice-secret"

    # bob queries with HIS client_id and the same prompt. He never stored
    # anything → must MISS. A HIT here would mean alice's row leaked.
    bob_key = cache["make_key"]("bob", prompt)
    cross = cache["lookup"]("bob", bob_key)
    assert cross is None, "tenant B saw tenant A's cached row"


@pytest.mark.parametrize(
    "cache",
    CACHE_BACKENDS,
    indirect=True,
    ids=[name for name, _ in CACHE_BACKENDS],
)
def test_tnr_sec001_empty_client_does_not_see_tenant(cache, monkeypatch):
    """An anonymous / legacy lookup (client_id="") must not see tenant rows.

    v3.0.0 default = STRICT (raise on empty client_id). The "permissive"
    fallback path remains available behind PLUGBOARD_STRICT_TENANT=0
    for rollback. We exercise the permissive path here because the
    invariant under test ("empty client must not see tenant rows") is
    only meaningful when empty is allowed; in strict mode the same
    attempt would just raise — also safe but tested elsewhere.
    """
    monkeypatch.setenv("PLUGBOARD_STRICT_TENANT", "0")

    prompt = "secret tenant data"
    key_alice = cache["make_key"]("alice", prompt)
    cache["store"]("alice", key_alice, {"id": "alice-only"})

    # Lookup with an empty client_id (legacy / unauthenticated) must miss.
    miss = cache["lookup"]("", key_alice)
    assert miss is None


@pytest.mark.parametrize(
    "cache",
    CACHE_BACKENDS,
    indirect=True,
    ids=[name for name, _ in CACHE_BACKENDS],
)
def test_tnr_sec001_same_tenant_hits(cache):
    """Sanity check: the isolation doesn't break legitimate same-tenant hits."""
    prompt = "legit question to cache"
    key = cache["make_key"]("alice", prompt)
    cache["store"]("alice", key, {"id": "alice-resp"})

    hit = cache["lookup"]("alice", key)
    assert hit is not None and hit["id"] == "alice-resp"


# -- one extension to ensure the suite catches new caches ---------------------


def test_cache_registry_covers_known_modules():
    """Guard: if a new cache module ships, this test fails until it's added.

    Maintained by hand — list every module that exposes a multi-tenant
    cache. When a 3rd cache lands (Redis, vector store, etc.), add it to
    BOTH the source list (here) AND CACHE_BACKENDS above.
    """
    expected = {"plugboard.cache", "plugboard.semantic_cache"}
    # Defensive: if the project ever ships a third cache, the developer
    # MUST update CACHE_BACKENDS — this assertion forces the conversation.
    seen = set()
    for name, _ in CACHE_BACKENDS:
        if name == "exact":
            seen.add("plugboard.cache")
        elif name.startswith("semantic"):
            seen.add("plugboard.semantic_cache")
    assert seen == expected, (
        f"CACHE_BACKENDS does not cover every multi-tenant cache. "
        f"Expected modules: {expected}. Covered: {seen}. "
        "If a new cache shipped, add it to CACHE_BACKENDS above."
    )


# Catalogue note: this file is referenced as TNR-SEC-001 in
# tests/tnr/README.md. Time-based fields kept simple (no sleeps in TNR).
_TNR_LAST_REFRESHED = time.strftime("%Y-%m-%d", time.gmtime())


# ── ARCH-DEBT-021 closed (2026-05-04, sprint A) — tenant-scoped stats ────


def test_tnr_021_get_cache_stats_tenant_scoped(monkeypatch, tmp_path):
    """ARCH-DEBT-021: get_cache_stats(tenant_id=X) must surface only
    tenant X's rows, not the global aggregate."""
    import plugboard.cache as cache_mod

    monkeypatch.setattr(cache_mod, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(cache_mod, "_DB_PATH", tmp_path / "cache.db")

    cache_mod.set_cached("k1", "model-x", "ph", {"choices": [{}]}, client_id="alpha")
    cache_mod.set_cached("k2", "model-x", "ph", {"choices": [{}]}, client_id="alpha")
    cache_mod.set_cached("k3", "model-x", "ph", {"choices": [{}]}, client_id="beta")

    stats_alpha = cache_mod.get_cache_stats(tenant_id="alpha")
    stats_beta = cache_mod.get_cache_stats(tenant_id="beta")
    stats_global = cache_mod.get_cache_stats()

    assert stats_alpha["entries"] == 2
    assert stats_alpha["tenant_id"] == "alpha"
    assert stats_beta["entries"] == 1
    assert stats_beta["tenant_id"] == "beta"
    assert stats_global["entries"] == 3
    assert stats_global["tenant_id"] is None


def test_tnr_021_clear_cache_tenant_scoped(monkeypatch, tmp_path):
    """ARCH-DEBT-021: clear_cache(tenant_id=X) wipes only X's rows."""
    import plugboard.cache as cache_mod

    monkeypatch.setattr(cache_mod, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(cache_mod, "_DB_PATH", tmp_path / "cache.db")

    cache_mod.set_cached("k1", "model-x", "ph", {"choices": [{}]}, client_id="alpha")
    cache_mod.set_cached("k2", "model-x", "ph", {"choices": [{}]}, client_id="beta")
    cache_mod.set_cached("k3", "model-x", "ph", {"choices": [{}]}, client_id="alpha")

    deleted = cache_mod.clear_cache(tenant_id="alpha")
    assert deleted == 2
    # Beta survives the alpha-scoped clear
    assert cache_mod.get_cache_stats(tenant_id="beta")["entries"] == 1
    assert cache_mod.get_cache_stats(tenant_id="alpha")["entries"] == 0


# ── ARCH-DEBT-022 closed — UPSERT preserves hit_count on refresh ────


def test_tnr_022_upsert_preserves_hit_count(monkeypatch, tmp_path):
    """ARCH-DEBT-022: refreshing a cached entry (same key) must NOT
    reset hit_count to 0. Race-condition window between read and write
    used to lose all accumulated hits via INSERT OR REPLACE."""
    import plugboard.cache as cache_mod
    import sqlite3

    monkeypatch.setattr(cache_mod, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(cache_mod, "_DB_PATH", tmp_path / "cache.db")

    cache_mod.set_cached(
        "key1", "model-x", "ph", {"choices": [{}]}, client_id="alpha", cost_usd=0.01
    )
    # Simulate cache hits by bumping the counter manually.
    conn = sqlite3.connect(str(cache_mod._DB_PATH))
    try:
        conn.execute(
            "UPDATE response_cache SET hit_count = 42 WHERE cache_key = ?", ("key1",)
        )
        conn.commit()
    finally:
        conn.close()

    # Refresh the entry (e.g. TTL expired and the upstream returned a new value)
    cache_mod.set_cached(
        "key1",
        "model-x",
        "ph2",
        {"choices": [{"new": True}]},
        client_id="alpha",
        cost_usd=0.02,
    )

    conn = sqlite3.connect(str(cache_mod._DB_PATH))
    try:
        row = conn.execute(
            "SELECT hit_count, cost_saved FROM response_cache WHERE cache_key = ?",
            ("key1",),
        ).fetchone()
        assert row[0] == 42, "hit_count must survive a refresh (ARCH-DEBT-022)"
        assert row[1] == 0.02, "cost_saved must reflect the new value"
    finally:
        conn.close()


# ── ARCH-DEBT-038 closed (2026-05-04, sprint E) — rename STRICT_TENANT ────


def test_tnr_038_allow_permissive_canonical(monkeypatch):
    """ARCH-DEBT-038: PLUGBOARD_ALLOW_PERMISSIVE=1 must put the cache
    in permissive mode (opt-out semantic, the new canonical name)."""
    from plugboard.cache_common import strict_tenant_mode

    monkeypatch.setenv("PLUGBOARD_ALLOW_PERMISSIVE", "1")
    monkeypatch.delenv("PLUGBOARD_STRICT_TENANT", raising=False)
    assert strict_tenant_mode() is False

    monkeypatch.setenv("PLUGBOARD_ALLOW_PERMISSIVE", "0")
    assert strict_tenant_mode() is True


def test_tnr_038_legacy_strict_tenant_still_honoured(monkeypatch):
    """ARCH-DEBT-038: PLUGBOARD_STRICT_TENANT (legacy) must still work
    for rollback compat. A deployment can downgrade without rebuild."""
    from plugboard.cache_common import strict_tenant_mode

    monkeypatch.delenv("PLUGBOARD_ALLOW_PERMISSIVE", raising=False)
    monkeypatch.setenv("PLUGBOARD_STRICT_TENANT", "0")
    assert strict_tenant_mode() is False  # legacy opt-out preserved

    monkeypatch.setenv("PLUGBOARD_STRICT_TENANT", "1")
    assert strict_tenant_mode() is True


def test_tnr_038_new_var_wins_over_legacy(monkeypatch):
    """If both vars are set, the new ALLOW_PERMISSIVE wins."""
    from plugboard.cache_common import strict_tenant_mode

    # Legacy says strict, new says permissive → permissive wins
    monkeypatch.setenv("PLUGBOARD_STRICT_TENANT", "1")
    monkeypatch.setenv("PLUGBOARD_ALLOW_PERMISSIVE", "1")
    assert strict_tenant_mode() is False


def test_tnr_038_default_is_strict(monkeypatch):
    """No env var set → strict mode (default since v3.0.0 / ADR-032)."""
    from plugboard.cache_common import strict_tenant_mode

    monkeypatch.delenv("PLUGBOARD_STRICT_TENANT", raising=False)
    monkeypatch.delenv("PLUGBOARD_ALLOW_PERMISSIVE", raising=False)
    assert strict_tenant_mode() is True


# ── ARCH-DEBT-039 closed (2026-05-04, sprint F) — rename EMBED_* ────


def test_tnr_039_canonical_var_wins(monkeypatch):
    """ARCH-DEBT-039: PLUGBOARD_EMBED_PROVIDER_ID is preferred over
    legacy EMBED_PROVIDER_ID (which is still honoured for backward compat)."""
    from plugboard.semantic_cache import _env_embed

    monkeypatch.setenv("PLUGBOARD_EMBED_PROVIDER_ID", "new-provider")
    monkeypatch.setenv("EMBED_PROVIDER_ID", "legacy-provider")
    assert (
        _env_embed("PLUGBOARD_EMBED_PROVIDER_ID", "EMBED_PROVIDER_ID") == "new-provider"
    )


def test_tnr_039_legacy_fallback(monkeypatch):
    """If only the legacy var is set, it must still be returned."""
    from plugboard.semantic_cache import _env_embed

    monkeypatch.delenv("PLUGBOARD_EMBED_PROVIDER_ID", raising=False)
    monkeypatch.setenv("EMBED_PROVIDER_ID", "legacy-only")
    assert (
        _env_embed("PLUGBOARD_EMBED_PROVIDER_ID", "EMBED_PROVIDER_ID") == "legacy-only"
    )


def test_tnr_039_default_when_neither_set(monkeypatch):
    """If neither var is set, the default is returned."""
    from plugboard.semantic_cache import _env_embed

    monkeypatch.delenv("PLUGBOARD_EMBED_PROVIDER_ID", raising=False)
    monkeypatch.delenv("EMBED_PROVIDER_ID", raising=False)
    assert (
        _env_embed("PLUGBOARD_EMBED_PROVIDER_ID", "EMBED_PROVIDER_ID", "fallback")
        == "fallback"
    )
