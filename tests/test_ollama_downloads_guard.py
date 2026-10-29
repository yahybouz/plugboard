"""Local model downloads switch, disk guard and cloud catalogue (admin, no network)."""

from __future__ import annotations

import json
import re
from collections import namedtuple

import pytest
from aioresponses import aioresponses

from plugboard import ollama, ollama_admin

OLLAMA_URL = "http://localhost:11434"
ADMIN = {"Authorization": "Bearer adminkey"}
GB = 1024**3
_Usage = namedtuple("_Usage", "total used free")
REGISTRY = re.compile(r"^https://registry\.ollama\.ai/v2/.*")


@pytest.fixture
def guard_env(tmp_path, monkeypatch):
    """Settings file in tmp, env cleared, disk free = 20 GB, margin 5 GB."""
    monkeypatch.setattr(
        ollama_admin, "_settings_path", lambda: tmp_path / "ollama_settings.json"
    )
    monkeypatch.delenv(ollama_admin.DOWNLOADS_ENV, raising=False)
    monkeypatch.setenv(ollama_admin.MIN_FREE_ENV, "5")
    monkeypatch.setenv(ollama_admin.DISK_PATH_ENV, str(tmp_path))
    state = {"free": 20 * GB}
    monkeypatch.setattr(
        ollama_admin.shutil,
        "disk_usage",
        lambda _p: _Usage(100 * GB, 100 * GB - state["free"], state["free"]),
    )
    return state


def _manifest(*sizes, config=500):
    return {
        "schemaVersion": 2,
        "config": {"digest": "sha256:c", "size": config},
        "layers": [{"digest": f"sha256:{i}", "size": s} for i, s in enumerate(sizes)],
    }


def _lines(text):
    return [json.loads(line) for line in text.strip().split("\n") if line]


# -- Pure helpers -------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "llama3",
        "llama3:8b",
        "qwen3-embedding:0.6b",
        "user/model:tag",
        "hf.co/u/m:Q4_K_M",
        "gpt-oss:120b-cloud",
        "nemotron-3-super:cloud",
    ],
)
def test_valid_model_names(name):
    assert ollama_admin.validate_model_name(f" {name} ") == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        " ",
        "../etc/passwd",
        "a b",
        "x;rm -rf",
        "a:b:c",
        "-lead",
        "a/b/c/d",
        "m:" + "t" * 200,
        "x" * 300,
        "na\nme",
        "http://evil/x",
    ],
)
def test_invalid_model_names(name):
    with pytest.raises(ValueError):
        ollama_admin.validate_model_name(name)


@pytest.mark.parametrize(
    ("name", "cloud"),
    [
        ("gpt-oss:120b-cloud", True),
        ("nemotron-3-super:cloud", True),
        ("x:CLOUD", True),
        ("llama3", False),
        ("cloud", False),
        ("cloudy:8b", False),
        ("m:cloud-8b", False),
    ],
)
def test_cloud_ids(name, cloud):
    assert ollama_admin.is_cloud_id(name) is cloud


def test_manifest_urls():
    base = "https://registry.ollama.ai/v2"
    assert (
        ollama_admin.manifest_url("llama3") == f"{base}/library/llama3/manifests/latest"
    )
    assert (
        ollama_admin.manifest_url("llama3:8b") == f"{base}/library/llama3/manifests/8b"
    )
    assert ollama_admin.manifest_url("user/m:q4") == f"{base}/user/m/manifests/q4"
    assert ollama_admin.manifest_url("hf.co/u/m:q4") is None


async def test_manifest_size_sums_layers_and_config():
    with aioresponses() as m:
        m.get(REGISTRY, payload=_manifest(1000, 2000, config=24))
        assert await ollama_admin.resolve_manifest_size("llama3:8b") == 3024
    with aioresponses() as m:
        m.get(REGISTRY, status=404)
        assert await ollama_admin.resolve_manifest_size("nope") is None
    with aioresponses() as m:
        m.get(REGISTRY, payload={"layers": [{"size": "big"}]})
        assert await ollama_admin.resolve_manifest_size("weird") is None


# -- Switch: persistence and precedence ---------------------------------------------


def test_switch_default_env_and_admin_override(guard_env, monkeypatch, tmp_path):
    assert ollama_admin.downloads_state() == (True, "env")
    monkeypatch.setenv(ollama_admin.DOWNLOADS_ENV, "0")
    assert ollama_admin.downloads_state() == (False, "env")
    ollama_admin.set_local_downloads(True)
    assert ollama_admin.downloads_state() == (True, "admin")  # override wins
    saved = json.loads((tmp_path / "ollama_settings.json").read_text())
    assert saved["local_downloads_enabled"] is True
    monkeypatch.setenv(ollama_admin.DOWNLOADS_ENV, "1")
    ollama_admin.set_local_downloads(False)
    assert ollama_admin.downloads_state() == (False, "admin")


def test_unreadable_override_falls_back_to_env(guard_env, tmp_path, monkeypatch):
    (tmp_path / "ollama_settings.json").write_text("{not json")
    monkeypatch.setenv(ollama_admin.DOWNLOADS_ENV, "off")
    assert ollama_admin.downloads_state() == (False, "env")


async def test_settings_endpoints_persist(admin_client, guard_env, monkeypatch):
    monkeypatch.setenv(ollama_admin.DOWNLOADS_ENV, "1")
    resp = await admin_client.get("/admin/ollama/settings", headers=ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    assert body["local_downloads_enabled"] is True and body["source"] == "env"
    assert body["min_free_gb"] == 5
    assert (
        body["disk"]["free_bytes"] == 20 * GB
        and body["disk"]["total_bytes"] == 100 * GB
    )

    resp = await admin_client.put(
        "/admin/ollama/settings", json={"local_downloads_enabled": False}, headers=ADMIN
    )
    assert resp.status_code == 200
    assert resp.json()["local_downloads_enabled"] is False
    assert resp.json()["source"] == "admin"
    # A fresh read (as after a restart) still sees the override.
    assert ollama_admin.downloads_state() == (False, "admin")


@pytest.mark.parametrize(
    "body",
    [
        {"local_downloads_enabled": "false"},
        {"local_downloads_enabled": 0},
        {},
        {"local_downloads_enabled": True, "extra": 1},
    ],
)
async def test_settings_put_is_strict(admin_client, guard_env, body):
    resp = await admin_client.put("/admin/ollama/settings", json=body, headers=ADMIN)
    assert resp.status_code == 422
    assert ollama_admin.downloads_state()[1] == "env"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/admin/ollama/settings", None),
        ("put", "/admin/ollama/settings", {"local_downloads_enabled": True}),
        ("get", "/admin/ollama/cloud-catalog", None),
    ],
)
async def test_admin_routes_need_the_admin_key(
    admin_client, guard_env, method, path, body
):
    kwargs = {"json": body} if body is not None else {}
    resp = await getattr(admin_client, method)(path, **kwargs)
    assert resp.status_code in (401, 403)
    resp = await getattr(admin_client, method)(
        path, headers={"Authorization": "Bearer testkey"}, **kwargs
    )
    assert resp.status_code in (401, 403)
    assert ollama_admin.downloads_state()[1] == "env"


# -- Pull: switch, name, pre-check --------------------------------------------------


async def test_pull_refused_when_downloads_off(admin_client, guard_env):
    ollama_admin.set_local_downloads(False)
    with aioresponses() as m:
        resp = await admin_client.post(
            "/admin/ollama/models/pull", json={"name": "llama3:8b"}, headers=ADMIN
        )
        assert not any("api/pull" in str(k[1]) for k in m.requests)
    assert resp.status_code == 403
    assert "cloud" in resp.json()["detail"].lower()


async def test_cloud_pull_allowed_when_downloads_off(admin_client, guard_env):
    ollama_admin.set_local_downloads(False)
    guard_env["free"] = 1 * GB  # below the margin: cloud ids skip the disk guard
    with aioresponses() as m:
        m.post(f"{OLLAMA_URL}/api/pull", body=b'{"status":"success"}\n')
        m.get(f"{OLLAMA_URL}/api/tags", payload={"models": []})
        resp = await admin_client.post(
            "/admin/ollama/models/pull",
            json={"name": "gpt-oss:120b-cloud"},
            headers=ADMIN,
        )
        assert not any("registry" in str(k[1]) for k in m.requests)
    assert resp.status_code == 200
    assert _lines(resp.text)[-1] == {
        "status": "complete",
        "model": "gpt-oss:120b-cloud",
    }


@pytest.mark.parametrize("name", ["../x", "a b", "x" * 300])
async def test_pull_rejects_invalid_names(admin_client, guard_env, name):
    resp = await admin_client.post(
        "/admin/ollama/models/pull", json={"name": name}, headers=ADMIN
    )
    assert resp.status_code == 422


async def test_pull_507_when_manifest_does_not_fit(admin_client, guard_env):
    # 65 GB model, 20 GB free: refused before Ollama is called.
    with aioresponses() as m:
        m.get(REGISTRY, payload=_manifest(65 * GB))
        resp = await admin_client.post(
            "/admin/ollama/models/pull", json={"name": "gpt-oss:120b"}, headers=ADMIN
        )
        assert not any("api/pull" in str(k[1]) for k in m.requests)
    assert resp.status_code == 507
    assert "disk" in resp.json()["detail"].lower()


async def test_pull_507_counts_the_margin(admin_client, guard_env):
    with aioresponses() as m:
        m.get(REGISTRY, payload=_manifest(16 * GB))  # 16 + 5 > 20
        resp = await admin_client.post(
            "/admin/ollama/models/pull", json={"name": "user/model:q4"}, headers=ADMIN
        )
    assert resp.status_code == 507


async def test_pull_proceeds_when_manifest_fits(admin_client, guard_env):
    with aioresponses() as m:
        m.get(REGISTRY, payload=_manifest(1 * GB))
        m.post(f"{OLLAMA_URL}/api/pull", body=b'{"status":"success"}\n')
        m.get(f"{OLLAMA_URL}/api/tags", payload={"models": []})
        resp = await admin_client.post(
            "/admin/ollama/models/pull", json={"name": "llama3:8b"}, headers=ADMIN
        )
    assert resp.status_code == 200
    assert _lines(resp.text)[-1]["status"] == "complete"


# -- Streaming guard --------------------------------------------------------------------


async def test_stream_guard_aborts_and_deletes(admin_client, guard_env):
    progress = b"".join(
        json.dumps(p).encode() + b"\n"
        for p in [
            {"status": "pulling manifest"},
            {
                "status": "pulling a",
                "digest": "sha256:a",
                "total": 10 * GB,
                "completed": 0,
            },
            {
                "status": "pulling b",
                "digest": "sha256:b",
                "total": 8 * GB,
                "completed": 0,
            },
            {
                "status": "pulling b",
                "digest": "sha256:b",
                "total": 8 * GB,
                "completed": GB,
            },
            {"status": "success"},
        ]
    )
    with aioresponses() as m:
        m.get(REGISTRY, status=500)  # size unknown: the stream guard applies
        m.post(f"{OLLAMA_URL}/api/pull", body=progress)
        m.delete(f"{OLLAMA_URL}/api/delete", status=200)
        resp = await admin_client.post(
            "/admin/ollama/models/pull", json={"name": "big:latest"}, headers=ADMIN
        )
        deletes = [
            k for k in m.requests if k[0] == "DELETE" and "api/delete" in str(k[1])
        ]
    assert resp.status_code == 200
    lines = _lines(resp.text)
    assert [line.get("digest") for line in lines[:2]] == [None, "sha256:a"]
    assert lines[-1]["status"] == "error" and lines[-1]["aborted"] is True
    assert "margin" in lines[-1]["error"]
    assert not any(line.get("status") in ("success", "complete") for line in lines)
    assert deletes, "partial model must be deleted"


async def test_stream_guard_counts_layers_already_on_disk():
    async def pull(_name):
        yield {"digest": "sha256:a", "total": 30 * GB, "completed": 29 * GB}
        yield {"status": "success"}

    async def delete(_name):
        raise AssertionError("must not delete")

    out = [
        p
        async for p in ollama_admin.guarded_pull(
            "m",
            free_at_start=10 * GB,
            margin=5 * GB,
            pull=pull,
            delete=delete,
            free_now=lambda: 10 * GB,
        )
    ]
    assert out[-1] == {"status": "success"}


async def test_stream_guard_stops_when_free_space_drops():
    closed = []
    deleted = []

    async def pull(_name):
        try:
            yield {"status": "pulling"}
            yield {"status": "pulling more"}
        finally:
            closed.append(True)

    async def delete(name):
        deleted.append(name)

    seen = []
    with pytest.raises(ollama_admin.PullAborted):
        async for p in ollama_admin.guarded_pull(
            "m",
            free_at_start=50 * GB,
            margin=5 * GB,
            pull=pull,
            delete=delete,
            free_now=lambda: 4 * GB,
        ):
            seen.append(p)
    assert seen[-1]["status"] == "error" and "below" in seen[-1]["error"]
    assert closed == [True] and deleted == ["m"]


# -- Cloud catalogue ----------------------------------------------------------------------


async def test_cloud_catalog_flags_installed(admin_client, guard_env, monkeypatch):
    async def fake_catalog():
        return ["gpt-oss:120b", "nemotron-3-super", "deepseek-v3.1:671b"]

    async def fake_list():
        return [{"name": "gpt-oss:120b-cloud"}, {"name": "llama3:8b"}]

    monkeypatch.setattr(ollama, "fetch_cloud_catalog", fake_catalog)
    monkeypatch.setattr(ollama, "list_models", fake_list)
    resp = await admin_client.get("/admin/ollama/cloud-catalog", headers=ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    by_id = {m["id"]: m["installed"] for m in body["models"]}
    assert by_id[ollama._to_cloud_id("gpt-oss:120b")] is True
    assert by_id[ollama._to_cloud_id("nemotron-3-super")] is False
    assert body["count"] == 3 and body["local_list_available"] is True
    assert "paid" in body["note"]


async def test_cloud_catalog_survives_ollama_down(admin_client, guard_env, monkeypatch):
    async def fake_catalog():
        return ["gpt-oss:20b"]

    async def broken_list():
        raise OSError("down")

    monkeypatch.setattr(ollama, "fetch_cloud_catalog", fake_catalog)
    monkeypatch.setattr(ollama, "list_models", broken_list)
    resp = await admin_client.get("/admin/ollama/cloud-catalog", headers=ADMIN)
    assert resp.status_code == 200
    assert resp.json()["local_list_available"] is False
    assert resp.json()["models"][0]["installed"] is False
