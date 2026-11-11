# Sign out every dashboard session (PB-SEC-04) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the admin revoke every dashboard session at once, from an admin route and a dashboard button, and revoke them all when `ENCRYPTION_KEY` is rotated.

**Architecture:** Sessions are already server-side (`plugboard/admin_sessions.py`, `revoke_all()`). A new admin-only route calls it; the dashboard sidebar gets a button that calls the route; the rotation script calls it after a completed rotation. No new store, no new module.

**Tech Stack:** Python 3.12, FastAPI, httpx `AsyncClient` tests (pytest-asyncio), vanilla JS dashboard, Playwright E2E (opt-in, `PLUGBOARD_RUN_E2E=1`).

**Spec:** `docs/superpowers/specs/2026-11-10-session-logout-all-design.md`

## Global Constraints

- Route: `POST /auth/admin/logout-all`, `include_in_schema=False`, `status_code=204`, guarded by `Depends(_require_admin)`.
- Audit action name: `auth.logout_all`.
- Button id `logout-all-btn`, label `Log out everywhere`, placed right after `#logout-btn` in `.sidebar-footer` of `static/index.html`.
- Rotation revokes only when the script reaches its success path (not `--dry-run`, not refused).
- Docs in English; tests run with `make test` (temp `PLUGBOARD_DATA`); no real keys or personal data.
- Conventional Commits; one commit per task.

## Review Focus

1. Session store not writable (read-only volume): the route must not claim success — `503`, no `auth.logout_all` audit row. Test in Task 1.
2. The calling browser is itself revoked: its next request gets `401`, and the response clears `plugboard_admin_session`. Test in Task 1.
3. A script calling with the admin bearer key (no session): `204` and every session revoked. Test in Task 1.
4. The operator cancels the confirmation: nothing is revoked, the dashboard stays open. E2E test in Task 2.
5. Rotation succeeds but the session store cannot be rewritten: the rotation still exits `0` (secrets are already rewritten) and stderr tells the operator to use "Log out everywhere" after the restart. Test in Task 3.

---

### Task 1: `POST /auth/admin/logout-all`

**Files:**
- Modify: `plugboard/app.py` — new handler right after `admin_logout` (~line 5410)
- Modify: `docs/guide/monitoring.md:21` — sessions paragraph
- Create: `tests/test_session_logout_all.py`

**Interfaces:**
- Consumes: `admin_sessions.revoke_all() -> None` (raises `OSError` when the store cannot be written), `_require_admin`, `_audit(action, details=...)`.
- Produces: `async def admin_logout_all(request: Request, _: None = Depends(_require_admin)) -> Response` at `POST /auth/admin/logout-all`.

- [ ] **Step 1: Write the failing tests** in `tests/test_session_logout_all.py`.

Fixture `sessions(tmp_path, monkeypatch)`: point `admin_sessions._path` at `tmp_path / "admin_sessions.json"`, unset `PLUGBOARD_ADMIN_MFA_REQUIRED`, clear `ratelimit._auth_buckets` and `ratelimit._lockouts` before and after (same pattern as the `mfa` fixture in `tests/test_auth_residuals_v7b.py:283`). Copy its `_login(client)` helper (returns `{"Cookie": "plugboard_admin_session=<value>"}`) and a `_keys(client, headers)` helper returning the status of `GET /admin/keys`. All tests use the `admin_client` fixture (admin key `adminkey`).

```python
async def test_logout_all_revokes_every_session(admin_client, sessions):
    first, second = await _login(admin_client), await _login(admin_client)
    assert await _keys(admin_client, first) == 200
    assert await _keys(admin_client, second) == 200
    admin_client.cookies.clear()
    resp = await admin_client.post("/auth/admin/logout-all", headers=first)
    assert resp.status_code == 204
    assert await _keys(admin_client, first) == 401
    assert await _keys(admin_client, second) == 401

async def test_logout_all_clears_this_browser_cookie(admin_client, sessions):
    first = await _login(admin_client)
    admin_client.cookies.clear()
    resp = await admin_client.post("/auth/admin/logout-all", headers=first)
    cookies = resp.headers.get_list("set-cookie")
    assert any(c.startswith("plugboard_admin_session=") and "Max-Age=0" in c for c in cookies)

async def test_logout_all_with_admin_key(admin_client, sessions):
    first = await _login(admin_client)
    admin_client.cookies.clear()
    resp = await admin_client.post("/auth/admin/logout-all", headers=ADMIN_BEARER)
    assert resp.status_code == 204
    assert await _keys(admin_client, first) == 401

async def test_logout_all_needs_admin(admin_client, sessions):
    first = await _login(admin_client)
    admin_client.cookies.clear()
    resp = await admin_client.post("/auth/admin/logout-all")
    assert resp.status_code == 401
    resp = await admin_client.post(
        "/auth/admin/logout-all", headers={"Authorization": "Bearer testkey"}
    )
    assert resp.status_code in (401, 403)
    assert await _keys(admin_client, first) == 200

async def test_logout_all_is_audited(admin_client, sessions):
    before = count_audit_log("auth.logout_all")
    admin_client.cookies.clear()
    await admin_client.post("/auth/admin/logout-all", headers=ADMIN_BEARER)
    assert count_audit_log("auth.logout_all") == before + 1

async def test_logout_all_unwritable_store_says_so(admin_client, sessions, monkeypatch):
    first = await _login(admin_client)
    before = count_audit_log("auth.logout_all")
    def refuse(*a, **k): raise PermissionError("read-only store")
    monkeypatch.setattr(admin_sessions, "_save", refuse)
    admin_client.cookies.clear()
    resp = await admin_client.post("/auth/admin/logout-all", headers=first)
    assert resp.status_code == 503
    assert "not writable" in resp.json()["detail"]
    assert count_audit_log("auth.logout_all") == before
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest tests/test_session_logout_all.py -q -p no:cacheprovider`
Expected: FAIL (route answers 404/405).

- [ ] **Step 3: Implement `admin_logout_all` in `plugboard/app.py`**

Call `admin_sessions.revoke_all()`; on `OSError` raise `HTTPException(503, "Sessions could not be revoked: the session store is not writable.")` before any audit. On success `_audit("auth.logout_all", details="every admin session revoked")`, then return a 204 `Response` that deletes `_ADMIN_COOKIE_NAME` and `llm_admin_key` on path `/`, as `admin_logout` does.

- [ ] **Step 4: Run the tests to see them pass**

Run: `.venv/bin/python -m pytest tests/test_session_logout_all.py tests/test_auth_residuals_v7b.py -q -p no:cacheprovider`
Expected: all PASS.

- [ ] **Step 5: Docs** — in `docs/guide/monitoring.md:21`, after "Logout revokes that session;", add: "`POST /auth/admin/logout-all` (dashboard: **Log out everywhere**) revokes every session, this one included;".

- [ ] **Step 6: Commit**

```bash
git add plugboard/app.py tests/test_session_logout_all.py docs/guide/monitoring.md
git commit -m "feat(security): revoke every admin session at once"
```

### Task 2: "Log out everywhere" button

**Files:**
- Modify: `static/index.html:104` — button after `#logout-btn`
- Modify: `static/app.js:27-33,109` — function and listener next to `logout()`
- Create: `tests/e2e/test_logout_all.py`

**Interfaces:**
- Consumes: `POST /auth/admin/logout-all` (Task 1): 204 on success, 401 when the session is already gone, 503 when the store is not writable.
- Produces: `async function logoutEverywhere()` in `static/app.js`.

- [ ] **Step 1: Write the failing E2E tests** in `tests/e2e/test_logout_all.py` (`pytestmark = pytest.mark.asyncio(loop_scope="session")`, use `browser`, `e2e_server` and `_open_logged_in` from `tests/e2e/conftest.py`). Each test opens two logged-in contexts A and B and closes both at the end.

```python
async def _status(dash, path="/admin/keys"):
    return await dash.page.evaluate(
        "p => fetch(p, {credentials: 'same-origin'}).then(r => r.status)", path
    )

async def test_log_out_everywhere_signs_out_other_browsers(browser, e2e_server):
    # A accepts the confirm dialog and lands on /login; B is refused afterwards.
    a.page.once("dialog", lambda d: asyncio.ensure_future(d.accept()))
    await a.page.locator("#logout-all-btn").click()
    await a.page.wait_for_url("**/login**")
    assert await _status(b) == 401
    assert not a.js_errors

async def test_cancel_keeps_every_session(browser, e2e_server):
    a.page.once("dialog", lambda d: asyncio.ensure_future(d.dismiss()))
    await a.page.locator("#logout-all-btn").click()
    assert await _status(a) == 200 and await _status(b) == 200
```

- [ ] **Step 2: Run them to see them fail**

Run: `PLUGBOARD_RUN_E2E=1 .venv/bin/python -m pytest tests/e2e/test_logout_all.py -q -p no:cacheprovider`
Expected: FAIL (no `#logout-all-btn`). If Chromium is not installed here, say so in the report and rely on Step 4's unit run plus the manual browser check at verification.

- [ ] **Step 3: Implement**

`static/index.html`: `<button type="button" id="logout-all-btn" class="btn btn-ghost btn-sm">Log out everywhere</button>` right after `#logout-btn`.
`static/app.js`: `logoutEverywhere()` asks `confirm("Log out every browser and device signed in to this dashboard, this one included?")`; on cancel returns. POSTs `/auth/admin/logout-all`; on 204 or 401 goes to `/login?logged_out=1` (explicit logout: no auto-login); on any other status or a network error shows `alert("Could not log out everywhere (HTTP <status>). Sessions are unchanged.")` and stays. Register it on `#logout-all-btn` next to the `#logout-btn` listener (line 109).

- [ ] **Step 4: Run the E2E tests and the dashboard smoke/a11y tests**

Run: `PLUGBOARD_RUN_E2E=1 .venv/bin/python -m pytest tests/e2e/test_logout_all.py tests/e2e/test_dashboard_smoke.py tests/e2e/test_mobile_nav.py tests/e2e/test_a11y.py -q -p no:cacheprovider`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add static/index.html static/app.js tests/e2e/test_logout_all.py
git commit -m "feat(dashboard): log out everywhere button"
```

### Task 3: Rotation of `ENCRYPTION_KEY` signs everyone out

**Files:**
- Modify: `scripts/rotate_encryption_key.py:106-118` — success path
- Modify: `tests/test_secrets_rekey.py` — new CLI tests using `_run_cli` and `_seed`
- Modify: `docs/guide/configuration.md` (section "Changing `ENCRYPTION_KEY`"), `CHANGELOG.md` (`[Unreleased]` → `### Security`), `TODOS.md` (remove "### Session revocation"; PB-AUTH-01 "Depends on: Session revocation" → "Depends on: None")

**Interfaces:**
- Consumes: `admin_sessions.revoke_all()`; the script and `admin_sessions._path()` both resolve `data_dir()` from `PLUGBOARD_DATA`.
- Produces: nothing used elsewhere.

- [ ] **Step 1: Write the failing tests** in `tests/test_secrets_rekey.py`. Helper `_seed_session(data)` writes `data/admin_sessions.json` as `{"sessions": {"0"*64: {"issued_at": 1800000000, "mfa": False, "mfa_generation": None}}}`; helper `_sessions(data)` returns the `sessions` dict.

```python
def test_cli_rotation_signs_out_every_session(tmp_path):
    data = tmp_path / "data"; _seed(data); _seed_session(data)
    (data / ".encryption_key").write_text(OLD)
    run = _run_cli(tmp_path, "--new-key-file", str(tmp_path / "new.key"), "--generate")
    assert run.returncode == 0, run.stderr
    assert _sessions(data) == {}
    assert "Every dashboard session was signed out" in run.stdout

def test_cli_dry_run_keeps_sessions(tmp_path):
    # same seed, with "--dry-run": returncode 0, _sessions(data) unchanged

def test_cli_refused_rotation_keeps_sessions(tmp_path):
    # seed an unreadable secret as test_cli_refuses_a_weak_key_and_reports_unreadable_secrets does:
    # returncode 1, _sessions(data) unchanged

def test_rotation_warns_when_sessions_cannot_be_revoked(tmp_path, monkeypatch, capsys):
    # In-process: load scripts/rotate_encryption_key.py with importlib (as
    # tests/test_auth_residuals_v7b.py loads scripts), set PLUGBOARD_DATA to
    # tmp_path / "data", PLUGBOARD_SKIP_DOTENV=1, unset ENCRYPTION_KEY, and make
    # admin_sessions.revoke_all raise PermissionError("read-only store").
    # An unreadable store needs no test here: revoke_all already treats it as
    # "no session valid" (fail closed) and does not raise.
    code = script.main(["--new-key-file", str(tmp_path / "new.key"), "--generate"])
    assert code == 0
    assert "Log out everywhere" in capsys.readouterr().err
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest tests/test_secrets_rekey.py -q -p no:cacheprovider`
Expected: the signed-out and warning tests FAIL; dry-run and refused may already pass.

- [ ] **Step 3: Implement** in `scripts/rotate_encryption_key.py`, after the "Done." line and before the next-step messages: `admin_sessions.revoke_all()`; on success print `Every dashboard session was signed out: log in again once Plugboard restarts.`; on `OSError` print to stderr `Warning: dashboard sessions could not be signed out (<error>). Use "Log out everywhere" in the dashboard once Plugboard restarts.` and keep exit code 0.

- [ ] **Step 4: Run the tests to see them pass**

Run: `.venv/bin/python -m pytest tests/test_secrets_rekey.py -q -p no:cacheprovider`
Expected: all PASS.

- [ ] **Step 5: Docs** — `configuration.md`: one sentence after the rotation paragraph: "A completed rotation also signs out every dashboard session." `CHANGELOG.md` `### Security`: one bullet "**Log out everywhere**" covering the route, the button and the rotation, linking `docs/guide/monitoring.md`. `TODOS.md` as listed above.

- [ ] **Step 6: Full suite, lint, commit**

Run: `make lint && make test`
Expected: ruff clean; all tests pass (same count as the baseline plus the new ones).

```bash
git add scripts/rotate_encryption_key.py tests/test_secrets_rekey.py docs/guide/configuration.md CHANGELOG.md TODOS.md
git commit -m "feat(security): sign out every session after an encryption key rotation"
```
