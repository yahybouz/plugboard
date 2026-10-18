"""Tests for plugboard.semantic_cache.

The module is opt-in via PLUGBOARD_SEMANTIC_CACHE=1; these tests enable
the flag per-test via monkeypatch so they don't leak into other suites.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def sem_mod(tmp_path, monkeypatch):
    """Reload semantic_cache bound to a fresh tmp_path + enabled flag."""
    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path))
    monkeypatch.setenv("PLUGBOARD_SEMANTIC_CACHE", "1")
    import plugboard.semantic_cache as sem

    sem = importlib.reload(sem)
    # Reloading re-reads env vars at module level, but _DB_PATH is cached
    # from the previous state; patch it explicitly.
    sem._DATA_DIR = tmp_path
    sem._DB_PATH = tmp_path / "semantic_cache.db"
    yield sem
    sem.clear()


def test_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("PLUGBOARD_SEMANTIC_CACHE", raising=False)
    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path))
    import plugboard.semantic_cache as sem

    sem = importlib.reload(sem)
    assert sem.is_enabled() is False
    # lookup/store are no-ops when disabled
    assert (
        sem.lookup_similar(
            "gpt-4o",
            [{"role": "user", "content": "hello world how are you today ?"}],
        )
        is None
    )


def test_shingles_and_minhash_are_stable(sem_mod):
    a = sem_mod._shingles("How do I reset my password today please")
    b = sem_mod._shingles("how do i reset my password today please")
    assert a == b  # case-insensitive normalization

    sig_a = sem_mod._minhash(a)
    sig_b = sem_mod._minhash(b)
    assert sig_a == sig_b  # stable hashing


def test_store_then_lookup_identical(sem_mod):
    messages = [{"role": "user", "content": "How do I reset my password today ?"}]
    response = {
        "id": "r1",
        "choices": [{"message": {"content": "Click forgot password."}}],
    }
    sem_mod.store("gpt-4o", messages, response, ttl=60, cost_usd=0.01)

    hit = sem_mod.lookup_similar("gpt-4o", messages)
    assert hit is not None
    assert hit["id"] == "r1"
    assert hit["_cache"]["semantic"] is True
    assert hit["_cache"]["similarity"] >= 0.99


def test_lookup_near_duplicate_matches(sem_mod):
    # n-gram Jaccard (what MinHash approximates) reliably catches near-
    # duplicates: typos, extra/missing punctuation, minor phrasing changes.
    # True synonym-level paraphrasing needs embeddings (out of scope for
    # the stdlib-only phase 1).
    original = [
        {
            "role": "user",
            "content": (
                "What is the difference between Python lists and tuples in "
                "terms of performance and memory usage ?"
            ),
        }
    ]
    near_dup = [
        {
            "role": "user",
            "content": (
                "What is difference between Python lists and tuples in terms "
                "of performance and memory usage"
            ),
        }
    ]
    sem_mod.store("gpt-4o", original, {"id": "orig", "choices": []}, ttl=60)

    hit = sem_mod.lookup_similar("gpt-4o", near_dup, threshold=0.5)
    assert hit is not None, "near-duplicate should hit with threshold 0.5"
    assert hit["id"] == "orig"


def test_lookup_unrelated_misses(sem_mod):
    sem_mod.store(
        "gpt-4o",
        [{"role": "user", "content": "How do I reset my password on this service ?"}],
        {"id": "a", "choices": []},
        ttl=60,
    )
    hit = sem_mod.lookup_similar(
        "gpt-4o",
        [
            {
                "role": "user",
                "content": "Describe the history of the Roman empire please",
            }
        ],
        threshold=0.85,
    )
    assert hit is None


def test_lookup_model_isolation(sem_mod):
    messages = [{"role": "user", "content": "How do I reset my password today ?"}]
    sem_mod.store("gpt-4o", messages, {"id": "a", "choices": []}, ttl=60)
    # Different model id → candidates must not cross models.
    hit = sem_mod.lookup_similar("claude-sonnet-4", messages)
    assert hit is None


def test_short_prompt_skipped(sem_mod):
    sem_mod.store("gpt-4o", [{"role": "user", "content": "hi"}], {"id": "x"}, ttl=60)
    # Still stored? No — _MIN_PROMPT_CHARS filters at store too.
    stats = sem_mod.get_stats()
    assert stats["entries"] == 0


def test_ttl_expires(sem_mod, monkeypatch):
    import time as _time

    messages = [{"role": "user", "content": "How do I reset my password today ?"}]
    sem_mod.store("gpt-4o", messages, {"id": "old", "choices": []}, ttl=1)
    # Simulate time passing by patching time.time inside the module.
    real_time = _time.time
    monkeypatch.setattr("plugboard.semantic_cache.time.time", lambda: real_time() + 5)
    hit = sem_mod.lookup_similar("gpt-4o", messages)
    assert hit is None


def test_stats_and_clear(sem_mod):
    messages = [{"role": "user", "content": "How do I reset my password today ?"}]
    sem_mod.store("gpt-4o", messages, {"id": "a", "choices": []}, ttl=60, cost_usd=0.02)
    stats = sem_mod.get_stats()
    assert stats["enabled"] is True
    assert stats["entries"] == 1
    sem_mod.clear()
    stats2 = sem_mod.get_stats()
    assert stats2["entries"] == 0


def test_purge_expired_removes_row(sem_mod, monkeypatch):
    """purge_expired() actually deletes rows past TTL and reports the count.

    Regression test (S3): the function existed but had no test asserting it
    physically removes the row — only that lookup returns None on expiry.
    """
    import time as _time

    messages_old = [{"role": "user", "content": "How do I reset my password today ?"}]
    messages_fresh = [
        {"role": "user", "content": "What is the capital of France today ?"}
    ]
    sem_mod.store("gpt-4o", messages_old, {"id": "old"}, ttl=1)
    sem_mod.store("gpt-4o", messages_fresh, {"id": "fresh"}, ttl=3600)
    assert sem_mod.get_stats()["entries"] == 2

    real_time = _time.time
    monkeypatch.setattr("plugboard.semantic_cache.time.time", lambda: real_time() + 10)

    deleted = sem_mod.purge_expired()
    assert deleted == 1, "exactly one expired row should be removed"
    assert sem_mod.get_stats()["entries"] == 1
    # The fresh entry must still be retrievable (proves we deleted the right one).
    hit = sem_mod.lookup_similar("gpt-4o", messages_fresh)
    assert hit is not None and hit["id"] == "fresh"


def test_lookup_client_isolation(sem_mod):
    """SEC-001: tenant A must never see tenant B's cached responses.

    Same model, same prompt, two different client_ids → two distinct cache
    entries; lookups are scoped, no cross-tenant bleed.
    """
    messages = [{"role": "user", "content": "How do I reset my password today ?"}]

    sem_mod.store("gpt-4o", messages, {"id": "secret-A"}, ttl=60, client_id="tenant-A")
    sem_mod.store("gpt-4o", messages, {"id": "secret-B"}, ttl=60, client_id="tenant-B")

    # Both rows coexist (different cache_keys because client_id is hashed in).
    assert sem_mod.get_stats()["entries"] == 2

    # Each tenant only sees its own entry.
    hit_a = sem_mod.lookup_similar("gpt-4o", messages, client_id="tenant-A")
    hit_b = sem_mod.lookup_similar("gpt-4o", messages, client_id="tenant-B")
    assert hit_a is not None and hit_a["id"] == "secret-A"
    assert hit_b is not None and hit_b["id"] == "secret-B"

    # An unknown / empty client must NOT see either tenant's data.
    assert sem_mod.lookup_similar("gpt-4o", messages, client_id="other") is None
    assert sem_mod.lookup_similar("gpt-4o", messages) is None  # default ""


def test_extract_multimodal_user_prompt(sem_mod):
    messages = [
        {"role": "system", "content": "be helpful"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Summarize this picture please"},
                {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
            ],
        },
    ]
    prompt = sem_mod._extract_user_prompt(messages)
    assert "Summarize this picture please" in prompt


def test_store_replaces_same_prompt(sem_mod):
    messages = [{"role": "user", "content": "How do I reset my password today ?"}]
    sem_mod.store("gpt-4o", messages, {"id": "v1"}, ttl=60)
    sem_mod.store("gpt-4o", messages, {"id": "v2"}, ttl=60)
    assert sem_mod.get_stats()["entries"] == 1
    hit = sem_mod.lookup_similar("gpt-4o", messages)
    assert hit["id"] == "v2"


# -- Phase 2 (embedding) tests ------------------------------------------------


@pytest.fixture()
def sem_embed_mod(tmp_path, monkeypatch):
    """Reload semantic_cache with phase 2 enabled, but stub the embed call.

    The actual embedding HTTP path is exercised in
    test_embed_call_uses_provider; here we stub _embed to a deterministic
    fake vector so we can test cosine lookup logic.
    """
    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path))
    monkeypatch.setenv("PLUGBOARD_SEMANTIC_CACHE", "1")
    monkeypatch.setenv("PLUGBOARD_SEMANTIC_EMBED", "1")
    monkeypatch.setenv("EMBED_PROVIDER_ID", "stub-provider")
    monkeypatch.setenv("EMBED_MODEL", "stub-embed")
    import plugboard.semantic_cache as sem

    sem = importlib.reload(sem)
    sem._DATA_DIR = tmp_path
    sem._DB_PATH = tmp_path / "semantic_cache.db"
    yield sem
    sem.clear()


def test_embed_disabled_when_flag_off(monkeypatch, tmp_path):
    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path))
    monkeypatch.setenv("PLUGBOARD_SEMANTIC_CACHE", "1")
    monkeypatch.delenv("PLUGBOARD_SEMANTIC_EMBED", raising=False)
    import plugboard.semantic_cache as sem

    sem = importlib.reload(sem)
    assert sem.embed_is_enabled() is False


def test_embed_disabled_when_provider_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path))
    monkeypatch.setenv("PLUGBOARD_SEMANTIC_CACHE", "1")
    monkeypatch.setenv("PLUGBOARD_SEMANTIC_EMBED", "1")
    monkeypatch.delenv("EMBED_PROVIDER_ID", raising=False)
    monkeypatch.delenv("EMBED_MODEL", raising=False)
    import plugboard.semantic_cache as sem

    sem = importlib.reload(sem)
    assert sem.embed_is_enabled() is False


def test_pack_unpack_embedding_roundtrip(sem_embed_mod):
    vec = [0.1, -0.2, 0.3, 0.4, -0.5]
    packed = sem_embed_mod._pack_embedding(vec)
    out = sem_embed_mod._unpack_embedding(packed)
    assert len(out) == len(vec)
    for a, b in zip(vec, out):
        assert abs(a - b) < 1e-6


def test_schema_migration_adds_embedding_columns(sem_embed_mod):
    conn = sem_embed_mod._get_conn()
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(semantic_cache)")}
        assert "embed_model" in cols
        assert "embedding" in cols
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_store_async_attaches_embedding_when_enabled(sem_embed_mod, monkeypatch):
    """store_async should populate the embedding column via _attach_embedding."""
    fake_vec = [0.1] * 32

    async def fake_embed(text):
        return fake_vec

    monkeypatch.setattr(sem_embed_mod, "_embed", fake_embed)

    messages = [{"role": "user", "content": "How do I reset my password today ?"}]
    cache_key = await sem_embed_mod.store_async(
        "gpt-4o", messages, {"id": "with-embed"}, ttl=60
    )
    assert cache_key is not None
    # Wait for fire-and-forget _attach_embedding to finish.
    await sem_embed_mod.flush_pending()

    conn = sem_embed_mod._get_conn()
    try:
        row = conn.execute(
            "SELECT embedding, embed_model FROM semantic_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        assert row["embedding"] is not None
        assert row["embed_model"] == "stub-embed"
        recovered = sem_embed_mod._unpack_embedding(row["embedding"])
        assert len(recovered) == len(fake_vec)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_lookup_similar_async_falls_back_to_embedding(sem_embed_mod, monkeypatch):
    """When MinHash misses but embeddings are close, cosine path should hit."""

    # Fake embed: same prompt → same vec, completely different prompt → orthogonal.
    def fake_vec(text):
        if "password" in text:
            return [1.0, 0.0, 0.0, 0.0]
        if "login" in text:
            # Near-paraphrase of "password" → nearly-aligned vector
            return [0.99, 0.0, 0.05, 0.0]
        return [0.0, 1.0, 0.0, 0.0]

    async def fake_embed(text):
        return fake_vec(text)

    monkeypatch.setattr(sem_embed_mod, "_embed", fake_embed)

    # Store a "password" entry with embedding attached.
    pw_msg = [{"role": "user", "content": "How do I reset my password today please"}]
    await sem_embed_mod.store_async("gpt-4o", pw_msg, {"id": "pw"}, ttl=60)
    await sem_embed_mod.flush_pending()

    # Lookup with a "login" wording — MinHash trigrams won't overlap, but
    # cosine on the fake embeddings is ~0.99 → should hit.
    login_msg = [
        {"role": "user", "content": "I urgently need to recover my login credential"}
    ]
    hit = await sem_embed_mod.lookup_similar_async(
        "gpt-4o", login_msg, embed_threshold=0.9
    )
    assert hit is not None
    assert hit["id"] == "pw"
    assert hit["_cache"]["embedding"] is True
    assert hit["_cache"]["similarity"] >= 0.9


@pytest.mark.asyncio
async def test_lookup_similar_async_minhash_path_short_circuits(
    sem_embed_mod, monkeypatch
):
    """If MinHash hits, we MUST NOT call the embedding API (saves latency)."""
    calls = {"n": 0}

    async def counting_embed(text):
        calls["n"] += 1
        return [1.0] * 4

    monkeypatch.setattr(sem_embed_mod, "_embed", counting_embed)

    msg = [{"role": "user", "content": "How do I reset my password today please"}]
    await sem_embed_mod.store_async("gpt-4o", msg, {"id": "exact"}, ttl=60)
    await sem_embed_mod.flush_pending()

    # Reset call counter (store_async did one embed for attach).
    calls["n"] = 0

    hit = await sem_embed_mod.lookup_similar_async("gpt-4o", msg)
    assert hit is not None
    assert hit["id"] == "exact"
    # MinHash hit → embedding API must not have been called.
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_embed_call_returns_none_when_provider_missing(
    sem_embed_mod, monkeypatch
):
    """If EMBED_PROVIDER_ID points to a non-existent provider, _embed returns None gracefully."""

    # Mock get_provider to return None
    def fake_get_provider(pid):
        return None

    import plugboard.config as cfg

    monkeypatch.setattr(cfg, "get_provider", fake_get_provider)
    vec = await sem_embed_mod._embed("hello world this is a test prompt")
    assert vec is None


def test_get_stats_exposes_embed_state(sem_embed_mod):
    stats = sem_embed_mod.get_stats()
    assert stats["enabled"] is True
    assert stats["embed_enabled"] is True
    assert stats["embed_provider_id"] == "stub-provider"
    assert stats["embed_model"] == "stub-embed"
    assert "embed_threshold" in stats
    assert "entries_with_embedding" in stats
