"""MCP stdio surface client for the live-test framework.

Satisfies the base ``SurfaceClient`` Protocol (structural typing). Does NOT
satisfy ``StreamingCapableSurface`` (raw_sse_lines=None → D5 skip) nor
``BillingAwareSurface`` (supports_billing=False → D6 skip).

The framework venv does NOT contain the ``mcp`` SDK (it lives in ``.venv-mcp``),
so this client hand-rolls the JSON-RPC 2.0 / stdio protocol against a spawned
``.venv-mcp/bin/python scripts/run_mcp.py`` subprocess.

Council C8 (2026-05-31), triplet:
  - PROPOSER : developer
  - CRITIC   : codex-coder (1 BLOCKER + 4 MAJOR integrated)

Key robustness decisions (Council C8):
  1. INTERRUPTIBLE reads (BLOCKER B-1): the subprocess is spawned with
     ``bufsize=0`` and stdout is read via ``select`` + ``os.read`` into a manual
     line buffer. A plain ``BufferedReader.readline()`` is NOT interruptible by a
     deadline and is incompatible with ``select`` (the buffer hides bytes), so a
     slow/hung model (MCP timeout up to 270s) would block the whole run.
  2. stderr is drained by a daemon thread so a full OS pipe buffer never blocks
     the subprocess.
  3. The chat tool's markdown footer ``\\n\\n---\\n_(tokens: X, cost: $Y)_`` is
     stripped before returning, so D2 receives pure JSON.
  4. Only the EXACT empty-content sentinels from mcp_server map to empty content;
     a model literally answering "Error: ..." is returned as content, not a tool
     error (Council C8 M-2).
  5. ``startup_timeout`` bounds the handshake separately from the per-call
     timeout (Council C8 M-1). Paths are resolved from ``__file__`` (M-3).
"""

from __future__ import annotations

import json
import os
import re
import select
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Protocol, Tuple, runtime_checkable

# tests/live_model_tests/surfaces/mcp_client.py → parents[3] == project root.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_SERVER_CMD: List[str] = [
    str(_PROJECT_ROOT / ".venv-mcp" / "bin" / "python"),
    str(_PROJECT_ROOT / "scripts" / "run_mcp.py"),
]

_PROTOCOL_VERSION = "2024-11-05"
_DEFAULT_STARTUP_TIMEOUT = 10.0
_MAX_SPAWN_RETRIES = 3

# Footer appended by mcp_server.chat(): "\n\n---\n_(<meta>)_" anchored at end.
_FOOTER_RE = re.compile(r"\n\n---\n_\(([^)]*)\)_\s*$")

# Exact sentinels from mcp_server.chat() that mean "no content" (Council C8 M-2).
_EMPTY_SENTINELS = frozenset({
    "Error: Empty response from LLM (no choices)",
    "Error: Empty content in LLM response",
})


# ── Exceptions ────────────────────────────────────────────────────────────────

class McpSurfaceError(Exception):
    """``error_type`` is consumed by dimension run_dX handlers (like ApiSurfaceError)."""

    def __init__(self, error_type: str, detail: str = "") -> None:
        self.error_type = error_type
        super().__init__(f"[{error_type}] {detail}"[:300])


class McpTransportError(McpSurfaceError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("mcp_transport_error", detail)


class McpHandshakeError(McpSurfaceError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("mcp_handshake_error", detail)


class McpToolError(McpSurfaceError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("mcp_tool_error", detail)


# ── Transport Protocol ────────────────────────────────────────────────────────

@runtime_checkable
class JsonRpcTransport(Protocol):
    def send(self, obj: Dict[str, Any]) -> None: ...
    def recv(self, deadline: Optional[float] = None) -> Dict[str, Any]: ...
    def close(self) -> None: ...


# ── SubprocessTransport ───────────────────────────────────────────────────────

class SubprocessTransport:
    """Concrete ``JsonRpcTransport`` over a subprocess stdio channel.

    Spawned with ``bufsize=0`` so stdout is an unbuffered ``FileIO`` whose fd can
    be ``select``-ed reliably (Council C8 B-1). stderr is drained by a daemon
    thread to avoid a full-pipe deadlock.
    """

    def __init__(self, cmd: List[str], env: Optional[Dict[str, str]] = None) -> None:
        self._cmd = cmd
        self._env = env
        self._proc: Optional[subprocess.Popen] = None  # type: ignore[type-arg]
        self._stdout_buf = bytearray()
        self._stderr_lines: List[str] = []
        self._stderr_lock = threading.Lock()
        self._spawn()

    def _spawn(self) -> None:
        full_env = os.environ.copy()
        if self._env:
            full_env.update(self._env)
        try:
            self._proc = subprocess.Popen(  # noqa: S603
                self._cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=full_env,
                cwd=str(_PROJECT_ROOT),
                bufsize=0,  # Council C8 B-1 — unbuffered so select(stdout) is reliable
            )
        except (FileNotFoundError, PermissionError) as exc:
            raise McpTransportError(f"cannot launch {self._cmd[0]!r}: {exc}") from exc

        t = threading.Thread(target=self._drain_stderr, daemon=True, name="mcp-stderr-drain")
        t.start()

    def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        stderr = self._proc.stderr
        try:
            for chunk in iter(lambda: stderr.read(4096), b""):
                text = chunk.decode("utf-8", errors="replace")
                with self._stderr_lock:
                    self._stderr_lines.append(text)
        except Exception:  # noqa: BLE001
            pass

    def get_stderr_snapshot(self) -> str:
        with self._stderr_lock:
            return "".join(self._stderr_lines)[-2000:]

    def send(self, obj: Dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise McpTransportError("transport is closed")
        if self._proc.poll() is not None:
            raise McpTransportError(f"subprocess exited (code {self._proc.returncode})")
        try:
            self._proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            self._proc.stdin.flush()
        except (OSError, BrokenPipeError) as exc:
            raise McpTransportError(f"write failed: {exc}") from exc

    def recv(self, deadline: Optional[float] = None) -> Dict[str, Any]:
        """Interruptible line read (Council C8 B-1): drain the manual buffer,
        then ``select`` the raw fd with the remaining time, then ``os.read``."""
        assert self._proc is not None and self._proc.stdout is not None
        fd = self._proc.stdout.fileno()
        while True:
            nl = self._stdout_buf.find(b"\n")
            if nl >= 0:
                raw = bytes(self._stdout_buf[:nl]).strip()
                del self._stdout_buf[: nl + 1]
                if not raw:
                    continue
                try:
                    return json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue  # non-JSON stdout line — skip defensively

            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("MCP recv deadline exceeded")
            else:
                remaining = None

            readable, _, _ = select.select([fd], [], [], remaining)
            if not readable:
                raise TimeoutError("MCP recv deadline exceeded")
            try:
                chunk = os.read(fd, 65536)
            except InterruptedError:
                continue
            except OSError as exc:
                raise McpTransportError(f"stdout read failed: {exc}") from exc
            if not chunk:  # EOF
                raise McpTransportError(
                    f"subprocess closed stdout (code={self._proc.poll()}; "
                    f"stderr tail: {self.get_stderr_snapshot()[-300:]})"
                )
            self._stdout_buf.extend(chunk)

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            pass


# ── Helpers ──────────────────────────────────────────────────────────────────

def _strip_footer(text: str) -> Tuple[str, Optional[Dict[str, str]]]:
    """Remove the mcp_server billing footer (anchored at end). The anchor
    (``\\n\\n---\\n_(`` ... ``)_`` at end) makes a false match inside JSON
    content effectively impossible."""
    m = _FOOTER_RE.search(text)
    if m is None:
        return text, None
    meta: Dict[str, str] = {}
    for part in m.group(1).split(","):
        k, _, v = part.strip().partition(":")
        if k:
            meta[k.strip()] = v.strip()
    return text[: m.start()], meta


def _extract_mcp_args(messages: List[Dict[str, Any]]) -> Dict[str, str]:
    """Last user message → ``message``; a system message → ``system_prompt``."""
    user_msg = next((m for m in reversed(messages) if (m.get("role") or "").lower() == "user"), None)
    if user_msg is None:
        raise ValueError("McpSurfaceClient.chat requires at least one user message")
    system = next((m.get("content") or "" for m in messages
                   if (m.get("role") or "").lower() == "system"), "")
    return {"message": user_msg.get("content") or "", "system_prompt": system}


def _make_unified_dict(content: str) -> Dict[str, Any]:
    # MCP is request-response: finish_reason="stop", done_received=True,
    # first_token_latency_ms=None are intentional fixed values (do not "fix").
    return {
        "content": content,
        "reasoning": "",
        "finish_reason": "stop" if content else None,
        "first_token_latency_ms": None,
        "done_received": True,
        "raw_chunks": [],
        "raw_sse_lines": None,  # non-streaming → D5 skips
        "billing": None,        # supports_billing=False → D6 skips
        "model_echoed": None,
        "request_id": None,
    }


# ── McpSurfaceClient ──────────────────────────────────────────────────────────

class McpSurfaceClient:
    """MCP stdio surface client. NOT thread-safe (one instance per worker)."""

    name: str = "mcp"
    supports_billing: ClassVar[bool] = False

    def __init__(
        self,
        server_cmd: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        transport: Optional[JsonRpcTransport] = None,
        startup_timeout: float = _DEFAULT_STARTUP_TIMEOUT,
    ) -> None:
        self._startup_timeout = startup_timeout
        self._rpc_id = 0
        self._handshake_done = False
        if transport is not None:
            self._transport = transport
            self._owned_transport = False
        else:
            cmd = server_cmd if server_cmd is not None else list(_DEFAULT_SERVER_CMD)
            self._transport = SubprocessTransport(cmd=cmd, env=env)
            self._owned_transport = True

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    def _do_handshake(self, chat_deadline: float) -> None:
        if self._handshake_done:
            return
        # Bound the handshake separately from the per-call timeout (Council C8 M-1).
        deadline = min(chat_deadline, time.monotonic() + self._startup_timeout)
        req_id = self._next_id()
        self._transport.send({
            "jsonrpc": "2.0", "id": req_id, "method": "initialize",
            "params": {"protocolVersion": _PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": {"name": "live-test", "version": "1.0"}},
        })
        while True:
            msg = self._transport.recv(deadline=deadline)
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise McpHandshakeError(f"initialize error: {msg['error']}")
                break
        self._transport.send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        self._handshake_done = True

    def chat(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        *,
        stream: bool,          # accepted for Protocol compatibility; ignored (MCP is request-response)
        timeout: float,
        max_tokens: int,
        temperature: float,
    ) -> Dict[str, Any]:
        last_exc: Optional[Exception] = None
        for attempt in range(1, _MAX_SPAWN_RETRIES + 1):
            try:
                return self._chat_once(model, messages, timeout, max_tokens, temperature)
            except TimeoutError:
                raise  # not transient — propagate so the dimension attributes "timeout"
            except (McpTransportError, McpHandshakeError) as exc:
                last_exc = exc
                if attempt < _MAX_SPAWN_RETRIES and self._owned_transport:
                    self._respawn()
                else:
                    raise
        assert last_exc is not None
        raise last_exc

    def _chat_once(self, model, messages, timeout, max_tokens, temperature) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        self._do_handshake(deadline)
        args = _extract_mcp_args(messages)
        req_id = self._next_id()
        arguments: Dict[str, Any] = {
            "model": model, "message": args["message"],
            "max_tokens": max_tokens, "temperature": temperature,
        }
        if args["system_prompt"]:
            arguments["system_prompt"] = args["system_prompt"]
        self._transport.send({
            "jsonrpc": "2.0", "id": req_id, "method": "tools/call",
            "params": {"name": "chat", "arguments": arguments},
        })
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError(f"MCP tools/call timed out after {timeout:.1f}s")
            msg = self._transport.recv(deadline=deadline)
            if msg.get("id") != req_id:
                continue  # notification / other id — skip
            if "error" in msg:
                err = msg["error"]
                raise McpToolError(f"JSON-RPC error {err.get('code')}: {err.get('message')}")
            result = msg.get("result") or {}
            is_error = bool(result.get("isError", False))
            content_list = result.get("content") or []
            raw_text = ""
            if isinstance(content_list, list) and content_list:
                first = content_list[0]
                if isinstance(first, dict) and first.get("type") == "text":
                    raw_text = first.get("text") or ""
            return self._build_result(raw_text, is_error)

    def _build_result(self, raw_text: str, is_error: bool) -> Dict[str, Any]:
        if is_error:
            raise McpToolError(f"isError=true: {raw_text[:200]}")
        clean, _meta = _strip_footer(raw_text)
        # Only the EXACT empty sentinels map to empty content (Council C8 M-2);
        # a model literally answering "Error: ..." is returned as content.
        if clean in _EMPTY_SENTINELS:
            return _make_unified_dict("")
        return _make_unified_dict(clean)

    def _respawn(self) -> None:
        try:
            self._transport.close()
        except Exception:  # noqa: BLE001
            pass
        if isinstance(self._transport, SubprocessTransport):
            old = self._transport
            self._transport = SubprocessTransport(cmd=old._cmd, env=old._env)
            self._handshake_done = False
            self._rpc_id = 0

    def close(self) -> None:
        if self._owned_transport:
            try:
                self._transport.close()
            except Exception:  # noqa: BLE001
                pass

    def __del__(self) -> None:
        self.close()

    def __enter__(self) -> "McpSurfaceClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
