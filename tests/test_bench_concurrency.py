"""PLU-17: scripts/bench_concurrency.py, run against a fake gateway on
127.0.0.1 (no inference, no real gateway). The fake serves one chat at a
time and refuses the others with the gateway's 503 ``providers_busy``.

Nothing here depends on real time, so a loaded machine changes no result:
the script's clock is simulated (each reading advances ``FakeClock.step``),
and the fake orders its answers with events, never with delays."""

import importlib.util
import json
import re
import threading
import time
import types
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


class FakeClock:
    """``time.perf_counter`` of the script: each reading is ``step`` later."""

    def __init__(self, step=0.1):
        self.step = step
        self.now = 0.0

    def __call__(self):
        self.now += self.step
        return self.now


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(
        bench, "time", types.SimpleNamespace(perf_counter=fake, strftime=time.strftime)
    )
    return fake


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # a client that left mid-answer (timeout tests)


class FakeGateway:
    """One slot: a chat that comes while another holds it is refused at once.
    With ``hold``, the chat that takes the slot keeps it until every other
    chat of its level (the "N clients" of its prompt) has been refused, so
    exactly one is served per level, whatever the order of arrival.
    ``mode`` changes every answer:

    - ``status``: answer ``status`` with a JSON body;
    - ``html``: a 200 login page; ``cut``: two words then the connection
      closes; ``error``: an error event, then ``[DONE]``;
    - ``hang``: no answer until the gateway stops;
    - ``redirect``: a 307 to another path;
    - ``echo``: a 429 that repeats the key in its code and Retry-After.
    """

    def __init__(self, words=5, mode=None, status=500, hold=False):
        self.words, self.mode, self.status, self.hold = words, mode, status, hold
        self.cond = threading.Condition()
        self.busy = False
        self.refused = 0
        self.stopped = threading.Event()
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

        self.server = _Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}
        )

    def handle(self, h, body):
        auth = h.headers.get("Authorization", "")
        if self.mode == "status":
            return self._json(h, self.status, {"detail": "boom"})
        if self.mode == "echo":
            key = auth.removeprefix("Bearer ")
            return self._json(h, 429, {"code": key}, {"Retry-After": key})
        if self.mode == "redirect":
            return self._json(h, 307, {}, {"Location": "/elsewhere"})
        if self.mode == "html":
            raw = b"<html>sign in</html>"
            h.send_response(200)
            h.send_header("Content-Type", "text/html")
            h.send_header("Content-Length", str(len(raw)))
            h.end_headers()
            h.wfile.write(raw)
            return
        if self.mode == "hang":
            self.stopped.wait(30)
            return
        with self.cond:
            served = not self.busy
            if served:
                self.busy, self.refused = True, 0
            else:
                self.refused += 1
                self.cond.notify_all()
        if not served:
            busy = {"detail": "busy", "code": "providers_busy", "retry_after": 5}
            return self._json(h, 503, busy, {"Retry-After": "5"})
        try:
            if self.hold:
                prompt = body["messages"][0]["content"]
                level = re.search(r"(\d+) clients", prompt)
                others = int(level.group(1)) - 1 if level else 0
                with self.cond:  # the timeout only guards against a hang
                    assert self.cond.wait_for(lambda: self.refused >= others, 30)
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.end_headers()
            self._event(
                h, {"choices": [{"delta": {"role": "assistant", "content": ""}}]}
            )
            if self.mode == "error":
                self._event(h, {"error": {"message": "provider failed"}})
                h.wfile.write(b"data: [DONE]\n\n")
                return
            words = 2 if self.mode == "cut" else self.words
            for i in range(words):
                self._event(h, {"choices": [{"delta": {"content": f"w{i} "}}]})
            if self.mode == "cut":
                return  # HTTP/1.0: the closed connection ends the body
            self._event(
                h,
                {
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": self.words},
                },
            )
            h.wfile.write(b"data: [DONE]\n\n")
        finally:
            with self.cond:
                self.busy = False

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
        self.stopped.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    return KEY


def _run(gateway, *extra, clients="1,2,4,8", report=None):
    argv = ["--url", gateway.url, "--key-env", ENV, "--model", "local:model"]
    argv += ["--clients", clients, *extra]
    if report is not None:
        argv += ["--json", str(report)]
    return bench.main(argv)


def _report(path):
    return json.loads(path.read_text())


def _failures(path):
    return _report(path)["check"]["failures"]


def test_levels_report_codes_times_and_first_word(key, tmp_path, capsys):
    out = tmp_path / "bench.json"
    with FakeGateway(hold=True) as gw:
        rc = _run(gw, "--check", report=out)
    printed = capsys.readouterr().out
    assert rc == 0, printed
    levels = {lvl["clients"]: lvl for lvl in _report(out)["levels"]}
    assert sorted(levels) == [1, 2, 4, 8]
    for n, level in levels.items():  # one slot: one served, the rest refused
        expected = {"200": 1, "503 providers_busy": n - 1} if n > 1 else {"200": 1}
        assert level["codes"] == expected
    [served] = [r for r in levels[8]["requests"] if r["status"] == 200]
    assert 0 < served["status_s"] < served["first_word_s"] < served["total_s"]
    assert served["completion_tokens"] == 5
    # 4 tokens after the first, over at least 4 readings of 0.1 s.
    assert 0 < served["tokens_per_second"] <= 10.0 + 1e-9
    refused = [r for r in levels[8]["requests"] if r["status"] == 503]
    assert all(r["retry_after"] == "5" and r["status_s"] > 0 for r in refused)
    assert sorted({(r["client"], r["round"]) for r in levels[8]["requests"]}) == [
        (c, 1) for c in range(1, 9)
    ]
    assert "503 providers_busy x7" in printed and "first word" in printed
    assert _report(out)["check"] == {"passed": True, "failures": []}


def test_each_request_is_a_fresh_streamed_chat(key):
    with FakeGateway() as gw:
        _run(gw, "--rounds", "2", "--max-tokens", "7", "--warmup", "1", clients="2")
    paths = {p for p, _, _ in gw.seen}
    assert paths == {"/v1/chat/completions"}
    bodies = [b for _, _, b in gw.seen]
    assert len(bodies) == 5  # one warm-up, then 2 clients x 2 rounds
    assert all(b["stream"] is True and b["cache"] is False for b in bodies)
    assert all(b["model"] == "local:model" and b["max_tokens"] == 7 for b in bodies)
    prompts = [b["messages"][0]["content"] for b in bodies]
    assert len(set(prompts)) == 5  # no reply can come from a cache
    assert "warm-up" in prompts[0]


@pytest.mark.parametrize("mode", ["status", "echo"])
def test_the_key_is_sent_but_never_shown(key, tmp_path, capsys, mode):
    out = tmp_path / "bench.json"
    with FakeGateway(mode=mode, status=401) as gw:
        rc = _run(gw, "--check", clients="1", report=out)
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


@pytest.mark.parametrize(
    "argv",
    [
        ["--url", "ftp://127.0.0.1"],
        ["--url", "127.0.0.1:8765"],
        ["--url", "http://"],
        ["--url", "http://u:p@127.0.0.1"],
        ["--url", "http://127.0.0.1", "--timeout", "nan"],
        ["--url", "http://127.0.0.1", "--max-wait-s", "-1"],
        ["--url", "http://127.0.0.1", "--clients", "0"],
        ["--url", "http://127.0.0.1", "--rounds", "0"],
    ],
)
def test_bad_arguments_are_refused(key, argv, capsys):
    assert bench.main([*argv, "--key-env", ENV, "--model", "m"]) == 2


def test_the_key_never_goes_over_plain_http_to_another_host(key, capsys):
    rc = bench.main(["--url", "http://192.0.2.1", "--key-env", ENV, "--model", "m"])
    assert rc == 2
    assert "--allow-http" in capsys.readouterr().err


def test_an_unwritable_report_stops_before_any_call(key, tmp_path, capsys):
    with FakeGateway() as gw:
        rc = _run(gw, clients="1", report=tmp_path / "missing" / "bench.json")
    assert rc == 3 and gw.seen == []


def test_check_fails_on_another_status(key, tmp_path):
    out = tmp_path / "bench.json"
    with FakeGateway(mode="status", status=500) as gw:
        assert _run(gw, "--check", clients="2", report=out) == 1
    assert _failures(out) == [
        "2 clients: answers other than 200 and 503 providers_busy: {'500': 2}"
    ]


def test_check_fails_on_a_refusal_slower_than_the_bound(key, tmp_path, clock):
    out = tmp_path / "bench.json"
    clock.step = 1.0  # a refusal takes at least one reading: 1 s
    with FakeGateway(hold=True) as gw:
        rc = _run(gw, "--check", "--max-wait-s", "0.5", clients="2", report=out)
    assert rc == 1
    # The 200 comes within 0.5 + 5 s: at most 4 readings after its start.
    [line] = _failures(out)
    assert line.startswith("2 clients: a 503 providers_busy came after")
    assert line.endswith("(bound 0.5 s)")


def test_check_fails_on_a_late_200(key, tmp_path, clock):
    """A gateway that queues without bound answers 200, but late."""
    out = tmp_path / "bench.json"
    clock.step = 1.0  # the status comes one reading, 1 s, after the start
    with FakeGateway() as gw:
        rc = _run(
            gw,
            "--check",
            "--max-wait-s",
            "0.1",
            "--max-first-chunk-s",
            "0.1",
            clients="1",
            report=out,
        )
    assert rc == 1
    assert _failures(out) == ["1 clients: a 200 came after 1.00 s (bound 0.2 s)"]


@pytest.mark.parametrize(
    "mode, error",
    [
        ("html", "200 without an event stream"),
        ("cut", "stream ended without [DONE]"),
        ("error", "error event in the stream"),
    ],
)
def test_a_200_without_a_full_answer_is_a_stream_error(key, tmp_path, mode, error):
    out = tmp_path / "bench.json"
    with FakeGateway(mode=mode) as gw:
        assert _run(gw, "--check", clients="1", report=out) == 1
    [level] = _report(out)["levels"]
    assert level["codes"] == {"200 stream error": 1}
    assert level["requests"][0]["error"] == error


def test_a_redirect_is_not_followed(key, tmp_path):
    out = tmp_path / "bench.json"
    with FakeGateway(mode="redirect") as gw:
        assert _run(gw, "--check", clients="1", report=out) == 1
    assert len(gw.seen) == 1
    assert _report(out)["levels"][0]["codes"] == {"307": 1}


def test_a_chat_over_its_timeout_is_no_answer(key, tmp_path):
    out = tmp_path / "bench.json"
    with FakeGateway(mode="hang") as gw:  # never answers while the bench runs
        rc = _run(gw, "--check", "--timeout", "0.2", clients="1", report=out)
    assert rc == 1
    [req] = _report(out)["levels"][0]["requests"]
    assert (req["status"], req["error"]) == (0, "timeout after 0.2 s")


def test_without_check_a_failure_still_exits_zero(key):
    with FakeGateway(mode="status", status=500) as gw:
        assert _run(gw, clients="1") == 0


def test_d26_targets_need_every_chat_served(key, tmp_path):
    out = tmp_path / "bench.json"
    with FakeGateway(hold=True) as gw:  # one slot: 3 of 4 clients refused
        rc = _run(
            gw, "--first-word-p95-max", "5", "--tps-min", "1", clients="4", report=out
        )
    assert rc == 1
    failures = _failures(out)
    assert "4 clients: 3/4 chats not served" in failures
    # Any of the 4 clients may be the one served.
    assert sum(f.endswith("has no rate measured") for f in failures) == 3


def test_d26_targets_check_each_client_rate(key, tmp_path, capsys):
    out = tmp_path / "bench.json"
    # Readings 0.1 s apart: start, status, 5 words, end. 4 tokens after the
    # first word, over 0.4 s: 10 tokens per second.
    with FakeGateway() as gw:
        rc = _run(gw, "--targets-at", "1", "--tps-min", "1000", clients="1", report=out)
    assert rc == 1
    assert _failures(out) == ["1 clients: client 1 at 10.0 tok/s < 1000.0 tok/s"]
    assert "check: FAILED" in capsys.readouterr().out


def test_d26_targets_need_their_level(key, tmp_path):
    out = tmp_path / "bench.json"
    with FakeGateway() as gw:
        assert _run(gw, "--tps-min", "1", clients="1", report=out) == 1
    assert _failures(out) == ["targets: no level at 4 clients"]


def test_unreachable_gateway_is_no_answer(key, tmp_path):
    with FakeGateway() as gw:
        url = gw.url
    out = tmp_path / "bench.json"
    argv = ["--url", url, "--key-env", ENV, "--model", "m", "--clients", "1"]
    assert bench.main([*argv, "--check", "--json", str(out)]) == 1
    [req] = _report(out)["levels"][0]["requests"]
    assert req["status"] == 0 and req["error"].startswith("connection failed")


def test_words_in_a_burst_give_no_rate(key, tmp_path, clock):
    out = tmp_path / "bench.json"
    clock.step = 0.001  # 5 words within 4 ms, under the 50 ms floor
    with FakeGateway() as gw:
        assert _run(gw, clients="1", report=out) == 0
    [req] = _report(out)["levels"][0]["requests"]
    assert req["first_word_s"] is not None and req["tokens_per_second"] is None


def test_percentiles():
    assert bench.percentile([], 0.95) is None
    assert bench.percentile([3.0], 0.5) == 3.0
    assert bench.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.95) == 10
    assert bench.percentile([1, 2, 3, 4], 0.5) == 2
