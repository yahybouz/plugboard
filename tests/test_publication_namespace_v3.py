"""SPEC2: reserved namespace, safe fallback and warm publication changes.

Literal identities catch an unchecked fallback, a consumed namespace iterator,
legacy/inactive reservations disappearing, and cached IDs surviving mutations.
"""

import pytest

from plugboard.model_identity import published_model_id
from tests.test_decision_routing_v5 import provider
from tests.test_decision_routing_v5 import routing as routing


FAMILIES = [
    ("claude-via-claude-cli", "claude-code"),
    ("chatgpt-via-codex-cli", "Codex"),
    ("google-gemini-via-gemini-cli", "Gemini"),
]


@pytest.mark.parametrize("iterator", [False, True])
@pytest.mark.parametrize(
    "owner,native,reserved,want",
    [
        ("owner", "qwen3:latest", ["owner", "owner:qwen3"], None),
        (
            "owner:lane",
            "qwen3:latest",
            ["owner", "owner:lane"],
            "owner:lane:qwen3:latest",
        ),
        (
            "owner:lane",
            "qwen3:deep:latest",
            ["owner:lane", "owner:lane:qwen3:deep"],
            None,
        ),
        (
            "claude-via-claude-cli",
            "qwen3:latest",
            ["claude-via-claude-cli"],
            "claude-code:qwen3:latest",
        ),
        (
            "claude-via-claude-cli",
            "qwen3:latest",
            ["claude-via-claude-cli", "claude-code"],
            "claude-via-claude-cli:qwen3:latest",
        ),
        (
            "claude-via-claude-cli",
            "qwen3:latest",
            ["claude-code", "claude-via-claude-cli", "claude-via-claude-cli:qwen3"],
            None,
        ),
        (
            "claude-via-claude-cli",
            "qwen3:latest",
            ["claude-via-claude-cli:qwen3", "claude-via-claude-cli"],
            "claude-code:qwen3:latest",
        ),
        (
            "claude-via-claude-cli",
            "qwen3:deep:latest",
            ["claude-code:qwen3:deep", "claude-via-claude-cli"],
            "claude-via-claude-cli:qwen3:deep:latest",
        ),
    ],
)
def test_publication_candidates_use_the_same_complete_namespace(
    iterator, owner, native, reserved, want
):
    namespace = (pid for pid in reserved) if iterator else reserved
    assert published_model_id(owner, native, namespace) == want


@pytest.mark.parametrize("canonical,alias", FAMILIES)
@pytest.mark.parametrize(
    "kind", ["homonym", "alias-nephew", "canonical-nephew", "both"]
)
@pytest.mark.parametrize("state", ["active", "inactive", "removed"])
async def test_warm_namespace_add_change_remove(
    routing, monkeypatch, canonical, alias, kind, state
):
    direct = provider(canonical, "qwen3:deep:latest", ["qwen3:deep:latest"])
    accounts = [direct]
    reads = []

    def registry():
        reads.append(True)
        return list(accounts)

    monkeypatch.setattr(routing, "load_providers", registry)
    monkeypatch.setattr(routing, "_model_status_mtime_changed", lambda: False)

    async def check(want):
        before = len(reads)
        rows = await routing.list_models()
        assert len(reads) == before + 1  # One coherent registry per operation.
        entries = [row for row in rows if row["owned_by"] == canonical]
        assert [row["id"] for row in entries] == ([] if want is None else [want])
        assert all(isinstance(row["id"], str) and row["id"] for row in rows)
        if entries:
            assert entries[0]["family"] == "qwen3"
            assert entries[0]["variant"] == "deep:latest"
            assert routing.resolve_provider_and_model(want) == (
                direct,
                "qwen3:deep:latest",
            )
        return rows

    await check(alias + ":qwen3:deep:latest")
    ids = {
        "homonym": [alias],
        "alias-nephew": [alias + ":qwen3:deep"],
        "canonical-nephew": [canonical + ":qwen3:deep"],
        "both": [alias, canonical + ":qwen3:deep"],
    }[kind]
    blockers = [provider(pid, "gpt-5", ["gpt-5"]) for pid in ids]
    accounts.extend(blockers)
    want = (
        None
        if kind == "both"
        else alias + ":qwen3:deep:latest"
        if kind == "canonical-nephew"
        else canonical + ":qwen3:deep:latest"
    )
    await check(want)
    for blocker in blockers:
        if state == "inactive":
            blocker["is_active"] = False
        elif state == "removed":
            blocker["auth_source"] = "openclaw"
    rows = await check(want)
    if state != "active":
        assert not any(row["owned_by"] in ids for row in rows)
    accounts[:] = [direct]
    await check(alias + ":qwen3:deep:latest")
    routing.invalidate_models_cache()
    await check(alias + ":qwen3:deep:latest")


async def test_omitted_owner_does_not_force_rebuild_or_leave_promotion(
    routing, monkeypatch
):
    from plugboard import model_catalog

    accounts = [
        provider("owner", "qwen3:latest", ["qwen3:latest"]),
        provider("owner:qwen3", "gpt-5-20260101", ["gpt-5-20260101"]),
    ]
    monkeypatch.setattr(routing, "load_providers", lambda: accounts)
    monkeypatch.setattr(routing, "_model_status_mtime_changed", lambda: False)
    rows = await routing.list_models()
    assert [row["id"] for row in rows] == ["owner:qwen3:gpt-5-20260101"]
    assert routing._models_alias_cache == {
        "owner:qwen3:gpt-5": "owner:qwen3:gpt-5-20260101"
    }
    assert (
        routing.resolve_provider_and_model("owner:qwen3:gpt-5")[1] == "gpt-5-20260101"
    )

    def no_rebuild(_):
        pytest.fail("An omitted owner is still represented in the cache namespace")

    monkeypatch.setattr(model_catalog, "enrich", no_rebuild)
    assert await routing.list_models() == rows


@pytest.mark.parametrize("curated", [False, True])
async def test_filters_and_extra_default_rows_cannot_restore_omissions(
    routing, monkeypatch, curated
):
    from plugboard import model_exposure

    short = provider("owner", "gpt-5:latest", [], extra_model_ids=["gpt-5:latest"])
    nested = provider("owner:gpt-5", "gpt-5", [])
    safe = provider("safe", "gpt-5", ["gpt-5"])
    monkeypatch.setattr(routing, "load_providers", lambda: [short, nested, safe])
    if curated:
        monkeypatch.setattr(routing, "_model_filter_mode", lambda: "curated")
        monkeypatch.setattr(model_exposure, "mode", lambda: "latest")
        monkeypatch.setenv("PLUGBOARD_TOP_N_PER_PROVIDER", "1")
    rows = await routing.list_models()
    assert {row["id"] for row in rows} == {"owner:gpt-5:gpt-5", "safe:gpt-5"}
    for row in rows:
        assert routing.resolve_provider_and_model(row["id"])[0]["id"] == row["owned_by"]
    assert not any(
        value == "owner:gpt-5:latest" for value in routing._models_alias_cache.values()
    )
