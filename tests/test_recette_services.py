"""scripts/recette_services.py against a fake gateway (read-only, no inference)."""

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
KEY = "adm_recette_fake_key_0123456789"


def _load():
    spec = importlib.util.spec_from_file_location(
        "recette_services", ROOT / "scripts" / "recette_services.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


recette = _load()
ALL = list(recette.EXPECTED_JOBS)


def _jobs(**states):
    return [
        {
            "id": job,
            "name": job,
            "last_run": "2026-11-14 10:00 UTC",
            "last_status": states.get(job, "success"),
        }
        for job in ALL
        if states.get(job) != "ABSENT"
    ]


class _Handler(BaseHTTPRequestHandler):
    jobs = _jobs()
    health = (200, {"ok": True, "version": "9.9.9"})
    ready = (200, {"ready": True, "checks": {}})
    methods = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        type(self).methods.append(("GET", self.path))
        if self.path == "/health":
            status, body = self.health
        elif self.path == "/health/ready":
            status, body = self.ready
        elif self.path == "/admin/jobs":
            if self.headers.get("Authorization") != f"Bearer {KEY}":
                status, body = 401, {"detail": f"denied {KEY}"}
            else:
                status, body = 200, self.jobs
        else:
            status, body = 404, {}
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):  # the recipe is read-only
        type(self).methods.append(("POST", self.path))
        self.send_response(405)
        self.end_headers()


@pytest.fixture
def gateway(monkeypatch):
    handler = type("H", (_Handler,), {"methods": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv(
        "PLUGBOARD_RECETTE_URL", f"http://127.0.0.1:{server.server_port}"
    )
    monkeypatch.setenv("PLUGBOARD_RECETTE_ADMIN_KEY", KEY)
    yield handler
    server.shutdown()
    server.server_close()


def test_all_green_exits_0_and_only_reads(gateway, capsys):
    assert recette.main([]) == 0
    out = capsys.readouterr()
    assert "recette services: OK" in out.out
    assert KEY not in out.out + out.err
    assert {m for m, _ in gateway.methods} == {"GET"}


@pytest.mark.parametrize(
    "state", ["disabled", "not_started", "stopped", "error", "stale", "never_run"]
)
def test_a_job_not_healthy_fails(gateway, state, capsys):
    gateway.jobs = _jobs(pricing_refresh=state)
    assert recette.main([]) == 1
    assert f"pricing_refresh: {state}" in capsys.readouterr().err


def test_never_run_is_accepted_on_request(gateway):
    gateway.jobs = _jobs(data_retention="never_run")
    assert recette.main(["--allow-never-run"]) == 0


def test_an_absent_job_fails(gateway, capsys):
    gateway.jobs = _jobs(model_refresh="ABSENT")
    assert recette.main([]) == 1
    assert "model_refresh: absent" in capsys.readouterr().err


def test_a_not_ready_gateway_fails(gateway, capsys):
    gateway.ready = (503, {"ready": False, "checks": {"usage_db": "fail"}})
    assert recette.main([]) == 1
    assert "usage_db" in capsys.readouterr().err


def test_a_wrong_admin_key_fails_without_echoing_it(gateway, monkeypatch, capsys):
    monkeypatch.setenv("PLUGBOARD_RECETTE_ADMIN_KEY", "wrong_key_value")
    assert recette.main([]) == 1
    out = capsys.readouterr()
    assert "/admin/jobs: HTTP 401" in out.err
    assert KEY not in out.out + out.err


def test_wait_polls_until_a_starting_job_settles(gateway, monkeypatch):
    calls = {"n": 0}
    original = recette.judge_jobs

    def judge(jobs, expected, allow):
        calls["n"] += 1
        if calls["n"] >= 2:
            gateway.jobs = _jobs()
        return original(jobs, expected, allow)

    gateway.jobs = _jobs(pricing_refresh="never_run")
    monkeypatch.setattr(recette, "judge_jobs", judge)
    monkeypatch.setattr(recette.time, "sleep", lambda s: None)
    assert recette.main(["--wait", "30"]) == 0
    assert calls["n"] >= 2


def test_usage_errors_exit_2(monkeypatch, gateway):
    monkeypatch.delenv("PLUGBOARD_RECETTE_ADMIN_KEY")
    assert recette.main([]) == 2


def test_an_unreachable_gateway_exits_2(monkeypatch):
    monkeypatch.setenv("PLUGBOARD_RECETTE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("PLUGBOARD_RECETTE_ADMIN_KEY", KEY)
    assert recette.main(["--timeout", "1"]) == 2


def test_plain_http_to_another_machine_needs_the_flag(monkeypatch):
    monkeypatch.setenv("PLUGBOARD_RECETTE_URL", "http://192.0.2.1:8765")
    monkeypatch.setenv("PLUGBOARD_RECETTE_ADMIN_KEY", KEY)
    assert recette.main([]) == 2


def test_retention_never_run_right_after_a_start_is_accepted(gateway):
    gateway.jobs = _jobs(data_retention="never_run")
    assert recette.main([]) == 0


def test_wait_does_not_spin_on_a_running_watchdog(gateway, monkeypatch):
    gateway.jobs = _jobs(provider_watchdog="running")
    sleeps = []
    monkeypatch.setattr(recette.time, "sleep", lambda s: sleeps.append(s))
    assert recette.main(["--wait", "60"]) == 0
    assert sleeps == []


def test_with_a_totp_code_the_jobs_are_asked_once(gateway, monkeypatch):
    gateway.jobs = _jobs(pricing_refresh="never_run")
    monkeypatch.setenv("PLUGBOARD_RECETTE_TOTP", "123456")
    monkeypatch.setattr(recette.time, "sleep", lambda s: None)
    assert recette.main(["--wait", "60"]) == 1
    assert [p for m, p in gateway.methods if p == "/admin/jobs"] == ["/admin/jobs"]
