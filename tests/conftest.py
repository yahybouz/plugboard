"""Shared fixtures for plugboard tests."""

from __future__ import annotations

import os
import inspect
from types import SimpleNamespace
from typing import Any

# Must be set BEFORE any `plugboard` import so `config.py`'s module-level
# `load_dotenv()` call is skipped. Otherwise the real `.env` (API_KEY="",
# CORS_ORIGINS=*, OPENCLAW_URL=...) leaks into every test process and
# breaks fixtures that assume a clean environment.
# The suite sets its own configuration: drop Plugboard variables exported by
# the developer's shell (old and new names), except the opt-in switches for
# live and end-to-end tests.
for _name in list(os.environ):
    if _name.startswith(("PLUGBOARD_", "LLM_" + "ENABLER_")) and "_RUN_" not in _name:
        del os.environ[_name]

os.environ.setdefault("PLUGBOARD_SKIP_DOTENV", "1")

# Give `app.py` a non-empty `API_KEY` at import time so the import-time
# CORS + auth-disabled banner compute the "prod-like" branch by default.
# Individual tests that exercise the empty-key path still monkeypatch
# `_API_KEY` directly on the module.
os.environ.setdefault("API_KEY", "testkey-conftest")
os.environ.setdefault("ADMIN_KEY", "adminkey-conftest")

# No network discovery of the public Ollama cloud catalogue during tests;
# tests that exercise it enable it explicitly.
os.environ.setdefault("PLUGBOARD_OLLAMA_CLOUD_AUTOSYNC", "0")

# Every module resolves its data directory at import time: point it at a
# throwaway directory so the suite never writes into the repository's data/
# (or a live install's providers, keys and pricing grids).
if not os.environ.get("PLUGBOARD_DATA"):
    import tempfile as _tempfile

    os.environ["PLUGBOARD_DATA"] = _tempfile.mkdtemp(prefix="plugboard-tests-")

import json  # noqa: E402
import aiohttp  # noqa: E402
import pytest  # noqa: E402
from aioresponses import core as aioresponses_core  # noqa: E402
from pathlib import Path  # noqa: E402
from httpx import AsyncClient, ASGITransport  # noqa: E402

from tests.factories import PROVIDER_A, PROVIDER_B  # noqa: E402


def _install_aioresponses_aiohttp_compat() -> None:
    """Let aioresponses build responses for aiohttp requiring stream_writer."""
    stream_writer = inspect.signature(aiohttp.ClientResponse).parameters.get(
        "stream_writer"
    )
    if stream_writer is None or stream_writer.default is not inspect.Parameter.empty:
        return

    class _CompatibleClientResponse(aiohttp.ClientResponse):
        def __init__(
            self,
            *args: Any,
            stream_writer: Any = None,
            **kwargs: Any,
        ) -> None:
            if stream_writer is None:
                stream_writer = SimpleNamespace(output_size=0)
            super().__init__(*args, stream_writer=stream_writer, **kwargs)

    aioresponses_core.ClientResponse = _CompatibleClientResponse


_install_aioresponses_aiohttp_compat()


@pytest.fixture(autouse=True)
async def _close_http_pool():
    """Reset the shared aiohttp pool after each test.

    PERF-2 introduced a singleton ClientSession. Pytest-asyncio gives
    each test its own event loop, so the singleton session must be
    dropped between tests — otherwise aiohttp logs "Unclosed client
    session" warnings and will eventually raise because the session
    was bound to the previous loop.
    """
    yield
    try:
        from plugboard.http_pool import reset_for_tests

        await reset_for_tests()
    except Exception:
        pass


def _setup_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, providers: list
) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    pfile = data_dir / "providers.json"
    pfile.write_text(json.dumps({"providers": providers}))
    monkeypatch.setenv("PLUGBOARD_DATA", str(data_dir))
    import plugboard.config as cfg

    cfg.PROVIDERS_FILE = pfile
    cfg.PROVIDERS_LOCK = data_dir / "providers.json.lock"
    # Invalidate models cache so tests see fresh provider data
    from plugboard.router import invalidate_models_cache

    invalidate_models_cache()
    return pfile


@pytest.fixture
def _providers_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Temp providers.json with two providers."""
    return _setup_providers(tmp_path, monkeypatch, [PROVIDER_A, PROVIDER_B])


@pytest.fixture
def _empty_providers_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Temp providers.json with no providers."""
    # Prevent _bootstrap() from auto-creating a provider from env vars
    monkeypatch.delenv("OPENCLAW_URL", raising=False)
    monkeypatch.delenv("OPENCLAW_KEY", raising=False)
    return _setup_providers(tmp_path, monkeypatch, [])


@pytest.fixture
def _api_key(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr("plugboard.app._API_KEY", "testkey")
    return "testkey"


@pytest.fixture
def _admin_key(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "adminkey")
    monkeypatch.setattr("plugboard.app._API_KEY", "testkey")
    return "adminkey"


@pytest.fixture
async def client(
    _providers_file: Path, _api_key: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    """Async HTTP client wired to the FastAPI app."""
    monkeypatch.setattr(
        "plugboard.app._ADMIN_KEY", "testkey"
    )  # same key for simplicity
    # Disable rate limiting in tests
    import plugboard.ratelimit as rl

    monkeypatch.setattr(rl, "_LIMIT", 0)
    from plugboard.app import app

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


@pytest.fixture
async def admin_client(
    _providers_file: Path, _admin_key: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    """Async HTTP client with admin auth.

    Disables the global RATE_LIMIT (same as the `client` fixture) so tests
    that create many admin resources don't poison the shared `_buckets`
    dict and cause downstream tests to return 429. Tests that *want* to
    assert rate-limit behaviour (`test_rate_limit_actually_blocks`) build
    their own client directly and set `_LIMIT` explicitly.
    """
    import plugboard.ratelimit as rl

    monkeypatch.setattr(rl, "_LIMIT", 0)
    # Clear any leaked buckets from earlier tests that ran without this
    # fixture's monkeypatch (e.g. on session startup).
    rl._buckets.clear()
    rl._client_buckets.clear()

    from plugboard.app import app

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c
