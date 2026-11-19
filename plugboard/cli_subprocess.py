"""
CLI subprocess executor — fork of OpenClaw's `cli-backend` pattern.

OpenClaw discovered that Gemini / Codex / Claude OAuth cannot be easily
reimplemented over HTTP (proprietary endpoints, Cloud Code Assist
adapters, shifting internals). Instead it **spawns the provider's
installed CLI binary as a subprocess** and parses the JSON/JSONL output.

We do the same here. A provider with `auth_source=subprocess:<kind>`
is served by invoking the local CLI rather than forwarding HTTP.

Supported kinds (match OpenClaw's config):
- subprocess:gemini  → `gemini --output-format json ...`
- subprocess:codex   → `codex exec --json ... -`
- subprocess:claude  → `claude -p --output-format stream-json ...`
- subprocess:agy     → `agy --output-format json ...` (Antigravity CLI)

Every CLI reads the prompt on stdin, never from its argv: an argument is
readable by any local user in the process list, and one over ~128 KiB fails
with "Argument list too long".

The CLI's OAuth state is managed by the CLI itself (e.g. `gemini auth
login` writes ~/.gemini/oauth_creds.json). Plugboard's "Connect"
button in the dashboard just verifies the CLI is installed and
logged in, it doesn't re-run OAuth.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import functools
import json
import logging
import os
import re
import shutil
import signal
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Tuple,
)

from plugboard import agy_cage, agy_egress, agy_store, envcompat, net_cage, web_access
from plugboard.circuit_breaker import (
    is_model_unsupported_by_subscription,
    is_request_too_long,
)
from plugboard import live_requests as _live

log = logging.getLogger(__name__)


class ModelNotServedError(RuntimeError):
    """A model ID a CLI route must not be given (BUG-120): nothing was started.

    Raised by the router for an ID outside the route's catalogue, and by
    ``complete()`` for an ID that would read as a command-line option or
    holds a blank or a control character (``is_malformed_model_id``). The
    request's own error: no other route is tried, nothing is quarantined or
    counted against the route, and the chat routes answer 404
    ``model_not_found``. The message never quotes the ID.
    """

    def __init__(self, provider: str):
        super().__init__(
            f"Model ID refused for provider {provider!r}: not one it serves; "
            "nothing was started"
        )
        self.provider = provider


def is_malformed_model_id(model: str) -> bool:
    """Whether ``model`` cannot be a CLI's ``--model`` value (BUG-120).

    It starts with ``-`` (the CLI would read it as an option), or holds a
    space, a line break or any other blank, control or format character
    (zero-width included). No model name of any CLI has one.
    """
    return model.startswith("-") or any(
        ch.isspace() or not ch.isprintable() for ch in model
    )


class CliSubprocessError(RuntimeError):
    """Raised when a CLI subprocess fails.

    Attributes:
        exit_code: subprocess exit code (None if not started/killed).
        stderr: captured stderr text (truncated to 500 chars in payload).
        pid: subprocess pid (None if not started).
        provider_name: CLI kind (e.g. 'gemini', 'codex', 'claude').
        model_id: model string requested.
        quota: the CLI's own error or its stderr says its quota or rate
            limit is exhausted (decided when raised, never from the model's
            output).
        retry_after: seconds until the CLI said its quota reopens, or None.
        request_too_long: the CLI's own error or its stderr says the messages
            do not fit the model's context (decided like ``quota``, and never
            set with it): the request caused it, no other provider is tried.
        model_unsupported: the CLI's own error or its stderr says the signed-in
            subscription does not serve the model (codex with a ChatGPT
            account, BUG-78; decided like ``request_too_long``, never set with
            ``quota``): no other provider is tried, nothing is quarantined.
        timed_out: the CLI did not finish within its deadline (BUG-67): the
            client gets 503 with Retry-After, as for an HTTP provider's timeout.
        sandbox_failed: agy's cage (bubblewrap, see agy_cage) could not be
            built or could not start, so agy was not run (BUG-86): the client
            gets 503 ``provider_sandbox_unavailable``; the router treats it
            like ``timed_out`` (next route, no quarantine).
        tool_refused: headless agy refused a tool the answer needed (its valid
            JSON names ``denied_actions``, BUG-100): agy works, the request
            asked for what it does not do without a human. Not a provider
            fault (BUG-104): no circuit count, no provider_down alert, no
            error metric, no quarantine; the client's answer is unchanged.
    """

    def __init__(
        self,
        message: str,
        *,
        exit_code: Optional[int] = None,
        stderr: str = "",
        pid: Optional[int] = None,
        provider_name: str = "",
        model_id: str = "",
        quota: bool = False,
        retry_after: Optional[int] = None,
        request_too_long: bool = False,
        model_unsupported: bool = False,
        timed_out: bool = False,
        sandbox_failed: bool = False,
        tool_refused: bool = False,
    ):
        super().__init__(message)
        self.timed_out = timed_out
        self.sandbox_failed = sandbox_failed
        self.tool_refused = tool_refused
        # PLU-158: a web run that spent its turns without answering.
        self.web_exhausted = False
        # BUG-159: the tokens such a run used, as claude reported them
        # (claude_reported_usage), or None; the chat route bills them.
        self.reported_usage: Optional[Dict[str, Any]] = None
        self.exit_code = exit_code
        self.stderr = stderr
        self.pid = pid
        self.provider_name = provider_name
        self.model_id = model_id
        self.quota = quota
        self.retry_after = retry_after
        self.request_too_long = request_too_long
        self.model_unsupported = model_unsupported


_SPAWN_CWD_CACHE: Optional[str] = None


def _clean_spawn_cwd() -> str:
    """Return the private directory CLI subprocesses are spawned from.

    It is the only application-data path a CLI child may read (Codex also
    needs read-only OS runtime files on Linux). It holds nothing of the
    server's: a fresh
    `mkdtemp` directory (mode 0700, unpredictable name) created on first call
    and reused for the process lifetime. A fixed name under a shared /tmp
    could be pre-created or swapped by another local user.

    Being a subdir we own also keeps CLIs out of macOS's Spotlight-owned
    `TemporaryItems` folder (default tempdir), whose restricted perms made
    Gemini-CLI's recursive cwd scan exit non-zero (BUG-2026-05-04).
    """
    global _SPAWN_CWD_CACHE
    if _SPAWN_CWD_CACHE and os.path.isdir(_SPAWN_CWD_CACHE):
        return _SPAWN_CWD_CACHE
    # realpath: macOS tempdirs sit behind the /var -> /private/var symlink,
    # and the codex sandbox matches resolved paths.
    path = os.path.realpath(tempfile.mkdtemp(prefix="plugboard-spawn-"))
    # Removed when the process exits: one directory per process otherwise
    # piles up in /tmp across restarts and test runs.
    atexit.register(shutil.rmtree, path, True)
    _SPAWN_CWD_CACHE = path
    return path


# ── Concurrency limit per backend (TM-001 mitigation) ────────────────────────
# Cap concurrent CLI spawns per kind so a single client can't OOM the machine
# by spamming 100 parallel `gemini` processes. Tunable via env:
#   PLUGBOARD_SUBPROCESS_LIMIT=<int>   (applies to all backends)
_CONCURRENCY_LIMIT = max(1, envcompat.get_int("PLUGBOARD_SUBPROCESS_LIMIT", default=4))
_semaphores: Dict[str, asyncio.Semaphore] = {}


def _get_semaphore(kind: str) -> asyncio.Semaphore:
    """Return (lazily create) the per-kind semaphore.

    Lazy init matters: Semaphore() binds to the current event loop, so we
    cannot create them at import time in a sync context.
    """
    sem = _semaphores.get(kind)
    if sem is None:
        sem = asyncio.Semaphore(_CONCURRENCY_LIMIT)
        _semaphores[kind] = sem
    return sem


# ── Absolute binary path cache (TM-001 mitigation — anti PATH hijack) ────────
# Resolve each CLI's absolute path ONCE via shutil.which() and cache it so
# subsequent spawns don't re-scan the PATH. This prevents an attacker from
# shadowing `gemini` with a malicious binary in a path prepended at runtime
# (e.g. by modifying the uvicorn process's PATH env after boot).
_binary_path_cache: Dict[str, str] = {}


def _resolve_binary(command: str) -> Optional[str]:
    """Return an absolute path to the CLI binary, cached after first hit."""
    cached = _binary_path_cache.get(command)
    if cached and os.path.exists(cached):
        return cached
    resolved = shutil.which(command)
    if resolved:
        _binary_path_cache[command] = resolved
    return resolved


# ── Child environment allowlist (SEC-2026-10 tenant → admin exposure) ────────
# A CLI child runs a prompt written by a tenant (or by content a tenant
# forwards), so it must never see the server's own secrets. Copying
# os.environ handed ADMIN_KEY / API_KEY / ENCRYPTION_KEY to codex, gemini and
# claude, and a prompt could make the CLI print them. The child gets an
# allowlisted environment instead: process basics, locale, proxies/CA bundles,
# and only the auth variables its own CLI reads.
_BASE_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "USER",  # claude reads its macOS Keychain entry by $USER
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LANGUAGE",
        "TZ",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "all_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",  # Linux keyring used by CLI OAuth stores
        # Windows process basics
        "SYSTEMROOT",
        "COMSPEC",
        "PATHEXT",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
    }
)
_BASE_ENV_PREFIXES = ("LC_",)

_PROVIDER_ENV_ALLOWLIST: Dict[str, frozenset] = {
    "codex": frozenset(
        {
            "CODEX_HOME",
            "CODEX_API_KEY",
            "CODEX_ACCESS_TOKEN",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
        }
    ),
    "gemini": frozenset(
        {
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_LOCATION",
            "GOOGLE_GENAI_USE_VERTEXAI",
            "GOOGLE_GENAI_USE_GCA",
            "CLOUDSDK_CONFIG",
        }
    ),
    # agy keeps its Google sign-in under ~/.gemini/antigravity-cli
    # (antigravity-oauth-token): HOME is enough. Each call runs on a private
    # copy of that store, thrown away after it (BUG-157, see agy_store).
    "agy": frozenset(),
    "claude": frozenset(
        {
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CONFIG_DIR",
        }
    ),
}

# Server secrets that never reach a child, even when an operator lists them
# in PLUGBOARD_SUBPROCESS_ENV_PASSTHROUGH by mistake.
_NEVER_FORWARD_ENV = frozenset(
    {"ADMIN_KEY", "API_KEY", "ENCRYPTION_KEY", "OPENCLAW_KEY", "ALERT_WEBHOOK_URL"}
)
# Pre-rename names are copied to PLUGBOARD_* at startup but stay in the
# environment too.
_NEVER_FORWARD_PREFIXES = ("PLUGBOARD_", "LLM_" + "ENABLER_")

# Operator escape hatch for setups the allowlist does not cover (Bedrock or
# Vertex auth for claude, a custom CA variable...): comma-separated names.
_ENV_PASSTHROUGH_VAR = "PLUGBOARD_SUBPROCESS_ENV_PASSTHROUGH"


def _is_never_forwarded(name: str) -> bool:
    return name in _NEVER_FORWARD_ENV or name.startswith(_NEVER_FORWARD_PREFIXES)


def _passthrough_names() -> List[str]:
    raw = os.environ.get(_ENV_PASSTHROUGH_VAR, "")
    names = [n.strip() for n in raw.split(",") if n.strip()]
    refused = [n for n in names if _is_never_forwarded(n)]
    if refused:
        log.warning(
            "%s lists server secrets that are never forwarded to CLI subprocesses: %s",
            _ENV_PASSTHROUGH_VAR,
            ", ".join(refused),
        )
    return [n for n in names if not _is_never_forwarded(n)]


def build_child_env(kind: str) -> Dict[str, str]:
    """Return the environment for a `kind` CLI child, built from an allowlist.

    Nothing from the server environment is inherited unless it is a process
    basic, a locale/proxy/CA variable, an auth variable of that CLI, or an
    operator passthrough. Output-format variables are then set so the CLIs
    keep emitting parseable JSON.
    """
    allowed = _BASE_ENV_ALLOWLIST | _PROVIDER_ENV_ALLOWLIST.get(kind, frozenset())
    allowed = allowed | frozenset(_passthrough_names())
    env = {
        name: value
        for name, value in os.environ.items()
        if (name in allowed or name.startswith(_BASE_ENV_PREFIXES))
        and not _is_never_forwarded(name)
    }
    # Avoid paged output / colours that break JSON parsing
    env["NO_COLOR"] = "1"
    # Gemini CLI v0.41+ writes a
    # "Warning: Basic terminal detected (TERM=dumb). Visual rendering
    # will be limited." line to stdout when TERM=dumb, which corrupts
    # the JSON parser even though the LLM response is otherwise valid.
    # Gemini needs a real terminfo entry. The other CLIs (claude, codex,
    # ollama) keep TERM=dumb to avoid pager / colour escapes in stdout.
    if kind == "gemini":
        env["TERM"] = "xterm-256color"
        # GEMINI-FIX-2026-05-14: declare 24-bit color support so Gemini CLI
        # v0.41+ stops emitting "True color (24-bit) support not detected"
        # warning on stdout — that warning corrupts the JSON parser when the
        # bench runs many sessions concurrently.
        env["COLORTERM"] = "truecolor"
        env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
    else:
        env["TERM"] = "dumb"
    if kind == "agy":
        # agy updates itself by replacing its own executable from a program
        # left in the background: turned off for every launch (BUG-86). The
        # cage holds anyway: binary read-only, background programs end with
        # its pid namespace (agy_cage).
        env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "1"
    return env


# ── Backend configs (mirrors OpenClaw cli-backend definitions) ───────────────


@dataclass
class CliBackend:
    """Declarative config for invoking a provider CLI."""

    kind: str  # "gemini" | "codex" | "claude" | "agy"
    command: str  # binary name (must be on PATH)
    args: List[str] = field(default_factory=list)  # args prefix
    # The prompt always goes to stdin. ``stdin_arg`` is appended last to the
    # argv when the CLI needs an operand to read it (codex: ``-``).
    input_mode: str = "stdin"
    stdin_arg: Optional[str] = None
    output_mode: str = "json"  # "json" (single object) | "jsonl" (line-per-event)
    model_arg: Optional[str] = None  # e.g. "--model" to pass the model name
    default_model: str = ""
    timeout_s: float = 300.0  # wall-clock ceiling per request


CLI_BACKENDS: Dict[str, CliBackend] = {
    "gemini": CliBackend(
        kind="gemini",
        command="gemini",
        # GEMINI-FIX-2026-05-14: --skip-trust avoids the workspace scan that
        # emits "Ripgrep is not available" warnings (Gemini-CLI v0.41+ otherwise
        # scans cwd recursively and pollutes stdout, breaking the JSON parser
        # downstream when bench launches many sessions in parallel).
        #
        # --skip-trust trusts the workspace, which also re-enables the user's
        # MCP servers and extensions (gemini suppresses them in untrusted
        # folders). A tenant prompt must not reach them: `-e none` loads no
        # extension and the MCP allowlist names no real server (SEC-2026-10).
        # The operator's settings, memory and policies are replaced per call,
        # see gemini_private_home(); `default` approval mode keeps the
        # settings' tool lockdown active (it does not apply in `plan` mode).
        args=[
            "--skip-trust",
            "-e",
            "none",
            "--allowed-mcp-server-names",
            "__plugboard_no_mcp__",
            "--approval-mode",
            "default",
            # No --prompt: a non-TTY stdin is the prompt. Never add --sandbox:
            # gemini's sandbox re-launches it with stdin copied into its argv.
            "--output-format",
            "json",
        ],
        input_mode="stdin",
        output_mode="json",
        model_arg="--model",
        default_model="gemini-2.5-flash",
    ),
    "codex": CliBackend(
        kind="codex",
        command="codex",
        args=[
            "exec",
            "--json",
            "--color",
            "never",
            # Least privilege (audit 2026-04-21, SEC-2026-10): the sandbox
            # is a per-call permission profile limited to the spawn cwd, see
            # codex_isolation_args(). The user config.toml (MCP servers,
            # profiles, hooks) and execpolicy rules (which can run commands
            # outside the sandbox) are not loaded, and tenant sessions are
            # not persisted under ~/.codex/sessions.
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "--skip-git-repo-check",
        ],
        input_mode="stdin",
        stdin_arg="-",  # `codex exec ... -` reads the instructions from stdin
        output_mode="jsonl",
        model_arg="--model",
        default_model="gpt-5.5",
    ),
    "claude": CliBackend(
        kind="claude",
        command="claude",
        args=[
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            # Tool list is empty below (--tools "") so no tool-use is
            # ever proposed by the model. The permission mode is a no-op
            # in that state but we pin it to `default` anyway so a
            # misconfigured --tools override cannot silently bypass
            # permission prompts (audit 2026-04-21).
            "--permission-mode",
            "default",
            # Minimal context: the chat window is the ONLY source of
            # context. Skip tools, MCP, settings, slash-commands, and
            # user/project CLAUDE.md. Measured : 145k -> 5.8k prompt
            # tokens, 9.6s -> 2.6s cold call on haiku-4-5.
            # (We keep OAuth auth — `--bare` forces ANTHROPIC_API_KEY
            # and would break users on a Claude subscription.)
            "--tools",
            "",
            "--disable-slash-commands",
            "--strict-mcp-config",
            "--setting-sources",
            "",
        ],
        input_mode="stdin",
        output_mode="jsonl",
        model_arg="--model",
        default_model="claude-sonnet-5-5",
    ),
    "agy": CliBackend(
        kind="agy",
        command="agy",
        # Antigravity CLI in print mode. Headless agy cannot prompt for a tool
        # permission, so every tool call (file read, shell, web) is denied
        # unless the operator's agy settings.json pre-allows it (SEC-2026-10).
        # --sandbox restricts the terminal on top of that, and slash commands /
        # skills are not expanded. Proven in real (UAT, 14/11): reads, commands
        # and writes are refused, except a write inside TMPDIR, hence
        # agy_private_tmp() in complete(). These are agy's own guards; the
        # gateway's is the bubblewrap cage every agy launch runs in (BUG-86,
        # agy_cage). `--mode plan` was removed: agy 1.4.4 says it "has no
        # effect while slash command expansion is disabled".
        # Never add --dangerously-skip-permissions here. No -p: agy reads a
        # piped prompt when -p is omitted.
        args=[
            "--output-format",
            "json",
            "--disable-slash-commands",
            "--sandbox",
        ],
        input_mode="stdin",
        output_mode="json",
        model_arg="--model",
        default_model="gemini-3.8-flash-medium",
    ),
}


# ── Codex read isolation (SEC-2026-10) ───────────────────────────────────────
# `codex exec --sandbox read-only` lets model-run commands read the WHOLE
# filesystem (its read-only policy is `":root" = "read"`), so a tenant prompt
# could `cat` the server's .env. Codex gets a permission profile instead:
# `":root" = "deny"` plus access to the spawn cwd only. The `--sandbox` flag
# and a permission profile cannot be combined, so the profile carries the
# mode chosen by PLUGBOARD_CODEX_SANDBOX (read-only by default;
# workspace-write for agentic flows that need to write in the spawn cwd).
_CODEX_PROFILE = "plugboard_chat"
_CODEX_SANDBOX_ACCESS = {"read-only": "read", "workspace-write": "write"}
# Features that reach beyond the chat turn: ChatGPT connectors, plugins (they
# bring their own MCP servers), hooks, browser/computer control, sub-agents.
# `-c features.<name>=false` is ignored by codex versions without that flag,
# whereas `--disable <name>` would abort the call.
_CODEX_DISABLED_FEATURES = (
    "apps",
    "plugins",
    "remote_plugin",
    "hooks",
    "computer_use",
    "browser_use",
    "browser_use_external",
    "in_app_browser",
    "multi_agent",
    "skill_mcp_dependency_install",
    "memories",
)
_codex_danger_warned = False


def _toml_string(value: str) -> str:
    """Quote `value` as a TOML basic string (JSON escapes are valid TOML)."""
    return json.dumps(value, ensure_ascii=False)


def codex_isolation_args(spawn_cwd: str) -> List[str]:
    """Return the `codex exec` options confining the child to `spawn_cwd`."""
    global _codex_danger_warned
    mode = os.environ.get("PLUGBOARD_CODEX_SANDBOX", "").strip() or "read-only"
    if mode == "danger-full-access":
        if not _codex_danger_warned:
            log.warning(
                "PLUGBOARD_CODEX_SANDBOX=danger-full-access: codex runs tenant "
                "prompts without any sandbox and can read the server's files"
            )
            _codex_danger_warned = True
        return ["--sandbox", "danger-full-access"]
    access = _CODEX_SANDBOX_ACCESS.get(mode)
    if access is None:
        log.warning(
            "PLUGBOARD_CODEX_SANDBOX=%r is not a supported mode; using read-only",
            mode,
        )
        access = "read"
    root = _toml_string(os.path.realpath(spawn_cwd))
    # Bubblewrap starts from an empty filesystem when :root is denied.
    # Linux needs Codex's built-in runtime roots (binaries, loader and libs)
    # to execute its own fs helper. User homes and application data stay
    # hidden; mask /etc again because :minimal otherwise includes it.
    runtime = (
        '":minimal"="read","/etc"="deny",' if sys.platform.startswith("linux") else ""
    )
    filesystem = f'{{":root"="deny",{runtime}{root}={_toml_string(access)}}}'
    args = [
        "-c",
        f"default_permissions={_toml_string(_CODEX_PROFILE)}",
        "-c",
        f"permissions.{_CODEX_PROFILE}.filesystem={filesystem}",
    ]
    for feature in _CODEX_DISABLED_FEATURES:
        args += ["-c", f"features.{feature}=false"]
    return args


# ── Gemini settings isolation (SEC-2026-10) ──────────────────────────────────
# With --skip-trust gemini honours every settings layer: the operator's
# ~/.gemini/settings.json and the system files can pre-approve tools
# (tools.allowed, tools.core), widen the workspace (context.includeDirectories),
# add MCP servers, hooks and skills; ~/.gemini/GEMINI.md is injected as memory
# and ~/.gemini/policies, ~/.agents/skills are loaded too. Each call therefore
# gets its own GEMINI_CLI_HOME: it holds only the settings below and links to
# the operator's OAuth token and account cache, and both system settings paths
# point inside it.
# A chat completion needs no tool, so all of them are refused, twice: the
# settings' empty core-tool allowlist (user tier) and a deny-all admin policy
# (highest tier, which a stray allow rule cannot outrank).
# oauth_creds.json is the token; google_accounts.json caches the account so
# each call does not fetch it again from Google's userinfo endpoint.
_GEMINI_LINKED_FILES = ("oauth_creds.json", "google_accounts.json")
_GEMINI_DENY_ALL_POLICY = """\
# LLM Enabler: tenant chats run without any gemini tool (SEC-2026-10).
[[rule]]
toolName = "*"
decision = "deny"
priority = 999
denyMessage = "Tools are disabled for LLM Enabler chat completions."
"""


def _operator_gemini_dir() -> Path:
    """Return the `.gemini` directory of the account running the server."""
    root = os.environ.get("GEMINI_CLI_HOME") or str(Path.home())
    return Path(root) / ".gemini"


def _operator_gemini_auth_type() -> Optional[str]:
    """Return the auth method chosen in the operator's settings, if any.

    It is the only operator setting carried over: without it gemini, in
    headless mode, refuses to run unless an auth variable is set.
    """
    try:
        raw = json.loads((_operator_gemini_dir() / "settings.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    security = raw.get("security")
    auth = security.get("auth") if isinstance(security, dict) else None
    selected = auth.get("selectedType") if isinstance(auth, dict) else None
    return selected if isinstance(selected, str) and selected else None


def gemini_lockdown_settings() -> Dict[str, Any]:
    """Settings every tenant gemini call runs with (user and system layers)."""
    settings: Dict[str, Any] = {
        "tools": {"core": []},
        "mcpServers": {},
        "hooksConfig": {"enabled": False},
        "skills": {"enabled": False},
    }
    auth_type = _operator_gemini_auth_type()
    if auth_type:
        settings["security"] = {"auth": {"selectedType": auth_type}}
    return settings


def prepare_gemini_home() -> str:
    """Create a private GEMINI_CLI_HOME for one gemini call and return it.

    It sits outside the spawn cwd (the gemini workspace), so the OAuth link it
    holds is not a workspace file. gemini follows the link to read the token
    and to write a refreshed one back.
    """
    home = Path(os.path.realpath(tempfile.mkdtemp(prefix="plugboard-gemini-home-")))
    try:
        settings = json.dumps(gemini_lockdown_settings(), indent=2)
        gemini_dir = home / ".gemini"
        gemini_dir.mkdir(mode=0o700)
        (gemini_dir / "settings.json").write_text(settings)
        (home / "system-settings.json").write_text(settings)
        (home / "system-defaults.json").write_text("{}")
        (home / "deny-all-tools.toml").write_text(_GEMINI_DENY_ALL_POLICY)
        for name in _GEMINI_LINKED_FILES:
            source = _operator_gemini_dir() / name
            if source.is_file():
                (gemini_dir / name).symlink_to(source.absolute())
    except BaseException:
        shutil.rmtree(home, ignore_errors=True)
        raise
    return str(home)


@contextlib.contextmanager
def gemini_private_home() -> Iterator[str]:
    """Yield a fresh private gemini home and remove it afterwards.

    rmtree unlinks the symlinks without touching the operator's files.
    """
    home = prepare_gemini_home()
    try:
        yield home
    finally:
        shutil.rmtree(home, ignore_errors=True)


@contextlib.contextmanager
def agy_private_tmp(spawn_cwd: str) -> Iterator[str]:
    """Yield the ``TMPDIR`` of one agy request and remove it afterwards.

    agy 1.4.4 without a terminal refuses every read, every command and every
    write, except a write inside ``TMPDIR``, which it does without asking
    (real run on the UAT, 14/11). With none set it took ``/tmp``, shared with
    the launch directories of the other requests: it created files there and
    overwrote one in another private directory. Each request therefore gets
    its own: a fresh directory under the launch directory of the CLIs
    (``mkdtemp``, mode 0700), holding nothing of the server or of another
    request, gone when the request ends. This bounds the writes a prompt can
    make through agy's file tool; it is not a sandbox. agy keeps its own
    record of each conversation in its store, the call's private one
    (agy_store, BUG-157), gone with the request too.
    """
    path = tempfile.mkdtemp(prefix="agy-tmp-", dir=spawn_cwd)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def agy_sandbox_error(cause: str, model: str = "") -> CliSubprocessError:
    """The error of an agy call whose cage could not start (agy not run).

    ``cause`` (bwrap missing, a link on the store path, bwrap's first error
    line...) is for the gateway log and the admin only.
    """
    return CliSubprocessError(
        f"agy sandbox (bubblewrap) could not start, agy was not run: {cause}",
        stderr=cause[:500],
        provider_name="agy",
        model_id=model,
        sandbox_failed=True,
    )


# The threads that copy and remove agy's private stores (BUG-157): off the
# event loop, and off asyncio's default executor (the billing writes there).
_store_threads: Optional[ThreadPoolExecutor] = None


async def _off_loop(function: Callable[..., Any], *args: Any) -> Any:
    """``function(*args)`` in one of the store threads, awaited."""
    global _store_threads
    if _store_threads is None:
        _store_threads = ThreadPoolExecutor(
            max_workers=max(2, _CONCURRENCY_LIMIT), thread_name_prefix="agy-store"
        )
    return await asyncio.get_running_loop().run_in_executor(
        _store_threads, functools.partial(function, *args)
    )


@contextlib.asynccontextmanager
async def agy_caged(
    binary: str,
    args: List[str],
    spawn_cwd: str,
    slot: Optional[contextlib.AbstractAsyncContextManager] = None,
) -> AsyncIterator[tuple]:
    """Yield ``(argv, env)`` running agy ``args`` in its cage, with a private
    ``TMPDIR``, a private store (BUG-157) and the cage's egress, all removed
    afterwards (keep the context open until the child is dead and reaped).

    The cage has no network (BUG-156): its only way out is the unix socket
    served here, in the TMPDIR, by agy's proxy (agy_egress), which agy
    reaches through the relay its proxy variables name. agy's store is a
    folder of this call only, beside the TMPDIR (agy_store.private_store),
    which is filled with copies of the token, ``bin/`` and ``builtin/``
    (agy_cage.fill_private_store) and shown at the store's path: what agy
    writes there never reaches the shared store. The copy and the removal
    run in a thread of their own (they can take a while: a large ``bin/``),
    never on the event loop. ``slot``, when given (the CLI slot of
    ``complete()``), is taken once the cage's checks have passed and before
    the copy, and held until the context ends: a call still waiting for its
    slot has an empty store, nothing copied.

    Raises :class:`agy_cage.CageUnavailable` before anything is launched when
    the cage, its store or its egress cannot be built.
    """
    env = build_child_env("agy")
    # Every connection through the cage's relay, never an inherited proxy.
    env.update(agy_cage.proxy_env())
    # Resolved: the cage binds the TMPDIR and the store by their real path.
    spawn = os.path.realpath(spawn_cwd)
    # Left in reverse order: the slot, the egress, the store, the TMPDIR.
    async with contextlib.AsyncExitStack() as stack:
        tmpdir = stack.enter_context(agy_private_tmp(spawn))
        try:
            store = agy_store.create_private_store(spawn)
        except OSError as exc:
            raise agy_cage.CageUnavailable(
                f"agy private store cannot be created ({type(exc).__name__})"
            ) from None
        stack.push_async_callback(_off_loop, agy_store.remove_private_store, store)
        env["TMPDIR"] = tmpdir
        try:
            egress = await agy_egress.start(
                os.path.join(tmpdir, agy_cage.EGRESS_SOCKET)
            )
        except OSError as exc:
            raise agy_cage.CageUnavailable(
                f"agy egress socket cannot be created ({type(exc).__name__})"
            ) from None
        stack.push_async_callback(egress.close)
        argv = agy_cage.cage_argv(
            binary, args, env=env, tmpdir=tmpdir, private_store=store
        )
        if slot is not None:
            await stack.enter_async_context(slot)
        await _off_loop(agy_cage.fill_private_store, env, store)
        yield argv, env


def web_sandbox_error(cause: str, model: str = "") -> CliSubprocessError:
    """The error of a claude web run whose network cage could not start
    (claude not run): ``503 provider_sandbox_unavailable``, as for agy.

    ``cause`` is for the gateway log and the admin only.
    """
    return CliSubprocessError(
        f"claude web sandbox (bubblewrap) could not start, claude was not run: {cause}",
        stderr=cause[:500],
        provider_name="claude",
        model_id=model,
        sandbox_failed=True,
    )


@contextlib.asynccontextmanager
async def web_caged(
    command: List[str], spawn_cwd: str, env: Mapping[str, str]
) -> AsyncIterator[List[str]]:
    """Yield the command line running a claude web run ``command`` (claude
    and its arguments, unchanged) in its network cage (PLU-162), with an
    egress of its own; keep the context open until the child is dead and
    reaped.

    The run's folder (``web-run-*``, 0700, in the launch directory) holds
    only the egress socket, served here by the run's own proxy
    (``net_cage.start_web_egress``: port 443 of public addresses, its own
    connection cap), and is all the cage shows of the launch directory (the
    other calls' folders and sockets hidden, PLU-162); the cage has no other network, its relay on the cage's
    loopback is what ``HTTPS_PROXY`` names. Socket, proxy and folder are
    removed afterwards. Raises :class:`net_cage.CageUnavailable` before
    anything is launched: no bwrap, no socket, no relay, no web run.
    """
    spawn = os.path.realpath(spawn_cwd)
    async with contextlib.AsyncExitStack() as stack:
        bwrap = agy_cage._bwrap()
        folder = tempfile.mkdtemp(prefix="web-run-", dir=spawn)
        stack.callback(shutil.rmtree, folder, True)
        try:
            egress = await net_cage.start_web_egress(
                os.path.join(folder, net_cage.EGRESS_SOCKET)
            )
        except OSError as exc:
            raise net_cage.CageUnavailable(
                f"web egress socket cannot be created ({type(exc).__name__})"
            ) from None
        stack.push_async_callback(egress.close)
        yield net_cage.web_cage_argv(
            bwrap, command, env=env, cwd=spawn_cwd, folder=folder
        )


def gemini_isolation_env(home: str) -> Dict[str, str]:
    """Environment that makes gemini read its settings from `home` only."""
    return {
        "GEMINI_CLI_HOME": home,
        "GEMINI_CLI_SYSTEM_SETTINGS_PATH": os.path.join(home, "system-settings.json"),
        "GEMINI_CLI_SYSTEM_DEFAULTS_PATH": os.path.join(home, "system-defaults.json"),
    }


def gemini_isolation_args(home: str) -> List[str]:
    """Return the gemini options refusing every tool via an admin policy.

    gemini ignores --admin-policy when the host already defines system
    policies (root-owned); the settings' core-tool lockdown still applies.
    """
    return ["--admin-policy", os.path.join(home, "deny-all-tools.toml")]


def parse_agy_models(stdout_text: str) -> List[str]:
    """Return the model IDs listed by `agy models` (one `id<TAB>name` per line)."""
    ids: List[str] = []
    for line in (stdout_text or "").splitlines():
        line = line.strip()
        if not line or "\t" not in line:
            continue
        model_id = line.split("\t", 1)[0].strip()
        if model_id and " " not in model_id and model_id not in ids:
            ids.append(model_id)
    return ids


# Stopping a CLI: how long SIGTERM gets before SIGKILL, then how long SIGKILL
# gets before the caller gives up on the process.
_KILL_GRACE_S = 1.0
_KILL_REAP_S = 1.0


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


async def _reaped_within(proc: asyncio.subprocess.Process, timeout_s: float) -> bool:
    try:
        await asyncio.wait_for(proc.wait(), timeout_s)
    except asyncio.TimeoutError:
        return False
    return True


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """Stop the CLI and its descendants (node, sandbox helpers), then reap it.

    Every CLI is started in its own session, so its pid is the group id.
    SIGTERM first, then SIGKILL for whatever is left of the group, even when
    this wait is cancelled. Process.wait() also waits for the CLI's pipes to
    close, so each wait is bounded: a CLI stuck in the kernel (state D), or a
    descendant that left the group and holds the pipes, is logged and left
    behind instead of holding the caller forever.
    """
    _signal_group(proc.pid, signal.SIGTERM)
    try:
        reaped = await _reaped_within(proc, _KILL_GRACE_S)
    finally:
        # Also after a clean exit: it ends descendants that ignored SIGTERM.
        _signal_group(proc.pid, signal.SIGKILL)
    if reaped or await _reaped_within(proc, _KILL_REAP_S):
        return
    log.warning(
        "CLI pid %d not reaped %.0fs after SIGKILL (stuck, or a process outside "
        "its group holds its output); giving up on it",
        proc.pid,
        _KILL_REAP_S,
    )


async def list_agy_models(timeout_s: float = 30.0) -> List[str]:
    """Ask the Antigravity CLI which models the signed-in account can use.

    Returns an empty list when agy is missing, fails, times out or its cage
    cannot start (agy is then not run): the caller then falls back to the
    preset baseline.
    """
    binary = _resolve_binary("agy")
    if binary is None:
        return []
    spawn_cwd = _clean_spawn_cwd()
    try:
        async with agy_caged(binary, ["models"], spawn_cwd) as (argv, env):
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=spawn_cwd,
                start_new_session=True,
            )
            finished = False
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout_s)
                finished = True
            except asyncio.TimeoutError:
                log.warning("agy models timed out after %.0fs", timeout_s)
                return []
            finally:
                if not finished:
                    await _kill_group(proc)
    except agy_cage.CageUnavailable as exc:
        log.warning(
            "agy models not run: its sandbox (bubblewrap) cannot start: %s", exc
        )
        return []
    except Exception as exc:  # noqa: BLE001
        log.warning("agy models failed: %s", exc)
        return []
    if proc.returncode != 0:
        failure = agy_cage.setup_failure(proc.returncode, stdout, stderr)
        if failure:
            log.warning("agy models not run: its sandbox could not start: %s", failure)
        else:
            log.warning("agy models exited %s", proc.returncode)
        return []
    return parse_agy_models((stdout or b"").decode("utf-8", "replace"))


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"))


def codex_models_cache_path() -> Path:
    """Where the codex CLI caches the model list of the signed-in account."""
    return _codex_home() / "models_cache.json"


# Models codex lists in its cache (``models_cache.json`` has no field for
# it) but refuses on a ChatGPT sign-in: "The '<model>' model is not supported
# when using Codex with a ChatGPT account". Found on 11/11 (BUG-78); with an
# API key, codex serves them.
CHATGPT_REFUSED_CODEX_MODELS = frozenset(
    {"gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-sol", "gpt-6-astra"}
)


# (auth.json path, mtime, size) -> sign-in read from it: the routing asks on
# every chat request, the file is read again only when it changes.
_CODEX_SIGN_IN_SEEN: Optional[Tuple[Tuple[str, int, int], bool]] = None


def codex_uses_chatgpt_sign_in() -> bool:
    """Whether codex runs on a ChatGPT sign-in (a subscription), not an API key.

    ``CODEX_API_KEY`` in the gateway's environment (passed to codex) means an
    API key. Otherwise codex's ``auth.json`` says: ``auth_mode`` ``chatgpt``,
    or, in a file without ``auth_mode``, sign-in tokens and no API key.
    Signed out, unreadable or no home directory: False (nothing is filtered
    on a guess). Only ``auth_mode`` and whether ``tokens`` and
    ``OPENAI_API_KEY`` are set are looked at; nothing of the file is logged
    or returned.
    """
    global _CODEX_SIGN_IN_SEEN
    if os.environ.get("CODEX_API_KEY", "").strip():
        return False
    try:
        path = _codex_home() / "auth.json"
        st = path.stat()
    except (OSError, RuntimeError):
        return False
    key = (str(path), st.st_mtime_ns, st.st_size)
    if _CODEX_SIGN_IN_SEEN is not None and _CODEX_SIGN_IN_SEEN[0] == key:
        return _CODEX_SIGN_IN_SEEN[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    chatgpt = False
    if isinstance(data, dict):
        mode = str(data.get("auth_mode") or "").strip().lower()
        if mode:
            chatgpt = mode.startswith("chatgpt")
        else:
            chatgpt = bool(data.get("tokens")) and not data.get("OPENAI_API_KEY")
    _CODEX_SIGN_IN_SEEN = (key, chatgpt)
    return chatgpt


def codex_refused_models() -> frozenset:
    """The models the signed-in codex refuses: none unless on a ChatGPT sign-in."""
    if codex_uses_chatgpt_sign_in():
        return CHATGPT_REFUSED_CODEX_MODELS
    return frozenset()


def list_codex_models(path: Optional[Path] = None) -> List[str]:
    """Model slugs the codex CLI lists for this account (``visibility: list``).

    codex refreshes ``models_cache.json`` itself on each run; the account's
    real catalog (a ChatGPT plan does not accept every API model) is there.
    Returns an empty list when the file is missing or unreadable.
    """
    path = path or codex_models_cache_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("models") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    ranked = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if (
            not isinstance(slug, str)
            or not slug
            or entry.get("visibility", "list") != "list"
        ):
            continue
        priority = entry.get("priority")
        ranked.append((priority if isinstance(priority, (int, float)) else 1e9, slug))
    ranked.sort(key=lambda item: item[0])
    return list(dict.fromkeys(slug for _, slug in ranked))


def get_backend(kind: str) -> CliBackend:
    backend = CLI_BACKENDS.get(kind)
    if backend is None:
        raise ValueError(f"Unknown CLI subprocess kind: {kind}")
    return backend


# ── Installation check ──────────────────────────────────────────────────────


def check_cli_installed(kind: str) -> Dict[str, Any]:
    """Return {ok, binary?, version?, error?}. Used by the dashboard wizard."""
    backend = CLI_BACKENDS.get(kind)
    if backend is None:
        return {"ok": False, "error": f"Unknown CLI kind: {kind}"}
    path = shutil.which(backend.command)
    if not path:
        return {
            "ok": False,
            "error": (f"{backend.command!r} not found on PATH. Install it and retry."),
        }
    return {"ok": True, "binary": path, "kind": kind}


async def probe_cli_version(kind: str, timeout_s: float = 5.0) -> Dict[str, Any]:
    """Spawn `<cli> --version` to confirm the CLI runs. Non-blocking for Connect."""
    backend = CLI_BACKENDS.get(kind)
    if backend is None:
        return {"ok": False, "error": f"Unknown CLI kind: {kind}"}
    binary = shutil.which(backend.command)
    if binary is None:
        return {"ok": False, "error": f"{backend.command} not on PATH"}
    spawn_cwd = _clean_spawn_cwd()
    try:
        async with contextlib.AsyncExitStack() as cleanup:
            argv = [backend.command, "--version"]
            env = build_child_env(kind)
            if kind == "agy":
                # Like every agy launch: in its cage, or not at all (BUG-86).
                try:
                    argv, env = await cleanup.enter_async_context(
                        agy_caged(binary, ["--version"], spawn_cwd)
                    )
                except agy_cage.CageUnavailable as exc:
                    return {
                        "ok": False,
                        "error": f"agy sandbox (bubblewrap) cannot start: {exc}",
                    }
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=spawn_cwd,
                # Own process group: a timeout kills the CLI's descendants too.
                start_new_session=True,
            )
            finished = False
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout_s)
                finished = True
            finally:
                if not finished:
                    await _kill_group(proc)
        if kind == "agy":
            failure = agy_cage.setup_failure(proc.returncode, stdout, stderr)
            if failure:
                return {
                    "ok": False,
                    "error": f"agy sandbox (bubblewrap) could not start: {failure}",
                }
        version = (stdout or b"").decode("utf-8", "replace").strip().splitlines()
        stderr_text = (stderr or b"").decode("utf-8", "replace").strip()
        error = stderr_text or None
        if error and ("Library not loaded" in stderr_text or "dyld:" in stderr_text):
            error += _dyld_hint(stderr_text, backend.command)
        return {
            "ok": proc.returncode == 0,
            "version": version[0] if version else "",
            "error": error,
        }
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"{backend.command} --version timed out"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# Account status commands: no inference, no quota. kind -> argv.
_SIGN_IN_STATUS_ARGV = {
    "claude": ["claude", "auth", "status"],
    "codex": ["codex", "login", "status"],
}


async def _run_status(argv: List[str], kind: str, timeout_s: float):
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=build_child_env(kind),
        cwd=_clean_spawn_cwd(),
        # Own process group: a timeout kills the CLI's descendants too.
        start_new_session=True,
    )
    finished = False
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout_s)
        finished = True
    finally:
        if not finished:
            await _kill_group(proc)
    return (
        proc.returncode,
        (stdout or b"").decode("utf-8", "replace"),
        (stderr or b"").decode("utf-8", "replace"),
    )


async def probe_cli_signed_in(kind: str, timeout_s: float = 10.0) -> Dict[str, Any]:
    """Whether a subscription CLI has an account signed in, without a model call.

    ``{"signed_in": True|False|None, "error": str}``; ``None`` when the CLI
    has no status command (agy) or the check itself failed, so the caller
    keeps its previous verdict instead of reporting a false outage.
    """
    argv = _SIGN_IN_STATUS_ARGV.get(kind)
    if not argv or shutil.which(argv[0]) is None:
        return {"signed_in": None, "error": ""}
    try:
        rc, out, err = await _run_status(argv, kind, timeout_s)
    except Exception as exc:  # noqa: BLE001 - the probe never raises
        return {"signed_in": None, "error": f"{type(exc).__name__}: {exc}"[:200]}
    text = (out + "\n" + err).lower()
    if kind == "claude":
        try:
            signed_in = bool(json.loads(out).get("loggedIn"))
        except (ValueError, AttributeError):
            signed_in = rc == 0 and "not logged in" not in text
    else:
        signed_in = rc == 0 and not any(m in text for m in _SIGNED_OUT_MARKERS)
    if signed_in:
        return {"signed_in": True, "error": ""}
    hint = (
        CODEX_SIGN_IN_HINT
        if kind == "codex"
        else (
            "claude is not signed in for this server account: run `claude` then "
            "`/login` (or `claude auth login`) in a shell of the gateway user."
        )
    )
    return {"signed_in": False, "error": hint}


async def warm_up_cli(kind: str, timeout_s: float = 15.0) -> Dict[str, Any]:
    """Pre-heat a CLI so the first user request is faster.

    Runs `<cli> --version` which:
    - Loads the Node.js runtime (gemini/codex/claude are Node apps).
    - Triggers the CLI to read ~/.gemini/oauth_creds.json (or equivalent).
    - Warms OS-level caches: exec, dylib, DNS resolver for the provider's API host.

    The OS page cache keeps the interpreter warm for subsequent spawns
    even though each call is a fresh process. Measured gain ≈ 300-600ms
    on the next real request (macOS, local SSD). Safe to call on boot.
    """
    t0 = time.perf_counter()
    result = await probe_cli_version(kind, timeout_s=timeout_s)
    result["warmup_elapsed_s"] = round(time.perf_counter() - t0, 3)
    return result


async def warm_up_all_installed(timeout_s: float = 15.0) -> Dict[str, Dict[str, Any]]:
    """Warm up every CLI backend that's actually on PATH. Silent no-op otherwise.

    Returns a dict {kind: warm_up_result} for logging/telemetry.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for kind, backend in CLI_BACKENDS.items():
        if shutil.which(backend.command) is None:
            continue
        try:
            out[kind] = await warm_up_cli(kind, timeout_s=timeout_s)
        except Exception as exc:  # noqa: BLE001
            out[kind] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return out


# ── Prompt serialization (OpenAI messages → single text prompt) ──────────────


def messages_to_prompt(messages: List[Dict[str, Any]]) -> str:
    """Flatten OpenAI chat messages into a single prompt string.

    Each CLI has its own notion of conversation state; we serialise the
    history as tagged turns. This is lossy vs native multi-turn APIs but
    matches what OpenClaw does (each CLI call is stateless — conversation
    memory is handled via --resume/session IDs, not the prompt).
    """
    parts: List[str] = []
    for msg in messages:
        role = str(msg.get("role", "user")).lower()
        content = msg.get("content", "")
        if isinstance(content, list):
            # content can be a list of blocks (text + image); keep text only
            content = "\n".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        content = str(content).strip()
        if not content:
            continue
        if role == "system":
            parts.append(f"[System]\n{content}")
        elif role == "assistant":
            parts.append(f"[Assistant]\n{content}")
        else:
            parts.append(f"[User]\n{content}")
    return "\n\n".join(parts).strip()


# ── Response extraction (CLI output → OpenAI response dict) ──────────────────


def _extract_text_from_gemini_json(payload: Any) -> str:
    """Extract the response text from `gemini --output-format json` output.

    Gemini CLI emits either the candidate structure or a flattened
    {"response": "..."} depending on version. Try common paths.
    """
    if isinstance(payload, dict):
        # Gemini native FIRST: candidates[0].content.parts[*].text
        # Long responses include BOTH a flat "response" key (often a partial
        # summary or first segment) AND the full structured candidates path.
        # Returning the flat key early truncates the response — always prefer
        # the canonical structured path when present.
        candidates = payload.get("candidates")
        if isinstance(candidates, list) and candidates:
            parts = ((candidates[0] or {}).get("content") or {}).get("parts") or []
            texts = [
                p.get("text", "")
                for p in parts
                if isinstance(p, dict) and isinstance(p.get("text"), str)
            ]
            joined = "".join(texts).strip()
            if joined:
                return joined
        # Fallback for older CLI versions: {"response": "text"} / {"text": ...}
        for key in ("response", "text", "output", "message"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val
    if isinstance(payload, str):
        return payload
    return json.dumps(payload)


def _parse_gemini_json_stdout(stdout_text: str) -> Any:
    """Parse Gemini stdout robustly across CLI output variants.

    BENCH-002: some Gemini CLI versions/flags can emit extra lines around the
    JSON payload (warnings, JSONL fragments, fenced blocks). We try strict
    JSON first, then fallback to line/object extraction.
    """
    text = (stdout_text or "").strip()
    if not text:
        raise json.JSONDecodeError("Empty Gemini stdout", stdout_text, 0)

    # Fast-path: valid JSON object/array/string.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try JSON per-line and keep the last parseable object.
    last_obj: Any = None
    for line in text.splitlines():
        line = line.strip().strip("`")
        if not line:
            continue
        try:
            last_obj = json.loads(line)
        except json.JSONDecodeError:
            continue
    if last_obj is not None:
        return last_obj

    # Fallback: extract largest JSON object slice.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start : end + 1]
        return json.loads(candidate)

    raise json.JSONDecodeError("Unable to parse Gemini JSON payload", stdout_text, 0)


def _extract_cli_error(
    stdout_text: str, kind: str, *, terminal_only: bool = False
) -> str:
    """Pull the first actionable error message from a CLI's stdout stream.

    Codex and Claude CLIs emit JSONL events; a non-zero exit is usually
    preceded by `{"type":"error",...}` or `{"type":"turn.failed",...}`
    with a nested JSON payload describing WHY. Surfacing that text gives
    the user something actionable instead of "Reading additional input
    from stdin..." (a harmless stderr warning). ``terminal_only`` (exit 0)
    ignores codex/claude events that do not end the run; agy and gemini
    write one final payload.
    """
    if kind == "agy":
        return _extract_agy_error(stdout_text)
    if kind == "gemini":
        return _extract_gemini_error(stdout_text)
    if kind not in {"codex", "claude"}:
        return ""
    msg = _first_jsonl_error(stdout_text, terminal_only=terminal_only)
    low = msg.lower()
    if msg and any(marker in low for marker in _SIGNED_OUT_MARKERS):
        return CLAUDE_SIGN_IN_HINT if kind == "claude" else CODEX_SIGN_IN_HINT
    return msg


def _extract_gemini_error(stdout_text: str) -> str:
    """Message of the ``{"error": {"type", "message", "code"}}`` object gemini
    writes on stdout with ``--output-format json`` (JsonFormatter.formatError).

    The object must end stdout: CLI log lines may come before it ("Loaded
    cached credentials."), but a line the model wrote among others is not
    the CLI's error (no lenient per-line fallback here).
    """
    lines = stdout_text.strip().splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith("{")), None)
    if start is None:
        return ""
    try:
        payload = json.loads("\n".join(lines[start:]))
    except ValueError:  # no strict JSON, no structured error
        return ""
    error = payload.get("error") if isinstance(payload, dict) else None
    msg = error.get("message") if isinstance(error, dict) else None
    return msg[:400] if isinstance(msg, str) else ""


CLAUDE_SIGN_IN_HINT = (
    "claude is not signed in for this server account: run `claude` once in an "
    "interactive shell of the gateway user and type /login (Claude "
    "subscription), or use the 'Anthropic Direct' provider for API-key access."
)

CODEX_SIGN_IN_HINT = (
    "codex is not signed in for this server account: run `codex login "
    "--device-auth` in a shell of the gateway user (ChatGPT subscription), or "
    "use the 'OpenAI Direct' provider for API-key access."
)

# Lower-case fragments a signed-out claude or codex CLI prints.
_SIGNED_OUT_MARKERS = (
    "not logged in",
    "please run /login",
    "authentication_failed",
    "401 unauthorized",
    "missing bearer",
)

# Lower-case fragments of a CLI that failed on its subscription quota or rate
# limit. Only sourced formats: the repository's tests; gemini/agy (Google
# API 429: "Resource has been exhausted (e.g. check quota).",
# RESOURCE_EXHAUSTED, reason rateLimitExceeded); codex ("You've hit your
# usage limit", "exceeded retry limit, last status: 429"); Claude Code
# ("You've hit your limit", "usage limit reached", "5-hour limit reached",
# "You've reached your weekly limit"); the Anthropic rate_limit_error and
# OpenAI insufficient_quota error types.
_QUOTA_MARKERS = (
    "rate limit exceeded",
    "quota exceeded",
    "resource exhausted",
    "resource has been exhausted",
    "resource_exhausted",
    "ratelimitexceeded",
    "hit your usage limit",
    "exceeded retry limit, last status: 429",
    "you've hit your limit",
    "usage limit reached",
    "5-hour limit reached",
    "reached your weekly limit",
    "rate_limit_error",
    "insufficient_quota",
)
# A full disk ("[Errno 122] Disk quota exceeded") is not a provider quota.
_NOT_QUOTA = "disk quota exceeded"

# Longest delay a quota error may announce; HTTP Retry-After is capped too.
MAX_RETRY_AFTER_S = 7 * 24 * 3600

# codex: "... or try again at 6:00 PM." in the gateway's local time.
_RETRY_TIME = re.compile(r"\btry again at (\d{1,2}):(\d{2})(?:\s?([ap]m))?\b", re.I)


def is_quota_error(message: str) -> bool:
    """True when a CLI error says its quota or rate limit is exhausted."""
    low = (message or "").lower().replace("’", "'").replace(_NOT_QUOTA, "")
    return any(marker in low for marker in _QUOTA_MARKERS)


def _local_now() -> datetime:
    """The gateway's local wall clock: the CLI runs here and prints its time."""
    return datetime.now()


def quota_retry_after(message: str) -> Optional[int]:
    """Seconds until the next "try again at HH:MM [AM|PM]" of a quota error.

    The time has no zone: the CLI prints it in the local time of the machine
    that runs it, the gateway. A time already past means tomorrow.
    """
    match = _RETRY_TIME.search(message or "")
    if not match:
        return None
    hour, minute, half = int(match[1]), int(match[2]), (match[3] or "").lower()
    if half:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if half == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    now = _local_now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return min(int((target - now).total_seconds()), MAX_RETRY_AFTER_S)


def _quota_fields(
    cli_error: str, stderr_text: str, *, rate_limited: bool = False
) -> Dict[str, Any]:
    """quota/retry_after/request_too_long/model_unsupported of a
    CliSubprocessError, never from the model's stdout.

    The CLI's own error decides; stderr is read only when the CLI gave none
    (a retry log there must not override "model not found"). ``rate_limited``
    is claude's structured rate-limit sign (``_claude_rate_limited``). A quota
    is never also a prompt too long.
    """
    reason = cli_error if cli_error else stderr_text
    quota = rate_limited or is_quota_error(reason)
    unsupported = not quota and is_model_unsupported_by_subscription(reason)
    return {
        "quota": quota,
        "retry_after": quota_retry_after(cli_error) if quota else None,
        "request_too_long": not quota
        and not unsupported
        and is_request_too_long(reason),
        "model_unsupported": unsupported,
    }


def _first_jsonl_error(stdout_text: str, *, terminal_only: bool = False) -> str:
    """Error that ended a codex/claude JSONL stream, or ``""``.

    The event that ends the run wins (codex ``turn.failed``, claude
    ``result`` with ``is_error``); otherwise the last ``error`` event, then
    an ``assistant`` event's ``error`` code (``authentication_failed`` when
    signed out). An ``error`` event can be transient ("Reconnecting... 1/5")
    and followed by the real outcome, so the first one never decides.

    ``terminal_only`` keeps only the events that end a run in failure: after
    a transient ``error`` the CLI may still answer normally.
    """
    failed_error = ""
    last_error = ""
    result_error = ""
    assistant_error = ""
    skipped = {"error", "assistant"} if terminal_only else set()
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = str(evt.get("type") or "")
        if etype in skipped:
            continue
        if etype in {"error", "turn.failed"}:
            # Payload shape: `{"error": {"message": "..."}}` or `{"message": "..."}`
            msg = evt.get("message") or (evt.get("error") or {}).get("message") or ""
            if isinstance(msg, str) and msg:
                # Codex nests a JSON string inside `message` — unwrap one layer
                try:
                    nested = json.loads(msg)
                    deep = (nested.get("error") or {}).get("message") or nested.get(
                        "message"
                    )
                    if isinstance(deep, str) and deep:
                        msg = deep
                except (json.JSONDecodeError, AttributeError, TypeError):
                    pass
                if etype == "turn.failed":
                    failed_error = msg[:400]
                else:
                    last_error = msg[:400]
        elif etype == "result" and evt.get("is_error") is True:
            text = evt.get("result")
            if isinstance(text, str) and text and not result_error:
                result_error = text[:400]
        elif etype == "assistant":
            code = evt.get("error")
            if isinstance(code, str) and code and not assistant_error:
                assistant_error = code[:400]
    return failed_error or result_error or last_error or assistant_error


def _jsonl_events(stdout_text: str) -> List[Dict[str, Any]]:
    """The JSON objects of a codex/claude JSONL stream; other lines dropped."""
    events: List[Dict[str, Any]] = []
    for line in stdout_text.splitlines():
        try:
            evt = json.loads(line)
        except ValueError:
            continue
        if isinstance(evt, dict):
            events.append(evt)
    return events


def _ended_in_error(stdout_text: str, kind: str) -> bool:
    """True when the CLI's own output says the run failed, message or not.

    codex ``turn.failed``; claude ``result`` with ``is_error`` or an error
    ``subtype`` (``error_during_execution``..., which has no ``result``
    text); an ``error`` object in gemini's payload. A codex ``error`` event
    alone is not one: a reconnect can be followed by the answer.
    """
    if kind == "gemini":
        try:
            payload = _parse_gemini_json_stdout(stdout_text)
        except ValueError:
            return False
        return isinstance(payload, dict) and bool(payload.get("error"))
    if kind not in {"codex", "claude"}:
        return False
    for evt in _jsonl_events(stdout_text):
        etype = evt.get("type")
        if etype == "turn.failed":
            return True
        if etype == "result" and (
            evt.get("is_error") is True
            or str(evt.get("subtype") or "").startswith("error")
        ):
            return True
    return False


# claude's rate_limit errors that say no usage limit is behind them (claude
# 2.1.321 binary): "Opus (or Fable) is experiencing high load...", also
# matched when an API error carries it, and the server's own throttling.
_CLAUDE_NOT_USAGE_LIMIT = ("experiencing high load", "not your usage limit")
# Prefix of every API error claude reports ("API Error: ...").
_CLAUDE_API_ERROR = "API Error"


def _claude_event_text(evt: Dict[str, Any]) -> str:
    """Text of a claude ``assistant`` event's blocks or of its ``result``."""
    if evt.get("type") == "result":
        text = evt.get("result")
        return text if isinstance(text, str) else ""
    content = (evt.get("message") or {}).get("content")
    if not isinstance(content, list):
        return ""
    return "".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _claude_usage_limit(evt: Dict[str, Any]) -> bool:
    """True when a claude rate-limit event is the account's usage limit."""
    text = _claude_event_text(evt)
    if any(marker in text.lower() for marker in _CLAUDE_NOT_USAGE_LIMIT):
        return False
    status = evt.get("api_error_status")
    if status is not None:
        return status == 429
    return not text or text.startswith(_CLAUDE_API_ERROR)


def _claude_rate_limited(stdout_text: str) -> bool:
    """claude's structured sign of a usage limit: an ``assistant`` event with
    ``error: "rate_limit"`` or a ``result`` with ``api_error_status: 429``,
    unless the CLI says no usage limit is behind it.

    Read in the claude 2.1.321 binary, not seen in a captured run: the CLI
    sets ``error: "rate_limit"`` on an API 429 (usage or spend limit, usage
    credits required, "Request rejected (429)") and gives it
    ``api_error_status: 429`` and an "API Error" text, or a usage-limit
    message. It also sets it on failures that are not the account's usage
    limit: an API 429 it reports as "Server is temporarily limiting requests
    (not your usage limit)", and errors it raises itself, without an HTTP
    status nor the "API Error" prefix: "Opus (or Fable) is experiencing high
    load" and a per-model block (``model_blocked``, free text set by the
    server). Those are a provider failure here (502, next provider), not a
    quota 429. A ``system`` ``api_retry`` event carries ``error:
    "rate_limit"`` too; it announces a retry, not the outcome, and is not
    read.
    """
    for evt in _jsonl_events(stdout_text):
        etype = evt.get("type")
        if etype == "assistant" and evt.get("error") == "rate_limit":
            if _claude_usage_limit(evt):
                return True
        elif etype == "result" and evt.get("api_error_status") == 429:
            if _claude_usage_limit(evt):
                return True
    return False


AGY_SIGN_IN_HINT = (
    "agy is not signed in for this server account: run `agy` once in an "
    "interactive shell of the gateway user and sign in with Google "
    "(Antigravity subscription). agy does not read GEMINI_API_KEY; use the "
    "'Google Gemini Direct' provider for API-key access."
)


def _extract_agy_error(stdout_text: str) -> str:
    """Error carried by agy's ``{"status": "ERROR", "error": ...}`` payload."""
    try:
        payload = _parse_gemini_json_stdout(stdout_text)
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(payload, dict):
        return ""
    if str(payload.get("status", "")).upper() == "SUCCESS":
        return ""
    msg = payload.get("error")
    if not isinstance(msg, str) or not msg:
        return ""
    low = msg.lower()
    if "authentication" in low or "sign in" in low:
        return AGY_SIGN_IN_HINT
    return msg[:400]


# Names of the actions agy says it refused, quoted back only when plain.
_AGY_ACTION_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_AGY_ACTIONS_SHOWN = 8


def _agy_empty_answer(stdout_text: str, command: str) -> Tuple[str, bool]:
    """Why agy's valid payload holds no answer (BUG-100), else ``""``; and
    whether agy refused a tool (``denied_actions``, BUG-104).

    Headless agy cannot ask for a tool permission: it refuses the tool and
    answers ``{"status": "SUCCESS", "response": "", "denied_actions": [...]}``
    (real run, UAT, 14/11). That is valid JSON, not an invalid one. agy's
    output is never quoted: only the refused action names, when plain.
    """
    try:
        payload = _parse_gemini_json_stdout(stdout_text)
    except Exception:  # noqa: BLE001 - then it is invalid JSON indeed
        return "", False
    if not isinstance(payload, dict) or "response" not in payload:
        return "", False
    if str(payload.get("status", "SUCCESS")).upper() != "SUCCESS":
        return "", False
    response = payload.get("response")
    if not isinstance(response, str) or response.strip():
        return "", False
    denied = payload.get("denied_actions")
    if isinstance(denied, list) and denied:
        names: List[str] = []
        for item in denied:
            action = item.get("action") if isinstance(item, dict) else None
            if (
                isinstance(action, str)
                and _AGY_ACTION_RE.fullmatch(action)
                and action not in names
            ):
                names.append(action)
        listed = f" ({', '.join(names[:_AGY_ACTIONS_SHOWN])})" if names else ""
        return (
            f"CLI {command!r} refused the tools its answer needed{listed}: "
            "headless agy cannot ask for a tool permission, so nothing was done "
            "and there is no answer."
        ), True
    return f"CLI {command!r} returned an empty answer and gave no reason.", False


# Key set on complete()'s result when the CLI reported validated token counts.
# The router pops it before the result leaves the process.
USAGE_REPORTED_KEY = "_plugboard_usage_reported"


# No real call reports more: a larger counter is not trusted (and would not
# fit the usage store, whose write would then fail and bill nothing).
MAX_REPORTED_TOKENS = 10**12


def _usage_count(value: Any) -> Optional[int]:
    """A token counter: an int from 0 to MAX_REPORTED_TOKENS (bool, str,
    float, None rejected)."""
    if type(value) is not int or not 0 <= value <= MAX_REPORTED_TOKENS:
        return None
    return value


def agy_reported_usage(payload: Any) -> Optional[Dict[str, Any]]:
    """OpenAI usage from agy's ``usage`` block, or None when it cannot be trusted.

    input/output map to prompt/completion. A ``total_tokens`` that is present
    must equal their sum, otherwise the whole report is rejected. Detail
    counters (cache reads, thinking) are kept as OpenAI sub-counts only when
    valid and within their parent counter; they are never added to the total.
    """
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt = _usage_count(usage.get("input_tokens"))
    completion = _usage_count(usage.get("output_tokens"))
    if prompt is None or completion is None:
        return None
    total = prompt + completion
    if "total_tokens" in usage and _usage_count(usage["total_tokens"]) != total:
        # Counters only: never the payload, the reply or the prompt.
        log.debug(
            "agy usage rejected: total_tokens (%s) differs from input+output "
            "(%d); tokens will be estimated",
            type(usage["total_tokens"]).__name__,
            total,
        )
        return None
    out: Dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }
    cached = _usage_count(usage.get("cache_read_tokens"))
    if cached is not None and cached <= prompt:
        out["prompt_tokens_details"] = {"cached_tokens": cached}
    thinking = _usage_count(usage.get("thinking_tokens"))
    if thinking is not None and thinking <= completion:
        out["completion_tokens_details"] = {"reasoning_tokens": thinking}
    return out


# claude's per-model counters (``modelUsage``) and those of its ``usage``
# block: (prompt counters, completion counter, cache-read counter).
_CLAUDE_MODEL_COUNTERS = (
    ("inputTokens", "cacheCreationInputTokens", "cacheReadInputTokens"),
    "outputTokens",
    "cacheReadInputTokens",
)
_CLAUDE_USAGE_COUNTERS = (
    ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"),
    "output_tokens",
    "cache_read_input_tokens",
)


def _claude_counts(entry: Any, names: tuple, required: bool) -> Optional[tuple]:
    """(prompt, completion, cache reads) of one claude counter block, or None
    when a counter is not a non-negative int (missing ones too, ``required``;
    otherwise a missing cache counter reads 0)."""
    if not isinstance(entry, dict):
        return None
    prompt_names, completion_name, cached_name = names
    optional = () if required else prompt_names[1:]  # the cache counters
    values: Dict[str, int] = {}
    for name in (*prompt_names, completion_name):
        if name in optional and name not in entry:
            values[name] = 0
            continue
        value = _usage_count(entry.get(name))
        if value is None:
            return None
        values[name] = value
    prompt = sum(values[name] for name in prompt_names)
    return prompt, values[completion_name], values[cached_name]


def claude_reported_usage(stdout_text: str) -> Optional[Dict[str, Any]]:
    """OpenAI usage from claude's stream-json ``result`` event (BUG-159), or
    None when there is none or it cannot be trusted.

    ``modelUsage`` has one entry per model the run called, the page-summary
    helper included: claude adds every API call's tokens to its model's
    entry (read in the claude 2.1.328 binary). ``usage`` holds the main
    model's only, so ``modelUsage`` is summed and ``usage`` is read only
    when ``modelUsage`` is absent; never both. prompt = input + cache
    creation + cache reads, completion = output, cache reads also as
    ``prompt_tokens_details.cached_tokens`` (as agy_reported_usage). Any
    counter that is not a non-negative int rejects the whole report.
    """
    evt: Any = None
    for line in reversed(stdout_text.splitlines()):
        line = line.strip()
        if not line.startswith("{") or '"result"' not in line:
            continue
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict) and candidate.get("type") == "result":
            evt = candidate
            break
    if evt is None:
        return None
    if "modelUsage" in evt:
        per_model = evt["modelUsage"]
        if not isinstance(per_model, dict) or not per_model:
            return None
        counts = [
            _claude_counts(entry, _CLAUDE_MODEL_COUNTERS, required=True)
            for entry in per_model.values()
        ]
    else:
        counts = [_claude_counts(evt.get("usage"), _CLAUDE_USAGE_COUNTERS, False)]
    if any(c is None for c in counts):
        return None
    prompt = sum(c[0] for c in counts)
    completion = sum(c[1] for c in counts)
    cached = sum(c[2] for c in counts)
    out: Dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }
    if cached:
        out["prompt_tokens_details"] = {"cached_tokens": cached}
    return out


def _extract_text_from_codex_jsonl(lines: List[str]) -> str:
    """Parse Codex CLI JSONL events and extract the final assistant message.

    Codex CLI 0.121+ emits `{"type":"item.completed","item":{"type":"agent_message","text":"..."}}`.
    Older shapes (`assistant_message`, `response.output_text.delta`) are kept
    for forward/backward compatibility.
    """
    final_parts: List[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = str(evt.get("type") or evt.get("event") or "")
        # Codex ≥0.121 wraps the answer in item.completed
        if etype == "item.completed":
            item = evt.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text", "")
                if isinstance(text, str):
                    final_parts.append(text)
            continue
        # Legacy / alternate event names
        if etype in {
            "assistant_message",
            "agent_message",
            "response.output_text.delta",
        }:
            text = evt.get("text") or evt.get("delta") or ""
            if isinstance(text, str):
                final_parts.append(text)
        elif etype == "message" and evt.get("role") == "assistant":
            content = evt.get("content", "")
            if isinstance(content, str):
                final_parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        final_parts.append(block["text"])
    return "".join(final_parts).strip()


def _extract_text_from_claude_jsonl(lines: List[str]) -> str:
    """Parse Claude CLI `--output-format stream-json` events.

    The answer is the ``text`` blocks (and ``text_delta`` deltas), the same
    text a streamed call sends (BUG-92): ``thinking`` is not part of it. An
    answer made of ``thinking`` only is returned rather than an empty one
    (BENCH-001); without any block, the ``result`` text.
    """
    texts: List[str] = []
    thoughts: List[str] = []
    result_text = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = str(evt.get("type") or "")
        if etype == "assistant":
            msg = evt.get("message", {})
            for block in msg.get("content", []) or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "")
                elif btype == "thinking":
                    text = block.get("text") or block.get("thinking", "")
                else:
                    continue
                if isinstance(text, str):
                    (texts if btype == "text" else thoughts).append(text)
        elif etype == "content_block_delta":
            delta = evt.get("delta", {})
            dtype = delta.get("type")
            if dtype == "text_delta":
                texts.append(delta.get("text", ""))
            elif dtype == "thinking_delta":
                thoughts.append(delta.get("text") or delta.get("thinking", ""))
        elif etype == "result":
            # Fallback: some CLI versions surface the final text here only.
            text = evt.get("result", "")
            if isinstance(text, str) and text and not result_text:
                result_text = text
    for parts in (texts, thoughts):
        answer = "".join(p for p in parts if isinstance(p, str)).strip()
        if answer:
            return answer
    return result_text.strip()


def _try_parse_stdout_text(
    stdout_text: str,
    backend: "CliBackend",
    kind: str,
) -> Optional[str]:
    """Attempt to extract assistant text from a CLI stdout blob.

    Returns the extracted text string when parsing succeeds and the text is
    non-empty.  Returns ``None`` when parsing fails or the result is empty,
    so the caller can decide whether to recover or raise.

    W004 (2026-05-13): Introduced to support Gemini CLI non-zero exit
    recovery — the same logic is reused in the normal (exit=0) path so
    there is no duplication.

    Args:
        stdout_text: Raw stdout captured from the CLI process.
        backend: The ``CliBackend`` config that describes output format.
        kind: CLI kind string (``"gemini"`` | ``"codex"`` | ``"claude"``).

    Returns:
        Extracted assistant text, or ``None`` if not parseable / empty.
    """
    try:
        if backend.output_mode == "json":
            payload = _parse_gemini_json_stdout(stdout_text)
            # W004-followup (2026-05-13): reject Gemini error payloads.
            # `_extract_text_from_gemini_json` falls back to json.dumps when no
            # candidates/text field is present, which would surface error blobs
            # like {"error": {"code": 429}} as fake LLM responses. We reject
            # payloads that carry an "error" key without any valid response
            # content so the caller raises CliSubprocessError as expected.
            # agy reports {"status": "SUCCESS" | ..., "response": "..."}.
            if kind == "agy":
                # Only the "response" field is model text. An empty one (e.g.
                # a denied tool call) must not fall back to dumping the JSON.
                if not isinstance(payload, dict):
                    return None
                if str(payload.get("status", "SUCCESS")).upper() != "SUCCESS":
                    return None
                response = payload.get("response")
                return (
                    response if isinstance(response, str) and response.strip() else None
                )
            if isinstance(payload, dict) and "error" in payload:
                candidates = payload.get("candidates")
                has_text = bool(
                    payload.get("text")
                    or payload.get("response")
                    or payload.get("output")
                    or payload.get("message")
                )
                if not candidates and not has_text:
                    return None
            text = _extract_text_from_gemini_json(payload)
        elif backend.output_mode == "jsonl":
            lines = stdout_text.splitlines()
            if kind == "codex":
                text = _extract_text_from_codex_jsonl(lines)
            else:  # claude
                text = _extract_text_from_claude_jsonl(lines)
        else:
            return None
    except (KeyError, ValueError):
        return None
    return text if text else None


def _dyld_hint(stderr_text: str, command: str) -> str:
    """Return a remediation hint if stderr contains a dyld library-loading error.

    macOS Homebrew upgrades can remove old `.dylib` versions (e.g. simdutf
    33→34) while Node-based CLIs still link against them. The resulting
    `dyld: Library not loaded` is cryptic — surface a actionable hint.
    """
    if "Library not loaded" in stderr_text or "dyld:" in stderr_text:
        return (
            f" — dyld library error: try `brew reinstall {command}` "
            f"or `brew upgrade` to fix outdated native dependencies."
        )
    return ""


# ── Incremental output (INC-2) ──────────────────────────────────────────────
# Which CLIs can hand the answer over while they run, and how to read it:
# claude (``--include-partial-messages``: ``stream_event`` lines carrying
# ``text_delta``) and agy (``--output-format stream-json``: ``step_update``
# events of the ``agent_response`` step carrying ``text_delta``); both shapes
# captured on the UAT (claude 2.1.306, agy 1.4.4). codex ``exec --json`` only
# reports whole messages and gemini is not installed on the servers: those
# keep the single answer at the end.
STREAM_EXTRA_ARGS: Dict[str, List[str]] = {"claude": ["--include-partial-messages"]}


def _stream_args(kind: str, args: List[str]) -> List[str]:
    """``args`` of a CLI whose answer is read as it comes."""
    out = list(args)
    if kind == "agy" and "--output-format" in out:
        out[out.index("--output-format") + 1] = "stream-json"
    return [*out, *STREAM_EXTRA_ARGS.get(kind, [])]


def _agy_stream_to_json(stdout_text: str) -> str:
    """agy's ``stream-json`` run as its ``json`` payload: the object of the last
    ``result`` event (status, response, usage, error). Unchanged without one,
    so the usual failure paths (exit code, stderr) apply."""
    for line in reversed(stdout_text.splitlines()):
        if '"result"' not in line:
            continue
        try:
            evt = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(evt, dict)
            and evt.get("event") == "result"
            and isinstance(evt.get("result"), dict)
        ):
            return json.dumps(evt["result"])
    # Events but no result: the run was cut. Their text (cwd, tools, partial
    # answer) is not an error message; the exit code and stderr say why.
    if '"event"' in stdout_text:
        return ""
    return stdout_text


def _agy_text_delta(line: bytes) -> str:
    """Text of an agy ``agent_response`` ``step_update`` line, else ``""``."""
    if b"text_delta" not in line:
        return ""
    try:
        evt = json.loads(line)
    except ValueError:
        return ""
    step = evt.get("step_update") if isinstance(evt, dict) else None
    if (
        isinstance(step, dict)
        and evt.get("event") == "step_update"
        and step.get("step_type") == "agent_response"
    ):
        text = step.get("text_delta")
        return text if isinstance(text, str) else ""
    return ""


def _claude_text_delta(line: bytes) -> str:
    """Text of a claude ``stream_event`` ``text_delta`` line, else ``""``."""
    if b"text_delta" not in line:
        return ""
    try:
        evt = json.loads(line)
    except ValueError:
        return ""
    if not isinstance(evt, dict) or evt.get("type") != "stream_event":
        return ""
    inner = evt.get("event")
    if not isinstance(inner, dict) or inner.get("type") != "content_block_delta":
        return ""
    delta = inner.get("delta")
    if isinstance(delta, dict) and delta.get("type") == "text_delta":
        text = delta.get("text")
        return text if isinstance(text, str) else ""
    return ""


_DELTA_READERS: Dict[str, Callable[[bytes], str]] = {
    "claude": _claude_text_delta,
    "agy": _agy_text_delta,
}


# The words a CLI's streaming option is refused with, by an older version
# (BUG-93). claude: commander's "error: unknown option '<flag>'" (read in the
# claude 2.1.325 binary); agy: Go's flag package, `invalid value "stream-json"
# for flag -output-format: ...` (read in the agy 1.5.8 binary; per agy's own
# changelog, json and stream-json came together in 1.1.8). Neither seen in a
# real run: a refusal is recognised only on one stderr line naming the
# option, with an option-refusal word.
_STREAM_OPTION = {"claude": "include-partial-messages", "agy": "stream-json"}
_OPTION_REFUSED = (
    "unknown option",
    "unknown flag",
    "unknown argument",
    "unexpected argument",
    "unrecognized",
    "unrecognised",
    "not defined",
    "invalid value",
    "invalid argument",
    "invalid choice",
    "invalid option",
    "unsupported",
    "not supported",
)


def _refused_streaming(
    kind: str, returncode: Optional[int], stdout: bytes, stderr: bytes
) -> bool:
    """True when the CLI refused its streaming option before doing anything:
    a non-zero exit, no JSON event on stdout (the run never started, nothing
    was asked of the model) and a stderr line that names the option with an
    option-refusal word."""
    option = _STREAM_OPTION.get(kind)
    if not option or returncode in (0, None):
        return False
    if _jsonl_events((stdout or b"").decode("utf-8", "replace")):
        return False
    for line in (stderr or b"").decode("utf-8", "replace").lower().splitlines():
        if option in line and any(word in line for word in _OPTION_REFUSED):
            return True
    return False


def can_stream(kind: str) -> bool:
    """True when ``complete(kind, ..., on_delta=...)`` reports text as it comes."""
    return kind in _DELTA_READERS


async def _communicate_streaming(
    proc: "asyncio.subprocess.Process",
    data: bytes,
    read_delta: Optional[Callable[[bytes], str]],
    on_delta: Optional[Callable[[str], None]],
    stop_when: Optional[Callable[[bytes], bool]] = None,
) -> Tuple[bytes, bytes]:
    """``proc.communicate(data)`` that also reports the text deltas of stdout
    line by line as they are written. stdout and stderr are returned whole.

    ``stop_when``, when given, sees each line of stdout first: once it says
    so, the CLI's whole process group is killed at once (no grace: what it
    was doing must not go on) and no delta is reported any more; the output
    written until then is returned as usual (PLU-159, a web run past its
    tool calls)."""
    stopped = False

    async def feed() -> None:
        try:
            assert proc.stdin is not None
            proc.stdin.write(data)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the CLI left without reading: its exit code tells
        finally:
            with contextlib.suppress(Exception):
                proc.stdin.close()  # type: ignore[union-attr]

    async def read_stdout() -> bytes:
        nonlocal stopped
        assert proc.stdout is not None
        chunks: List[bytes] = []
        pending = b""
        while True:
            block = await proc.stdout.read(65536)
            if not block:
                break
            chunks.append(block)
            pending += block
            while True:
                end = pending.find(b"\n")
                if end == -1:
                    break
                line, pending = pending[:end], pending[end + 1 :]
                if stopped:
                    continue
                if stop_when is not None and stop_when(line):
                    stopped = True
                    _signal_group(proc.pid, signal.SIGKILL)
                    continue
                if read_delta is None or on_delta is None:
                    continue
                text = read_delta(line)
                if text:
                    try:
                        on_delta(text)
                    except Exception as exc:  # noqa: BLE001 - consumer's problem
                        log.debug("stream callback failed: %r", exc)
        return b"".join(chunks)

    async def read_stderr() -> bytes:
        assert proc.stderr is not None
        return await proc.stderr.read()

    _, stdout, stderr = await asyncio.gather(feed(), read_stdout(), read_stderr())
    await proc.wait()
    return stdout, stderr


# ── Main entry point ────────────────────────────────────────────────────────


async def complete(
    kind: str,
    body: Dict[str, Any],
    *,
    timeout_s: Optional[float] = None,
    on_delta: Optional[Callable[[str], None]] = None,
    web: Optional["web_access.WebPolicy"] = None,
) -> Dict[str, Any]:
    """Run the CLI and return an OpenAI chat.completions-compatible response.

    Args:
        kind: CLI kind ("gemini" | "codex" | "claude").
        body: OpenAI-format request body (model, messages, max_tokens, ...).
        timeout_s: per-request wall-clock limit (overrides backend default).
        on_delta: called with each piece of answer text as the CLI writes it
            (see ``can_stream``). The returned answer is then the text those
            pieces carried, or extends it: what was sent cannot be taken back,
            so the answer billed and replayed is the one the client got
            (BUG-92). A CLI that refuses its streaming option before doing
            anything is run once more without it, without callbacks (BUG-93).
        web: the request's web policy (PLU-158), claude only: the CLI gets
            the two web tools and nothing else, bounded turns, its own
            timeout, no Haiku helper, and it runs in a network cage whose
            only way out is the run's egress (PLU-162, ``web_caged``):
            without the cage it is never started. None: no tool at all, no
            cage, as always.

    Returns:
        OpenAI-style response dict.

    Raises:
        RuntimeError: if the CLI fails or returns unparseable output.
    """
    backend = get_backend(kind)
    if web is not None and kind != "claude":
        # The router keeps only claude routes for a web request (PLU-158).
        raise RuntimeError(f"web access is not available on the {kind} CLI")
    # The router passes the provider's own model name: kept whole, since a
    # native name may hold a colon (``qwen3:8b``).
    model = body.get("model") or backend.default_model
    if not isinstance(model, str) or is_malformed_model_id(model):
        # Last guard before the command line is built (BUG-120): the router
        # already refuses such an ID, it never reaches the CLI as an option.
        raise ModelNotServedError(backend.command)
    prompt = messages_to_prompt(body.get("messages", []) or [])
    if not prompt:
        raise RuntimeError("Empty prompt — no user/assistant/system messages")

    # Resolve an absolute path ONCE and reuse (TM-001 mitigation: anti
    # PATH hijack + saves ~1-2ms per call).
    binary_path = _resolve_binary(backend.command)
    if binary_path is None:
        raise CliSubprocessError(
            f"{backend.command!r} not installed. Run `brew install {backend.command}` "
            f"or `npm install -g` the provider CLI.",
            provider_name=backend.command,
            model_id=model,
        )

    # PERF + SEC-2026-10: spawn from a private, empty cwd. Claude/Codex/
    # Gemini CLIs then don't pick up the current project's MCP servers,
    # slash commands, memory, CLAUDE.md, etc. (Claude: prompt tokens drop
    # from ~145k to ~30k, wall-clock 9.5s → 5s per cold call), and it is the
    # only directory codex's sandbox may read (gemini gets no tool at all) —
    # the project `.env` stays out of reach. PLUGBOARD_SUBPROCESS_CWD overrides
    # it for agentic coding flows; whatever it points to becomes readable by
    # tenant prompts.
    spawn_cwd = os.environ.get("PLUGBOARD_SUBPROCESS_CWD") or _clean_spawn_cwd()

    read_delta = _DELTA_READERS.get(kind) if on_delta is not None else None
    # The answer text handed to on_delta, in order (BUG-92).
    streamed: List[str] = []

    def relay(text: str) -> None:
        streamed.append(text)
        if on_delta is not None:
            on_delta(text)

    # A web run's tool calls, counted as claude writes them (PLU-159).
    web_calls = web_access.ToolUseCounter(web) if web is not None else None
    base_args = backend.args
    if web is not None:
        # Only WebSearch and WebFetch, checked (web_access.check_web_args).
        base_args = web_access.claude_args(backend.args, web)
    argv = [
        binary_path,
        *(_stream_args(kind, base_args) if read_delta else base_args),
    ]
    if kind == "codex":
        argv.extend(codex_isolation_args(spawn_cwd))
    if backend.model_arg and model:
        argv.extend([backend.model_arg, model])

    # Never argv: it holds the isolation options and the operator's paths.
    # The prompt itself is never in argv; it goes to the CLI's stdin.
    log.info(
        "spawn CLI: %s (model=%s, prompt_chars=%d%s)",
        backend.kind,
        model,
        len(prompt),
        f", web max_uses={web.max_uses}" if web is not None else "",
    )

    deadline = timeout_s or backend.timeout_s
    # Allowlisted env: the server's secrets (ADMIN_KEY, API_KEY,
    # ENCRYPTION_KEY...) must never reach a CLI running a tenant prompt.
    env = build_child_env(kind)
    if web is not None:
        deadline = web_access.TIMEOUT_S
        # The run has no network but its relay, which goes out through the
        # run's own egress (public addresses only, PLU-162); the
        # page-summary helper is never Haiku.
        env.update(web_access.child_env(net_cage.RELAY_PORT))

    async with contextlib.AsyncExitStack() as cleanup:
        if kind == "gemini":
            # Fresh private home: the operator's gemini settings, memory and
            # policies never apply to a tenant prompt (SEC-2026-10).
            gemini_home = cleanup.enter_context(gemini_private_home())
            argv.extend(gemini_isolation_args(gemini_home))
            env.update(gemini_isolation_env(gemini_home))
        if backend.stdin_arg:
            # Last, after every option: the operand telling the CLI to read stdin.
            argv.append(backend.stdin_arg)
        t0 = time.perf_counter()
        # Per-backend semaphore: cap concurrent spawns to PLUGBOARD_SUBPROCESS_LIMIT
        # so a burst of requests can't fork-bomb the host (TM-001 mitigation).
        _live.update(phase="cli_queue")
        slot: Any = _get_semaphore(kind)
        if kind == "agy":
            # Inside its bubblewrap cage, or not at all (BUG-86, agy_cage);
            # raised before anything is launched. Its TMPDIR and its store are
            # private, never the server's, /tmp nor the shared store (see
            # agy_private_tmp(), agy_store); like the gemini home, removed once
            # the child is dead and reaped. agy_caged takes the CLI slot
            # itself, before it copies the store (BUG-157).
            try:
                argv, env = await cleanup.enter_async_context(
                    agy_caged(argv[0], argv[1:], spawn_cwd, slot=slot)
                )
            except agy_cage.CageUnavailable as exc:
                raise agy_sandbox_error(str(exc), model) from None
            slot = contextlib.nullcontext()
        if web is not None:
            # A web run in its network cage, or not at all (PLU-162): no
            # bwrap, no egress, no relay, and claude is never started.
            try:
                argv = await cleanup.enter_async_context(
                    web_caged(argv, spawn_cwd, env)
                )
            except net_cage.CageUnavailable as exc:
                raise web_sandbox_error(str(exc), model) from None
        async with slot:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    # The prompt is written to this pipe, which is then closed:
                    # never an argument (process list, ~128 KiB argv limit),
                    # and never an inherited stdin the CLI could block on.
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    cwd=spawn_cwd,
                    # Own process group: a timeout or a cancellation kills the
                    # CLI's descendants too, see _kill_group().
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                if web is not None:
                    # bwrap itself vanished between the cage and the launch.
                    raise web_sandbox_error(
                        f"{os.path.basename(argv[0])} could not be executed", model
                    ) from exc
                if kind == "agy" and argv[0] != binary_path:
                    # bwrap itself vanished between the cage and the launch.
                    raise agy_sandbox_error(
                        f"{os.path.basename(argv[0])} could not be executed", model
                    ) from exc
                # The cached path was stale (binary was removed mid-run). Drop it
                # so the next call rediscovers via shutil.which().
                _binary_path_cache.pop(backend.command, None)
                raise CliSubprocessError(
                    f"{backend.command!r} not installed. Run `brew install {backend.command}` "
                    f"or `npm install -g` the provider CLI.",
                    provider_name=backend.command,
                    model_id=model,
                ) from exc

            _live.update(phase="cli_running", pid=proc.pid)
            finished = False
            try:
                # A CLI that exits without reading its stdin is not an error
                # here: communicate() ignores the broken pipe and the exit
                # code is reported below.
                if web_calls is not None:
                    # A web run is read line by line, streamed or not: its
                    # tool calls are counted as claude writes them, and the
                    # run is stopped past web_max_uses (PLU-159).
                    pending_io = _communicate_streaming(
                        proc,
                        prompt.encode("utf-8"),
                        read_delta,
                        relay if read_delta is not None else None,
                        stop_when=web_calls.over,
                    )
                elif read_delta is not None and on_delta is not None:
                    pending_io = _communicate_streaming(
                        proc, prompt.encode("utf-8"), read_delta, relay
                    )
                else:
                    pending_io = proc.communicate(prompt.encode("utf-8"))
                stdout, stderr = await asyncio.wait_for(pending_io, timeout=deadline)
                finished = True
            except asyncio.TimeoutError:
                raise CliSubprocessError(
                    f"CLI {backend.command!r} timed out after {deadline:.0f}s",
                    exit_code=proc.returncode,
                    stderr="",
                    pid=proc.pid,
                    provider_name=backend.command,
                    model_id=model,
                    timed_out=True,
                ) from None
            finally:
                # Timeout, cancellation or any error: the semaphore slot and
                # the private gemini home are released only once the child
                # is dead and reaped, or given up on (see _kill_group).
                if not finished:
                    await _kill_group(proc)

    elapsed = time.perf_counter() - t0
    _live.update(phase="cli_parse")
    if kind == "agy":
        # bwrap started but could not build the cage: agy never ran.
        failure = agy_cage.setup_failure(proc.returncode, stdout, stderr)
        if failure:
            raise agy_sandbox_error(failure, model)
    if web is not None:
        # Nor claude, when its web cage or its relay could not start.
        failure = net_cage.setup_failure(proc.returncode, stdout, stderr)
        if failure:
            raise web_sandbox_error(failure, model)
    if (
        read_delta is not None
        and not streamed
        and _refused_streaming(kind, proc.returncode, stdout, stderr)
    ):
        # A CLI version without the streaming option (BUG-93): nothing was
        # sent nor run, so the same request runs once more as a plain call,
        # today's command line, and the caller replays its whole answer.
        log.warning(
            "CLI %r refused its streaming option; answered without streaming "
            "(update the CLI to stream)",
            backend.command,
        )
        return await complete(
            kind, body, timeout_s=max(deadline - elapsed, 1.0), web=web
        )
    stdout_text = (stdout or b"").decode("utf-8", "replace")
    if kind == "agy" and read_delta is not None:
        stdout_text = _agy_stream_to_json(stdout_text)
    stderr_text = (stderr or b"").decode("utf-8", "replace")
    rate_limited = kind == "claude" and _claude_rate_limited(stdout_text)
    # A web run is billed on the tokens claude reports, every model it called
    # counted (BUG-159); without a valid report, on the estimate as before.
    web_usage: Optional[Dict[str, Any]] = None
    if web is not None:
        try:
            web_usage = claude_reported_usage(stdout_text)
        except Exception:  # noqa: BLE001 - malformed usage never fails a reply
            web_usage = None
        if web_usage is None:
            # Counters only, never the output: it holds the prompt and pages.
            log.warning(
                "web billing: no valid reported usage from claude; tokens estimated"
            )
        web_access.count_tool_uses(stdout_text, web)
        log.info(
            "web run: model=%s uses=%d tools=%s models_used=%s",
            model,
            web.uses,
            ",".join(web.tools_seen) or "-",
            ",".join(web_access.models_used(stdout_text, web)) or "-",
        )
        past_uses = web_calls is not None and web_calls.uses > web.max_uses
        if past_uses:
            # Parallel calls in one message passed web_max_uses: the run was
            # stopped as soon as they were written (PLU-159).
            log.warning(
                "web run stopped: model=%s asked for %d web tool calls, %d allowed",
                model,
                web_calls.uses,
                web.max_uses,
            )
        if past_uses or web_access.max_turns_reached(stdout_text):
            # The model spent its turns (or its calls) on the web without
            # answering: the request's limit, not the route's fault (no
            # circuit, no alert).
            exhausted = CliSubprocessError(
                f"web: the model used its {web.max_uses} web tool calls without "
                "a final answer; ask a narrower question or raise web_max_uses",
                exit_code=proc.returncode,
                provider_name=backend.command,
                model_id=model,
                tool_refused=True,
            )
            exhausted.web_exhausted = True  # 502 web_budget_exhausted
            exhausted.reported_usage = web_usage  # billed all the same
            raise exhausted

    if proc.returncode != 0:
        # W004 (2026-05-13): Gemini CLI v0.41+ may exit != 0 due to
        # non-fatal stderr warnings (terminal detection, ripgrep missing,
        # EPERM on Spotlight tmp scan) even when stdout contains a fully
        # valid JSON response.  Attempt recovery by parsing stdout first;
        # only raise if recovery fails or yields empty content.
        #
        # Recovery is intentionally scoped to Gemini (json output mode):
        # Codex/Claude JSONL CLIs reliably set exit=0 on success and
        # surface errors via typed events; treating their non-zero exits
        # as recoverable would mask real failures.
        if kind == "gemini" and stdout_text.strip():
            recovered_text = _try_parse_stdout_text(stdout_text, backend, kind)
            if recovered_text:
                log.warning(
                    "CLI %r exited %d but stdout contains a valid response — "
                    "recovering (W004). stderr_head=%r",
                    backend.command,
                    proc.returncode,
                    stderr_text[:200],
                )
                text = recovered_text
                # Fall through to the wrap-and-return block below.
            else:
                # Gemini stdout was present but unparseable — raise as usual.
                cli_error = _extract_cli_error(stdout_text, kind)
                detail = cli_error or stderr_text[:400] or stdout_text[:400]
                detail += _dyld_hint(stderr_text, backend.command)
                raise CliSubprocessError(
                    f"CLI {backend.command!r} exited {proc.returncode}: {detail}",
                    exit_code=proc.returncode,
                    stderr=stderr_text[:500],
                    pid=proc.pid,
                    provider_name=backend.command,
                    model_id=model,
                    **_quota_fields(cli_error, stderr_text),
                )
        else:
            # Non-Gemini backend, or Gemini with empty stdout — raise
            # immediately as before.
            cli_error = _extract_cli_error(stdout_text, kind)
            detail = cli_error or stderr_text[:400] or stdout_text[:400]
            detail += _dyld_hint(stderr_text, backend.command)
            raise CliSubprocessError(
                f"CLI {backend.command!r} exited {proc.returncode}: {detail}",
                exit_code=proc.returncode,
                stderr=stderr_text[:500],
                pid=proc.pid,
                provider_name=backend.command,
                model_id=model,
                **_quota_fields(cli_error, stderr_text, rate_limited=rate_limited),
            )
    else:
        # Normal exit=0 path: extract assistant text via the shared helper.
        text = _try_parse_stdout_text(stdout_text, backend, kind)
        if text is None and backend.output_mode not in {"json", "jsonl"}:
            raise RuntimeError(f"Unsupported output_mode: {backend.output_mode}")
        # JSON decode errors on the happy path produce a CliSubprocessError with
        # context (exit=0 guarantees the CLI thought it succeeded).
        # A run its CLI ended in error while exiting 0 is not an answer:
        # the text that came with it (claude's `is_error` result, a partial
        # message before codex's turn.failed, gemini's response next to an
        # error object) would otherwise be served, billed and cached. Same
        # for agy's ERROR payload and for a quota error. Without such an
        # ending, only terminal events count when there is an answer: a
        # codex reconnect `error` followed by an answer and turn.completed
        # is fine.
        failed = bool(text) and _ended_in_error(stdout_text, kind)
        cli_error = _extract_cli_error(
            stdout_text, kind, terminal_only=bool(text) and not failed
        )
        if failed or (
            cli_error
            and ((text is None and kind == "agy") or is_quota_error(cli_error))
        ):
            # Once the run ended in error, the quota is read from the event
            # that ended it, not from a reconnect `error` the CLI answered
            # after ("Reconnecting... 1/5 (rate limit exceeded)", the answer,
            # a bare turn.failed). stderr, as before, only when the CLI gave
            # no message at all.
            quota_error = cli_error
            if failed:
                quota_error = _extract_cli_error(stdout_text, kind, terminal_only=True)
            quota_stderr = "" if cli_error else stderr_text
            # Without a message, stderr is the reason, as on a non-zero exit
            # (the router reads it for quarantine); never stdout, which holds
            # the partial answer. A client key gets none of it.
            detail = (
                cli_error
                or stderr_text[:400]
                or "the run ended in error without a message"
            )
            raise CliSubprocessError(
                f"CLI {backend.command!r} failed: {detail}",
                exit_code=proc.returncode,
                stderr=stderr_text[:500],
                pid=proc.pid,
                provider_name=backend.command,
                model_id=model,
                **_quota_fields(quota_error, quota_stderr, rate_limited=rate_limited),
            )
        if text is None and backend.output_mode == "json":
            # Valid agy JSON without an answer is not invalid JSON (BUG-100):
            # said as such, same error and same classification otherwise; a
            # refused tool is flagged, it is not agy's fault (BUG-104).
            empty, tool_refused = (
                _agy_empty_answer(stdout_text, backend.command)
                if kind == "agy"
                else ("", False)
            )
            fields = _quota_fields("", stderr_text)
            raise CliSubprocessError(
                empty
                or f"CLI {backend.command!r} returned invalid JSON. "
                f"First 200 chars: {stdout_text[:200]!r}",
                exit_code=proc.returncode,
                stderr=stderr_text[:500],
                pid=proc.pid,
                provider_name=backend.command,
                model_id=model,
                tool_refused=tool_refused and not fields["quota"],
                **fields,
            )
        if text is None:
            text = ""

    if not text:
        # No answer: the event that ended the run in error (codex turn.failed
        # with exit 0) classifies the failure; a transient error event
        # ("Reconnecting... 5/5") does not, stderr does as before.
        ended_error = _extract_cli_error(stdout_text, kind, terminal_only=True)
        raise CliSubprocessError(
            f"CLI {backend.command!r} returned empty content"
            + (f": {ended_error}" if ended_error else "")
            + f" (stderr: {stderr_text[:200]!r})",
            exit_code=proc.returncode,
            stderr=stderr_text[:500],
            pid=proc.pid,
            provider_name=backend.command,
            model_id=model,
            **_quota_fields(ended_error, stderr_text, rate_limited=rate_limited),
        )

    sent = "".join(streamed)
    if sent and not text.startswith(sent):
        # The deltas already sent differ from the CLI's final text (several
        # agy agent_response steps, whitespace the final text strips): the
        # answer is the text the client received, never one it did not get.
        log.info(
            "CLI %r: answer taken from its streamed text (%d chars, final text "
            "%d chars)",
            backend.command,
            len(sent),
            len(text),
        )
        text = sent

    # agy reports token counts, and claude on a web run (BUG-159); the other
    # runs keep usage=0 and the router estimates. Errors raise above, so this
    # is a success payload.
    reported: Optional[Dict[str, Any]] = web_usage
    if kind == "agy":
        try:
            reported = agy_reported_usage(_parse_gemini_json_stdout(stdout_text))
        except Exception:  # noqa: BLE001 - malformed usage never fails a reply
            reported = None

    # Wrap into OpenAI chat.completions shape
    result: Dict[str, Any] = {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": f"{kind}-cli:{model}",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": reported
        or {
            # CLI doesn't reliably report tokens; leave zeros — the billing
            # layer can estimate via tiktoken if needed.
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
        "_cli_elapsed_s": round(elapsed, 3),
    }
    if reported is not None:
        result[USAGE_REPORTED_KEY] = True
    return result
