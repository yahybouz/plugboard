"""scripts/recette_ollama_off.py against the real app, in process (PLU-53)."""

# ruff: noqa: F811
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from tests.test_ollama_off_by_config import bare, config_off  # noqa: F401

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "recette_ollama_off.py"


@pytest.fixture
def recette(monkeypatch):
    spec = importlib.util.spec_from_file_location("recette_ollama_off", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_the_script_passes_against_an_app_with_ollama_off(
    bare, recette, monkeypatch
):
    import asyncio

    client, auth, calls = bare
    monkeypatch.setattr(recette, "KEY", auth["Authorization"].split()[1])

    def fake_call(method, path, body=None, timeout=20.0):
        fut = asyncio.run_coroutine_threadsafe(
            _send(client, auth, method, path, body), loop
        )
        return fut.result(10)

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(recette, "call", fake_call)
    code = await asyncio.to_thread(recette.run)
    assert code == 0
    assert calls == []


async def _send(client, auth, method, path, body):
    resp = await client.request(method, path, headers=auth, json=body)
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, None


def test_a_missing_key_is_a_usage_error(recette, monkeypatch):
    monkeypatch.setattr(recette, "KEY", "")
    assert recette.main() == 2


def test_a_control_character_inside_the_key_is_refused_without_echo(
    recette, monkeypatch, capsys
):
    """Python would quote the whole ``Authorization`` value in its refusal."""
    called = []
    monkeypatch.setattr(recette, "KEY", "pb_fake_key\rnot_real_0123456789")
    monkeypatch.setattr(recette, "call", lambda *a, **k: called.append(a))
    assert recette.main() == 2
    shown = capsys.readouterr()
    text = shown.out + shown.err
    assert "PLUGBOARD_SMOKE_KEY" in shown.err and "not shown" in shown.err
    assert "pb_fake_key" not in text and "not_real" not in text
    assert called == []


def test_a_key_python_cannot_send_is_a_usage_error_without_a_trace(
    recette, monkeypatch, capsys
):
    """A character outside latin-1 cannot go in a header: http.client raises a
    ValueError (UnicodeEncodeError) before anything is sent."""
    monkeypatch.setattr(recette, "KEY", "pb_fake_key_€_not_real_0123456789")
    monkeypatch.setattr(recette, "URL", "http://127.0.0.1:9")
    assert recette.main() == 2
    shown = capsys.readouterr()
    text = shown.out + shown.err
    assert "UnicodeEncodeError" in shown.err and "Traceback" not in text
    assert "pb_fake_key" not in text and "not_real" not in text
