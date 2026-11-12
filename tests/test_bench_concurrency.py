"""PLU-17: scripts/bench_concurrency.py, run against a fake gateway on
127.0.0.1 (no inference, no real gateway). The fake serves one chat at a
time: the others wait up to ``wait_s`` for the slot, then get the gateway's
503 ``providers_busy``."""

import importlib.util
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
KEY = "pb_bench_fake_key_0123456789"
ENV = "PLUGBOARD_BENCH_TEST_KEY"


def _load():
    path = ROOT / "scripts" / "bench_concurrency.py"
    spec = importlib.util.spec_from_file_location("bench_concurrency", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bench = _load()


class FakeGateway:
    """One slot; ``words`` streamed ``gap_s`` apart (longer than the wait, so
    the others are refused); ``status`` answers every request instead."""

    def __init__(self, wait_s=0.3, words=5, gap_s=0.1, status=None, refuse_after=None):
        self.wait_s, self.words, self.gap_s = wait_s, words, gap_s
        self.status, self.refuse_after = status, refuse_after
        self.slot = threading.Semaphore(1)
        self.seen = []
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                gateway.seen.append(
                    (self.path, self.headers.get("Authorization"), body)
                )
                gateway.handle(self, body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def handle(self, h, body):
        if self.status is not None:
            self._json(h, self.status, {"detail": "boom"})
            return
        if not self.slot.acquire(timeout=self.wait_s):
            time.sleep(self.refuse_after or 0)
            self._json(
                h,
                503,
                {"detail": "busy", "code": "providers_busy", "retry_after": 5},
                {"Retry-After": "5"},
            )
            return
        try:
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.end_headers()
            self._event(
                h, {"choices": [{"delta": {"role": "assistant", "content": ""}}]}
            )
            for i in range(self.words):
                time.sleep(self.gap_s)
                self._event(h, {"choices": [{"delta": {"content": f"w{i} "}}]})
            self._event(
                h,
                {
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": self.words},
                },
            )
            h.wfile.write(b"data: [DONE]\n\n")
        finally:
            self.slot.release()

    @staticmethod
    def _event(h, evt):
        h.wfile.write(f"data: {json.dumps(evt)}\n\n".encode())
        h.wfile.flush()

    @staticmethod
    def _json(h, status, payload, headers=None):
        raw = json.dumps(payload).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(raw)))
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(raw)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    return KEY


def _run(gateway, *extra, clients="1,2,4,8"):
    argv = [
        "--url",
        gateway.url,
        "--key-env",
        ENV,
        "--model",
        "local:model",
        "--clients",
        clients,
        *extra,
    ]
    return bench.main(argv)


def test_levels_report_codes_times_and_first_word(key, tmp_path, capsys):
    out = tmp_path / "bench.json"
    with FakeGateway() as gw:
        rc = _run(gw, "--json", str(out), "--check", "--max-refusal-s", "2")
    printed = capsys.readouterr().out
    assert rc == 0, printed
    report = json.loads(out.read_text())
    levels = {lvl["clients"]: lvl for lvl in report["levels"]}
    assert sorted(levels) == [1, 2, 4, 8]
    assert levels[1]["codes"] == {"200": 1}
    # One slot, a generation longer than the wait: one served, the rest refused.
    assert levels[8]["codes"] == {"200": 1, "503 providers_busy": 7}
    served = [r for r in levels[8]["requests"] if r["status"] == 200]
    assert served[0]["first_word_s"] > 0 and served[0]["completion_tokens"] == 5
    assert served[0]["tokens_per_second"] > 0
    refused = [r for r in levels[8]["requests"] if r["status"] == 503]
    assert all(r["retry_after"] == "5" for r in refused)
    assert all(0.25 <= r["status_s"] < 2 for r in refused)
    assert levels[8]["first_word_s"]["p95"] is not None
    assert "503 providers_busy" in printed and "first word" in printed
    assert report["check"] == {"passed": True, "failures": []}


def test_each_request_is_a_fresh_streamed_chat(key):
    with FakeGateway() as gw:
        _run(gw, "--rounds", "2", "--max-tokens", "7", clients="2")
    paths = {p for p, _, _ in gw.seen}
    assert paths == {"/v1/chat/completions"}
    bodies = [b for _, _, b in gw.seen]
    assert len(bodies) == 4
    assert all(b["stream"] is True and b["cache"] is False for b in bodies)
    assert all(b["model"] == "local:model" and b["max_tokens"] == 7 for b in bodies)
    prompts = [b["messages"][0]["content"] for b in bodies]
    assert len(set(prompts)) == 4  # no reply can come from a cache


def test_the_key_is_sent_but_never_shown(key, tmp_path, capsys):
    out = tmp_path / "bench.json"
    with FakeGateway(status=401) as gw:
        rc = _run(gw, "--json", str(out), "--check", clients="1")
    assert {auth for _, auth, _ in gw.seen} == {f"Bearer {KEY}"}
    captured = capsys.readouterr()
    assert rc == 1
    assert KEY not in captured.out + captured.err
    assert KEY not in out.read_text()


def test_missing_key_stops_before_any_call(monkeypatch, capsys):
    monkeypatch.delenv(ENV, raising=False)
    with FakeGateway() as gw:
        rc = _run(gw, clients="1")
    assert rc == 2 and gw.seen == []
    assert ENV in capsys.readouterr().err


@pytest.mark.parametrize("url", ["ftp://127.0.0.1", "127.0.0.1:8765", "http://"])
def test_bad_url_is_refused(key, url, capsys):
    rc = bench.main(["--url", url, "--key-env", ENV, "--model", "m"])
    assert rc == 2


def test_check_fails_on_another_status(key, capsys):
    with FakeGateway(status=500) as gw:
        rc = _run(gw, "--check", clients="2")
    assert rc == 1
    assert "500" in capsys.readouterr().out


def test_check_fails_on_a_refusal_slower_than_the_bound(key, capsys):
    with FakeGateway(wait_s=0.05, refuse_after=0.4) as gw:
        rc = _run(gw, "--check", "--max-refusal-s", "0.2", clients="2")
    assert rc == 1
    assert "0.2" in capsys.readouterr().out


def test_without_check_a_failure_still_exits_zero(key):
    with FakeGateway(status=500) as gw:
        assert _run(gw, clients="1") == 0


def test_d26_thresholds_are_optional_checks(key, capsys):
    with FakeGateway(words=5, gap_s=0.03) as gw:
        rc = _run(
            gw, "--first-word-p95-max", "5", "--tps-median-min", "1000", clients="1"
        )
    assert rc == 1  # ~33 tokens/s, under 1000
    assert "tok/s" in capsys.readouterr().out


def test_unreachable_gateway_is_status_zero(key, capsys):
    with FakeGateway() as gw:
        url = gw.url
    rc = bench.main(
        ["--url", url, "--key-env", ENV, "--model", "m", "--clients", "1", "--check"]
    )
    assert rc == 1
    assert "connection" in capsys.readouterr().out


def test_percentiles():
    assert bench.percentile([], 0.95) is None
    assert bench.percentile([3.0], 0.5) == 3.0
    assert bench.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.95) == 10
    assert bench.percentile([1, 2, 3, 4], 0.5) == 2
