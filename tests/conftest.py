"""Shared fixtures for plugboard tests."""

from __future__ import annotations

import os
import inspect
import sys
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
    import atexit as _atexit
    import shutil as _shutil
    import tempfile as _tempfile

    os.environ["PLUGBOARD_DATA"] = _tempfile.mkdtemp(prefix="plugboard-tests-")
    # Removed when the run ends, so test runs leave nothing in /tmp.
    _atexit.register(_shutil.rmtree, os.environ["PLUGBOARD_DATA"], True)

# The codex catalogue depends on codex's sign-in and model cache (BUG-78):
# the suite never reads the developer's ~/.codex. Live tests keep it.
if os.environ.get("PLUGBOARD_RUN_LIVE_TESTS", "0") != "1":
    import atexit as _atexit_codex
    import shutil as _shutil_codex
    import tempfile as _tempfile_codex

    os.environ["CODEX_HOME"] = _tempfile_codex.mkdtemp(prefix="plugboard-tests-codex-")
    _atexit_codex.register(_shutil_codex.rmtree, os.environ["CODEX_HOME"], True)

import json  # noqa: E402
import aiohttp  # noqa: E402
import pytest  # noqa: E402
from aioresponses import core as aioresponses_core  # noqa: E402
from pathlib import Path  # noqa: E402
from httpx import AsyncClient, ASGITransport  # noqa: E402

from tests.factories import PROVIDER_A, PROVIDER_B  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "live: calls external services; requires PLUGBOARD_RUN_LIVE_TESTS=1"
    )


def pytest_collection_modifyitems(config, items):
    """Keep real service calls opt-in regardless of their test directory."""
    if os.environ.get("PLUGBOARD_RUN_LIVE_TESTS", "0") == "1":
        return
    skip_live = pytest.mark.skip(
        reason="Live tests skipped — set PLUGBOARD_RUN_LIVE_TESTS=1 to enable."
    )
    for item in items:
        if item.get_closest_marker("live") is not None:
            item.add_marker(skip_live)


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


@pytest.fixture(autouse=True)
def _restore_process_state():
    """Undo process-wide state a test can leave behind.

    ``scripts/run_mcp.py`` run in-process in network mode keeps only the MCP
    variables in ``os.environ`` (here, the pytest process), and an unrecorded
    decision opens the per-process usage breaker of ``plugboard.decision_api``.
    """
    environ = dict(os.environ)
    yield
    if dict(os.environ) != environ:
        os.environ.clear()
        os.environ.update(environ)
    decision_api = sys.modules.get("plugboard.decision_api")
    if decision_api is not None:
        decision_api._usage_down_since = None


@pytest.fixture(autouse=True)
def _no_real_agy_store():
    """complete("agy") purges agy's store under the HOME given to agy
    (BUG-97), and fake agy binaries run with the developer's HOME: the purge
    finds no store, so the suite never touches a real ~/.gemini. The tests of
    the purge (test_agy_store_purge.py) give agy a throwaway HOME and put the
    real lookup back. Without ``monkeypatch``, see _reset_load_state."""
    from plugboard import agy_store

    real = agy_store.store_dir
    agy_store.store_dir = lambda home: None
    yield
    agy_store.store_dir = real


@pytest.fixture
def agy_without_cage(monkeypatch):
    """TESTS ONLY, never autouse: replaces agy's bubblewrap cage (BUG-86,
    plugboard/agy_cage.py) by a pass-through, so agy's command line is the
    bare binary and its arguments.

    For the tests written before the cage whose fake agy is a shell script
    run outside bwrap (it needs /bin/sh, which the cage does not hold), or
    whose fake process stands in for agy itself: what they check (purge,
    private TMPDIR, stream, usage, process group...) does not depend on the
    cage. The cage itself, and agy run inside it, are tested in
    tests/test_agy_cage.py. The gateway has no such switch."""
    from plugboard import agy_cage

    monkeypatch.setattr(
        agy_cage, "cage_argv", lambda binary, args, **_kw: [binary, *args]
    )


@pytest.fixture(autouse=True)
def _fresh_agy_cage():
    """What agy's cage checks or resolves once per process (bwrap's path,
    whether a new /proc can be mounted) starts unknown in every test: a test
    puts its own bwrap on PATH."""
    from plugboard import agy_cage

    agy_cage._reset_for_tests()
    yield
    agy_cage._reset_for_tests()


@pytest.fixture(autouse=True)
def _reset_load_state():
    """Provider leases, route pauses, circuits, session turns and the global
    chat slots start empty in every test (PLU-9, PLU-8, PLU-14): a circuit
    left open by a test must not reorder the next one's routes, a turn or a
    slot left held must not block the next test (the cap is on by default).

    Without ``monkeypatch``: autouse fixtures are set up in name order, so
    requesting it here would set it up before ``_restore_process_state``
    and undo a test's ``setattr`` after that fixture's reset (a test that
    sets ``decision_api._usage_down_since`` would leave the breaker open).
    """
    from plugboard import (
        circuit_breaker,
        provider_admission,
        route_pauses,
        session_turns,
    )

    saved = (circuit_breaker._circuits, session_turns._turns)
    provider_admission.reset()
    route_pauses.reset()
    circuit_breaker._circuits = {}
    session_turns._turns = {}
    _reset_chat_slots()
    yield
    provider_admission.reset()
    route_pauses.reset()
    circuit_breaker._circuits, session_turns._turns = saved
    _reset_chat_slots()


def _reset_chat_slots() -> None:
    """A new global semaphore for the next test, once the app is loaded: an
    asyncio semaphore belongs to the event loop of its first wait."""
    app = sys.modules.get("plugboard.app")
    if app is not None:
        app._inflight_chat_sem = None
        app._inflight_chat_active = 0


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
