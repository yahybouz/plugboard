"""Removed bridge admission and exact direct-provider transport, fully offline."""

import asyncio

import pytest

from tests.test_decision_routing_v5 import BeforeTransport, observe, provider
from tests.test_decision_routing_v5 import routing as routing


@pytest.mark.parametrize("mode", ["single", "multiple", "forward", "stream"])
@pytest.mark.parametrize(
    "model_request",
    [
        "openclaw:gpt-5",
        "openclaw-llm-proxy:gpt-5",
        "openclaw/openai--gpt-5",
        "direct:openclaw/openai--gpt-5",
        "direct:openclaw:gpt-5",
    ],
)
async def test_removed_model_never_reaches_pool(
    routing, monkeypatch, mode, model_request
):
    from plugboard import http_pool

    monkeypatch.setattr(
        routing, "load_providers", lambda: [provider("direct", "gpt-5")]
    )
    acquisitions = []

    async def pool():
        acquisitions.append(True)
        raise BeforeTransport

    monkeypatch.setattr(http_pool, "get_session", pool)
    with pytest.raises(
        ValueError, match="(?i)(unsupported|removed).*(legacy|connector)"
    ):
        if mode == "single":
            routing.resolve_provider_and_model(model_request)
        elif mode == "multiple":
            routing._resolve_all_providers(model_request)
        elif mode == "forward":
            await asyncio.wait_for(routing.forward({"model": model_request}), 2)
        else:
            await asyncio.wait_for(
                anext(routing.forward_stream({"model": model_request})), 2
            )
    assert acquisitions == []


@pytest.mark.parametrize(
    "marker",
    [{"id": "openclaw"}, {"provider_type": "openclaw"}, {"auth_source": "openclaw"}],
)
async def test_discovery_refuses_removed_record_before_pool(
    routing, monkeypatch, marker
):
    from plugboard import http_pool

    acquisitions = []

    async def pool():
        acquisitions.append(True)
        raise BeforeTransport

    monkeypatch.setattr(http_pool, "get_session", pool)
    record = provider("renamed", "gpt-5") | marker
    with pytest.raises(
        ValueError, match="(?i)(unsupported|removed).*(legacy|connector)"
    ):
        await routing.discover_models(record)
    assert acquisitions == []


async def test_catalogue_excludes_removed_records_and_models(routing, monkeypatch):
    records = [
        provider("openclaw", "gpt-5"),
        provider("renamed", "gpt-5", auth_source="openclaw"),
        provider(
            "direct", "gpt-5", ["gpt-5", "openclaw/openai--gpt-5", "openclaw:gpt-5"]
        ),
    ]
    monkeypatch.setattr(routing, "load_providers", lambda: records)
    assert [(m["owned_by"], m["id"]) for m in await routing.list_models()] == [
        ("direct", "direct:gpt-5")
    ]


@pytest.mark.parametrize("mode", ["single", "multiple", "forward", "stream"])
@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize(
    "native",
    ["qwen3:kev", "qwen3:laya", "qwen3:jev", "acme/qwen3@rev", "ollama:qwen3:kev"],
)
async def test_substring_name_keeps_native_model_exact(
    routing, monkeypatch, mode, warm, native
):
    pid = "team-openclaw-notes"
    monkeypatch.setattr(
        routing, "load_providers", lambda: [provider(pid, native, [native])]
    )
    if warm:
        await routing.list_models()
    await observe(routing, monkeypatch, mode, native, (pid, native))


@pytest.mark.parametrize(
    "resolve", ["resolve_provider_and_model", "_resolve_all_providers"]
)
def test_legacy_alias_cache_cannot_reactivate_bridge(routing, monkeypatch, resolve):
    monkeypatch.setattr(
        routing, "load_providers", lambda: [provider("direct", "gpt-5")]
    )
    monkeypatch.setattr(
        routing,
        "_models_alias_cache",
        {"direct:short": "direct:openclaw/openai--gpt-5"},
    )
    with pytest.raises(
        ValueError, match="(?i)(unsupported|removed).*(legacy|connector)"
    ):
        getattr(routing, resolve)("direct:short")


@pytest.mark.parametrize("mode", ["single", "multiple", "forward", "stream"])
async def test_longest_registered_owner_precedes_shorter_removed_owner(
    routing, monkeypatch, mode
):
    records = [
        provider("owner", "gpt-5", auth_source="openclaw"),
        provider("owner:lane", "qwen3:kev"),
    ]
    monkeypatch.setattr(routing, "load_providers", lambda: records)
    await observe(
        routing, monkeypatch, mode, "owner:lane:qwen3:kev", ("owner:lane", "qwen3:kev")
    )


async def test_warm_catalogue_refilters_removed_owner_metadata(routing, monkeypatch):
    records = [
        provider("direct", "gpt-5"),
        provider("renamed", "gpt-5", auth_source="openclaw"),
    ]
    monkeypatch.setattr(routing, "load_providers", lambda: records)
    monkeypatch.setattr(routing, "_models_cache_ts", routing._time.monotonic())
    monkeypatch.setattr(routing, "_model_status_mtime_changed", lambda: False)
    monkeypatch.setattr(
        routing,
        "_models_cache",
        [
            {"id": "direct:gpt-5", "owned_by": "direct"},
            {"id": "Codex:gpt-5", "owned_by": "renamed"},
            {"id": "openclaw:gpt-5", "owned_by": "openclaw"},
            {"id": "direct:openclaw/openai--gpt-5", "owned_by": "direct"},
        ],
    )
    assert [(row["id"], row["owned_by"]) for row in await routing.list_models()] == [
        ("direct:gpt-5", "direct")
    ]


async def test_warm_catalogue_rebuilds_when_registered_owner_changes(
    routing, monkeypatch
):
    records = [provider("old-owner", "qwen3:kev")]
    monkeypatch.setattr(routing, "load_providers", lambda: records)
    monkeypatch.setattr(routing, "_model_status_mtime_changed", lambda: False)
    assert [(row["id"], row["owned_by"]) for row in await routing.list_models()] == [
        ("old-owner:qwen3:kev", "old-owner")
    ]
    records[:] = [provider("new-owner", "qwen3:jev")]
    assert [(row["id"], row["owned_by"]) for row in await routing.list_models()] == [
        ("new-owner:qwen3:jev", "new-owner")
    ]
