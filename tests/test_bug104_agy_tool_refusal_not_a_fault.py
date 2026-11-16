"""BUG-104: a tool agy refused is not a provider fault.

Headless agy cannot ask for a tool permission: it refuses the tool and
answers valid JSON with an empty response and ``denied_actions`` (real
payload of agy 1.4.4, BUG-100). The request asked for something the CLI
does not do without a human; agy itself works. Three such requests in a row
opened agy's circuit, sent the provider_down alert and counted errors in
the metrics. Now: none of the three, and the client gets the same answer as
before (the CLI wrapper's message, 502 "provider failed" for a client key).

Every test goes through the real router and the real CLI wrapper: only the
agy process is a fake (a shell script playing agy's real output).
"""

from __future__ import annotations

import json

import pytest

from plugboard import alerts, circuit_breaker, cli_subprocess, metrics, router
from plugboard.cli_subprocess import CliSubprocessError
from tests.test_cli_stream import (  # noqa: F401 (fake_cli is a fixture)
    _AGY_DENIED,
    _AGY_EMPTY,
    _agy_lines,
    fake_cli,
)

_AGY_ID = "pbtest-agy"
_MODEL = f"{_AGY_ID}:m"
_KEY = _MODEL
_MESSAGES = [{"role": "user", "content": "write a file"}]


@pytest.fixture
def agy_route(_providers_file, monkeypatch):
    """A lone agy provider; fresh circuits; the alerts, error metrics and
    quarantine writes recorded instead of sent."""
    import plugboard.model_quarantine as quarantine

    _providers_file.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "id": _AGY_ID,
                        "name": "agy test",
                        "base_url": "subprocess://agy",
                        "bearer_token": "(via CLI)",
                        "default_model": "m",
                        "auth_source": "subprocess:agy",
                        "is_active": True,
                        "created_at": "2026-01-01T00:00:00+00:00",
                    }
                ]
            }
        )
    )
    router.invalidate_models_cache()
    monkeypatch.setattr(circuit_breaker, "_circuits", {})
    monkeypatch.setattr(alerts, "_provider_failures", {})
    seen = {"alerts": [], "errors": [], "quarantined": []}

    async def alert(provider_id):
        seen["alerts"].append(provider_id)

    monkeypatch.setattr(router, "record_provider_failure", alert)
    real_record = metrics.record

    def record(provider_id, latency, *, error=False):
        if error:
            seen["errors"].append(provider_id)
        return real_record(provider_id, latency, error=error)

    monkeypatch.setattr(metrics, "record", record)
    for name in ("record_terminal_unavailable", "record_account_unsupported"):
        monkeypatch.setattr(
            quarantine,
            name,
            lambda *a, _name=name, **k: seen["quarantined"].append(_name),
        )
    return seen


def _failures() -> int:
    circuit = circuit_breaker._circuits.get(_KEY)
    return circuit.failures if circuit else 0


# ── the wrapper names the refusal ───────────────────────────────────────────


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
async def test_the_wrapper_flags_a_refused_tool(fake_cli, stream):  # noqa: F811
    fake_cli(_agy_lines(_AGY_DENIED, stream))
    with pytest.raises(CliSubprocessError) as info:
        await cli_subprocess.complete(
            "agy",
            {"model": "m", "messages": _MESSAGES},
            on_delta=(lambda t: None) if stream else None,
        )
    assert info.value.tool_refused is True
    assert "refused the tools its answer needed" in str(info.value)


async def test_an_empty_answer_without_refusal_is_not_flagged(fake_cli):  # noqa: F811
    fake_cli(_agy_lines(_AGY_EMPTY, False))
    with pytest.raises(CliSubprocessError) as info:
        await cli_subprocess.complete("agy", {"model": "m", "messages": _MESSAGES})
    assert info.value.tool_refused is False


# ── the router does not count it ────────────────────────────────────────────


async def test_three_refused_tools_do_not_open_agys_circuit(
    agy_route,
    fake_cli,  # noqa: F811
):
    fake_cli(_agy_lines(_AGY_DENIED, False))
    for _ in range(3):
        with pytest.raises(CliSubprocessError, match="refused the tools"):
            await router.forward({"model": _MODEL, "messages": _MESSAGES})

    assert _failures() == 0
    assert circuit_breaker.check_circuit(_KEY)["state"] == circuit_breaker.CLOSED
    assert agy_route["alerts"] == []
    assert agy_route["errors"] == []
    assert agy_route["quarantined"] == []


async def test_a_streamed_refused_tool_is_not_counted_either(
    agy_route,
    fake_cli,  # noqa: F811
):
    fake_cli(_agy_lines(_AGY_DENIED, True))
    body = {"model": _MODEL, "messages": _MESSAGES, "stream": True}
    for _ in range(3):
        with pytest.raises(CliSubprocessError, match="refused the tools"):
            async for _chunk in router.forward_stream(body):
                pass

    assert _failures() == 0
    assert agy_route["alerts"] == []
    assert agy_route["errors"] == []
    assert agy_route["quarantined"] == []


async def test_an_empty_answer_without_refusal_still_counts(
    agy_route,
    fake_cli,  # noqa: F811
):
    """Only the refusal changes: agy answering nothing for no reason is still
    a failure of the provider."""
    fake_cli(_agy_lines(_AGY_EMPTY, False))
    with pytest.raises(CliSubprocessError, match="empty answer"):
        await router.forward({"model": _MODEL, "messages": _MESSAGES})

    assert _failures() == 1
    assert agy_route["alerts"] == [_AGY_ID]
    assert agy_route["errors"] == [_AGY_ID]


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
async def test_the_client_gets_the_same_answer(
    client,
    agy_route,
    fake_cli,  # noqa: F811
    monkeypatch,
    stream,
):
    """502 with BUG-100's message (the test key is the admin's), as before:
    only the counting changes."""
    from plugboard import app as app_mod

    monkeypatch.setattr(app_mod, "_check_price_known", lambda *a, **k: None)
    fake_cli(_agy_lines(_AGY_DENIED, stream))
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer testkey"},
        json={"model": _MODEL, "messages": _MESSAGES, "stream": stream},
    )
    assert resp.status_code == 502, resp.text
    assert "refused the tools its answer needed" in resp.json()["detail"]
    assert _failures() == 0
    assert agy_route["alerts"] == []
