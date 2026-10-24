"""Private one-shot local workers; no ML imports in the gateway interpreter."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
from pathlib import Path
import signal
import tempfile

from plugboard import decision_assets

TIMEOUT_SECONDS = 30.0
MAX_OUTPUT_BYTES = 128 * 1024
MAX_INPUT_BYTES = 64 * 1024
_active = False


class RuntimeFailure(Exception):
    """Bounded error codes only, no worker stderr or request contents."""


def available(model_id: str) -> tuple[bool, str]:
    if model_id not in {"laya", "laya-multilingual"}:
        return False, "unknown_model"
    executable = os.environ.get("PLUGBOARD_DECISION_PYTHON", "")
    root = os.environ.get("PLUGBOARD_DECISION_ASSETS", "")
    if not executable or not root:
        return False, "runtime_not_configured"
    python = Path(executable)
    if (
        not python.is_absolute()
        or not python.is_file()
        or not os.access(python, os.X_OK)
    ):
        return False, "runtime_not_configured"
    return decision_assets.verify_assets(model_id, Path(root))


def _command(model_id: str, root: Path, python: str) -> list[str]:
    worker = Path(__file__).resolve().parents[1] / "scripts" / "decision_worker.py"
    return [python, "-I", str(worker), "--model", model_id, "--assets", str(root)]


def _environment(root: Path) -> dict[str, str]:
    # Explicit allowlist; no inherited dotenv, gateway/provider keys, proxies or PYTHONPATH.
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "HOME": str(root),
        "TMPDIR": str(root),
        "HF_HOME": str(root / "hf-home"),
        "HF_HUB_CACHE": str(root / "hub"),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "DO_NOT_TRACK": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "OMP_NUM_THREADS": "2",
        "PLUGBOARD_SKIP_DOTENV": "1",
    }


async def _bounded_read(stream: asyncio.StreamReader) -> bytes:
    chunks, length = [], 0
    while chunk := await stream.read(8192):
        length += len(chunk)
        if length > MAX_OUTPUT_BYTES:
            raise RuntimeFailure("worker_output_limit")
        chunks.append(chunk)
    return b"".join(chunks)


async def _kill(process: asyncio.subprocess.Process) -> None:
    # Every worker owns its session. Kill descendants too, including after leader exit.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()


async def infer(model_id: str, request: dict) -> dict:
    global _active
    ready, reason = available(model_id)
    if not ready:
        raise RuntimeFailure(reason)
    encoded = json.dumps(request, allow_nan=False).encode()
    if len(encoded) > MAX_INPUT_BYTES:
        raise RuntimeFailure("worker_input_limit")
    if _active:
        raise RuntimeFailure("worker_busy")
    # No await between check and set; one model at a time in this gateway process.
    _active = True
    process = None
    lock_fd = None
    workdir = None
    try:
        root = Path(os.environ["PLUGBOARD_DECISION_ASSETS"]).absolute()
        lock_fd = os.open(
            root / ".inference.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeFailure("worker_busy") from None
        workdir = tempfile.TemporaryDirectory(prefix=".worker-", dir=root)
        environment = _environment(root)
        environment["TMPDIR"] = workdir.name
        process = await asyncio.create_subprocess_exec(
            *_command(model_id, root, os.environ["PLUGBOARD_DECISION_PYTHON"]),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=environment,
            cwd=workdir.name,
            start_new_session=True,
        )

        async def exchange():
            process.stdin.write(encoded)
            await process.stdin.drain()
            process.stdin.close()
            await process.stdin.wait_closed()
            output = await _bounded_read(process.stdout)
            await process.wait()
            return output

        try:
            output = await asyncio.wait_for(exchange(), timeout=TIMEOUT_SECONDS)
        except TimeoutError:
            raise RuntimeFailure("worker_timeout") from None
        if process.returncode == 42:
            raise RuntimeFailure("worker_context_limit")
        if process.returncode != 0:
            raise RuntimeFailure("worker_failed")
        try:
            result = json.loads(output)
        except (ValueError, UnicodeError):
            raise RuntimeFailure("worker_invalid_output") from None
        if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
            raise RuntimeFailure("worker_invalid_output")
        return result
    except (OSError, BrokenPipeError, ConnectionError):
        raise RuntimeFailure("worker_failed") from None
    finally:
        if process is not None:
            await _kill(process)
        if workdir is not None:
            workdir.cleanup()
        if lock_fd is not None:
            os.close(lock_fd)
        _active = False
