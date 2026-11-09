import pytest
from fastapi import HTTPException
import plugboard.router as router
from plugboard.app import _check_model_access


def test_codex_model_access(monkeypatch):
    provider = {
        "id": "Codex",
        "auth_source": "subprocess:codex",
        "default_model": "gpt-5-mini",
        "is_active": True,
        "models": [{"id": "gpt-5-mini"}],
    }
    monkeypatch.setattr(router, "load_providers", lambda: [provider])
    monkeypatch.setattr(router, "_models_alias_cache", {})
    record = {"client_id": "client", "allowed_models": ["gpt-5-mini"]}
    _check_model_access("client", "Codex:gpt-5-mini", record)
    with pytest.raises(HTTPException) as exc:
        _check_model_access("client", "Codex:gpt-5-mini:gpt-6", record)
    assert exc.value.status_code == 403
