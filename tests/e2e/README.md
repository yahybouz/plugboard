# Dashboard E2E suite (Playwright / Chromium)

Browser-level end-to-end tests of the **admin dashboard** — the 10 tabs, their
CRUD flows, rendering, and accessibility.

> **Not** to be confused with [`tests/live_model_tests/`](../live_model_tests/),
> which drives the **Playground** to test the *models* (D1–D6 across API/MCP/UI).
> This suite tests the dashboard **features** against a fully isolated server,
> with **no real LLM calls** and **zero pollution** of the real `data/`.

---

## Running

```bash
make test-e2e                 # the whole suite (sets PLUGBOARD_RUN_E2E=1)

# or directly:
PLUGBOARD_RUN_E2E=1 pytest tests/e2e -v
PLUGBOARD_RUN_E2E=1 pytest tests/e2e/test_keys.py
PLUGBOARD_RUN_E2E=1 pytest "tests/e2e/test_a11y.py::test_tab_has_no_blocking_a11y_violations[jobs]"
```

**Opt-in by design.** Like the live tests, the E2E suite is **auto-skipped** by a
plain `pytest tests/` / `make test` (it boots a server + Chromium). It only runs
when `PLUGBOARD_RUN_E2E=1` is set. This keeps the unit suite fast and free of a
Chromium dependency.

First run downloads `axe-core` once into `vendor/` (then hermetic/offline).

---

## Architecture (`conftest.py`)

| Fixture | Scope | What it gives you |
|---------|-------|-------------------|
| `e2e_server` | session | An **isolated** Plugboard server (subprocess). `PLUGBOARD_DATA`=tmpdir, ephemeral `ENCRYPTION_KEY`, schedulers/probes/internet-check OFF, free port. Seeds 1 provider (2 models) + a pricing grid. Yields the base URL. |
| `browser` | session | One headless Chromium (async API), reused across tests. |
| `dashboard` | function | A logged-in page (`Dashboard`) — **real CSP enforced**. `.page`, `.goto_tab(tab)`, `.console_errors`, `.page_errors`, `.js_errors`. |
| `a11y_dashboard` | function | Same, but `bypass_csp=True` so `axe-core` can be injected (see a11y notes). |
| `axe_script` | session | The vendored `axe-core` source (downloaded once into `vendor/`). |

### Why these choices

- **Isolation = zero pollution.** `PLUGBOARD_DATA` is read at import time by 14+
  modules, so a subprocess server with it set to a temp dir routes *every* write
  (keys, billing, providers, cache, audit…) into throwaway space. Verified: the
  real `data/providers.json` and `data/pricing/*` are unchanged after a run.
- **Async Playwright.** The project runs pytest with `asyncio_mode = auto` (a live
  event loop), and Playwright's *sync* API refuses to run inside one. So fixtures
  and tests are `async` and share **one session-scoped loop**
  (`@pytest.mark.asyncio(loop_scope="session")`), letting a single Chromium be
  reused.
- **Auto-login.** On localhost the dashboard auto-authenticates (drops `.hidden`
  off `#app`), so no key handling is needed.

### Selector conventions

- Tabs: navigate with `dashboard.goto_tab("jobs")` → waits for `#page-jobs.active`.
- Playground has stable `data-testid="pg-*"` hooks; other tabs use their element
  ids (`#keys-table-container`, `#providers-table-container`, `#jobs-list`, …).
- Assert **classes/structure**, not colors (e.g. `.badge-green` / `.badge-red`),
  so the functional specs are immune to the a11y color changes.

---

## Coverage matrix

| Tab | Smoke | Interaction / data | a11y |
|-----|:----:|--------------------|:----:|
| overview | ✅ | seeded provider count (`test_data_tabs`) | ✅ |
| keys | ✅ | **CRUD**: create → `lle_` key → revoke (`test_keys`) | ✅ |
| usage | ✅ | summary container (`test_data_tabs`) | ✅ |
| providers | ✅ | seeded row + **edit toggle active↔inactive** + add-form (`test_providers`) | ✅ |
| connections | ✅ | table container (`test_data_tabs`) | ✅ |
| pricing | ✅ | seeded grid renders models (`test_pricing_playground`) | ✅ |
| playground | ✅ | dropdown populated + Send ready, hermetic (`test_pricing_playground`) | ✅ |
| ollama | ✅ | models container (`test_data_tabs`) | ✅ |
| monitoring | ✅ | seeded provider listed (`test_data_tabs`) | ✅ |
| jobs | ✅ | watchdog Run now disabled, scheduler jobs enabled, Details viewer (`test_jobs`) | ✅ |

Cross-cutting: `test_responsive` (mobile viewport), `test_a11y` (WCAG 2.1 AA per tab).

---

## Accessibility notes (`test_a11y.py`, MANIFESTO §19)

- Injects `axe-core` and asserts **no `critical` or `serious`** WCAG 2.1 A/AA
  violations per tab. `moderate`/`minor` are reported but don't fail.
- **CSP bypass**: the dashboard ships a strict `Content-Security-Policy` (good —
  enforced by every non-a11y test). That CSP blocks inline `<script>` injection,
  which is how axe loads — hence the dedicated `a11y_dashboard` context with
  `bypass_csp=True`. The product policy is untouched.
- **Settle before auditing**: the `.page` fade-in animates `opacity 0→1`. Auditing
  mid-animation reports *phantom* contrast failures (every element measured at
  partial opacity). `_settle()` waits for `opacity === '1'` first.
- Real violations found **and fixed** while building this suite:
  - `select-name` / `label` (critical): added `aria-label` to `#pg-model`,
    `#usage-month`, `#pricing-month`, and the dynamic pricing inputs.
  - `color-contrast` (serious): darkened `--text-muted`, `--green`, `--red` in
    `static/style.css` to meet ≥4.5:1.

---

## Extending

Add a spec by depending on the `dashboard` fixture:

```python
import pytest
pytestmark = pytest.mark.asyncio(loop_scope="session")

async def test_my_flow(dashboard):
    await dashboard.goto_tab("providers")
    await dashboard.page.click("#some-button")
    assert await dashboard.page.locator("#result").is_visible()
    assert dashboard.js_errors == []
```

If a test mutates session state (the server is session-scoped), **restore it** at
the end (see `test_edit_provider_toggles_active_state`) so it stays order-independent.

---

## Deferred (not yet covered)

- **Visual regression** (screenshot baselines): needs a baseline-management harness
  and is inherently flaky across OS/font rendering — deliberately out of scope here.
- Deepest multi-step flows (full Connections OAuth wizard end-to-end).

These are tracked in `TODOS.md`.
