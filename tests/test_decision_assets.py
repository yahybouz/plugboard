"""Asset supply-chain tests use tiny fake HTTP bytes, never model weights."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest


def assets_module():
    import plugboard.decision_assets as assets

    return assets


def tiny_manifest(monkeypatch):
    assets = assets_module()
    payload = b"official-fixture-bytes"
    item = {
        "path": "hub/models--fixture/snapshots/" + "a" * 40 + "/config.json",
        "url": "https://huggingface.co/fixture/model/resolve/"
        + "a" * 40
        + "/config.json",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }
    manifest = {"version": 1, "models": {"laya": {"files": [item], "runtime": "laya"}}}
    monkeypatch.setattr(assets, "read_manifest", lambda: manifest)
    monkeypatch.setattr(assets, "open_url", lambda url: io.BytesIO(payload))
    monkeypatch.setattr(
        assets.shutil,
        "disk_usage",
        lambda _: type("Disk", (), {"free": 20 * 1024**3})(),
    )
    return assets, item, payload


def test_manifest_pins_complete_official_dependencies():
    manifest = assets_module().read_manifest()
    assert set(manifest["models"]) == {"laya", "laya-multilingual"}
    for model in manifest["models"].values():
        assert len(model["revision"]) == 40
        assert len(model["source_revision"]) == 40
        paths = [f["path"] for f in model["files"]]
        assert any(p.endswith("tokenizer.json") for p in paths)
        assert any(p.endswith(".safetensors") for p in paths)
        for item in model["files"]:
            assert len(item["sha256"]) == 64
            assert item["size"] >= 0
            assert item["url"].startswith(
                ("https://huggingface.co/", "https://raw.githubusercontent.com/")
            )
            assert not Path(item["path"]).is_absolute()
            assert ".." not in Path(item["path"]).parts
    for model in manifest["models"].values():
        paths = [item["path"] for item in model["files"]]
        assert model["runtime"] == "laya"
        assert any(p.endswith("/rl_agent_config.json") for p in paths)
        assert any(p.endswith("/encoder/config.json") for p in paths)
        assert any(p.endswith("/laya/agent.py") for p in paths)
        assert not any(
            "Qwen" in p or "/kev/" in p or p.endswith("/head.pt") for p in paths
        )


def test_explicit_download_marks_complete_and_detects_tampering(monkeypatch, tmp_path):
    assets, item, _ = tiny_manifest(monkeypatch)
    root = tmp_path / "private-assets"
    assert assets.verify_assets("laya", root)[0] is False
    assets.download_assets("laya", root)
    assert root.stat().st_mode & 0o077 == 0
    assert assets.verify_assets("laya", root, deep=True) == (True, "ready")
    target = root / item["path"]
    target.write_bytes(b"x" * item["size"])
    assert assets.verify_assets("laya", root, deep=True)[0] is False


def test_corrupt_download_never_marks_complete(monkeypatch, tmp_path):
    assets, _, _ = tiny_manifest(monkeypatch)
    monkeypatch.setattr(assets, "open_url", lambda _: io.BytesIO(b"corrupted"))
    with pytest.raises(assets.AssetError, match="integrity"):
        assets.download_assets("laya", tmp_path / "private-assets")
    assert assets.verify_assets("laya", tmp_path / "private-assets")[0] is False


def test_download_refuses_disk_reserve_before_network(monkeypatch, tmp_path):
    assets, _, _ = tiny_manifest(monkeypatch)
    monkeypatch.setattr(
        assets.shutil, "disk_usage", lambda _: type("Disk", (), {"free": 6 * 1024**3})()
    )
    with pytest.raises(assets.AssetError, match="disk"):
        assets.download_assets("laya", tmp_path / "private-assets")


def test_download_size_budget_prevents_large_download(monkeypatch, tmp_path):
    assets, _, _ = tiny_manifest(monkeypatch)
    with pytest.raises(assets.AssetError, match="budget"):
        assets.download_assets("laya", tmp_path / "private-assets", max_file_bytes=1)


def test_symlink_destination_and_unknown_model_refused(monkeypatch, tmp_path):
    assets, _, _ = tiny_manifest(monkeypatch)
    root = tmp_path / "assets"
    root.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(assets.AssetError):
        assets.download_assets("laya", root)
    with pytest.raises(assets.AssetError):
        assets.download_assets("../outside", tmp_path / "safe")


def test_marker_does_not_hide_missing_assets(monkeypatch, tmp_path):
    assets, item, _ = tiny_manifest(monkeypatch)
    root = tmp_path / "assets"
    assets.download_assets("laya", root)
    (root / item["path"]).unlink()
    assert assets.verify_assets("laya", root)[0] is False


def test_manifest_rejects_user_controlled_paths(monkeypatch, tmp_path):
    assets, item, _ = tiny_manifest(monkeypatch)
    item["path"] = "../escape"
    with pytest.raises(assets.AssetError):
        assets.download_assets("laya", tmp_path / "assets")
    assert not (tmp_path / "escape").exists()


def test_removed_kev_download_is_rejected_before_creating_store(tmp_path):
    assets = assets_module()
    root = tmp_path / "removed-assets"
    with pytest.raises(assets.AssetError, match="unknown_model"):
        assets.download_assets("kev-0.8b", root, max_file_bytes=0)
    assert not root.exists()


def test_removed_kev_assets_cannot_be_reenabled_by_stale_manifest(
    monkeypatch, tmp_path
):
    assets, _, _ = tiny_manifest(monkeypatch)
    stale = {"models": {"kev-0.8b": assets.read_manifest()["models"]["laya"]}}
    monkeypatch.setattr(assets, "read_manifest", lambda: stale)
    with pytest.raises(assets.AssetError, match="unknown_model"):
        assets.download_assets("kev-0.8b", tmp_path / "removed-assets")
