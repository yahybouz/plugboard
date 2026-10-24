"""Real routing transformations with literal oracles and no upstream calls.

Catches provider re-selection from display aliases, native tag destruction,
and admission performed only after a destructive rewrite.
"""

from __future__ import annotations

import asyncio

import pytest


def provider(pid, default, models=(), **extra):
    return {
        "id": pid,
        "name": pid,
        "is_active": True,
        "base_url": f"https://{pid}.synthetic.invalid/v1",
        "default_model": default,
        "models": [{"id": name} for name in models],
        **extra,
    }


@pytest.fixture
def routing(monkeypatch):
    from plugboard import model_exposure, router

    monkeypatch.setattr(router, "_models_cache", None)
    monkeypatch.setattr(router, "_models_cache_ts", 0)
    monkeypatch.setattr(router, "_models_alias_cache", {})
    monkeypatch.setattr(router, "_model_filter_mode", lambda: "all")
    monkeypatch.setattr(model_exposure, "mode", lambda: "all")
    monkeypatch.setattr(router.metrics, "get_stats", lambda: {})
    return router


class BeforeTransport(BaseException):
    """Escape retries before session.post can issue any I/O."""


async def observe(router, monkeypatch, mode, request, expected):
    """Exercise the actual resolver/forwarder; expected is a literal pair or None."""
    calls = []
    acquisitions = []
    if mode in ("single", "multiple"):
        resolve = (
            router.resolve_provider_and_model
            if mode == "single"
            else router._resolve_all_providers
        )
        if expected is None:
            with pytest.raises(ValueError, match="Use /v1/decisions"):
                resolve(request)
            return
        result = resolve(request)
        pairs = result if mode == "multiple" else [result]
        assert [(p["id"], name) for p, name in pairs] == [expected]
        return

    from plugboard import circuit_breaker, http_pool

    monkeypatch.setattr(circuit_breaker, "check_circuit", lambda _: {"allowed": True})

    class Session:
        def post(self, url, **kwargs):
            calls.append((url, kwargs["json"]["model"]))
            raise BeforeTransport

    async def get_session():
        acquisitions.append(True)
        return Session()

    async def auth(_provider):
        return {}

    monkeypatch.setattr(http_pool, "get_session", get_session)
    monkeypatch.setattr(router, "_build_auth_header_async", auth)
    body = {"model": request, "messages": [{"role": "user", "content": "hi"}]}
    call = (
        router.forward(body)
        if mode == "forward"
        else anext(router.forward_stream(body))
    )
    if expected is None:
        with pytest.raises(ValueError, match="Use /v1/decisions"):
            await asyncio.wait_for(call, 2)
        assert acquisitions == []
        assert calls == []
    else:
        with pytest.raises(BeforeTransport):
            await asyncio.wait_for(call, 2)
        pid, native = expected
        assert calls == [
            (f"https://{pid}.synthetic.invalid/v1/chat/completions", native)
        ]
        assert acquisitions == [True]


# Expectations do not use any production classifier, cleaner or codec.
DIRECT_NATIVE = [
    ("qwen3:kev", "qwen3:kev"),
    ("qwen3:laya", "qwen3:laya"),
    ("qwen3:jev", "qwen3:jev"),
    ("gpt-5:laya", "gpt-5:laya"),
    ("qwen3:convai/laya@rev", "qwen3:convai/laya@rev"),
    ("unknown:jev", "unknown:jev"),
    ("laya:latest", None),
    ("jev:latest", None),
    ("kev-0.8b:latest", None),
    ("convaiinnovations/laya:latest", None),
    ("ollama:kev-0.8b:latest", None),
]


@pytest.mark.parametrize("mode", ["single", "multiple", "forward", "stream"])
@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("form", ["bare", "qualified", "default", "qualified_default"])
@pytest.mark.parametrize("name,wire", DIRECT_NATIVE)
async def test_direct_identity_through_both_transports(
    routing, monkeypatch, mode, warm, form, name, wire
):
    # YB #78 supersedes bridge encoding; native identity remains opaque.
    pid = "direct"
    monkeypatch.setattr(routing, "load_providers", lambda: [provider(pid, name)])
    if warm:
        rows = await routing.list_models()
        assert bool(rows) is (wire is not None)
    request = {
        "bare": name,
        "qualified": pid + ":" + name,
        "default": "",
        "qualified_default": pid + ":",
    }[form]
    expected = (
        None if wire is None else (pid, name if form.startswith("qualified") else wire)
    )
    await observe(routing, monkeypatch, mode, request, expected)


CLI_PAIRS = [
    ("chatgpt-via-codex-cli", "Codex", "gpt-5", "codex-cli"),
    ("google-gemini-via-gemini-cli", "Gemini", "gemini-2.5-pro", "gemini-cli"),
    ("claude-via-claude-cli", "claude-code", "claude-sonnet-4-5", "claude-cli"),
]


@pytest.mark.parametrize("canonical,homonym,stem,legacy", CLI_PAIRS)
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("mode", ["single", "multiple", "forward", "stream"])
async def test_real_catalogue_promotes_only_selected_owner(
    routing, monkeypatch, canonical, homonym, stem, legacy, reverse, warm, mode
):
    # Same family, two owners, distinct revisions. Display IDs may collide;
    # their native routing identities must not, in either registration order.
    a = provider(canonical, stem + "-20260101", [stem + "-20260101"])
    b = provider(homonym, stem + "-20260202", [stem + "-20260202"])
    providers = [b, a] if reverse else [a, b]
    monkeypatch.setattr(routing, "load_providers", lambda: providers)
    if warm:
        await routing.list_models()
        assert canonical + ":" + stem in routing._models_alias_cache
        assert homonym + ":" + stem in routing._models_alias_cache
    for pid, suffix in [(canonical, "-20260101"), (homonym, "-20260202")]:
        await observe(
            routing,
            monkeypatch,
            mode,
            pid + ":" + stem,
            (pid, stem + suffix if warm else stem),
        )
        await observe(routing, monkeypatch, mode, pid + ":", (pid, stem + suffix))
        await observe(
            routing, monkeypatch, mode, pid + ":" + stem + suffix, (pid, stem + suffix)
        )
    # With no registered homonym, display aliases still promote the canonical.
    monkeypatch.setattr(routing, "load_providers", lambda: [a])
    await observe(
        routing,
        monkeypatch,
        mode,
        homonym + ":" + stem,
        (canonical, stem + "-20260101" if warm else stem),
    )


@pytest.mark.parametrize("mode", ["single", "multiple", "forward", "stream"])
@pytest.mark.parametrize("reverse", [False, True])
async def test_longest_original_registered_provider_precedes_alias(
    routing, monkeypatch, mode, reverse
):
    providers = [provider("owner", "gpt-5"), provider("owner:lane", "qwen3:kev")]
    if reverse:
        providers.reverse()
    monkeypatch.setattr(routing, "load_providers", lambda: providers)
    monkeypatch.setattr(
        routing, "_models_alias_cache", {"owner:lane:short": "owner:lane:qwen3:kev"}
    )
    await observe(
        routing, monkeypatch, mode, "owner:lane:short", ("owner:lane", "qwen3:kev")
    )


@pytest.mark.parametrize("mode", ["single", "multiple", "forward", "stream"])
@pytest.mark.parametrize(
    "legacy,auth",
    [
        ("codex-cli", "subprocess:codex"),
        ("gemini-cli", "subprocess:gemini"),
        ("claude-cli", "subprocess:claude"),
    ],
)
def test_legacy_selection_uses_registered_auth_source(
    routing, monkeypatch, mode, legacy, auth
):
    # Resolution only: transport tests never execute a subprocess provider.
    providers = [
        provider("ordinary", "gpt-5"),
        provider("actual-cli", "qwen3:kev", auth_source=auth),
    ]
    monkeypatch.setattr(routing, "load_providers", lambda: providers)
    resolve = (
        routing.resolve_provider_and_model
        if mode in ("single", "stream")
        else routing._resolve_all_providers
    )
    result = resolve(legacy + ":qwen3:kev")
    pairs = result if isinstance(result, list) else [result]
    assert [(p["id"], name) for p, name in pairs] == [("actual-cli", "qwen3:kev")]


@pytest.mark.parametrize("mode", ["single", "multiple", "forward", "stream"])
async def test_injected_alias_cannot_reassign_owner_and_decision_is_guarded(
    routing, monkeypatch, mode
):
    monkeypatch.setattr(
        routing,
        "load_providers",
        lambda: [provider("a", "gpt-5"), provider("b", "gpt-5")],
    )
    monkeypatch.setattr(routing, "_models_alias_cache", {"a:ordinary": "b:gpt-5"})
    await observe(routing, monkeypatch, mode, "a:ordinary", ("a", "ordinary"))
    monkeypatch.setattr(
        routing, "_models_alias_cache", {"a:ordinary": "a:ollama:kev-0.8b:latest"}
    )
    await observe(routing, monkeypatch, mode, "a:ordinary", None)


@pytest.mark.parametrize("mode", ["single", "multiple", "forward", "stream"])
async def test_alias_cannot_launder_configured_decision_default(
    routing, monkeypatch, mode
):
    monkeypatch.setattr(
        routing, "load_providers", lambda: [provider("owner", "laya:latest")]
    )
    monkeypatch.setattr(
        routing, "_models_alias_cache", {"owner:laya:latest": "owner:gpt-5"}
    )
    await observe(routing, monkeypatch, mode, "", None)


@pytest.mark.parametrize("mode", ["forward", "stream"])
@pytest.mark.parametrize(
    "legacy,auth,kind",
    [
        ("codex-cli", "subprocess:codex", "codex"),
        ("gemini-cli", "subprocess:gemini", "gemini"),
        ("claude-cli", "subprocess:claude", "claude"),
    ],
)
async def test_legacy_alias_reaches_only_expected_cli_boundary(
    routing, monkeypatch, mode, legacy, auth, kind
):
    from plugboard import circuit_breaker, cli_subprocess, http_pool

    providers = [
        provider("ordinary", "gpt-5"),
        provider("actual-cli", "qwen3:kev", auth_source=auth),
    ]
    monkeypatch.setattr(routing, "load_providers", lambda: providers)
    monkeypatch.setattr(circuit_breaker, "check_circuit", lambda _: {"allowed": True})
    calls = []

    async def complete(selected_kind, body, **kwargs):
        calls.append((selected_kind, body["model"]))
        raise BeforeTransport

    async def pool():
        return object()  # HTTP .post must never be used for this request.

    monkeypatch.setattr(cli_subprocess, "complete", complete)
    monkeypatch.setattr(http_pool, "get_session", pool)
    body = {"model": legacy + ":qwen3:kev"}
    call = (
        routing.forward(body)
        if mode == "forward"
        else anext(routing.forward_stream(body))
    )
    with pytest.raises(BeforeTransport):
        await asyncio.wait_for(call, 2)
    assert calls == [(kind, "qwen3:kev")]


@pytest.mark.parametrize("mode", ["single", "multiple", "forward", "stream"])
@pytest.mark.parametrize(
    "native",
    [
        "ollama:qwen3:kev",
        "Codex:gpt-5:laya",
        "qwen3@convai/laya",
    ],
)
async def test_direct_preserves_native_prefix_and_revision(
    routing, monkeypatch, mode, native
):
    monkeypatch.setattr(routing, "load_providers", lambda: [provider("direct", native)])
    await observe(routing, monkeypatch, mode, "", ("direct", native))


def test_multiple_fallback_chooses_each_providers_own_default(routing, monkeypatch):
    monkeypatch.setattr(
        routing,
        "load_providers",
        lambda: [provider("a", "qwen3:kev"), provider("b", "gpt-5:laya")],
    )
    assert [(p["id"], name) for p, name in routing._resolve_all_providers("")] == [
        ("a", "qwen3:kev"),
        ("b", "gpt-5:laya"),
    ]


@pytest.mark.parametrize("canonical,homonym,stem,legacy", CLI_PAIRS)
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("request_form", ["bare", "default"])
async def test_unqualified_fallback_keeps_each_owners_alias_and_default(
    routing, monkeypatch, canonical, homonym, stem, legacy, reverse, warm, request_form
):
    a = provider(canonical, stem, [stem + "-20260101"])
    b = provider(homonym, stem, [stem + "-20260202"])
    providers = [b, a] if reverse else [a, b]
    monkeypatch.setattr(routing, "load_providers", lambda: providers)
    if warm:
        await routing.list_models()
    request = stem if request_form == "bare" else ""
    expected = [
        (canonical, stem + "-20260101" if warm else stem),
        (homonym, stem + "-20260202" if warm else stem),
    ]
    if reverse:
        expected.reverse()
    assert [
        (p["id"], name) for p, name in routing._resolve_all_providers(request)
    ] == expected
    selected = routing.resolve_provider_and_model(request)
    assert (selected[0]["id"], selected[1]) == expected[0]
    for mode in ("forward", "stream"):
        await observe(routing, monkeypatch, mode, request, expected[0])
