"""The client API contract (docs/api/openapi.json) matches the code.

Three ways it could drift, three checks:

* a public route is added, renamed or removed without the contract;
* the code changes the contract but docs/api/openapi.json is not regenerated;
* a route answers with a body the contract does not describe.

The answers are real: the app runs with a client key and a mocked provider,
and each body is validated with the Pydantic model the contract publishes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aioresponses import aioresponses
from fastapi.routing import APIRoute

from plugboard import api_contract
from tests.factories import FAKE_COMPLETION, FAKE_STREAM_BODY

ROOT = Path(__file__).resolve().parent.parent
CHAT_MODEL = "test-provider:fake:model-v1"


def _needs_admin(route: APIRoute) -> bool:
    from plugboard.app import _require_admin

    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        if dep.call is _require_admin:
            return True
        stack.extend(dep.dependencies)
    return False


def test_every_public_route_is_in_the_contract_and_nothing_else():
    """Routes a client key reaches == routes the contract documents.

    Pages (dashboard, login, OAuth callback) are not in the API schema and
    stay out; admin routes are documented by the admin-only /openapi.json.
    """
    from plugboard.app import app

    public = {
        (method, route.path.replace("{model_id:path}", "{model_id}"))
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.include_in_schema
        and not _needs_admin(route)
        for method in route.methods - {"HEAD"}
    }
    missing = public - api_contract.PUBLIC_ROUTES
    assert not missing, f"public routes missing from the contract: {missing}"
    stale = api_contract.PUBLIC_ROUTES - public
    assert not stale, f"contract routes the app no longer has: {stale}"


def test_committed_contract_is_up_to_date():
    from scripts.export_contract import OPENAPI_PATH, openapi_document, render

    assert OPENAPI_PATH.read_text(encoding="utf-8") == render(openapi_document()), (
        "docs/api/openapi.json is stale: run `make contract` and review the diff"
    )


def _walk_refs(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref":
                yield value
            else:
                yield from _walk_refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_refs(item)


def test_contract_is_a_consistent_openapi_document():
    doc = api_contract.build_openapi()
    assert doc["openapi"].startswith("3.1")
    assert doc["info"]["version"] == api_contract.CONTRACT_VERSION
    schemas = doc["components"]["schemas"]
    refs = {
        ref.rsplit("/", 1)[-1]
        for ref in _walk_refs(doc)
        if ref.startswith("#/components/schemas/")
    }
    assert refs <= set(schemas), f"undefined schemas: {refs - set(schemas)}"
    for path, ops in doc["paths"].items():
        for method, op in ops.items():
            assert "200" in op["responses"], (method, path)
            assert op["operationId"], (method, path)


@pytest.fixture
def client_key(client) -> str:
    """A real client key (pb_...), so the answers are the ones clients get."""
    from plugboard import apikeys

    return apikeys.create_key("acme", "contract", monthly_limit_usd=5.0)["key"]


def _validate(path: str, body, method: str = "GET"):
    model = api_contract.RESPONSE_MODELS[(method, path)]
    if path in api_contract._ARRAY_RESPONSES:
        assert isinstance(body, list), body
        return [model.model_validate(item) for item in body]
    return model.model_validate(body)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/health",
        "/v1/models",
        "/v2/models",
        "/v1/models/benchmarks",
        "/v2/models/benchmarks",
        "/v1/me",
        "/v1/me/keys",
        "/usage/me",
    ],
)
async def test_get_routes_answer_as_the_contract_says(client, client_key, path):
    resp = await client.get(path, headers={"Authorization": f"Bearer {client_key}"})
    assert resp.status_code == 200, resp.text
    _validate(path, resp.json())


@pytest.mark.asyncio
async def test_model_info_answers_as_the_contract_says(client, client_key):
    auth = {"Authorization": f"Bearer {client_key}"}
    model_id = (await client.get("/v1/models", headers=auth)).json()["data"][0]["id"]
    resp = await client.get(f"/v1/models/{model_id}/info", headers=auth)
    assert resp.status_code == 200, resp.text
    _validate("/v1/models/{model_id}/info", resp.json())

    missing = await client.get("/v1/models/no-such-model-xyz/info", headers=auth)
    if missing.status_code == 404:
        api_contract.ErrorResponse.model_validate(missing.json())


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v2/chat/completions"])
async def test_chat_completion_answers_as_the_contract_says(client, client_key, path):
    request = api_contract.ChatCompletionRequest(
        model=CHAT_MODEL,
        messages=[{"role": "user", "content": "Hi"}],
        temperature=0.2,
        cache=False,
    ).model_dump(exclude_none=True)
    with aioresponses() as mock:
        mock.post("http://fake-llm.test/v1/chat/completions", payload=FAKE_COMPLETION)
        resp = await client.post(
            path, headers={"Authorization": f"Bearer {client_key}"}, json=request
        )
    assert resp.status_code == 200, resp.text
    completion = _validate(path, resp.json(), method="POST")
    assert completion.billing is not None
    assert completion.billing.price_key is None  # admin keys only
    for header in ("X-Cache", "X-Cost-USD", "X-Prompt-Tokens", "X-Completion-Tokens"):
        assert header.lower() in resp.headers, header


@pytest.mark.asyncio
async def test_streamed_completion_answers_as_the_contract_says(client, client_key):
    request = {
        "model": CHAT_MODEL,
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": True,
    }
    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            body=FAKE_STREAM_BODY,
            content_type="text/event-stream",
        )
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {client_key}"},
            json=request,
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = [
        line[len("data: ") :]
        for line in resp.text.splitlines()
        if line.startswith("data: ")
    ]
    assert events[-1] == "[DONE]" and events.count("[DONE]") == 1
    chunks = [
        api_contract.ChatCompletionChunk.model_validate_json(e) for e in events[:-1]
    ]
    assert any(c.usage is not None for c in chunks), "no usage chunk before [DONE]"


@pytest.mark.asyncio
async def test_preflight_answers_as_the_contract_says(client, client_key):
    body = api_contract.PreflightRequest(
        items=[{"provider": "test-provider", "model": "fake:model-v1"}]
    ).model_dump()
    with aioresponses() as mock:
        mock.post("http://fake-llm.test/v1/chat/completions", payload=FAKE_COMPLETION)
        resp = await client.post(
            "/v1/preflight",
            headers={"Authorization": f"Bearer {client_key}"},
            json=body,
        )
    assert resp.status_code == 200, resp.text
    result = _validate("/v1/preflight", resp.json(), method="POST")
    assert result.results[0].ok is True


@pytest.mark.asyncio
async def test_errors_answer_as_the_contract_says(client):
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer not-a-key"},
        json={"model": CHAT_MODEL, "messages": [{"role": "user", "content": "Hi"}]},
    )
    assert resp.status_code == 401
    api_contract.ErrorResponse.model_validate(resp.json())


@pytest.mark.asyncio
async def test_instance_serves_the_contract_without_a_key(client):
    resp = await client.get("/v1/openapi.json")
    assert resp.status_code == 200
    committed = (ROOT / "docs" / "api" / "openapi.json").read_text(encoding="utf-8")
    assert resp.json() == json.loads(committed)
