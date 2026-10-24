"""Jev transport boundary tests with fake HTTP only; never real inference."""

import asyncio

import aiohttp
import pytest

from plugboard import decisions


def request():
    return {
        "model": "jev",
        "state": "PRIVATE STATE",
        "questions": {"ok": {"type": "noul", "instructions": "PRIVATE QUESTION"}},
    }


class Stream:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            yield chunk


class Response:
    def __init__(
        self, body=b'{"answers":{"ok":{"type":"noul","noul":0.8}}}', status=200
    ):
        self.status = status
        self.content = Stream([body])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


class Session:
    def __init__(self, response, calls):
        self.response = response
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def fake_http(monkeypatch, response):
    calls, options = [], []

    def factory(**kwargs):
        options.append(kwargs)
        return Session(response, calls)

    monkeypatch.setattr(decisions.aiohttp, "ClientSession", factory)
    monkeypatch.setenv("PLUGBOARD_JEV_API_KEY", "fake-test-key")
    return calls, options


@pytest.mark.asyncio
async def test_missing_key_is_unavailable_and_never_uses_unrelated_key(monkeypatch):
    calls, _ = fake_http(monkeypatch, Response())
    monkeypatch.delenv("PLUGBOARD_JEV_API_KEY")
    monkeypatch.setenv("TYPESAFE_API_KEY", "unrelated-fake-key")
    with pytest.raises(decisions.DecisionUnavailable):
        await decisions.infer_jev(request())
    assert not calls


@pytest.mark.asyncio
async def test_official_tls_origin_no_redirects_no_proxy_and_bounded_timeout(
    monkeypatch,
):
    calls, options = fake_http(monkeypatch, Response())
    result = await decisions.infer_jev(request())
    assert result == {"answers": {"ok": {"type": "noul", "noul": 0.8}}}
    assert calls[0][0] == "https://api.typesafe.ai/v1/systemone"
    assert calls[0][1]["allow_redirects"] is False
    assert calls[0][1]["headers"]["Authorization"] == "Bearer fake-test-key"
    assert calls[0][1]["json"] == {**request(), "model": "jev-latest"}
    assert options[0]["trust_env"] is False
    assert options[0]["timeout"].total == 30


@pytest.mark.parametrize(
    "response",
    [
        Response(status=302),
        Response(status=401),
        Response(status=500),
        Response(b"PRIVATE UPSTREAM BODY"),
        Response(b"x" * 65_537),
        Response(b'{"answers":{"ok":{"type":"noul","noul":NaN}}}'),
        Response(b'{"answers":{"ok":{"type":"noul","noul":0.5,"noul":0.8}}}'),
        Response(
            b'{"answers":{"ok":{"type":"noul","noul":0.8,"private":"PRIVATE BODY"}}}'
        ),
        aiohttp.ClientError("PRIVATE UPSTREAM BODY fake-test-key"),
    ],
)
@pytest.mark.asyncio
async def test_remote_failures_are_bounded_and_redacted(monkeypatch, response, caplog):
    fake_http(monkeypatch, response)
    with pytest.raises(decisions.DecisionUpstreamError) as error:
        await decisions.infer_jev(request())
    assert str(error.value) == "Decision engine returned an invalid response."
    assert "PRIVATE" not in caplog.text
    assert "fake-test-key" not in caplog.text


@pytest.mark.asyncio
async def test_timeout_is_normalized(monkeypatch):
    fake_http(monkeypatch, asyncio.TimeoutError("PRIVATE STATE fake-test-key"))
    with pytest.raises(decisions.DecisionTimeout) as error:
        await decisions.infer_jev(request())
    assert str(error.value) == "Decision engine timed out."


@pytest.mark.asyncio
async def test_jev_rejects_more_than_ten_score_levels_before_provider_call(monkeypatch):
    calls, _ = fake_http(monkeypatch, Response())
    data = request()
    data["questions"] = {
        "score": {
            "type": "score",
            "instructions": "Rate?",
            "criteria": [str(i) for i in range(11)],
        }
    }
    with pytest.raises(ValueError, match="10"):
        await decisions.infer_jev(data)
    assert not calls
