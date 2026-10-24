"""MCP direct ownership and removed connector admission through local doubles."""

import pytest

pytest.importorskip("mcp")
from plugboard import mcp_server  # noqa: E402


async def test_catalogue_groups_by_actual_owner_and_excludes_legacy(monkeypatch):
    async def catalogue(path):
        assert path == "/v1/models"
        return {
            "data": [
                {
                    "id": "direct:org/vendor--model",
                    "owned_by": "Direct Owner",
                    "provider": "direct",
                },
                {"id": "openclaw:gpt-5", "owned_by": "openai"},
                {
                    "id": "direct:openclaw/anthropic--claude",
                    "owned_by": "Direct Owner",
                    "provider": "direct",
                },
            ]
        }

    monkeypatch.setattr(mcp_server, "_api_get", catalogue)
    result = await mcp_server.list_models()
    assert "**Direct Owner** (1)" in result
    assert "**1 models available:**" in result
    assert "openclaw" not in result
    assert "**vendor**" not in result


@pytest.mark.parametrize("method", ["chat", "chat_stream"])
async def test_mcp_legacy_refusal_before_gateway_transport(monkeypatch, method):
    attempts = []

    async def forbidden(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("legacy reached gateway transport")

    monkeypatch.setattr(mcp_server, "_api_post", forbidden)
    monkeypatch.setattr(mcp_server, "_get_http_session", forbidden)
    result = await getattr(mcp_server, method)("openclaw:gpt-5", "private")
    assert "removed" in result.lower()
    assert attempts == []
