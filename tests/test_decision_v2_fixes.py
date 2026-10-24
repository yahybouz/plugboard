"""SPEC v2 regressions with fake transports only; never upstream inference."""

from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize("pid", ["laya", "jev", "kev-proxy"])
async def test_http_legal_provider_name_reaches_regular_chat_port(
    client, monkeypatch, pid
):
    from plugboard import app as gateway

    provider = {"id": pid, "models": [{"id": "gpt-5"}], "default_model": "gpt-5"}
    monkeypatch.setattr(gateway, "load_providers", lambda: [provider])
    reply = {"choices": [{"message": {"content": "synthetic"}}]}
    monkeypatch.setattr(gateway, "forward", AsyncMock(return_value=reply))
    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer testkey"},
        json={
            "model": f"{pid}:gpt-5",
            "messages": [{"role": "user", "content": "synthetic"}],
            "cache": False,
        },
    )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "synthetic"


@pytest.mark.parametrize("pid", ["laya", "jev", "kev-proxy"])
async def test_http_catalogue_keeps_legal_provider_regular_model(
    client, monkeypatch, pid
):
    from plugboard import app as gateway

    monkeypatch.setattr(gateway, "load_providers", lambda: [{"id": pid}])
    monkeypatch.setattr(
        gateway, "list_models", AsyncMock(return_value=[{"id": f"{pid}:gpt-5"}])
    )
    response = await client.get(
        "/v1/models", headers={"Authorization": "Bearer testkey"}
    )
    assert response.status_code == 200
    assert [row["id"] for row in response.json()["data"]] == [f"{pid}:gpt-5"]


class Response:
    status = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, rows=None, completion=False):
        self.rows = rows or []
        self.completion = completion

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self, **kwargs):
        if self.completion:
            return {"choices": [{"message": {"content": "synthetic"}}]}
        return {"data": [{"id": name} for name in self.rows]}


class Session:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.posts = []
        self.gets = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def get(self, url, **kwargs):
        self.gets.append(url)
        return Response(self.rows)

    def post(self, url, **kwargs):
        self.posts.append(kwargs["json"])
        return Response(completion=True)


@pytest.mark.parametrize(
    "rows", [["kev-0.8b"], ["openclaw/typesafe--jev"], ["convaiinnovations/laya"]]
)
async def test_direct_provider_test_never_pings_a_decision_model(monkeypatch, rows):
    from plugboard.app import _test_openai_endpoint
    import aiohttp

    session = Session(rows)
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kwargs: session)
    result = await _test_openai_endpoint("https://synthetic.invalid/v1", "", True)
    assert len(session.gets) == 1
    assert session.posts == []
    assert result["completion_skipped"] == (
        "removed_legacy_connector"
        if rows == ["openclaw/typesafe--jev"]
        else "decision_model"
    )


async def test_direct_provider_test_selects_regular_model_and_preserves_catalogue(
    monkeypatch,
):
    from plugboard.app import _test_openai_endpoint
    import aiohttp

    session = Session(["kev-0.8b", "gpt-5"])
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kwargs: session)
    result = await _test_openai_endpoint("https://synthetic.invalid/v1", "", True)
    assert result["models"] == ["kev-0.8b", "gpt-5"]
    assert [p["model"] for p in session.posts] == ["gpt-5"]


@pytest.mark.parametrize(
    "model",
    [
        "kev-0.8b",
        "ollama:kev-0.8b:latest",
        "openclaw/typesafe--jev",
        "convaiinnovations/laya",
    ],
)
@pytest.mark.parametrize("port", ["monitoring", "watchdog"])
async def test_direct_health_completion_rejects_decisions_before_session(
    monkeypatch, model, port
):
    import aiohttp
    from plugboard import monitoring, provider_watchdog

    session = Session()
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kwargs: session)
    if port == "monitoring":
        monkeypatch.setattr(monitoring, "load_providers", lambda: [])
        result = await monitoring.check_model_availability(
            model, "", "https://synthetic.invalid/v1"
        )
    else:
        result = await provider_watchdog._deep_health(
            "https://synthetic.invalid/v1", "", model
        )
    assert result["ok"] is False
    if model == "openclaw/typesafe--jev":
        assert "removed" in result["error"].lower()
    else:
        assert result["error"] == "decision_model_not_chat"
    assert session.posts == []


@pytest.mark.parametrize("port", ["monitoring", "watchdog"])
async def test_direct_health_ordinary_model_still_posts(monkeypatch, port):
    import aiohttp
    from plugboard import monitoring, provider_watchdog

    session = Session()
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kwargs: session)
    if port == "monitoring":
        monkeypatch.setattr(monitoring, "load_providers", lambda: [{"id": "laya"}])
        result = await monitoring.check_model_availability(
            "laya:gpt-5", "", "http://localhost:8765"
        )
    else:
        result = await provider_watchdog._deep_health(
            "https://synthetic.invalid/v1", "", "gpt-5"
        )
    assert result["ok"] is True
    assert len(session.posts) == 1


async def test_discovery_preserves_get_catalogue_for_decision_default(monkeypatch):
    from plugboard import router, http_pool

    session = Session(["gpt-5"])
    monkeypatch.setattr(http_pool, "get_session", AsyncMock(return_value=session))
    for name in [
        "_discover_ollama_models",
        "_discover_anthropic_api_models",
        "_discover_gemini_api_models",
    ]:
        monkeypatch.setattr(router, name, AsyncMock(return_value=None))
    result = await router.discover_models(
        {
            "id": "laya",
            "base_url": "https://synthetic.invalid/v1",
            "default_model": "kev-0.8b",
        }
    )
    assert result == [{"id": "gpt-5", "name": "gpt-5"}]
    assert session.posts == []


@pytest.mark.parametrize(
    "pid,model",
    [("laya", "laya:latest"), ("jev", "jev:latest"), ("kev-proxy", "kev-proxy:latest")],
)
@pytest.mark.parametrize(
    "resolve", ["resolve_provider_and_model", "_resolve_all_providers"]
)
def test_effective_decision_default_is_not_reinterpreted_as_own_provider(
    monkeypatch, pid, model, resolve
):
    from plugboard import router

    monkeypatch.setattr(
        router,
        "load_providers",
        lambda: [{"id": pid, "default_model": model, "models": []}],
    )
    with pytest.raises(ValueError, match="decisions"):
        getattr(router, resolve)("")


@pytest.mark.parametrize(
    "resolve", ["resolve_provider_and_model", "_resolve_all_providers"]
)
def test_known_provider_regular_tag_does_not_become_a_model_name(monkeypatch, resolve):
    from plugboard import router

    monkeypatch.setattr(
        router,
        "load_providers",
        lambda: [{"id": "laya", "models": [{"id": "gpt-5:kev"}]}],
    )
    result = getattr(router, resolve)("laya:gpt-5:kev")
    selected = result[0] if resolve == "_resolve_all_providers" else result
    assert selected[1] == "gpt-5:kev"


@pytest.mark.parametrize("port", ["ollama", "subprocess"])
@pytest.mark.parametrize(
    "model", ["kev-0.8b", "ollama:kev-0.8b:latest", "convaiinnovations/laya"]
)
async def test_native_connectivity_probes_skip_decision_inference(
    monkeypatch, port, model
):
    from plugboard import model_availability, cli_subprocess
    import aiohttp

    session = Session()
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kwargs: session)
    complete = AsyncMock(
        return_value={"choices": [{"message": {"content": "synthetic"}}]}
    )
    monkeypatch.setattr(cli_subprocess, "complete", complete)
    if port == "ollama":
        result = await model_availability.probe_ollama_model(model)
    else:
        result = await model_availability.probe_subprocess_model(
            "codex", model, timeout_s=1
        )
    assert result["ok"] is False
    assert result["reason"] == "decision_model_not_chat"
    assert session.posts == []
    assert complete.await_count == 0


@pytest.mark.parametrize("pid", ["laya", "jev", "kev-proxy"])
@pytest.mark.parametrize(
    "model",
    [
        "kev-0.8b:latest",
        "jev@revision",
        "convaiinnovations/laya@revision",
        "openclaw/typesafe--jev",
        "typesafe/jev",
        "ollama:kev-0.8b:latest",
    ],
)
@pytest.mark.parametrize(
    "resolve", ["resolve_provider_and_model", "_resolve_all_providers"]
)
def test_legal_provider_still_rejects_its_actual_decision_models(
    monkeypatch, pid, model, resolve
):
    from plugboard import router

    monkeypatch.setattr(
        router,
        "load_providers",
        lambda: [{"id": pid, "models": [], "default_model": "gpt-5"}],
    )
    with pytest.raises(
        ValueError,
        match="removed" if model == "openclaw/typesafe--jev" else "decisions",
    ):
        getattr(router, resolve)(f"{pid}:{model}")
