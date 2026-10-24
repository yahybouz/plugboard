"""Explicit, pinned local decision assets. Importing this module never downloads."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import urllib.request

MANIFEST = Path(__file__).resolve().parents[1] / "manifests" / "decisions.json"
LAYA_CONFIGS = MANIFEST.parent / "decision-configs"
RESERVE_BYTES = 6 * 1024**3


class AssetError(Exception):
    """Safe error code; never include an upstream body or input."""


def read_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def laya_config(model_id: str) -> dict:
    """Read small bundled upstream configs without touching/downloading model assets."""
    if model_id not in {"laya", "laya-multilingual"}:
        raise AssetError("unknown_model")
    model = read_manifest()["models"][model_id]
    entry = next(
        item
        for item in model["files"]
        if item["path"].endswith("/rl_agent_config.json")
    )
    data = (LAYA_CONFIGS / f"{model_id}.json").read_bytes()
    if (
        len(data) != entry["size"]
        or hashlib.sha256(data).hexdigest() != entry["sha256"]
    ):
        raise AssetError("config_integrity_failed")
    return json.loads(data)


def _model(model_id: str) -> tuple[dict, str]:
    manifest = read_manifest()
    if (
        model_id not in {"laya", "laya-multilingual"}
        or model_id not in manifest["models"]
    ):
        raise AssetError("unknown_model")
    model = manifest["models"][model_id]
    digest = hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest()
    return model, digest


def _path(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise AssetError("invalid_asset_path")
    target = root / path
    # Check every descendant explicitly (parents above root are outside our store).
    current = root
    for part in path.parts:
        current /= part
        if current.is_symlink():
            raise AssetError("symlink_asset_path")
    return target


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_assets(model_id: str, root: Path, *, deep: bool = False) -> tuple[bool, str]:
    """A marker alone is insufficient; all pinned files must still exist."""
    try:
        model, digest = _model(model_id)
        root = Path(root)
        if root.is_symlink() or not root.is_dir() or root.stat().st_mode & 0o077:
            return False, "assets_missing_or_not_private"
        marker = _path(root, f".complete-{model_id}.json")
        if json.loads(marker.read_text()) != {"manifest_sha256": digest}:
            return False, "assets_incomplete"
        for item in model["files"]:
            target = _path(root, item["path"])
            if not target.is_file() or target.stat().st_size != item["size"]:
                return False, "assets_incomplete"
            if deep and _sha256(target) != item["sha256"]:
                return False, "assets_integrity_failed"
        return True, "ready"
    except (OSError, ValueError, KeyError, AssetError):
        return False, "assets_incomplete"


def open_url(url: str):
    """Public files only; no authorization headers, shell commands or remote code."""
    if not url.startswith(
        ("https://huggingface.co/", "https://raw.githubusercontent.com/")
    ):
        raise AssetError("invalid_asset_origin")
    # https only, from the two origins checked above (bandit B310 is about file:/ and
    # custom schemes, which never reach this line).
    return urllib.request.urlopen(urllib.request.Request(url), timeout=30)  # nosec B310


def download_assets(
    model_id: str, root: Path, *, max_file_bytes: int | None = None
) -> None:
    """Only called by an explicit operator command, never by inference."""
    model, digest = _model(model_id)
    root = Path(root).absolute()
    if root.is_symlink():
        raise AssetError("symlink_asset_path")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_mode & 0o077:
        raise AssetError("asset_directory_not_private")
    for item in model["files"]:
        _path(root, item["path"])
    if max_file_bytes is not None and any(
        f["size"] > max_file_bytes for f in model["files"]
    ):
        raise AssetError("download_budget_exceeded")
    required = sum(f["size"] for f in model["files"])
    if shutil.disk_usage(root).free < required + RESERVE_BYTES:
        raise AssetError("insufficient_disk_reserve")
    marker = _path(root, f".complete-{model_id}.json")
    marker.unlink(missing_ok=True)
    try:
        for item in model["files"]:
            target = _path(root, item["path"])
            if (
                target.is_file()
                and target.stat().st_size == item["size"]
                and _sha256(target) == item["sha256"]
            ):
                continue
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            partial = _path(root, item["path"] + ".partial")
            partial.unlink(missing_ok=True)
            count, checksum = 0, hashlib.sha256()
            try:
                fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as output, open_url(item["url"]) as source:
                    while chunk := source.read(1024 * 1024):
                        count += len(chunk)
                        if count > item["size"]:
                            raise AssetError("asset_integrity_failed")
                        checksum.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if count != item["size"] or checksum.hexdigest() != item["sha256"]:
                    raise AssetError("asset_integrity_failed")
                os.replace(partial, target)
            finally:
                partial.unlink(missing_ok=True)
        temporary = _path(root, f".complete-{model_id}.json.partial")
        temporary.write_text(json.dumps({"manifest_sha256": digest}), encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, marker)
    except AssetError:
        raise
    except Exception:
        raise AssetError("asset_download_failed") from None
