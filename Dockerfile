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
#   full  api + the MCP server's own virtualenv (/opt/venv-mcp) + the CLIs
#         that the subprocess providers spawn
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

# Debian fixes published after the Python image was built (zlib1g for
# CVE-2026-76844 and CVE-2026-85091 at the time of writing). `upgrade`
# stays on the image's Debian release and only updates installed packages:
# it never installs or removes a package, nor changes the release.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

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

# Provider CLI versions: single source, bump here. Every deployment builds
# these exact releases; no Compose file or deployer passes them again
# (tests/test_cli_versions_single_source.py). When the native host tools
# follow, bump deploy/ovh/install-host-tools.sh in the same change (one
# checksum per release). A one-off local build may still override a version
# with --build-arg.
ARG GEMINI_CLI_VERSION=0.65.0
ARG CLAUDE_CODE_VERSION=2.1.306
ARG CODEX_VERSION=0.163.0

# - git: used by the CLIs and some npm post-install scripts
# - curl, gnupg: fetch the GitHub CLI signing key (written to a file,
#   never piped to a shell)
# - bubblewrap: the cage every agy launch runs in (BUG-86,
#   plugboard/agy_cage.py); without bwrap, agy is not run at all. Its bwrap
#   is moved off PATH, to /usr/libexec/plugboard/bwrap (agy_cage.GATEWAY_BWRAP),
#   INC-4: codex prefers a bwrap found on PATH to the one it ships, and
#   Debian's cannot mount /proc in the container (every codex call failed,
#   15/11). See the check after the CLIs below.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git gnupg bubblewrap \
 && install -d -m 0755 /usr/libexec/plugboard \
 && install -m 0755 -o root -g root /usr/bin/bwrap /usr/libexec/plugboard/bwrap \
 && rm /usr/bin/bwrap \
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
# npm 11 replaces the npm 10.9.x shipped with Node 22, whose bundled
# brace-expansion, picomatch, ip-address, pacote and sigstore carry HIGH
# CVEs (CVE-2026-102276, CVE-2026-102278, CVE-2026-13149, CVE-2026-14257,
# CVE-2026-69152, CVE-2026-33671, CVE-2026-69192, CVE-2026-9496,
# CVE-2026-48815) that no npm 10 release fixes. Major npm bump approved by
# the project owner on 2026-11-12; npm 11 needs Node ^20.17.0 || >=22.9.0.
RUN npm install -g npm@11.21.2 && npm --version

# One step per CLI (separate cache layers). A failed install fails the build
# (BUG-50): an image without one of its CLIs must never be shipped, since
# /health cannot tell that a subprocess provider has no binary.
RUN npm install -g --omit=dev "@google/gemini-cli@${GEMINI_CLI_VERSION}"
RUN npm install -g --omit=dev "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"
RUN npm install -g --omit=dev "@openai/codex@${CODEX_VERSION}"

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

# Every CLI must start; a missing or broken one fails the build (no pipes,
# whose status would hide the failure). agy only ships for amd64; bwrap
# (agy's cage) is checked on every architecture, by its fixed path.
RUN set -e; echo "── Installed CLI binaries ──"; \
    gemini --version; claude --version; codex --version; gh --version; /usr/libexec/plugboard/bwrap --version; \
    if [ "$(dpkg --print-architecture)" = "amd64" ]; then agy --version; fi

# INC-4: no bwrap on the image's PATH, after every install. codex would take
# it instead of the one it ships, and fail to mount /proc in the container.
# An `if`, not `! command -v`: `set -e` ignores a command negated by `!`.
RUN if command -v bwrap; then echo "bwrap is on PATH: codex would use it instead of its own (INC-4)" >&2; exit 1; fi

# MCP server (scripts/run_mcp.py): its own hash-checked environment, apart
# from the gateway's. It opens no port by itself: a client starts it over
# stdio (`docker exec -i <container> /opt/venv-mcp/bin/python
# scripts/run_mcp.py`, see docs/guide/mcp.md) and it calls the gateway with a
# client key. The build fails when the library does not import.
COPY requirements-mcp.lock /tmp/requirements-mcp.lock
RUN python -m venv /opt/venv-mcp \
 && /opt/venv-mcp/bin/pip install --no-cache-dir --require-hashes \
        -r /tmp/requirements-mcp.lock \
 && /opt/venv-mcp/bin/python -c "import mcp, aiohttp; print('mcp ok')" \
 && rm /tmp/requirements-mcp.lock

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
