"""Identity admission must precede destructive catalogue transformations.

Literal survivors catch late omission; real stage wrappers catch early wire
formatting, while dated promotion and health require the original native ID.
"""

import pytest

from tests.test_decision_routing_v5 import provider
from tests.test_decision_routing_v5 import routing as routing


ACCOUNTS = [
    ("owner", "owner", "qwen3"),
    ("owner:lane", "owner:lane", "qwen3"),
    ("claude-via-claude-cli", "claude-code", "claude-sonnet-4-7"),
    ("chatgpt-via-codex-cli", "Codex", "gpt-5"),
    ("google-gemini-via-gemini-cli", "Gemini", "gemini-3-pro"),
]


@pytest.fixture(autouse=True)
def synthetic_observations(monkeypatch):
    from plugboard import model_availability, model_quarantine

    monkeypatch.setattr(model_availability, "load_model_status", lambda: {"models": {}})
    monkeypatch.setattr(model_quarantine, "get_quarantined_suffixes", lambda: set())


@pytest.mark.parametrize("owner,alias,native", ACCOUNTS)
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("state", ["active", "inactive", "removed"])
@pytest.mark.parametrize("policy", ["all", "exposure", "curated", "topN"])
async def test_safe_bare_survives_in_every_publication_policy(
    routing, monkeypatch, owner, alias, native, reverse, state, policy
):
    from plugboard import model_exposure

    short = provider(owner, native, [native, native + ":latest"])
    long = provider(owner + ":" + native, "gpt-5", ["gpt-5"])
    blockers = [long]
    if alias != owner:
        blockers.append(provider(alias, native, [native]))
    for record in blockers:
        if state == "inactive":
            record["is_active"] = False
        elif state == "removed":
            record["auth_source"] = "openclaw"
    witness = provider("safe", "gpt-4o", ["gpt-4o"])
    accounts = [short, *blockers, witness]
    if reverse:
        accounts.reverse()
    monkeypatch.setattr(routing, "load_providers", lambda: list(accounts))
    monkeypatch.setattr(routing, "_model_status_mtime_changed", lambda: False)
    monkeypatch.setattr(
        model_exposure, "mode", lambda: "all" if policy == "all" else "latest"
    )
    monkeypatch.setattr(
        routing,
        "_model_filter_mode",
        lambda: "curated" if policy == "curated" else "all",
    )
    monkeypatch.setenv("PLUGBOARD_TOP_N_PER_PROVIDER", "1" if policy == "topN" else "0")
    rows = await routing.list_models()
    assert [row["id"] for row in rows if row["owned_by"] == owner] == [
        owner + ":" + native
    ]
    expected = {owner + ":" + native, "safe:gpt-4o"}
    if state == "active":
        expected.add(long["id"] + ":gpt-5")
        if alias != owner:
            expected.add(alias + ":" + native)
    assert {row["id"] for row in rows} == expected
    for row in rows:
        record, resolved = routing.resolve_provider_and_model(row["id"])
        assert record["id"] == row["owned_by"]
        assert resolved == (native if record is short else record["default_model"])
        assert not any(key.startswith("_") for key in row)
    assert await routing.list_models() == rows


@pytest.mark.parametrize("owner,alias,native", ACCOUNTS)
@pytest.mark.parametrize("source", ["models", "extras"])
async def test_real_stages_receive_admissible_raw_ids(
    routing, monkeypatch, owner, alias, native, source
):
    from plugboard import model_catalog

    short = provider(
        owner,
        native,
        [native, native + ":latest"] if source == "models" else [],
        extra_model_ids=[] if source == "models" else [native, native + ":latest"],
    )
    long = provider(owner + ":" + native, "gpt-5", ["gpt-5"])
    accounts = [short, long]
    if alias != owner:
        accounts.append(provider(alias, "gpt-4o", ["gpt-4o"]))
    reads = []

    def registry():
        reads.append(True)
        return list(accounts)

    monkeypatch.setattr(routing, "load_providers", registry)
    monkeypatch.setattr(routing, "_model_status_mtime_changed", lambda: False)
    stages = []

    def capture(name, original):
        def call(rows, *args):
            rows = list(rows)
            own = [row["id"] for row in rows if row["owned_by"] == owner]
            assert own == [owner + ":" + native], name
            stages.append(name)
            return original(rows, *args)

        return call

    for name in ("enrich", "dedupe_family_snapshots", "build_alias_map"):
        monkeypatch.setattr(
            model_catalog, name, capture(name, getattr(model_catalog, name))
        )
    monkeypatch.setattr(
        routing, "_apply_exposure", capture("exposure", routing._apply_exposure)
    )
    rows = await routing.list_models()
    assert len(reads) == 1
    assert stages[:4] == [
        "enrich",
        "dedupe_family_snapshots",
        "exposure",
        "build_alias_map",
    ]
    assert [row["id"] for row in rows if row["owned_by"] == owner] == [
        owner + ":" + native
    ]

    def no_rebuild(_):
        pytest.fail("A stable omitted identity must not force a cache rebuild")

    monkeypatch.setattr(model_catalog, "enrich", no_rebuild)
    assert await routing.list_models() == rows
    assert len(reads) == 2


@pytest.mark.parametrize("owner,alias,native", ACCOUNTS)
@pytest.mark.parametrize("shadow", [False, True])
async def test_dated_promotion_and_health_keep_native_identity(
    routing, monkeypatch, owner, alias, native, shadow
):
    from plugboard import model_availability, model_catalog, model_quarantine

    dated = native + "-20260101"
    short = provider(owner, dated, [dated, native + ":latest"])
    accounts = [short]
    if shadow:
        accounts.append(provider(owner + ":" + native, "gpt-5", ["gpt-5"]))
        if alias != owner:
            accounts.append(provider(alias, "gpt-4o", ["gpt-4o"]))
    raw = owner + ":" + dated
    monkeypatch.setattr(routing, "load_providers", lambda: accounts)
    monkeypatch.setattr(
        model_availability,
        "load_model_status",
        lambda: {
            "models": {
                raw: {
                    "provider": owner,
                    "connected": True,
                    "last_probed_at": "2026-10-24T00:00:00Z",
                }
            }
        },
    )
    monkeypatch.setattr(model_quarantine, "get_quarantined_suffixes", lambda: {dated})
    original_alias_map = model_catalog.build_alias_map
    seen_raw = []

    def aliases(rows):
        rows = list(rows)
        if not seen_raw:
            seen_raw.extend(row["id"] for row in rows if row["owned_by"] == owner)
        return original_alias_map(rows)

    monkeypatch.setattr(model_catalog, "build_alias_map", aliases)
    rows = await routing.list_models()
    own = [row for row in rows if row["owned_by"] == owner]
    assert len(own) == 1
    row = own[0]
    if shadow:
        assert seen_raw == [raw]
        assert row["id"] == raw
        assert (
            row["family"],
            row["variant"],
            row["promoted_from"],
            row["aliases"],
        ) == (native, "", "20260101", [native])
        assert row["connected"] is True
        assert row["last_probed_at"] == "2026-10-24T00:00:00Z"
        assert "reason" not in row
        assert routing._models_alias_cache[owner + ":" + native] == raw
        assert routing.resolve_provider_and_model(owner + ":" + native) == (
            short,
            dated,
        )
    else:
        assert seen_raw == [owner + ":" + native + ":latest"]
        assert row["id"] == alias + ":" + native + ":latest"
        assert row["variant"] == "latest"
        assert row["connected"] is False
        assert row["last_probed_at"] is None
        assert owner + ":" + native not in routing._models_alias_cache
    assert routing.resolve_provider_and_model(row["id"]) == (
        short,
        dated if shadow else native + ":latest",
    )


@pytest.mark.parametrize("warm", [False, True])
async def test_api_and_mcp_keep_the_safe_survivor(routing, monkeypatch, warm):
    from starlette.requests import Request
    from plugboard import app as api, mcp_server

    accounts = [
        provider("owner", "qwen3", ["qwen3", "qwen3:latest"]),
        provider("owner:qwen3", "gpt-5", ["gpt-5"]),
        provider("safe", "gpt-4o", ["gpt-4o"]),
    ]
    monkeypatch.setattr(routing, "load_providers", lambda: accounts)
    monkeypatch.setattr(api, "load_providers", lambda: accounts)
    monkeypatch.setattr(api, "_get_request_key_record", lambda *a, **k: None)
    monkeypatch.setattr(api, "_model_is_compatible", lambda *a, **k: True)
    if warm:
        await routing.list_models()
    result = await api.models(
        Request({"type": "http", "headers": [], "method": "GET", "path": "/v1/models"}),
        client_id=None,
    )
    expected = {"owner:qwen3", "owner:qwen3:gpt-5", "safe:gpt-4o"}
    assert {row["id"] for row in result["data"]} == expected
    assert {
        row["id"] for row in (await api.models_v2(client_id=None))["data"]
    } == expected

    async def api_get(path):
        assert path == "/v1/models"
        return result

    monkeypatch.setattr(mcp_server, "_api_get", api_get)
    rendered = await mcp_server.list_models()
    assert "**3 models available:**" in rendered
    assert all("`" + mid + "`" in rendered for mid in expected)
    assert "`owner:qwen3:latest`" not in rendered


@pytest.mark.parametrize("reverse", [False, True])
async def test_inexpressible_new_version_cannot_hide_safe_exposure_version(
    routing, monkeypatch, reverse
):
    from plugboard import model_exposure, usage

    short = provider("owner", "unrelated", ["gpt-5:cloud", "gpt-6:cloud"])
    long = provider("owner:gpt-6", "gpt-4o", ["gpt-4o"])
    accounts = [short, long]
    if reverse:
        accounts.reverse()
    monkeypatch.setattr(routing, "load_providers", lambda: accounts)
    monkeypatch.setattr(model_exposure, "mode", lambda: "latest")
    monkeypatch.setattr(usage, "get_all_used_models", lambda: [])
    monkeypatch.setenv("PLUGBOARD_MODEL_EXPOSE_VERSIONS", "1")
    rows = await routing.list_models()
    assert {row["id"] for row in rows} == {"owner:gpt-5:cloud", "owner:gpt-6:gpt-4o"}


@pytest.mark.parametrize("reverse", [False, True])
async def test_identical_raw_spelling_keeps_distinct_owner_admission(
    routing, monkeypatch, reverse
):
    short = provider("owner", "qwen3", ["qwen3", "qwen3:latest"])
    long = provider("owner:qwen3", "latest", ["latest"])
    accounts = [short, long]
    if reverse:
        accounts.reverse()
    monkeypatch.setattr(routing, "load_providers", lambda: accounts)
    rows = await routing.list_models()
    assert {(row["owned_by"], row["id"]) for row in rows} == {
        ("owner", "owner:qwen3"),
        ("owner:qwen3", "owner:qwen3:latest"),
    }
    assert routing.resolve_provider_and_model("owner:qwen3") == (short, "qwen3")
    assert routing.resolve_provider_and_model("owner:qwen3:latest") == (long, "latest")
