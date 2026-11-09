"""A subscription CLI that runs but is signed out is not healthy."""

from __future__ import annotations

import json

import pytest

import plugboard.cli_subprocess as cs
from plugboard import health_probe


def _fake_status(rc, out="", err=""):
    async def run(argv, kind, timeout_s):
        return rc, out, err

    return run


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind, rc, out, expected",
    [
        ("claude", 0, json.dumps({"loggedIn": True}), True),
        ("claude", 1, json.dumps({"loggedIn": False}), False),
        ("codex", 0, "Logged in using ChatGPT", True),
        ("codex", 1, "Not logged in", False),
    ],
)
async def test_probe_cli_signed_in(monkeypatch, kind, rc, out, expected):
    monkeypatch.setattr(cs.shutil, "which", lambda c: "/usr/local/bin/" + c)
    monkeypatch.setattr(cs, "_run_status", _fake_status(rc, out))
    res = await cs.probe_cli_signed_in(kind)
    assert res["signed_in"] is expected
    if not expected:
        assert "login" in res["error"]


@pytest.mark.asyncio
async def test_unknown_or_failing_check_is_not_a_verdict(monkeypatch):
    assert (await cs.probe_cli_signed_in("agy"))["signed_in"] is None

    async def boom(argv, kind, timeout_s):
        raise TimeoutError("slow")

    monkeypatch.setattr(cs.shutil, "which", lambda c: "/usr/local/bin/" + c)
    monkeypatch.setattr(cs, "_run_status", boom)
    assert (await cs.probe_cli_signed_in("codex"))["signed_in"] is None


@pytest.mark.asyncio
async def test_health_reports_signed_out_cli(monkeypatch):
    monkeypatch.setattr(cs, "check_cli_installed", lambda kind: {"ok": True})

    async def version(kind, timeout_s=5.0):
        return {"ok": True, "version": "0.163.0", "error": None}

    async def signed_out(kind, timeout_s=10.0):
        return {"signed_in": False, "error": cs.CODEX_SIGN_IN_HINT}

    monkeypatch.setattr(cs, "probe_cli_version", version)
    monkeypatch.setattr(cs, "probe_cli_signed_in", signed_out)
    res = await health_probe._probe_subprocess("subprocess:codex")
    assert res["ok"] is False
    assert res["status"] == "signed_out"
    assert "codex login" in res["error"]


@pytest.mark.asyncio
async def test_health_keeps_running_when_check_is_inconclusive(monkeypatch):
    monkeypatch.setattr(cs, "check_cli_installed", lambda kind: {"ok": True})

    async def version(kind, timeout_s=5.0):
        return {"ok": True, "version": "1.4.4", "error": None}

    async def unknown(kind, timeout_s=10.0):
        return {"signed_in": None, "error": ""}

    monkeypatch.setattr(cs, "probe_cli_version", version)
    monkeypatch.setattr(cs, "probe_cli_signed_in", unknown)
    res = await health_probe._probe_subprocess("subprocess:agy")
    assert res["ok"] is True and res["status"] == "running"
