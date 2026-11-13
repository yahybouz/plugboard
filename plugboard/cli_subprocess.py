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
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from plugboard import envcompat
from plugboard.circuit_breaker import (
    is_model_unsupported_by_subscription,
    is_request_too_long,
)
from plugboard import live_requests as _live

log = logging.getLogger(__name__)


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
    ):
        super().__init__(message)
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
    # agy keeps its Google sign-in under ~/.gemini/antigravity: HOME is enough.
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
        # --sandbox restricts the terminal on top of that, --mode plan keeps
        # the agent from editing, and slash commands / skills are not expanded.
        # Never add --dangerously-skip-permissions here. No -p: agy reads a
        # piped prompt when -p is omitted.
        args=[
            "--output-format",
            "json",
            "--disable-slash-commands",
            "--sandbox",
            "--mode",
            "plan",
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

    Returns an empty list when agy is missing, fails or times out: the caller
    then falls back to the preset baseline.
    """
    binary = _resolve_binary("agy")
    if binary is None:
        return []
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "models",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=build_child_env("agy"),
            cwd=_clean_spawn_cwd(),
            start_new_session=True,
        )
        finished = False
        try:
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout_s)
            finished = True
        except asyncio.TimeoutError:
            log.warning("agy models timed out after %.0fs", timeout_s)
            return []
        finally:
            if not finished:
                await _kill_group(proc)
    except Exception as exc:  # noqa: BLE001
        log.warning("agy models failed: %s", exc)
        return []
    if proc.returncode != 0:
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
    if shutil.which(backend.command) is None:
        return {"ok": False, "error": f"{backend.command} not on PATH"}
    try:
        proc = await asyncio.create_subprocess_exec(
            backend.command,
            "--version",
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


# Key set on complete()'s result when the CLI reported validated token counts.
# The router pops it before the result leaves the process.
USAGE_REPORTED_KEY = "_plugboard_usage_reported"


def _usage_count(value: Any) -> Optional[int]:
    """A token counter: a non-negative int (bool, str, float, None rejected)."""
    if type(value) is not int or value < 0:
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

    Handles ``text`` / ``thinking`` blocks (Anthropic "extended thinking"
    mode) and their corresponding deltas so that a thinking-only response
    does not silently yield empty content (BENCH-001).
    """
    parts: List[str] = []
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
                if btype in ("text", "thinking"):
                    text = block.get("text") or block.get("thinking", "")
                    if isinstance(text, str):
                        parts.append(text)
        elif etype == "content_block_delta":
            delta = evt.get("delta", {})
            if delta.get("type") in ("text_delta", "thinking_delta"):
                parts.append(delta.get("text", ""))
        elif etype == "result":
            # Fallback: some CLI versions surface the final text here.
            # Only use it when no earlier text/thinking blocks were found
            # so we don't duplicate content already collected above.
            if not parts:
                result_text = evt.get("result", "")
                if isinstance(result_text, str) and result_text:
                    parts.append(result_text)
    return "".join(parts).strip()


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


# ── Main entry point ────────────────────────────────────────────────────────


async def complete(
    kind: str,
    body: Dict[str, Any],
    *,
    timeout_s: Optional[float] = None,
) -> Dict[str, Any]:
    """Run the CLI and return an OpenAI chat.completions-compatible response.

    Args:
        kind: CLI kind ("gemini" | "codex" | "claude").
        body: OpenAI-format request body (model, messages, max_tokens, ...).
        timeout_s: per-request wall-clock limit (overrides backend default).

    Returns:
        OpenAI-style response dict.

    Raises:
        RuntimeError: if the CLI fails or returns unparseable output.
    """
    backend = get_backend(kind)
    # The router passes the provider's own model name: kept whole, since a
    # native name may hold a colon (``qwen3:8b``).
    model = body.get("model") or backend.default_model
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

    argv = [binary_path, *backend.args]
    if kind == "codex":
        argv.extend(codex_isolation_args(spawn_cwd))
    if backend.model_arg and model:
        argv.extend([backend.model_arg, model])

    # Never argv: it holds the isolation options and the operator's paths.
    # The prompt itself is never in argv; it goes to the CLI's stdin.
    log.info(
        "spawn CLI: %s (model=%s, prompt_chars=%d)", backend.kind, model, len(prompt)
    )

    deadline = timeout_s or backend.timeout_s
    # Allowlisted env: the server's secrets (ADMIN_KEY, API_KEY,
    # ENCRYPTION_KEY...) must never reach a CLI running a tenant prompt.
    env = build_child_env(kind)

    with contextlib.ExitStack() as cleanup:
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
        async with _get_semaphore(kind):
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
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(prompt.encode("utf-8")),
                    timeout=deadline,
                )
                finished = True
            except asyncio.TimeoutError:
                raise CliSubprocessError(
                    f"CLI {backend.command!r} timed out after {deadline:.0f}s",
                    exit_code=proc.returncode,
                    stderr="",
                    pid=proc.pid,
                    provider_name=backend.command,
                    model_id=model,
                ) from None
            finally:
                # Timeout, cancellation or any error: the semaphore slot and
                # the private gemini home are released only once the child
                # is dead and reaped, or given up on (see _kill_group).
                if not finished:
                    await _kill_group(proc)

    elapsed = time.perf_counter() - t0
    _live.update(phase="cli_parse")
    stdout_text = (stdout or b"").decode("utf-8", "replace")
    stderr_text = (stderr or b"").decode("utf-8", "replace")
    rate_limited = kind == "claude" and _claude_rate_limited(stdout_text)

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
            raise CliSubprocessError(
                f"CLI {backend.command!r} returned invalid JSON. "
                f"First 200 chars: {stdout_text[:200]!r}",
                exit_code=proc.returncode,
                stderr=stderr_text[:500],
                pid=proc.pid,
                provider_name=backend.command,
                model_id=model,
                **_quota_fields("", stderr_text),
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

    # Only agy reports token counts; the other CLIs keep usage=0 and the
    # router estimates. agy errors raise above, so this is a success payload.
    reported: Optional[Dict[str, Any]] = None
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
