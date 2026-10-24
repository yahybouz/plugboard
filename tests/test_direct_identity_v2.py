"""Interaction oracles for the owner/native/publication/health contract.

All transports and persisted state are synthetic. Expected identities are
literal fixtures, never computed by a production cleaner or classifier.
"""

from __future__ import annotations

import pytest

from plugboard import config, mcp_server, model_availability, router
from plugboard.legacy_connectors import is_removed_model
from tests.test_decision_routing_v5 import BeforeTransport, observe, provider
from tests.test_decision_routing_v5 import routing as routing  # pytest fixture


FAMILIES = [
    ("claude-via-claude-cli", "claude-code"),
    ("chatgpt-via-codex-cli", "Codex"),
    ("google-gemini-via-gemini-cli", "Gemini"),
]


@pytest.mark.parametrize("canonical,alias", FAMILIES)
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("mode", ["single", "multiple", "forward", "stream"])
async def test_published_long_owner_roundtrips_beside_short_owner(
    routing, monkeypatch, canonical, alias, reverse, warm, mode
):
    short = provider(canonical, "gpt-5", ["gpt-5"])
    long = provider(canonical + ":lane", "qwen3:jev", ["qwen3:jev"])
    providers = [long, short] if reverse else [short, long]
    monkeypatch.setattr(routing, "load_providers", lambda: providers)
    monkeypatch.setattr(routing, "_model_status_mtime_changed", lambda: False)
    rows = await routing.list_models()
    row = next(row for row in rows if row["owned_by"] == long["id"])
    assert row["id"] == canonical + ":lane:qwen3:jev"
    if warm:
        assert await routing.list_models() == rows
    else:
        routing.invalidate_models_cache()
    await observe(
        routing, monkeypatch, mode, row["id"], (canonical + ":lane", "qwen3:jev")
    )


@pytest.mark.parametrize("canonical,alias", FAMILIES)
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("mode", ["single", "multiple", "forward", "stream"])
async def test_display_alias_collision_publishes_both_actual_accounts(
    routing, monkeypatch, canonical, alias, reverse, warm, mode
):
    canonical_account = provider(canonical, "qwen3:jev", ["qwen3:jev"])
    display_homonym = provider(alias, "gpt-5", ["gpt-5"])
    providers = (
        [display_homonym, canonical_account]
        if reverse
        else [canonical_account, display_homonym]
    )
    monkeypatch.setattr(routing, "load_providers", lambda: providers)
    rows = await routing.list_models()
    by_owner = {row["owned_by"]: row["id"] for row in rows}
    assert by_owner == {
        canonical: canonical + ":qwen3:jev",
        alias: alias + ":gpt-5",
    }
    if not warm:
        routing.invalidate_models_cache()
    for request, expected in [
        (by_owner[canonical], (canonical, "qwen3:jev")),
        (by_owner[alias], (alias, "gpt-5")),
    ]:
        await observe(routing, monkeypatch, mode, request, expected)


class CountedModel(str):
    """Account for every copying transform, including returned tuple members."""

    copied = 0

    @classmethod
    def wrap(cls, value):
        if isinstance(value, (tuple, list)):
            return type(value)(cls.wrap(part) for part in value)
        cls.copied += len(value)
        return cls(value)

    def __str__(self):
        raise AssertionError("The scanner must not erase copy accounting via str()")

    def __getitem__(self, key):
        value = super().__getitem__(key)
        return self.wrap(value) if isinstance(key, slice) else value

    def strip(self, *args):
        return self.wrap(super().strip(*args))

    def lstrip(self, *args):
        return self.wrap(super().lstrip(*args))

    def rstrip(self, *args):
        return self.wrap(super().rstrip(*args))

    def lower(self):
        return self.wrap(super().lower())

    def casefold(self):
        return self.wrap(super().casefold())

    def partition(self, sep):
        return self.wrap(super().partition(sep))

    def rpartition(self, sep):
        return self.wrap(super().rpartition(sep))

    def split(self, *args):
        return self.wrap(super().split(*args))

    def rsplit(self, *args):
        return self.wrap(super().rsplit(*args))

    def removeprefix(self, prefix):
        return self.wrap(super().removeprefix(prefix))

    def removesuffix(self, suffix):
        return self.wrap(super().removesuffix(suffix))

    def replace(self, *args):
        return self.wrap(super().replace(*args))


@pytest.mark.parametrize("count", [128, 512, 2048])
@pytest.mark.parametrize(
    "tail,removed", [("gpt-5", False), ("openclaw/openai--gpt-5", True)]
)
def test_nested_alias_scanner_bounds_all_copies(count, tail, removed):
    model = CountedModel("codex:" * count + tail)
    CountedModel.copied = 0
    assert is_removed_model(model) is removed
    assert CountedModel.copied <= 32 * len(model), (
        f"{CountedModel.copied} copied characters for {len(model)} input characters"
    )


@pytest.mark.parametrize(
    "native,removed",
    [
        ("qwen3:openclaw", False),
        ("qwen3:openclaw/notes@rev", False),
        ("org/openclaw-notes:latest", False),
        ("openclaw/openai--gpt-5", True),
        ("openclaw:gpt-5", True),
    ],
)
def test_native_tokens_stay_opaque_but_actual_wrapper_is_rejected(native, removed):
    assert is_removed_model(native, effective_name=True) is removed


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("removed_is_long", [False, True])
async def test_quarantine_preserves_direct_observation_and_never_invents_probe(
    monkeypatch, reverse, removed_is_long
):
    short = provider("owner", "gpt-5", ["gpt-5"])
    long = provider("owner:lane", "qwen3:jev", ["qwen3:jev"])
    (long if removed_is_long else short)["auth_source"] = "openclaw"
    providers = [long, short] if reverse else [short, long]
    previous = {
        "models": {
            "owner:gpt-5": {
                "provider": "owner",
                "connected": True,
                "last_probed_at": "2026-10-01T01:02:03Z",
            },
            "owner:lane:qwen3:jev": {
                "provider": "owner:lane",
                "connected": True,
                "last_probed_at": "2026-10-02T04:05:06Z",
            },
        }
    }
    monkeypatch.setattr(config, "load_providers", lambda: providers)
    monkeypatch.setattr(model_availability, "load_model_status", lambda path: previous)
    monkeypatch.setattr(model_availability, "save_model_status", lambda *args: None)
    result = await model_availability.run_connectivity_probes(probe_kinds=set())
    direct_key = "owner:gpt-5" if removed_is_long else "owner:lane:qwen3:jev"
    removed_key = "owner:lane:qwen3:jev" if removed_is_long else "owner:gpt-5"
    assert result["models"][direct_key] == previous["models"][direct_key]
    assert result["models"][removed_key]["connected"] is False
    assert result["models"][removed_key]["reason"] == "legacy_connector_removed"
    assert (
        result["models"][removed_key]["last_probed_at"]
        == previous["models"][removed_key]["last_probed_at"]
    )


@pytest.mark.parametrize("reverse", [False, True])
async def test_explicit_health_owner_precedes_conflicting_key_prefix(
    monkeypatch, reverse
):
    providers = [
        provider("owner", "gpt-5", auth_source="openclaw"),
        provider("owner:lane", "qwen3:jev"),
    ]
    if reverse:
        providers.reverse()
    previous = {
        "models": {
            "owner:historical-label": {
                "provider": "owner:lane",
                "connected": False,
                "last_probed_at": "2026-10-03T01:00:00Z",
                "reason": "timeout",
            },
        }
    }
    monkeypatch.setattr(config, "load_providers", lambda: providers)
    monkeypatch.setattr(model_availability, "load_model_status", lambda path: previous)
    monkeypatch.setattr(model_availability, "save_model_status", lambda *args: None)
    result = await model_availability.run_connectivity_probes(probe_kinds=set())
    assert (
        result["models"]["owner:historical-label"]
        == previous["models"]["owner:historical-label"]
    )


async def test_mcp_stream_defers_registered_owner_question_to_authenticated_gateway(
    monkeypatch,
):
    sent = []

    class Content:
        def __aiter__(self):
            return self.lines()

        async def lines(self):
            yield b'data: {"choices":[{"delta":{"content":"synthetic"}}]}\n'
            yield b"data: [DONE]\n"

    class Response:
        status = 200
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class Gateway:
        def post(self, url, **kwargs):
            sent.append(
                (url, kwargs["json"]["model"], kwargs["headers"].get("Authorization"))
            )
            return Response()

    async def session():
        return Gateway()

    monkeypatch.setattr(mcp_server, "_get_http_session", session)
    monkeypatch.setattr(mcp_server, "_BASE_URL", "https://gateway.synthetic.invalid")
    monkeypatch.setattr(mcp_server, "_API_KEY", "synthetic-gateway-key")
    reply = await mcp_server.chat_stream(
        "claude-via-claude-cli:openclaw:qwen3:jev", "synthetic"
    )
    assert reply == "synthetic"
    assert sent == [
        (
            "https://gateway.synthetic.invalid/v1/chat/completions",
            "claude-via-claude-cli:openclaw:qwen3:jev",
            "Bearer synthetic-gateway-key",
        )
    ]


@pytest.mark.parametrize("mode", ["chat", "stream"])
@pytest.mark.parametrize(
    "model", ["openclaw:gpt-5", "openclaw/openai--gpt-5", "openclaw-llm-proxy:gpt-5"]
)
async def test_mcp_actual_legacy_is_rejected_before_gateway(monkeypatch, mode, model):
    async def forbidden(*args, **kwargs):
        pytest.fail("A removed connector reached the gateway")

    monkeypatch.setattr(mcp_server, "_api_post", forbidden)
    monkeypatch.setattr(mcp_server, "_get_http_session", forbidden)
    invoke = mcp_server.chat if mode == "chat" else mcp_server.chat_stream
    reply = await invoke(model, "synthetic")
    assert "removed" in reply.lower()


async def test_full_scheduled_refresh_rejects_effective_default_before_auth(
    monkeypatch,
):
    from plugboard import http_pool, usage

    invalid = provider("direct", "openclaw/openai--gpt-5", auto_discover=True)
    monkeypatch.setattr(config, "load_providers", lambda: [invalid])
    monkeypatch.setattr(usage, "get_all_used_models", lambda: set())
    calls = []

    async def auth(record):
        calls.append("auth")
        raise BeforeTransport()

    async def pool():
        calls.append("pool")
        raise BeforeTransport()

    monkeypatch.setattr(router, "_build_auth_header_async", auth)
    monkeypatch.setattr(http_pool, "get_session", pool)
    report = None
    try:
        report = await model_availability.refresh_all_model_catalogs(
            pass_kind="scheduled"
        )
    except BeforeTransport:
        pass
    assert calls == [], (
        f"Invalid default reached credential/transport boundary: {calls}"
    )
    assert report is not None
    assert any(
        item["provider"] == "direct" and "removed" in item.get("error", "").lower()
        for item in report["providers"]
    )


@pytest.mark.parametrize(
    "connected,stamp,icon",
    [
        (True, "2026-10-01T00:00:00Z", "🟢"),
        (False, "2026-10-02T00:00:00Z", "🔴"),
        (None, None, None),
    ],
)
async def test_catalogue_and_mcp_preserve_real_observations_but_unknown_is_neutral(
    routing, monkeypatch, connected, stamp, icon
):
    monkeypatch.setattr(
        routing,
        "load_providers",
        lambda: [provider("direct", "qwen3:jev", ["qwen3:jev"])],
    )
    status = (
        {}
        if connected is None
        else {
            "direct:qwen3:jev": {
                "provider": "direct",
                "connected": connected,
                "last_probed_at": stamp,
            }
        }
    )
    monkeypatch.setattr(
        model_availability, "load_model_status", lambda: {"models": status}
    )
    rows = await routing.list_models()
    assert len(rows) == 1
    assert rows[0]["connected"] is (False if connected is None else connected)
    assert rows[0]["last_probed_at"] == stamp
    # Unknown health must not make a newly configured provider unroutable.
    assert routing.resolve_provider_and_model("direct:qwen3:jev")[1] == "qwen3:jev"

    async def api_get(path):
        return {"data": rows}

    monkeypatch.setattr(mcp_server, "_api_get", api_get)
    rendered = await mcp_server.list_models()
    if icon is None:
        assert "🟢" not in rendered
        assert "🔴" not in rendered
        assert "never probed" in rendered
    else:
        assert icon in rendered
        assert stamp in rendered


async def test_missing_mcp_observation_has_neither_positive_nor_negative_icon(
    monkeypatch,
):
    async def api_get(path):
        return {
            "data": [
                {"id": "direct:qwen3:jev", "provider": "direct", "owned_by": "direct"}
            ]
        }

    monkeypatch.setattr(mcp_server, "_api_get", api_get)
    rendered = await mcp_server.list_models()
    assert "🟢" not in rendered
    assert "🔴" not in rendered
    assert "never probed" in rendered


async def test_catalogue_does_not_borrow_another_explicit_owners_observation(
    routing, monkeypatch
):
    short = provider("owner", "gpt-5", ["gpt-5"])
    long = provider("owner:lane", "qwen3:jev", ["qwen3:jev"])
    monkeypatch.setattr(routing, "load_providers", lambda: [short, long])
    monkeypatch.setattr(
        model_availability,
        "load_model_status",
        lambda: {
            "models": {
                "owner:lane:qwen3:jev": {
                    "provider": "owner",
                    "connected": True,
                    "last_probed_at": "2026-10-01T01:00:00Z",
                },
            }
        },
    )
    rows = await routing.list_models()
    row = next(row for row in rows if row["owned_by"] == "owner:lane")
    assert row["connected"] is False
    assert row["last_probed_at"] is None


async def test_mcp_catalogue_respects_canonical_owner_with_legacy_token(monkeypatch):
    async def api_get(path):
        return {
            "data": [
                {
                    "id": "claude-via-claude-cli:openclaw:qwen3:jev",
                    "owned_by": "claude-via-claude-cli:openclaw",
                    "provider": "claude-via-claude-cli:openclaw",
                    "connected": True,
                    "last_probed_at": "2026-10-01T01:00:00Z",
                }
            ]
        }

    monkeypatch.setattr(mcp_server, "_api_get", api_get)
    rendered = await mcp_server.list_models()
    assert "**1 models available:**" in rendered
    assert "`claude-via-claude-cli:openclaw:qwen3:jev`" in rendered
    assert "🟢" in rendered


async def test_quarantine_without_observation_does_not_manufacture_timestamp(
    monkeypatch,
):
    removed = provider("retired-account", "gpt-5", ["gpt-5"], auth_source="openclaw")
    monkeypatch.setattr(config, "load_providers", lambda: [removed])
    monkeypatch.setattr(
        model_availability, "load_model_status", lambda path: {"models": {}}
    )
    monkeypatch.setattr(model_availability, "save_model_status", lambda *args: None)
    result = await model_availability.run_connectivity_probes(probe_kinds=set())
    entry = result["models"]["retired-account:gpt-5"]
    assert entry["connected"] is False
    assert entry.get("last_probed_at") is None
