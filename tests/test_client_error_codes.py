"""Lot B: the client errors a program must tell apart carry a stable ``code``.

Subscription quota exhausted, model refused to the key, provider timeout: each
now has a ``code`` next to its ``detail``, like ``providers_busy`` and
``model_not_supported_by_subscription``. BUG-67: a CLI that exceeds its
deadline answers 503 with Retry-After, as documented, not 502.
"""

from __future__ import annotations

import json

import pytest

from plugboard.app import _cli_error_response, _quota_error_response
from plugboard.cli_subprocess import CliSubprocessError
from plugboard.router import ProviderQuotaError


def _body(resp):
    return json.loads(resp.body)


def test_cli_quota_has_a_code():
    exc = CliSubprocessError(
        "usage limit", provider_name="claude", model_id="m", quota=True, retry_after=30
    )
    resp = _cli_error_response(exc, "cust")
    assert resp.status_code == 429
    assert _body(resp)["code"] == "quota_exhausted"
    assert resp.headers["Retry-After"] == "30"


def test_http_quota_has_a_code():
    exc = ProviderQuotaError("429", provider_id="p", retry_after=None)
    resp = _quota_error_response(exc, "cust")
    assert resp.status_code == 429
    assert _body(resp)["code"] == "quota_exhausted"


def test_cli_timeout_is_a_503_with_a_code_and_retry_after():
    exc = CliSubprocessError(
        "CLI 'claude' timed out after 300s",
        provider_name="claude",
        model_id="m",
        timed_out=True,
    )
    resp = _cli_error_response(exc, "cust")
    assert resp.status_code == 503
    body = _body(resp)
    assert body["code"] == "provider_timeout"
    assert body["retry_after"] == 10
    assert resp.headers["Retry-After"] == "10"
    assert "timed out" not in body["detail"]  # the exception text stays in the log


def test_cli_failure_without_timeout_is_still_a_502():
    exc = CliSubprocessError("boom", provider_name="claude", model_id="m")
    assert _cli_error_response(exc, "cust").status_code == 502


@pytest.fixture
def limited_key(tmp_path, monkeypatch):
    import plugboard.apikeys as ak

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    rec = ak.create_key("limited", "synthetic", allowed_models=["fake:other-model"])
    ak._invalidate_cache()
    yield rec["key"]
    ak._invalidate_cache()


async def test_a_model_the_key_may_not_use_answers_403_forbidden_model(
    client, limited_key
):
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {limited_key}"},
        json={
            "model": "fake:model-v1",
            "messages": [{"role": "user", "content": "hi"}],
            "cache": False,
        },
    )
    assert resp.status_code == 403, resp.text
    body = resp.json()
    assert body["code"] == "forbidden_model"
    assert "not allowed" in body["detail"]
