# Plugboard — developer commands.
#
# Portable: everything runs from the repository root and uses the virtualenv
# in $(VENV) (default .venv). Point it elsewhere with `make test VENV=/path`.
#
#   make setup     create $(VENV) (Python 3.12) and install the dev lock
#   make init      create .env with fresh secrets (never overwrites)
#   make dev       run the server with auto-reload
#   make test      run the test suite against a throwaway data directory
#   make help      list every target

SHELL := /bin/bash

VENV       ?= .venv
MCP_VENV   ?= .venv-mcp
PYTHON     ?= python3.12
PY         := $(VENV)/bin/python
IMAGE      ?= plugboard
# Coverage floor for `make verify` and CI (measured: 84% on 2026-10-17).
COV_MIN    ?= 80
# Extra arguments for pytest, e.g. `make test PYTEST_ARGS="-k cache -x"`.
PYTEST_ARGS ?=
# Restrict the history scan of `make secret-scan`, e.g. GITLEAKS_LOG_OPTS=main..HEAD
GITLEAKS_LOG_OPTS ?=

LOCK_OPTS := --universal --python-version 3.12 --generate-hashes --custom-compile-command "make lock" --quiet

.DEFAULT_GOAL := help

.PHONY: help catalog-check billing-reconcile setup install setup-mcp lock init dev test test-e2e lint format format-check \
        audit check verify secret-scan install-hooks docker-build docker-build-full \
        compose-up compose-down smoke-models admin-mfa-setup \
        mcp mcp-sse mcp-http precommit-install precommit-run live-tests live-tests-mvp

help:          ## List the available targets
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*## "}; {printf "  %-18s %s\n", $$1, $$2}'

# ── Environment ───────────────────────────────────────────────

# A symlinked VENV usually points at an environment shared with other
# projects: installing the lock there would change their packages.
setup:         ## Create the virtualenv VENV (Python 3.12) and install requirements-dev.lock
	@if [ -L "$(VENV)" ] && [ "$(FORCE)" != "1" ]; then \
	  echo "$(VENV) is a symlink (shared environment?): not installing into it."; \
	  echo "Use another VENV=..., remove the link, or rerun with FORCE=1."; exit 1; \
	fi
	@if [ -x "$(PY)" ]; then \
	  echo "Using existing $(VENV)"; \
	elif command -v uv >/dev/null 2>&1; then \
	  uv venv --python 3.12 --seed "$(VENV)"; \
	else \
	  $(PYTHON) -m venv "$(VENV)"; \
	fi
	@"$(PY)" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))' \
	  || echo "WARNING: $(VENV) does not run Python 3.12; the locks target 3.12."
	@if command -v uv >/dev/null 2>&1; then \
	  uv pip install --python "$(PY)" --require-hashes -r requirements-dev.lock; \
	else \
	  "$(PY)" -m pip install --require-hashes -r requirements-dev.lock; \
	fi

install: setup ## Alias of setup (kept for existing scripts)

setup-mcp:     ## Create MCP_VENV for the MCP server and install requirements-mcp.lock
	@if [ -x "$(MCP_VENV)/bin/python" ]; then \
	  echo "Using existing $(MCP_VENV)"; \
	elif command -v uv >/dev/null 2>&1; then \
	  uv venv --python 3.12 --seed "$(MCP_VENV)"; \
	else \
	  $(PYTHON) -m venv "$(MCP_VENV)"; \
	fi
	@if command -v uv >/dev/null 2>&1; then \
	  uv pip install --python "$(MCP_VENV)/bin/python" --require-hashes -r requirements-mcp.lock; \
	else \
	  "$(MCP_VENV)/bin/python" -m pip install --require-hashes -r requirements-mcp.lock; \
	fi

lock:          ## Regenerate the *.lock files from the *.in files (needs uv)
	@command -v uv >/dev/null 2>&1 || { echo "uv is required: https://docs.astral.sh/uv/"; exit 1; }
	uv pip compile $(LOCK_OPTS) requirements.in -o requirements.lock
	uv pip compile $(LOCK_OPTS) requirements-dev.in -o requirements-dev.lock
	uv pip compile $(LOCK_OPTS) requirements-mcp.in -o requirements-mcp.lock

# Creates .env from .env.example with random ADMIN_KEY and API_KEY
# (48 hex characters each) and a Fernet ENCRYPTION_KEY. When
# $${PLUGBOARD_DATA:-data}/.encryption_key already exists, ENCRYPTION_KEY is
# left empty so the existing key keeps decrypting the stored provider tokens.
# Standard library only, so it works before `make setup`.
define INIT_ENV_PY
import base64, os, pathlib, secrets, sys

env = pathlib.Path(".env")
example = pathlib.Path(".env.example")
if env.exists():
    print(".env already exists: left untouched.")
    sys.exit(0)
if not example.exists():
    sys.exit(".env.example not found: run this from the repository root.")

key_file = pathlib.Path(os.environ.get("PLUGBOARD_DATA") or "data") / ".encryption_key"
values = {"ADMIN_KEY": secrets.token_hex(24), "API_KEY": secrets.token_hex(24)}
if key_file.exists():
    print(f"{key_file} exists: ENCRYPTION_KEY left empty, the existing key stays in use.")
else:
    values["ENCRYPTION_KEY"] = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")

comment = chr(35)
lines = []
for line in example.read_text(encoding="utf-8").splitlines():
    name, sep, rest = line.partition("=")
    name = name.strip()
    rest = rest.strip()
    if sep and name in values and (not rest or rest.startswith(comment)):
        line = name + "=" + values.pop(name)
    lines.append(line)
if values:
    sys.exit("Not found in .env.example: " + ", ".join(sorted(values)))

fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    handle.write("\n".join(lines) + "\n")
print("Created .env (mode 600) with fresh secrets. Keep it out of version control.")
endef
export INIT_ENV_PY

init:          ## Create .env from .env.example with fresh secrets (never overwrites)
	@python3 -c "$$INIT_ENV_PY"

# ── Run ───────────────────────────────────────────────────────

dev:           ## Run the server with auto-reload (reads .env)
	RELOAD=true "$(PY)" run.py

catalog-check: ## Show the presets generated from the live models.dev catalog (needs network)
	"$(PY)" scripts/catalog_check.py

billing-reconcile: ## Compare costs with and without provider prices on recorded usage (ARGS=...)
	"$(PY)" scripts/billing_reconcile.py $(ARGS)

smoke-models:  ## Ping every model through /v1/chat/completions (ARGS=...)
	"$(PY)" scripts/smoke_models.py $(ARGS)

admin-mfa-setup: ## Enrol an admin TOTP secret
	"$(PY)" scripts/admin_mfa_setup.py

mcp:           ## Run the MCP server (stdio) from MCP_VENV
	"$(MCP_VENV)/bin/python" scripts/run_mcp.py

mcp-sse:       ## Run the MCP server (SSE, port 8766)
	"$(MCP_VENV)/bin/python" scripts/run_mcp.py --sse

mcp-http:      ## Run the MCP server (streamable HTTP, port 8766)
	"$(MCP_VENV)/bin/python" scripts/run_mcp.py --http

# ── Quality ───────────────────────────────────────────────────

# Every test run gets its own empty data directory and ignores .env, so it
# can never read or overwrite a real installation's state.
test:          ## Run the tests with coverage (temp PLUGBOARD_DATA, .env ignored)
	@data_dir="$$(mktemp -d)"; trap 'rm -rf "$$data_dir"' EXIT; \
	PLUGBOARD_DATA="$$data_dir" PLUGBOARD_SKIP_DOTENV=1 \
	  "$(PY)" -m pytest tests/ -q -p no:cacheprovider \
	  --cov=plugboard --cov-report=term-missing --cov-report=xml $(PYTEST_ARGS)

test-e2e:      ## Browser E2E tests of the dashboard (needs Playwright + Chromium)
	@data_dir="$$(mktemp -d)"; trap 'rm -rf "$$data_dir"' EXIT; \
	PLUGBOARD_DATA="$$data_dir" PLUGBOARD_SKIP_DOTENV=1 PLUGBOARD_RUN_E2E=1 \
	  "$(PY)" -m pytest tests/e2e -v -p no:cacheprovider $(PYTEST_ARGS)

lint:          ## Lint with ruff
	"$(VENV)/bin/ruff" check plugboard/ tests/

format:        ## Format with ruff
	"$(VENV)/bin/ruff" format plugboard/ tests/

format-check:  ## Check formatting without changing files
	"$(VENV)/bin/ruff" format --check plugboard/ tests/

audit:         ## Static security analysis (bandit) and dependency CVEs (pip-audit)
	"$(VENV)/bin/bandit" -r plugboard/ -x tests/ -ll
	"$(VENV)/bin/pip-audit" --strict --progress-spinner off --disable-pip --require-hashes -r requirements-dev.lock

check: lint test ## Lint + tests + bandit
	"$(VENV)/bin/bandit" -r plugboard/ -x tests/ -ll

verify: lint   ## check + coverage floor (COV_MIN) + registry example JSON validation
	@$(MAKE) --no-print-directory test PYTEST_ARGS="--cov-fail-under=$(COV_MIN) $(PYTEST_ARGS)"
	"$(VENV)/bin/bandit" -r plugboard/ -x tests/ -ll
	"$(PY)" -m json.tool plugboard/registry.example.json > /dev/null

# ── Secrets ───────────────────────────────────────────────────

# The working-tree scan covers tracked and untracked files but honours
# .gitignore, so a local .env, data/ or virtualenv is not reported.
secret-scan:   ## gitleaks on the working tree and the git history
	@command -v gitleaks >/dev/null 2>&1 || { echo "gitleaks is required: https://github.com/gitleaks/gitleaks#installing"; exit 1; }
	@tree_dir="$$(mktemp -d)"; trap 'rm -rf "$$tree_dir"' EXIT; \
	git ls-files -z --cached --others --exclude-standard \
	  | while IFS= read -r -d '' f; do [ -f "$$f" ] && printf '%s\0' "$$f"; done \
	  | tar --null -T - -cf - | tar -xf - -C "$$tree_dir"; \
	echo "== gitleaks: working tree =="; \
	(cd "$$tree_dir" && gitleaks dir . --config "$(CURDIR)/.gitleaks.toml" --redact --no-banner) || exit 1; \
	echo "== gitleaks: git history =="; \
	gitleaks git . --config "$(CURDIR)/.gitleaks.toml" --redact --no-banner \
	  $(if $(GITLEAKS_LOG_OPTS),--log-opts="$(GITLEAKS_LOG_OPTS)")

install-hooks: ## Install scripts/pre-push as the git pre-push hook (FORCE=1 to replace)
	@hooks_dir="$$(git rev-parse --git-path hooks)"; target="$$hooks_dir/pre-push"; \
	mkdir -p "$$hooks_dir"; \
	if [ -e "$$target" ] && ! cmp -s scripts/pre-push "$$target" && [ "$(FORCE)" != "1" ]; then \
	  echo "$$target already exists and differs; rerun with FORCE=1 to replace it."; exit 1; \
	fi; \
	install -m 0755 scripts/pre-push "$$target"; \
	echo "Installed $$target"

precommit-install: ## Install the pre-commit hooks (once per clone)
	"$(PY)" -m pip install pre-commit
	"$(VENV)/bin/pre-commit" install --install-hooks
	"$(VENV)/bin/pre-commit" install --hook-type commit-msg

precommit-run: ## Run every pre-commit hook on all files
	"$(VENV)/bin/pre-commit" run --all-files

# ── Containers ────────────────────────────────────────────────

docker-build:  ## Build the slim API image (IMAGE:api)
	docker build --target api -t $(IMAGE):api .

docker-build-full: ## Build the image with the provider CLIs (IMAGE:full)
	docker build --target full -t $(IMAGE):full .

compose-up:    ## Start the API with docker compose (needs .env: make init)
	@test -f .env || { echo ".env is missing: run 'make init' first."; exit 1; }
	@grep -Eq '^ADMIN_KEY=[^[:space:]#]' .env \
	  || { echo "ADMIN_KEY is empty in .env: the server refuses to start without it."; exit 1; }
	docker compose up -d --build

compose-down:  ## Stop the docker compose stack (the data volume is kept)
	docker compose down

# ── Live tests (need a running server and PLUGBOARD_API_KEY) ─

live-tests:    ## Live test framework, D1-D6 on the API surface
	"$(PY)" -m tests.live_model_tests.run_live_tests \
		--surface api --dimensions d1,d2,d3,d4,d5,d6 \
		--base-url $${PLUGBOARD_BASE_URL:-http://localhost:8765}

live-tests-mvp: ## Live test framework, quick subset (D1+D3, 2 canary models)
	"$(PY)" -m tests.live_model_tests.run_live_tests \
		--surface api --dimensions d1,d3 --models claude-haiku-4-5,gpt-5.4 \
		--base-url $${PLUGBOARD_BASE_URL:-http://localhost:8765}
