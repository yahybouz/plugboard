"""Admin guards for Ollama model downloads: on/off switch and disk guard.

Small VMs (no GPU, a few dozen GB of disk) should only consume models through
subscriptions or Ollama Cloud. Two guards protect them:

* **Local downloads switch.** ``PLUGBOARD_LOCAL_MODEL_DOWNLOADS`` (default
  ``1``) sets the default; an admin override, saved in
  ``<data>/ollama_settings.json``, wins over the environment and survives
  restarts. When off, only Ollama Cloud ids (``<name>:cloud`` or a tag ending
  in ``-cloud``) can be pulled: they are manifest-only, a few KB on disk.
* **Disk guard.** Before a local pull, the size is read from the registry
  manifest (layers + config) and compared with the free space of
  ``PLUGBOARD_OLLAMA_DISK_PATH`` (default the data directory) minus a margin
  of ``PLUGBOARD_OLLAMA_MIN_FREE_GB`` (default 5). When the manifest cannot be
  read, the pull streams under a guard that sums the layer totals Ollama
  reports and stops the pull when they no longer fit, or when free space drops
  below the margin.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, AsyncIterator, Callable, Dict, Optional, Tuple

import aiohttp

from plugboard.storage import atomic_write_json, data_dir

log = logging.getLogger(__name__)

DOWNLOADS_ENV = "PLUGBOARD_LOCAL_MODEL_DOWNLOADS"
MIN_FREE_ENV = "PLUGBOARD_OLLAMA_MIN_FREE_GB"
DISK_PATH_ENV = "PLUGBOARD_OLLAMA_DISK_PATH"
REGISTRY_ENV = "PLUGBOARD_OLLAMA_REGISTRY_URL"
DEFAULT_REGISTRY = "https://registry.ollama.ai"
DEFAULT_MIN_FREE_GB = 5.0
MANIFEST_TIMEOUT_S = 5.0
SETTINGS_FILE = "ollama_settings.json"
MAX_NAME_LEN = 200
GB = 1024**3
_DISK_POLL_S = 1.0

_MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
    )
)
_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}"
# name, user/name or host/user/name (hf.co/...), optional :tag
_NAME_RE = re.compile(
    rf"^{_SEGMENT}(?:/{_SEGMENT}){{0,2}}(?::[A-Za-z0-9][A-Za-z0-9._-]{{0,127}})?$"
)
_FALSE = ("0", "false", "no", "off")


class PullRefused(Exception):
    """A pull refused before any download; ``status`` is the HTTP answer."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class PullAborted(Exception):
    """Raised by ``guarded_pull`` after its error line: skip the success path."""


# -- Model names ----------------------------------------------------------------


def validate_model_name(name: str) -> str:
    """Return the stripped name, or raise ``ValueError`` with a short reason."""
    name = (name or "").strip()
    if not name or len(name) > MAX_NAME_LEN:
        raise ValueError(f"model name must hold 1 to {MAX_NAME_LEN} characters")
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            "invalid model name: use name, name:tag or namespace/name:tag "
            "(letters, digits, '.', '_', '-')"
        )
    return name


def is_cloud_id(name: str) -> bool:
    """Ollama Cloud id: ``<name>:cloud`` or a tag ending in ``-cloud``."""
    _, sep, tag = (name or "").rpartition(":")
    if not sep:
        return False
    tag = tag.lower()
    return tag == "cloud" or tag.endswith("-cloud")


def _split_name(name: str) -> Tuple[str, str]:
    """(repository path without tag, tag), the tag defaulting to ``latest``."""
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        repo, tag = name.rsplit(":", 1)
        return repo, tag
    return name, "latest"


# -- Settings -------------------------------------------------------------------


def _settings_path() -> Path:
    return data_dir() / SETTINGS_FILE


def env_downloads_enabled() -> bool:
    raw = os.environ.get(DOWNLOADS_ENV, "1").strip().lower()
    return raw not in _FALSE and raw != ""


def _load_override() -> Optional[bool]:
    try:
        data = json.loads(_settings_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.error("ollama settings unreadable (%s): using the environment", exc)
        return None
    value = data.get("local_downloads_enabled") if isinstance(data, dict) else None
    return value if isinstance(value, bool) else None


def downloads_state() -> Tuple[bool, str]:
    """(enabled, source): the admin override wins over the environment."""
    override = _load_override()
    if override is not None:
        return override, "admin"
    return env_downloads_enabled(), "env"


def local_downloads_enabled() -> bool:
    return downloads_state()[0]


def set_local_downloads(enabled: bool) -> None:
    """Persist the admin override (atomic write, survives restarts)."""
    atomic_write_json(
        _settings_path(),
        {
            "local_downloads_enabled": bool(enabled),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def min_free_gb() -> float:
    try:
        value = float(os.environ.get(MIN_FREE_ENV, "") or DEFAULT_MIN_FREE_GB)
    except ValueError:
        return DEFAULT_MIN_FREE_GB
    return value if value >= 0 else DEFAULT_MIN_FREE_GB


def min_free_bytes() -> int:
    return int(min_free_gb() * GB)


def disk_path() -> Path:
    configured = os.environ.get(DISK_PATH_ENV, "").strip()
    return Path(configured).expanduser() if configured else data_dir()


def disk_usage() -> Dict[str, Any]:
    """Free and total bytes of the disk holding ``disk_path()``.

    A path that does not exist yet is measured on its nearest existing parent.
    """
    path = disk_path()
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    return {"path": str(path), "free_bytes": usage.free, "total_bytes": usage.total}


def settings_payload() -> Dict[str, Any]:
    enabled, source = downloads_state()
    try:
        disk: Dict[str, Any] = disk_usage()
    except OSError as exc:
        log.warning("ollama disk usage unavailable: %s", exc)
        disk = {"path": str(disk_path()), "free_bytes": None, "total_bytes": None}
    return {
        "local_downloads_enabled": enabled,
        "source": source,
        "min_free_gb": min_free_gb(),
        "disk": disk,
    }


# -- Registry manifest --------------------------------------------------------------


def _registry() -> str:
    return (os.environ.get(REGISTRY_ENV, "").strip() or DEFAULT_REGISTRY).rstrip("/")


def manifest_url(name: str) -> Optional[str]:
    """Manifest URL on the Ollama registry, or None for another registry."""
    repo, tag = _split_name(name)
    parts = repo.split("/")
    if len(parts) == 1:
        parts = ["library", parts[0]]
    if len(parts) != 2:
        return None  # host/user/model: another registry (hf.co, ...)
    return f"{_registry()}/v2/{parts[0]}/{parts[1]}/manifests/{tag}"


async def resolve_manifest_size(
    name: str, timeout: float = MANIFEST_TIMEOUT_S
) -> Optional[int]:
    """Download size (layers + config) from the registry, None when unknown."""
    url = manifest_url(name)
    if url is None:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers={"Accept": _MANIFEST_ACCEPT},
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    log.info("ollama manifest %s -> HTTP %s", name, resp.status)
                    return None
                raw = await resp.content.read(1024 * 1024)
        data = json.loads(raw)
        sizes = [layer.get("size") for layer in data.get("layers") or []]
        config = data.get("config") or {}
        sizes.append(config.get("size", 0))
        if not all(
            isinstance(s, int) and not isinstance(s, bool) and s >= 0 for s in sizes
        ):
            return None
        total = sum(sizes)
        return total if total > 0 else None
    except Exception as exc:  # noqa: BLE001 (best effort: fall back to the stream guard)
        log.info("ollama manifest %s unresolved: %s", name, exc)
        return None


def _fmt_gb(n: int) -> str:
    return f"{n / GB:.1f} GB"


async def precheck_pull(name: str) -> Dict[str, Any]:
    """Refuse a pull before any download, or return the guard context.

    Raises ``PullRefused`` (403 downloads off, 507 not enough disk). Returns
    ``{"cloud", "size", "free", "margin"}``; ``size`` None means the stream
    guard must enforce the limit.
    """
    cloud = is_cloud_id(name)
    if cloud:
        return {"cloud": True, "size": None, "free": None, "margin": 0}
    if not local_downloads_enabled():
        raise PullRefused(
            403,
            "Local model downloads are disabled: only Ollama Cloud models "
            "(<name>:cloud or a tag ending in -cloud) can be added. An admin "
            "can turn local downloads on in the Ollama tab.",
        )
    margin = min_free_bytes()
    try:
        free = disk_usage()["free_bytes"]
    except OSError as exc:
        log.error("ollama pull refused: disk usage unavailable (%s)", exc)
        raise PullRefused(
            503, f"Cannot measure free disk space at {disk_path()}; pull refused."
        ) from None
    size = await resolve_manifest_size(name)
    if size is not None and size + margin > free:
        raise PullRefused(
            507,
            f"Not enough disk space for {name}: needs {_fmt_gb(size)} plus a "
            f"{_fmt_gb(margin)} margin, {_fmt_gb(free)} free.",
        )
    return {"cloud": False, "size": size, "free": free, "margin": margin}


# -- Streaming guard -----------------------------------------------------------------


async def guarded_pull(
    name: str,
    *,
    free_at_start: Optional[int],
    margin: int,
    pull: Callable[[str], AsyncGenerator[Dict[str, Any], None]],
    delete: Callable[[str], Any],
    free_now: Optional[Callable[[], int]] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """Relay Ollama pull progress; stop the pull when it no longer fits.

    Ollama reports one ``digest``/``total``/``completed`` per layer. The bytes
    still to download are the totals minus what was already on disk when each
    layer first appeared. When they exceed the space measured at the start
    (minus the margin), or free space drops below the margin, the stream is
    closed (closing the HTTP response cancels the Ollama pull), an error line
    is emitted and the partial model is deleted (best effort).
    """
    if free_now is None:

        def free_now() -> int:
            return disk_usage()["free_bytes"]

    remaining: Dict[str, int] = {}
    last_poll = 0.0
    reason = ""
    stream = pull(name)
    try:
        async for progress in stream:
            digest = progress.get("digest") if isinstance(progress, dict) else None
            total = progress.get("total") if isinstance(progress, dict) else None
            if (
                free_at_start is not None
                and isinstance(digest, str)
                and isinstance(total, int)
                and digest not in remaining
            ):
                done = progress.get("completed")
                done = done if isinstance(done, int) and 0 <= done <= total else 0
                remaining[digest] = total - done
                needed = sum(remaining.values())
                if needed + margin > free_at_start:
                    reason = (
                        f"Pull stopped: {name} needs at least {_fmt_gb(needed)} plus a "
                        f"{_fmt_gb(margin)} margin, {_fmt_gb(free_at_start)} were free."
                    )
                    break
            now = time.monotonic()
            if free_at_start is not None and now - last_poll >= _DISK_POLL_S:
                last_poll = now
                try:
                    free = free_now()
                except OSError:
                    free = None
                if free is not None and free < margin:
                    reason = (
                        f"Pull stopped: free disk space fell to {_fmt_gb(free)}, "
                        f"below the {_fmt_gb(margin)} margin."
                    )
                    break
            yield progress
    finally:
        await stream.aclose()
    if reason:
        log.warning("ollama pull aborted model=%s reason=%s", name, reason)
        yield {"status": "error", "error": reason, "aborted": True}
        try:
            await delete(name)
        except Exception as exc:  # noqa: BLE001 (best effort cleanup)
            log.info("ollama cleanup after abort model=%s: %s", name, exc)
        raise PullAborted(reason)
