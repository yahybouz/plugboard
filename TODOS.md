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

**What:** Two checks are left: list the files of the built image in the CI `docker-build` step (no `.pyc`, `.env`, key, tests or docs inside), and check external documentation links (needs network, so not in the test suite).

**Done:** the registry path, override, default and legacy fallback (`tests/test_registry_path.py`); `make init` never overwriting `.env` (`tests/test_make_init.py`); every variable the code reads listed in `docs/guide/configuration.md` (`tests/test_config_docs_coverage.py`, seven names added); the image content read from the `Dockerfile` and `.dockerignore` without Docker (`tests/test_docker_image_content.py`, which found bytecode and key files of copied directories reaching the image, now fixed); `scripts/run_mcp.py` legacy names (`tests/test_run_mcp_legacy_names.py`); relative links and heading anchors of the docs, checked by the test suite and so in CI (`tests/test_docs_links.py`).

**Why:** These behaviours are documented but only checked by hand.

**Context:** Deferred from plan: design-brief.md (Stage 2).

**Effort:** S
**Priority:** P1
**Depends on:** None

### Docker data volume created `0755`

**What:** Make a new Docker volume start `0700`: `chmod 700 /app/data` in the Dockerfile, or let the server tighten a `data/` directory it owns.

**Why:** The image creates `/app/data` with mode `0755` at build time and a new named volume takes that mode, so the `0700` the server gives a data directory it creates does not apply to Docker installs. The files inside are `0600`.

**Context:** Found while closing the public-surface items (see Completed). Needs an owner decision and a check on a real image build. The [move runbook](docs/runbooks/move-install.md) sets `0700` on a restored volume.

**Effort:** S
**Priority:** P2
**Depends on:** None

## Auth

### Per-person dashboard keys

**What:** The admin creates, names, revokes and audits one dashboard key per person, instead of sharing the single `ADMIN_KEY`.

**Why:** Owner decision: a local install logs in without a key, a deployed instance gives each person their own key.

**Context:** Planned for the publishable release. Client API keys (`lle_*`, `POST /admin/keys`) already exist; dashboard sessions are signed with `ADMIN_KEY` only (`_create_admin_session_token`). Needs a session claim naming the key, revocation, and roles if read-only users are wanted.

**Effort:** L
**Priority:** P1
**Depends on:** None

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

### Remaining public-surface and robustness items

**Done:** the ten items, except the bundled guide at `/guide`, dropped (see Context). Provider preset `setup_steps` in English; data directory created `0700` and SQLite stores `0600`; a short or placeholder `ADMIN_KEY` refused outside dev mode; an unreadable `providers.json` logged once instead of on every call; the tiktoken encoding warmed at startup; local Ollama models skipped by the boot internet probe; an SDK status line and an OpenAI JS snippet in `docs/api/README.md`. Then, after the owner's decision: a chat model no provider knows answers `404 model_not_found` with a hint, while any ID a provider may serve keeps its routing (API contract 1.3.0, [details](docs/api/README.md#404-model_not_found)); `PLUGBOARD_DOCS_BASE_URL` turns the `docs/guide/...` references the dashboard gets into links; a [runbook](docs/runbooks/move-install.md) moves a 4.x install to another machine.

**Context:** Deferred from plan: design-brief.md (Stage 2) and the stage 2 ship review. The bundled guide at `/guide` was dropped by the owner's decision: Plugboard serves no guide page, `PLUGBOARD_DOCS_BASE_URL` points to where the repository is published.

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

**What:** Keep the traffic of a provider created from a catalog preset away from non-public addresses after it is created, not only during the connection test (pinned) and the creation check. Then finish the shared number parser: about 35 settings are still read inline with `int(os.environ…)` / `float(os.environ…)` (`http_pool`, `cache`, `provider_watchdog`, `app`, `model_availability`, `session_store`…), where a bad value raises, at import for most, instead of giving the default; and `envcompat.get_float` accepts `inf`, which crashes `PLUGBOARD_LLM_PRICING_MAX_MODELS` and `PLUGBOARD_CATALOG_MAX_MB` and, in `PLUGBOARD_PRICE_DRIFT_PCT`, holds no price change for review.

**Why:** Informational findings of the pre-landing review and the CSO audit of the auto-update stack. A catalog base URL comes from a third-party feed. Chat, streaming and model discovery go through the shared HTTP pool, which resolves the host again (DNS cache 300 s): a name re-pointed inside the network after the check (DNS rebinding) would receive the provider key.

**Context:** Done: the price update's locked read/merge/save, the queueing of held drifts and the catalog parse run in a worker thread (`storage.to_thread_to_the_end`: a scheduler timeout that falls during that work waits for it, then journals and announces the price changes and records the catalog meta before it is raised; it still counts as a failure for the circuit breaker, although the grid was written); misses of the twelve-month price walk-back are cached; the numeric settings of the auto-update stack go through `envcompat.get_int` / `get_float`. Waiting on a product choice: pin each catalog host to the address checked at creation (breaks when the provider moves) or check every new resolution of that host with `llm_pricing.ip_is_public` (keeps address changes working); keep or drop the stop on a bad inline value; refuse `inf` or not; count a timeout that fell during a write that went through as a failure or not. `llm_pricing._resolve_public` and `_PinnedResolver` already exist. The pool trusts `HTTPS_PROXY`, which a DNS check cannot cover.

**Effort:** M
**Priority:** P3
**Depends on:** the pinning, `inf` and write-timeout decisions
