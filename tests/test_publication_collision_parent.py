"""Publication round trips; omission is required if existing IDs cannot be safe.

The review explicitly allows suppressing an ambiguous row, not new syntax or
reassigning an explicit long owner. Controls still require safe rows to exist.
"""

import pytest

from plugboard import router
from tests.test_decision_routing_v5 import provider
from tests.test_decision_routing_v5 import routing as routing


async def _observe(monkeypatch, mode, request, expected, url):
    if mode in ("single", "multiple"):
        answer = (
            router.resolve_provider_and_model(request)
            if mode == "single"
            else router._resolve_all_providers(request)
        )
        pairs = [answer] if mode == "single" else answer
        assert [(p["id"], native) for p, native in pairs] == [expected]
        return
    from plugboard import circuit_breaker, http_pool

    calls = []

    class Boundary(BaseException):
        pass

    class Session:
        def post(self, address, **kwargs):
            calls.append((address, kwargs["json"]["model"]))
            raise Boundary()

    async def pool():
        return Session()

    async def auth(record):
        return {}

    monkeypatch.setattr(http_pool, "get_session", pool)
    monkeypatch.setattr(router, "_build_auth_header_async", auth)
    monkeypatch.setattr(circuit_breaker, "check_circuit", lambda _: {"allowed": True})
    with pytest.raises(Boundary):
        if mode == "forward":
            await router.forward({"model": request, "messages": []})
        else:
            await anext(router.forward_stream({"model": request, "messages": []}))
    assert calls == [(url + "/chat/completions", expected[1])]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("mode", ["single", "multiple", "forward", "stream"])
@pytest.mark.parametrize("shadow", [False, True])
async def test_published_short_owner_is_exact_or_omitted(
    routing, monkeypatch, reverse, warm, mode, shadow
):
    short = provider("owner", "qwen3:latest", ["qwen3:latest"])
    long = provider("owner:qwen3" if shadow else "owner:lane", "gpt-5", ["gpt-5"])
    short["base_url"] = "https://short.synthetic.invalid/v1"
    long["base_url"] = "https://long.synthetic.invalid/v1"
    monkeypatch.setattr(
        router, "load_providers", lambda: [long, short] if reverse else [short, long]
    )
    monkeypatch.setattr(router, "_model_status_mtime_changed", lambda: False)
    rows = await router.list_models()
    long_rows = [row for row in rows if row["owned_by"] == long["id"]]
    assert len(long_rows) == 1
    assert router.resolve_provider_and_model(long_rows[0]["id"]) == (long, "gpt-5")
    short_rows = [row for row in rows if row["owned_by"] == short["id"]]
    if warm:
        assert await router.list_models() == rows
    else:
        router.invalidate_models_cache()
    if shadow and not short_rows:
        # Accepted closed state, but the unaffected long account must still work.
        assert len(rows) == 1
        await _observe(
            monkeypatch,
            mode,
            long_rows[0]["id"],
            (long["id"], "gpt-5"),
            long["base_url"],
        )
    else:
        assert len(short_rows) == 1
        assert short_rows[0]["id"] == "owner:qwen3:latest"
        await _observe(
            monkeypatch,
            mode,
            short_rows[0]["id"],
            (short["id"], "qwen3:latest"),
            short["base_url"],
        )


@pytest.mark.parametrize(
    "canonical,alias",
    [
        ("claude-via-claude-cli", "claude-code"),
        ("chatgpt-via-codex-cli", "Codex"),
        ("google-gemini-via-gemini-cli", "Gemini"),
    ],
)
async def test_canonical_fallback_is_validated_or_omitted(
    routing, monkeypatch, canonical, alias
):
    short = provider(canonical, "qwen3:latest", ["qwen3:latest"])
    long = provider(canonical + ":qwen3", "gpt-5", ["gpt-5"])
    homonym = provider(alias, "gpt-4o", ["gpt-4o"])
    monkeypatch.setattr(router, "load_providers", lambda: [short, long, homonym])
    rows = await router.list_models()
    for record, native in ((long, "gpt-5"), (homonym, "gpt-4o")):
        entries = [row for row in rows if row["owned_by"] == record["id"]]
        assert len(entries) == 1
        assert router.resolve_provider_and_model(entries[0]["id"]) == (record, native)
    short_rows = [row for row in rows if row["owned_by"] == canonical]
    if short_rows:
        assert len(short_rows) == 1
        assert router.resolve_provider_and_model(short_rows[0]["id"]) == (
            short,
            "qwen3:latest",
        )
    else:
        assert len(rows) == 2


@pytest.mark.parametrize(
    "canonical,alias",
    [
        ("claude-via-claude-cli", "claude-code"),
        ("chatgpt-via-codex-cli", "Codex"),
        ("google-gemini-via-gemini-cli", "Gemini"),
    ],
)
@pytest.mark.parametrize("marked", [False, True])
@pytest.mark.parametrize("mode", ["single", "multiple", "forward", "stream"])
async def test_quarantined_alias_is_still_reserved(
    routing, monkeypatch, canonical, alias, marked, mode
):
    direct = provider(canonical, "qwen3:latest", ["qwen3:latest"])
    homonym = provider(
        alias, "gpt-5", ["gpt-5"], auth_source="openclaw" if marked else ""
    )
    direct["base_url"] = "https://direct.synthetic.invalid/v1"
    homonym["base_url"] = "https://homonym.synthetic.invalid/v1"
    monkeypatch.setattr(router, "load_providers", lambda: [direct, homonym])
    rows = await router.list_models()
    entries = [row for row in rows if row["owned_by"] == canonical]
    assert len(entries) == 1  # A safe existing canonical spelling is available.
    assert entries[0]["id"] == canonical + ":qwen3:latest"
    assert router.resolve_provider_and_model(canonical + ":qwen3:latest") == (
        direct,
        "qwen3:latest",
    )
    if marked:
        assert not any(row["owned_by"] == alias for row in rows)
        with pytest.raises(ValueError, match="removed"):
            router.resolve_provider_and_model(alias + ":gpt-5")
    await _observe(
        monkeypatch,
        mode,
        entries[0]["id"],
        (canonical, "qwen3:latest"),
        direct["base_url"],
    )
