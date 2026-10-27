# syntax=docker/dockerfile:1
# ============================================================
# Plugboard — container images
# ------------------------------------------------------------
# Two targets share the same Python base:
#
#   api   slim image: the gateway only (HTTP providers such as OpenAI-
#         compatible endpoints, Ollama, OpenClaw). No Node, no provider
#         CLIs. This is what docker-compose.yml builds.
#           docker build --target api -t plugboard:api .
#
#   full  api + the CLIs that the subprocess providers spawn
#         (gemini, claude, codex via npm; agy from Google's signed release
#         archive, SHA512-pinned; gh via its apt repository).
#         It is the LAST stage, so a plain `docker build .` (and the
#         Railway config, which names no target) still produces it.
#           docker build --target full -t plugboard:full .
#
# Both run as the non-root user `app` (uid 1000) and keep their state in
# /app/data (mount a volume there). Python dependencies come from the
# hash-checked requirements.lock.
#
# ADMIN_KEY is required at run time: without it the server refuses to
# start (unless PLUGBOARD_DEV=1), so the container exits and never turns
# healthy. Pass it with --env-file .env (`make init` creates one) or your
# platform's secret store. Dashboard auto-login does not apply in a
# container. See docs/guide/deployment.md.
# ============================================================

ARG PYTHON_IMAGE=python:3.12-slim
# Node is copied from the official image instead of running a vendor
# setup script through a shell (no `curl | bash`).
ARG NODE_IMAGE=node:22-slim

# ── base: Python runtime + application code ──────────────────
FROM ${PYTHON_IMAGE} AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --create-home --home-dir /home/app --shell /usr/sbin/nologin --uid 1000 app

WORKDIR /app

# Dependencies first (cache layer), pinned and hash-checked.
COPY requirements.lock ./
RUN pip install --require-hashes -r requirements.lock

# Only what the server needs at runtime: the package, the decision
# manifests it loads, the dashboard, the job scripts it can launch, and
# the entry point.
COPY --chown=app:app plugboard/ plugboard/
COPY --chown=app:app manifests/ manifests/
COPY --chown=app:app static/ static/
COPY --chown=app:app scripts/ scripts/
COPY --chown=app:app run.py VERSION ./

# State (SQLite databases, keys, pricing snapshots) lives in /app/data;
# job logs go to /app/logs. Both must be writable by `app`.
RUN mkdir -p /app/data /app/logs \
 && chown app:app /app/data /app/logs

ENV HOME=/home/app \
    HOST=0.0.0.0 \
    PORT=8765
# Data lives in /app/data (the default). Not set here so that an operator's
# PLUGBOARD_DATA, or a pre-rename LLM_ENABLER_DATA, still applies.

EXPOSE 8765

# python:*-slim ships no curl: probe /health with the standard library.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=5 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8765'), timeout=5)"]

CMD ["python", "run.py"]

# ── api: slim gateway image ──────────────────────────────────
FROM base AS api
USER app

# ── node: source of the Node.js runtime for the full image ───
FROM ${NODE_IMAGE} AS node

# ── full: gateway + provider CLIs ────────────────────────────
FROM base AS full

# Pin the CLI versions at build time if needed, e.g.
#   docker build --target full --build-arg CLAUDE_CODE_VERSION=2.1.0 .
ARG GEMINI_CLI_VERSION=latest
ARG CLAUDE_CODE_VERSION=latest
ARG CODEX_VERSION=latest

# - git: used by the CLIs and some npm post-install scripts
# - curl, gnupg: fetch the GitHub CLI signing key (written to a file,
#   never piped to a shell)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git gnupg \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && node --version && npm --version

# Each CLI is installed in its own step so that one failing package (rate
# limit, rename) does not prevent the others from landing.
RUN npm install -g --omit=dev "@google/gemini-cli@${GEMINI_CLI_VERSION}" || echo "[WARN] gemini-cli install failed"
RUN npm install -g --omit=dev "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" || echo "[WARN] claude-code install failed"
RUN npm install -g --omit=dev "@openai/codex@${CODEX_VERSION}" || echo "[WARN] codex install failed"

# Antigravity CLI (agy): official linux-x64 release archive, pinned and
# SHA512-verified against the vendor release manifest. Same artifact as
# deploy/ovh/install-host-tools.sh. Sign-in (Google subscription) is kept in
# the /home/app volume; agy does not accept an API key.
ARG AGY_URL=https://storage.googleapis.com/antigravity-public/antigravity-cli/1.4.4-4783783166214144/linux-x64/cli_linux_x64.tar.gz
ARG AGY_SHA512=7ce149d350344c1046c7b59b6659bcc5d659f351bf3a07c6c085887d93427ceb89d927752e4ccd0844b53ffb91d33d6a49f72835ceb276c7cffdd13cfb8023b8
RUN if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
        work="$(mktemp -d)" \
     && curl -fsSL --proto '=https' --tlsv1.2 "$AGY_URL" -o "$work/agy.tar.gz" \
     && printf '%s  %s\n' "$AGY_SHA512" "$work/agy.tar.gz" | sha512sum -c - \
     && tar --no-same-owner -xzf "$work/agy.tar.gz" -C "$work" antigravity \
     && install -m 0755 "$work/antigravity" /usr/local/bin/agy \
     && rm -rf "$work"; \
    else echo "[WARN] agy: no official archive for $(dpkg --print-architecture)"; fi

RUN echo "── Installed CLI binaries ──" \
 && (command -v gemini && gemini --version 2>/dev/null | head -1 || echo "gemini: not installed") \
 && (command -v claude && claude --version 2>/dev/null | head -1 || echo "claude: not installed") \
 && (command -v codex && codex --version 2>/dev/null | head -1 || echo "codex: not installed") \
 && (command -v agy && agy --version 2>/dev/null | head -1 || echo "agy: not installed") \
 && (command -v gh && gh --version | head -1 || echo "gh: not installed")

# CLI version probes can create private state as root during the build.
# Keep the persisted runtime HOME writable by the unprivileged app user.
RUN chown -R app:app /home/app

# The CLIs write their OAuth tokens under $HOME (~/.gemini, ~/.codex, ...):
# mount a volume at /home/app to keep them across redeploys.
# Parallel client sessions can spawn many CLI processes at once; a higher
# subprocess limit avoids saturating the CLI pool (truncated JSON replies
# from the Gemini CLI were observed at the default of 4).
ENV PLUGBOARD_SUBPROCESS_LIMIT=10 \
    GEMINI_CLI_TRUST_WORKSPACE=true

USER app
