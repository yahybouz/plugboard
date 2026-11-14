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
