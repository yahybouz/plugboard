import pytest
from tests.test_decision_routing_v5 import provider
from tests.test_decision_routing_v5 import routing as routing


@pytest.fixture(autouse=True)
def synthetic_observations(monkeypatch):
    from plugboard import model_availability, model_quarantine

    monkeypatch.setattr(model_availability, "load_model_status", lambda: {"models": {}})
    monkeypatch.setattr(model_quarantine, "get_quarantined_suffixes", lambda: set())


@pytest.mark.parametrize("shadow", [False, True])
@pytest.mark.parametrize("warm", [False, True])
async def test_safe_bare_survives_omission_before_dedup(
    routing, monkeypatch, shadow, warm
):
    # qwen3 is expressible even when qwen3:latest is not. No requirement that
    # the inexpressible latest row exist, and no invented separator/grammar.
    short = provider("owner", "qwen3", ["qwen3", "qwen3:latest"])
    long = provider("owner:qwen3" if shadow else "owner:other", "gpt-5", ["gpt-5"])
    witness = provider("safe", "gpt-4o", ["gpt-4o"])
    monkeypatch.setattr(routing, "load_providers", lambda: [short, long, witness])
    monkeypatch.setattr(routing, "_model_status_mtime_changed", lambda: False)
    assert routing.resolve_provider_and_model("owner:qwen3") == (short, "qwen3")
    rows = await routing.list_models()
    if warm:
        assert await routing.list_models() == rows
    expected = "owner:qwen3" if shadow else "owner:qwen3:latest"
    assert [r["id"] for r in rows if r["owned_by"] == "owner"] == [expected]
    assert {r["owned_by"] for r in rows} == {"owner", long["id"], "safe"}
