# TODOS

Deferred work, with the reason it was deferred. Items move into a release when picked up.

## Security

### ENCRYPTION_KEY re-encryption tool

**What:** A command that decrypts stored secrets with the old key and re-encrypts them with a new one; optionally expose the state in `/health`.

**Why:** Installs whose `.env` came from an older `.env.example` may hold comment text as `ENCRYPTION_KEY` (python-dotenv reads `KEY=   # note` as `# note`). Provider tokens, OAuth credentials and the MFA seed are then encrypted with a publicly derivable key. 3.0.2 only warns at startup, because changing the key makes them unreadable.

**Context:** `config._encrypt_token` / `_decrypt_token`; `admin_mfa.verify` should handle an undecryptable secret without a 500.

**Effort:** M
**Priority:** P1
**Depends on:** None

### Local auto-login trust root

**What:** Replace the loopback-peer trust of local auto-login with a secret only the operator holds: a Jupyter-style login token written to a `0600` file and opened by `make dev`, then a long-lived "remember this browser" cookie.

**Why:** Auto-login trusts a loopback TCP peer plus browser-like headers. Same-machine containers and VMs whose networking ends on the host loopback (Docker Desktop, Podman, Lima/Colima, OrbStack), a forwarder placed in front of a loopback-bound install, or a local process forging headers pass the checks.

**Context:** 3.0.2 limits auto-login to a loopback-bound install opened in the local browser. `PLUGBOARD_LOCAL_AUTO_LOGIN=0` disables it when untrusted containers run on the machine.

**Effort:** M
**Priority:** P2
**Depends on:** None

### MFA step-up

**What:** Require a current TOTP code to rotate (`/admin/mfa/setup`) or disable (`DELETE /admin/mfa`) MFA.

**Why:** Any admin session, including an auto-login one, can currently remove the second factor.

**Context:** `plugboard/app.py` MFA routes; `admin_mfa.verify`.

**Effort:** S
**Priority:** P2
**Depends on:** None

### Session revocation

**What:** Add a session epoch bumped on logout and on key rotation.

**Why:** Admin sessions are stateless HMAC tokens valid 8 h; logout only clears the cookie.

**Context:** `_create_admin_session_token` / `_is_admin_session` in `plugboard/app.py`.

**Effort:** S
**Priority:** P2
**Depends on:** None

### Keyless dev mode on a public bind

**What:** Refuse a non-loopback bind when `API_KEY` and `ADMIN_KEY` are both empty, or default `HOST` to `127.0.0.1` in `run.py`.

**Why:** In keyless mode every route is open to any peer while `run.py` binds `0.0.0.0` by default.

**Context:** `_require_admin` and `_check_dashboard_auth` return early when both keys are empty.

**Effort:** S
**Priority:** P2
**Depends on:** None

### Same-site CSRF on admin POSTs

**What:** Reject state-changing admin requests whose `Sec-Fetch-Site` is not `same-origin`.

**Why:** A page served on another localhost port is same-site, so the `SameSite=Strict` cookie does not stop it.

**Context:** Admin routes in `plugboard/app.py`.

**Effort:** S
**Priority:** P2
**Depends on:** None

## Release hardening (deferred from the stage 2 plan)

### Private companion and identity checks before publication

**What:** A private companion checkout under `private/` (live registry, operator notes, curated preferences, `scan-terms.txt`), a CI job that checks commit author/committer emails against an allowlist, and a CI term scan fed by a masked, protected variable.

**Why:** The public tree must never carry personal data; today the term scan only runs in the local pre-push hook and fails open without `private/scan-terms.txt`.

**Context:** `scripts/pre-push` already supports `PLUGBOARD_REQUIRE_TERMS=1`. Deferred from plan: design-brief.md (Stage 2).

**Effort:** M
**Priority:** P1
**Depends on:** None

### CI secret-scan range after a history replacement

**What:** In `.gitlab-ci.yml`, fall back to a full scan when `CI_COMMIT_BEFORE_SHA` is not an ancestor of `CI_COMMIT_SHA`, and fail when gitleaks scans 0 commits on a non-empty push. Add gitleaks to the GitHub mirror CI. Mark the smoke-job variables Protected and restrict the report artifact.

**Why:** After the planned fresh-history publication the scan range is invalid and gitleaks passes without scanning.

**Context:** The pre-push hook already fails closed in that case.

**Effort:** S
**Priority:** P1
**Depends on:** None

### Missing tests and checks from the stage 2 plan

**What:** Tests for the registry path (override, default, legacy fallback), `make init` never overwriting `.env`, an env-coverage test that `docs/guide/configuration.md` lists every variable the code reads, a Docker image content check, `scripts/run_mcp.py` legacy names; a link checker in CI.

**Why:** These behaviours are documented but only checked by hand.

**Context:** Deferred from plan: design-brief.md (Stage 2).

**Effort:** M
**Priority:** P1
**Depends on:** None

### Remaining public-surface and robustness items

**What:** Translate the provider preset `setup_steps` in `plugboard/direct_providers.py` to English; a `model_not_found` 404 with a hint for an unknown provider prefix; `DOCS_BASE_URL` and a bundled guide at `/guide`; data directory created `0700` and SQLite stores `0600`; refuse a short or placeholder `ADMIN_KEY` outside dev mode; log an unreadable `providers.json` once instead of on every call; warm the tiktoken encoding at startup; skip local Ollama models in the boot internet probe; cutover runbook for an existing install; SDK status line and an OpenAI JS snippet in the docs.

**Why:** Review findings and plan items that do not block the private merge request.

**Context:** Deferred from plan: design-brief.md (Stage 2) and the stage 2 ship review.

**Effort:** L
**Priority:** P1
**Depends on:** None

## Auth

### Per-person dashboard keys

**What:** The admin creates, names, revokes and audits one dashboard key per person, instead of sharing the single `ADMIN_KEY`.

**Why:** Owner decision: a local install logs in without a key, a deployed instance gives each person their own key.

**Context:** Planned for the publishable release. Client API keys (`lle_*`, `POST /admin/keys`) already exist; dashboard sessions are signed with `ADMIN_KEY` only (`_create_admin_session_token`). Needs a session claim naming the key, revocation, and roles if read-only users are wanted.

**Effort:** L
**Priority:** P1
**Depends on:** Session revocation

## MCP

### MCP client identity from the key

**What:** Derive `client_id` from the MCP caller's key instead of trusting the `client_id` argument.

**Why:** A caller could read another client's pending prompts.

**Context:** Needs a key to client mapping in the registry; transport authentication ships first.

**Effort:** M
**Priority:** P2
**Depends on:** MCP network transport authentication

### MCP admin tools for providers and catalog

**What:** MCP tools `add_provider` and `refresh_catalog`.

**Why:** Let coding agents add providers and refresh the catalog without the dashboard.

**Context:** Deferred because admin mutations over MCP need their own threat model (authentication of the MCP caller, audit). Start from `plugboard/mcp_server.py`.

**Effort:** M
**Priority:** P3
**Depends on:** MCP network transport authentication

## Providers and pricing

### Upstream price contributions to models.dev

**What:** Turn an approved price suggestion into a ready-to-submit models.dev contribution (provider.toml snippet).

**Why:** Fixes missing prices for every gateway, not only this one.

**Context:** Price assistant suggestions will live in `data/pricing/suggestions.json`.

**Effort:** S
**Priority:** P3
**Depends on:** Price assistant

### Catalog changelog view

**What:** Dashboard history of models added or removed per provider over time.

**Why:** Operators can see what changed and when.

**Context:** The `catalog.changed` webhook and monitoring card cover day-1 visibility.

**Effort:** M
**Priority:** P3
**Depends on:** Automatic catalog

### Zero-key demo provider

**What:** An echo/demo provider so the first call works without any vendor key.

**Why:** Faster first success for evaluators.

**Context:** New capability; local Ollama already gives a zero-cost path.

**Effort:** S
**Priority:** P4
**Depends on:** None

## Infrastructure

### Signed releases and prebuilt container images

**What:** CI-built images published to the GitLab registry (and mirrored), signed with cosign, with an SBOM.

**Why:** Quickstart becomes a pull, and supply-chain integrity.

**Context:** Needs CI secrets and a registry.

**Effort:** M
**Priority:** P3
**Depends on:** CI pipeline

### Multi-instance deployment

**What:** Shared storage and locking so more than one instance can run.

**Why:** Availability beyond a single process.

**Context:** Current design is single-instance (JSON files with file locks).

**Effort:** XL
**Priority:** P4
**Depends on:** None

## Completed

## Models

### Model family aliases

**What:** `<provider>:latest` (or `<family>:latest`) aliases that follow the newest model of a family.

**Why:** Clients could track new releases without editing their model id.

**Context:** Deferred from the catalog work (models.dev): family grouping is ambiguous across providers (dated snapshots, `-preview`, `-mini`), and an alias that moves changes a client's model silently. Needs an explicit opt-in per key and a `models.changed` webhook first.

**Effort:** M
**Priority:** P3
**Depends on:** models.dev catalog, `models.changed` webhook

## Dashboard

### Dashboard design system

**What:** A `DESIGN.md` with colour, type and spacing tokens for the admin dashboard, and the dashboard moved onto them.

**Why:** The dashboard has no design system; new pages reuse existing classes ad hoc, and labels mix languages.

**Context:** Deferred from the catalog/pricing dashboard work, which reuses the existing classes. Start with `/design-consultation`.

**Effort:** M
**Priority:** P3
**Depends on:** None

### Time-to-first-call measurement

**What:** Measure how long a new operator takes from opening the Providers page to a first successful call through a catalog preset.

**Why:** The catalog work targets about three minutes; nothing measures it.

**Context:** Deferred from the catalog/pricing work. Start with a `/devex-review` run after release, then decide whether to log timings locally.

**Effort:** S
**Priority:** P3
**Depends on:** catalog presets released

### Native adapters for non-OpenAI providers

**What:** Route catalog providers whose models need another API (Anthropic Messages, OpenAI Responses, Bedrock, Azure deployments) through dedicated adapters instead of excluding them.

**Why:** The catalog currently leaves those models out of its presets.

**Context:** Out of scope for the catalog/pricing work; models.dev marks them with a model-level `provider.npm`.

**Effort:** L
**Priority:** P2
**Depends on:** catalog presets released

### Several instances on one data store

**What:** Let more than one Plugboard process share providers, grids, suggestions and usage safely.

**Why:** File locks and in-process caches assume a single worker today.

**Context:** Documented limit (one uvicorn worker). Needs a shared store or leader election for the schedulers.

**Effort:** XL
**Priority:** P3
**Depends on:** none

### Remaining review follow-ups

**What:** Move the locked load/merge/save of the price update and the catalog parse off the event loop (`asyncio.to_thread`); cache negative price lookups across the twelve-month walk-back; share one env-number parser; pin catalog provider traffic (not only the connection test) to the checked address.

**Why:** Informational findings of the pre-landing review and the CSO audit of the auto-update stack.

**Effort:** M
**Priority:** P3
**Depends on:** none
