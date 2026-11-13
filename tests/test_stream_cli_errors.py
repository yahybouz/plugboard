"""A failed CLI route in a stream sends nothing (BUG-74, BUG-75).

The CLI branch of router._stream_route answered a failed run with an SSE
event carrying the CLI's message, stderr, pid, exit code, provider name and
model, then ``[DONE]``: to anyone, whatever the key. It was dead code for
the chat route (a stream to a CLI went through forward()) until a stream
could have several routes (PLU-26). It now raises: nothing is sent, the next
route takes over, and the chat route answers the error with those details
for the admin only. No CLI runs: ``cli_subprocess.complete`` is a fake.
"""

from __future__ import annotations

import json

import pytest

from plugboard import router

CLI = {
    "id": "pbtest-cli",
    "name": "CLI test",
    "base_url": "subprocess://claude",
    "bearer_token": "(via CLI)",
    "default_model": "model-x",
    "auth_source": "subprocess:claude",
    "is_active": True,
    "created_at": "2026-01-01T00:00:00+00:00",
}
SECRET = "SECRET_STDERR token=abc /home/someone/.claude"


@pytest.fixture
def failing_cli(_providers_file, monkeypatch):
    from plugboard import circuit_breaker, cli_subprocess
    from plugboard.cli_subprocess import CliSubprocessError

    _providers_file.write_text(json.dumps({"providers": [CLI]}))
    router.invalidate_models_cache()
    monkeypatch.setattr(circuit_breaker, "_circuits", {})

    async def fail(kind, body, *, timeout_s=None):
        raise CliSubprocessError(
            f"CLI 'claude' exited 1: {SECRET}",
            exit_code=1,
            stderr=SECRET,
            pid=4242,
            provider_name="claude",
            model_id=body["model"],
        )

    monkeypatch.setattr(cli_subprocess, "complete", fail)


async def test_a_failed_cli_route_sends_no_event_with_its_stderr_or_pid(failing_cli):
    """BUG-74: before, the stream's first chunk was the CLI's error event."""
    from plugboard.cli_subprocess import CliSubprocessError

    sent = []
    with pytest.raises(CliSubprocessError):
        async for chunk in router.forward_stream(
            {"model": "pbtest-cli:model-x", "messages": [], "stream": True}
        ):
            sent.append(chunk)
    assert sent == []
