"""E2E harness — an isolated Plugboard server + a logged-in Chromium page.

Design goals (in priority order):
1. **Zero pollution** of the real ``data/`` working tree. The server runs in a
   subprocess with ``PLUGBOARD_DATA`` pointed at a throwaway temp dir, so every
   write (keys, billing, providers, cache, audit…) lands there and is discarded.
2. **No real LLM calls.** Schedulers, boot probes and the internet check are all
   disabled; the seeded provider points at a local URL that is never actually hit
   by the render/CRUD specs.
3. **Reuse the existing UI conventions.** Auto-login on localhost (the app drops
   ``.hidden`` off ``#app``); tab pages are ``#page-<tab>.active``; the Playground
   exposes ``data-testid="pg-*"`` hooks. We do not reinvent selectors.
"""

from __future__ import annotations

import dataclasses
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Iterator, List

import pytest
import pytest_asyncio

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_RUN_PY = _PROJECT_ROOT / "run.py"

# The 10 dashboard tabs (data-tab / #page-<tab>) in nav order.
DASHBOARD_TABS: List[str] = [
    "overview",
    "keys",
    "usage",
    "providers",
    "connections",
    "pricing",
    "playground",
    "ollama",
    "monitoring",
    "jobs",
]


def pytest_collection_modifyitems(config, items):
    """Skip the E2E suite by default — it boots a real server + Chromium.

    Mirrors the live-test opt-in convention: enable with ``PLUGBOARD_RUN_E2E=1``
    (what ``make test-e2e`` sets), so the standard ``pytest tests/`` / ``make test``
    stays fast and free of a Chromium dependency.
    """
    if os.environ.get("PLUGBOARD_RUN_E2E", "0") == "1":
        return
    skip = pytest.mark.skip(
        reason="E2E skipped — set PLUGBOARD_RUN_E2E=1 (or run `make test-e2e`)."
    )
    for item in items:
        if f"{os.sep}e2e{os.sep}" in str(item.fspath):
            item.add_marker(skip)


def _free_port() -> int:
    """Reserve an ephemeral port, then release it for the server to bind."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _server_env(data_dir: Path, enc_key: str, port: int) -> dict:
    """Build a hermetic environment for the isolated server / seeder.

    We start from the real env (PATH, venv, etc.) but strip everything that
    could leak the operator's config into the test server or trigger network:
    no real provider bootstrap, no domain allowlist, no inherited encryption key.
    """
    env = dict(os.environ)
    for leak in (
        "OPENCLAW_URL",
        "OPENCLAW_KEY",
        "OPENCLAW_MODEL",
        "OPENCLAW_NAME",
        "PLUGBOARD_PROVIDER_DOMAINS_ALLOWED",
        "ADMIN_KEY",  # force the localhost auto-login path, not key auth
        "MODEL_STATUS_PATH",
    ):
        env.pop(leak, None)
    env.update(
        {
            "PLUGBOARD_DATA": str(data_dir),
            "ENCRYPTION_KEY": enc_key,
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "RELOAD": "false",
            # Keep the boot lightweight + offline + non-polluting.
            "PRICING_SCHEDULER_DISABLED": "1",
            "MODEL_AVAILABILITY_SCHEDULER_DISABLED": "1",
            "PLUGBOARD_PROBE_ON_BOOT": "false",
            "PLUGBOARD_INTERNET_CHECK_ON_BOOT": "0",
        }
    )
    return env


def _seed_provider(env: dict) -> None:
    """Seed one provider with two models into the isolated data dir.

    Runs in a fresh subprocess so ``config``'s import-time ``_DATA_DIR`` resolves
    to the temp dir. The provider has an explicit ``models`` list so ``/v1/models``
    (and therefore the Playground dropdown) is populated deterministically.
    """
    seed = (
        "from plugboard import config\n"
        "p = config.create_provider(\n"
        "    name='E2E Provider',\n"
        "    base_url='http://127.0.0.1:11434',\n"
        "    bearer_token='e2e-seed-token',\n"
        "    default_model='e2e-model-a',\n"
        "    provider_type='cloud',\n"
        ")\n"
        "config.update_provider(p['id'], {'models': [\n"
        "    {'id': 'e2e-model-a', 'name': 'e2e-model-a'},\n"
        "    {'id': 'e2e-model-b', 'name': 'e2e-model-b'},\n"
        "]})\n"
        "print('seeded', p['id'])\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", seed],
        env=env,
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"provider seed failed (rc={proc.returncode})\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )


def _seed_pricing(data_dir: Path) -> str:
    """Write a minimal pricing grid for the current month (plain JSON, no crypto).

    Without this the dashboard's Pricing tab fetches ``/admin/pricing/<month>``,
    gets a 404 (no data) and renders its empty state — correct behaviour, but it
    means we cannot assert the populated-grid path. Returns the seeded month.
    """
    month = f"{datetime.now(timezone.utc):%Y_%m}"
    pricing_dir = data_dir / "pricing"
    pricing_dir.mkdir(parents=True, exist_ok=True)
    grid = {
        "models": {
            "e2e-model-a": {"input_price": 1.0, "output_price": 2.0},
            "e2e-model-b": {"input_price": 3.0, "output_price": 6.0},
        },
        "note": "E2E seed grid",
    }
    (pricing_dir / f"{month}.json").write_text(
        json.dumps(grid, indent=2), encoding="utf-8"
    )
    return month


def _wait_health(
    base_url: str, proc: subprocess.Popen, timeout_s: float = 40.0
) -> None:
    """Poll /health until the server answers ok, or fail with the boot log."""
    deadline = time.monotonic() + timeout_s
    last_err = ""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"server exited early (rc={proc.returncode}) before /health"
            )
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=3) as resp:
                if resp.status == 200 and json.loads(resp.read()).get("ok"):
                    return
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last_err = str(exc)
        time.sleep(0.4)
    raise RuntimeError(
        f"server did not become healthy within {timeout_s}s (last: {last_err})"
    )


@pytest.fixture(scope="session")
def e2e_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Boot an isolated Plugboard server; yield its base URL; tear it down."""
    from cryptography.fernet import Fernet  # local import: only the harness needs it

    data_dir = tmp_path_factory.mktemp("e2e-data")
    enc_key = Fernet.generate_key().decode("ascii")
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = _server_env(data_dir, enc_key, port)

    _seed_provider(env)
    _seed_pricing(data_dir)

    log_path = data_dir / "server.log"
    with open(log_path, "w", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(
            [sys.executable, str(_RUN_PY)],
            env=env,
            cwd=str(_PROJECT_ROOT),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )
        try:
            try:
                _wait_health(base_url, proc)
            except RuntimeError as exc:
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                raise RuntimeError(f"{exc}\n--- server.log tail ---\n{tail}") from exc
            yield base_url
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


# The project runs pytest with ``asyncio_mode = auto``, which keeps an event loop
# running for the whole session. Playwright's *sync* API refuses to run inside a
# live loop, so the E2E suite uses the *async* API. All async fixtures and tests
# share one **session-scoped** loop so a single Chromium process is reused.


_AXE_CACHE = Path(__file__).parent / "vendor" / "axe.min.js"
_AXE_URL = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js"


@pytest.fixture(scope="session")
def axe_script() -> str:
    """The axe-core source, injected into pages for accessibility audits.

    Vendored under ``tests/e2e/vendor/`` and downloaded once on first use so the
    a11y tests stay hermetic (no network at test time) after the initial fetch.
    """
    if not _AXE_CACHE.exists():
        _AXE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        try:
            urllib.request.urlretrieve(_AXE_URL, _AXE_CACHE)  # noqa: S310 (trusted CDN)
        except OSError as exc:  # pragma: no cover - network/offline first run
            pytest.skip(f"axe-core unavailable (offline first run): {exc}")
    return _AXE_CACHE.read_text(encoding="utf-8")


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def browser(e2e_server):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            await browser.close()


@dataclasses.dataclass
class Dashboard:
    """A logged-in dashboard page plus its captured browser-side errors.

    ``page_errors`` are uncaught JS exceptions (``pageerror``) — these are always
    a real bug. ``console_errors`` are ``console.error`` texts, which include
    benign network noise (e.g. a 404 on an optional/missing dataset that the app
    handles by rendering an empty state). ``js_errors`` is the blocking subset:
    page errors plus console errors that are NOT mere failed resource loads.
    """

    page: object  # playwright.async_api.Page
    console_errors: List[str]
    page_errors: List[str]

    _NETWORK_NOISE = "Failed to load resource"

    @property
    def js_errors(self) -> List[str]:
        """Errors that should fail a render test (excludes network 404 noise)."""
        console = [e for e in self.console_errors if self._NETWORK_NOISE not in e]
        return self.page_errors + console

    async def goto_tab(self, tab: str, timeout_ms: int = 10_000) -> None:
        """Navigate to a tab by hash and wait for its page to become active."""
        await self.page.evaluate(f"location.hash = {tab!r}")
        await self.page.wait_for_selector(f"#page-{tab}.active", timeout=timeout_ms)


async def _open_logged_in(browser, base_url: str, *, bypass_csp: bool = False):
    """Open a fresh context, log in via auto-login, capture browser errors."""
    context = await browser.new_context(bypass_csp=bypass_csp)
    page = await context.new_page()
    console_errors: List[str] = []
    page_errors: List[str] = []
    page.on(
        "console",
        lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
    )
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    await page.goto(base_url, wait_until="domcontentloaded", timeout=20_000)
    # Auto-login on localhost reveals #app (app.js strips .hidden once authed).
    await page.wait_for_selector("#app:not(.hidden)", timeout=15_000)
    return context, Dashboard(
        page=page, console_errors=console_errors, page_errors=page_errors
    )


@pytest_asyncio.fixture(loop_scope="session")
async def dashboard(browser, e2e_server) -> "AsyncIterator[Dashboard]":
    """A fresh, logged-in dashboard context (real CSP enforced)."""
    context, dash = await _open_logged_in(browser, e2e_server)
    try:
        yield dash
    finally:
        await context.close()


@pytest_asyncio.fixture(loop_scope="session")
async def a11y_dashboard(browser, e2e_server) -> "AsyncIterator[Dashboard]":
    """Like ``dashboard`` but with ``bypass_csp=True`` so axe-core can be injected.

    The dashboard ships a strict Content-Security-Policy (good — verified by the
    normal-context tests). That CSP blocks inline ``<script>`` injection, which is
    exactly how axe-core loads. Bypassing CSP for THIS context only lets the a11y
    audit run without weakening the product's real policy or its other tests.
    """
    context, dash = await _open_logged_in(browser, e2e_server, bypass_csp=True)
    try:
        yield dash
    finally:
        await context.close()
