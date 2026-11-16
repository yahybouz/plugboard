#!/usr/bin/env python3
"""Time to the first streamed word of agy through the gateway (BUG-106).

The gateway's own chat route (``POST /v1/chat/completions``, ``stream: true``)
is driven in process, through ASGI, with a throwaway data directory and keys:
no server, no port, no network. agy is, by default, a FAKE written in shell
that answers like agy 1.4.4 in ``stream-json`` (an ``init`` event naming the
conversation, then the ``agent_response`` text, then the ``result``) and
writes in agy's store what a real call leaves there (``conversations/``,
``brain/``, ``annotations/``, ``presence/``, the shared summaries). No real
CLI is run and nothing is asked of a model.

Each call is split into:

* ``launch``     request received -> agy spawned (routing, admission, cage
                 command line, private TMPDIR, store snapshot, fork and exec);
* ``startup``    spawn -> the fake's first instruction (bwrap building the
                 cage, the shell; fake only);
* ``agy_word``   spawn -> the gateway reads agy's first word on the pipe;
* ``relay``      the gateway reads that word -> the client receives it;
* ``first_word`` request -> the client receives the first word;
* ``total``      request -> the end of the response.

Variants (``--variants``, ``;`` between variants, ``,`` between settings):

* ``purge=full``           as in service: the store purge of BUG-97 (the
                           call's entries, then the shared summaries);
* ``purge=keep-summaries`` the call's entries only, the shared summaries kept
                           (for the real agy: does agy start slower without
                           them?);
* ``purge=off``            no purge during the call (as before 4.14.1); the
                           bench removes what the call left once it is timed;
* ``cage=on`` / ``cage=off`` agy inside its bubblewrap cage, as in service, or
                           bare (fake only: the real agy always runs caged).

Calls alternate between the variants (A B A B ...), so a slower host affects
them alike; a first round per variant warms it up and is not counted. The
load average is printed with the figures: compare runs made under a similar
load only. ``--root DIR`` measures the ``plugboard/`` of another tree, for
instance a release exported with ``git archive v4.14.0 plugboard | tar -x
-C DIR`` (a tree without the cage takes ``cage=off``, one without the purge
``purge=off``).

THE REAL AGY (the operator only, never in the CI): ``--real-agy PATH`` runs
that binary instead of the fake, with the caller's HOME (agy's sign-in),
inside its cage, with a short prompt per call: that is real inference on the
subscription. Without event times from inside agy, ``startup`` is empty;
``agy_word`` and ``relay`` remain. Run it as the gateway's user, with its
HOME, where the gateway serves no agy call meanwhile (a quiet UAT): the
bench's purge does not see the gateway's own agy calls::

    python scripts/bench_agy_first_token.py --real-agy "$(command -v agy)" \\
        --model gemini-3.8-flash-low --rounds 8 \\
        --variants "purge=full;purge=keep-summaries;purge=off" --json agy-ttfw.json

Conclusion of the code measure (16/11, fake agy, host load 6 to 13; BUG-106
stays open): from the request to the client's first word the gateway adds
7 to 19 ms before the spawn and 2 to 5 ms between agy's first word and the
client, with and without the purge, in 4.14.0, 4.14.1 and 4.14.4 alike; the
cage (4.14.2) adds 35 to 55 ms to agy's start. The purge runs after agy has
exited: its snapshot before the spawn takes under 1 ms with 200 leftover
conversations and 18 ms with 5 000, its removal 1 to 11 ms. None of it
explains the 1.2 s seen on the UAT on 15/11 (three calls per mode, one
night). What 4.14.1 changed for agy itself is that the shared summaries are
gone when the next call starts; whether agy then starts slower, and whether
the 1.2 s was noise, is measured only with the real agy: ``purge=full``
against ``purge=keep-summaries`` and ``purge=off``, compared on
``agy_word``.

Exit codes: 0 done, 1 a call failed, 2 bad usage.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
PROVIDER_ID = "bench-agy"
FAKE_MODEL = "gemini-bench"
PURGES = ("full", "keep-summaries", "off")
CAGES = ("on", "off")
FIELDS = ("launch", "startup", "agy_word", "relay", "first_word", "total")
# init, first word, tail (word -> result), exit (result -> exit): seconds,
# close to agy 1.4.4 on the UAT minus its model time (C/CAPTURE-FLUX-CLI.txt).
DEFAULT_TIMINGS = "0.2,0.3,0.35,0.15"
# What a shell script needs on top of what agy needs in the cage (fake only,
# as in tests/test_agy_cage.py).
_SHELL_RO = ("/usr/bin", "/bin")

FAKE_AGY = r"""#!/bin/sh
# Fake agy for scripts/bench_agy_first_token.py: never a model.
if [ "$1" = "models" ]; then printf '%s\tBench\n' "@MODEL@"; exit 0; fi
t0=$(date +%s.%N)
cat >/dev/null
fmt=json; prev=
for a in "$@"; do
  [ "$prev" = "--output-format" ] && fmt=$a
  prev=$a
done
cid=bench$(date +%s%N)$$
S="$HOME/.gemini/antigravity-cli"
sleep @INIT@
if [ "$fmt" = stream-json ]; then
  printf '{"event":"init","conversation_id":"%s","init":{"model":"@MODEL@"}}\n' "$cid"
fi
mkdir -p "$S/conversations" "$S/brain/$cid/.system_generated/logs" "$S/annotations" "$S/presence"
head -c 65536 /dev/zero > "$S/conversations/$cid.db"
head -c 65536 /dev/zero > "$S/brain/$cid/.system_generated/logs/transcript.jsonl"
: > "$S/annotations/$cid.pbtxt"
: > "$S/presence/$cid.lock"
head -c 20480 /dev/zero > "$S/conversation_summaries.db"
head -c 861 /dev/zero > "$S/jetbox_summaries_proto.pb"
sleep @WORD@
t1=$(date +%s.%N)
text="PONG start=$t0 emit=$t1"
usage='{"input_tokens":12,"output_tokens":3,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":15}'
if [ "$fmt" = stream-json ]; then
  printf '{"event":"step_update","step_update":{"conversation_id":"%s","step_index":1,"state":"ACTIVE","step_type":"agent_response","text_delta":"%s"}}\n' "$cid" "$text"
  sleep @TAIL@
  printf '{"event":"result","result":{"conversation_id":"%s","status":"SUCCESS","response":"%s","num_turns":1,"usage":%s}}\n' "$cid" "$text" "$usage"
else
  sleep @TAIL@
  printf '{"conversation_id":"%s","status":"SUCCESS","response":"%s","num_turns":1,"usage":%s}\n' "$cid" "$text" "$usage"
fi
sleep @EXIT@
exit 0
"""


# ── Arguments ───────────────────────────────────────────────────────────────


def parse_variants(spec: str) -> List[Dict[str, str]]:
    """``purge=full,cage=on;purge=off`` -> settings, defaults filled in."""
    out: List[Dict[str, str]] = []
    for part in spec.split(";"):
        part = part.strip()
        if not part:
            continue
        settings = {"purge": "full", "cage": "on"}
        for item in part.split(","):
            name, _, value = item.strip().partition("=")
            if name == "purge" and value in PURGES:
                settings["purge"] = value
            elif name == "cage" and value in CAGES:
                settings["cage"] = value
            else:
                raise ValueError(
                    f"bad setting {item.strip()!r}: purge={'|'.join(PURGES)}, "
                    f"cage={'|'.join(CAGES)}"
                )
        out.append(settings)
    if not out:
        raise ValueError("no variant given")
    return out


def label(settings: Dict[str, str]) -> str:
    return f"purge={settings['purge']},cage={settings['cage']}"


def parse_timings(spec: str) -> List[float]:
    values = [float(x) for x in spec.split(",")]
    if len(values) != 4 or min(values) < 0:
        raise ValueError("--fake-timings takes four seconds >= 0: init,word,tail,exit")
    return values


# ── Store ───────────────────────────────────────────────────────────────────


def fill_store(store: Path, leftovers: int) -> None:
    """A store as after agy's sign-in, plus ``leftovers`` conversations left by
    runs the gateway did not make (bare runs, older releases)."""
    store.mkdir(parents=True, exist_ok=True)
    (store / "antigravity-oauth-token").write_text("fake-sign-in\n")
    for i in range(leftovers):
        cid = f"left{i:05d}"
        for sub in ("conversations", "annotations", "presence"):
            (store / sub).mkdir(exist_ok=True)
        (store / "conversations" / f"{cid}.db").write_bytes(b"\0" * 1024)
        (store / "annotations" / f"{cid}.pbtxt").write_text("")
        (store / "presence" / f"{cid}.lock").write_text("")
        logs = store / "brain" / cid / ".system_generated" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "transcript.jsonl").write_text("{}\n")


def store_state(store: Path, leftovers: int) -> Dict[str, Any]:
    conv = store / "conversations"
    names = os.listdir(conv) if conv.is_dir() else []
    return {
        # What the calls left (the leftovers put there by the bench excluded).
        "conversations": len([n for n in names if not n.startswith("left")]),
        "leftovers": len([n for n in names if n.startswith("left")]),
        "shared_summaries": any(
            (store / name).exists()
            for name in ("conversation_summaries.db", "jetbox_summaries_proto.pb")
        ),
        "sign_in": (store / "antigravity-oauth-token").is_file(),
        "leftovers_expected": leftovers,
    }


# ── One call through the chat route, in process ─────────────────────────────


async def asgi_stream(app: Any, key: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST the streamed chat to the ASGI app; time the first word and the end."""
    raw = json.dumps(body).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"127.0.0.1"),
            (b"content-type", b"application/json"),
            (b"authorization", f"Bearer {key}".encode()),
            (b"content-length", str(len(raw)).encode()),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 80),
    }
    out: Dict[str, Any] = {"status": None, "first_word_at": None, "text": ""}
    delivered = False
    finished = asyncio.Event()
    pending = b""

    async def receive() -> Dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await finished.wait()  # the client stays until the response ends
        return {"type": "http.disconnect"}

    async def send(message: Dict[str, Any]) -> None:
        nonlocal pending
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
            return
        if message["type"] != "http.response.body":
            return
        pending += message.get("body", b"")
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            if not line.startswith(b"data:") or b"[DONE]" in line:
                continue
            try:
                evt = json.loads(line[5:])
                content = (evt["choices"][0].get("delta") or {}).get("content")
            except (ValueError, KeyError, IndexError, TypeError):
                continue
            if content:
                if out["first_word_at"] is None:
                    out["first_word_at"] = time.time()
                out["text"] += content
        if not message.get("more_body", False):
            finished.set()

    try:
        await app(scope, receive, send)
    finally:
        finished.set()
    if pending.strip():
        out["tail"] = pending[:300].decode("utf-8", "replace")
    return out


def _stamps(text: str) -> Dict[str, float]:
    """``start=`` and ``emit=`` written by the fake agy in its answer."""
    found: Dict[str, float] = {}
    for word in text.split():
        name, _, value = word.partition("=")
        if name in ("start", "emit"):
            try:
                found[name] = float(value)
            except ValueError:
                pass
    return found


class _NoPurge:
    """Stands for agy_store.AgyCall when the purge is off."""

    conversation_id = None
    alone = True

    def see_line(self, line: bytes) -> None:
        pass

    def see_output(self, stdout: Optional[bytes]) -> None:
        pass

    def watching(self, read_delta):
        return read_delta

    def end(self) -> None:
        pass


def stats(values: Sequence[Optional[float]]) -> Optional[Dict[str, float]]:
    xs = [v for v in values if v is not None]
    if not xs:
        return None
    return {
        "n": len(xs),
        "min": round(min(xs), 4),
        "med": round(statistics.median(xs), 4),
        "max": round(max(xs), 4),
    }


def render(rows: List[Dict[str, Any]], load: str, agy: str) -> str:
    head = f"agy: {agy} | load (1, 5, 15 min) before: {load} | seconds, medians"
    cols = "".join(f"{f:>11}" for f in FIELDS)
    lines = [head, f"{'variant':<34}{'n':>3}{'fail':>5}{cols}", "-" * (42 + 11 * 6)]
    for r in rows:
        cells = "".join(f"{(r[f]['med'] if r[f] else '-'):>11}" for f in FIELDS)
        lines.append(f"{r['variant']:<34}{r['calls']:>3}{r['failed']:>5}{cells}")
    return "\n".join(lines)


def _load() -> str:
    try:
        return " ".join(f"{x:.2f}" for x in os.getloadavg())
    except OSError:
        return "?"


# ── Main ────────────────────────────────────────────────────────────────────


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rounds", type=int, default=5, help="calls per variant")
    ap.add_argument("--variants", default="purge=full;purge=off")
    ap.add_argument("--fake-timings", default=DEFAULT_TIMINGS)
    ap.add_argument(
        "--store-leftovers",
        type=int,
        default=200,
        help="conversations already in the fake store (fake only)",
    )
    ap.add_argument("--real-agy", metavar="PATH", help="REAL agy binary (Hermes only)")
    ap.add_argument("--model", default=None, help="agy model (real agy)")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument(
        "--root",
        default=str(ROOT),
        help="tree whose plugboard/ is measured (another release, exported "
        "with git archive), default this repository",
    )
    ap.add_argument("--json", dest="json_path")
    args = ap.parse_args(argv)

    try:
        variants = parse_variants(args.variants)
        timings = parse_timings(args.fake_timings)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.rounds < 1 or args.store_leftovers < 0:
        print("error: --rounds >= 1 and --store-leftovers >= 0", file=sys.stderr)
        return 2
    real = args.real_agy
    if real is not None:
        if not (os.path.isfile(real) and os.access(real, os.X_OK)):
            print(
                f"error: real agy binary not found or not executable: {real}",
                file=sys.stderr,
            )
            return 2
        if any(v["cage"] == "off" for v in variants):
            print(
                "error: the real agy runs only in its cage (cage=on)", file=sys.stderr
            )
            return 2
        if not os.environ.get("HOME"):
            print("error: the real agy needs HOME (its sign-in)", file=sys.stderr)
            return 2

    work = Path(tempfile.mkdtemp(prefix="pb-bench-agy-"))
    try:
        return asyncio.run(_bench(args, variants, timings, work))
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def _bench(args, variants, timings, work: Path) -> int:
    real = args.real_agy
    model = args.model or ("gemini-3.8-flash-low" if real else FAKE_MODEL)
    data = work / "data"
    data.mkdir()
    (data / "providers.json").write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "id": PROVIDER_ID,
                        "name": "agy (bench)",
                        "base_url": "subprocess://agy",
                        "bearer_token": "(via CLI)",
                        "default_model": model,
                        "auth_source": "subprocess:agy",
                        "is_active": True,
                        "created_at": "2026-01-01T00:00:00+00:00",
                    }
                ]
            }
        )
    )
    os.environ.update(
        {
            "PLUGBOARD_DATA": str(data),
            "PLUGBOARD_SKIP_DOTENV": "1",
            "ADMIN_KEY": secrets.token_hex(24),
            "API_KEY": secrets.token_hex(24),
        }
    )
    if real:
        binary = os.path.realpath(real)
        home = os.environ["HOME"]
    else:
        home_dir = work / "home"
        (home_dir / ".gemini" / "config").mkdir(parents=True)
        home = str(home_dir)
        os.environ["HOME"] = home
        bin_dir = work / "bin"
        bin_dir.mkdir()
        init, word, tail, exit_s = timings
        script = (
            FAKE_AGY.replace("@MODEL@", model)
            .replace("@INIT@", f"{init:g}")
            .replace("@WORD@", f"{word:g}")
            .replace("@TAIL@", f"{tail:g}")
            .replace("@EXIT@", f"{exit_s:g}")
        )
        binary = str(bin_dir / "agy")
        Path(binary).write_text(script)
        os.chmod(binary, 0o755)
        os.environ["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    store = Path(home, ".gemini", "antigravity-cli")
    if not real:
        fill_store(store, args.store_leftovers)
    sys.path.insert(0, str(Path(args.root).resolve()))

    import functools
    import importlib
    import logging

    logging.disable(logging.WARNING)
    from plugboard import apikeys
    from plugboard import cli_subprocess as cs
    from plugboard import live_requests, ratelimit, tokenizer
    from plugboard.app import app

    # Older trees (--root) have no cage (before 4.14.2) or no purge (before
    # 4.14.1).
    try:
        agy_cage: Any = importlib.import_module("plugboard.agy_cage")
    except ImportError:
        agy_cage = None
    try:
        agy_store: Any = importlib.import_module("plugboard.agy_store")
    except ImportError:
        agy_store = None
    if agy_cage is None and any(v["cage"] == "on" for v in variants):
        print("error: this tree has no agy cage: use cage=off", file=sys.stderr)
        return 2
    if agy_store is None and any(v["purge"] != "off" for v in variants):
        print("error: this tree has no agy purge: use purge=off", file=sys.stderr)
        return 2

    ratelimit._LIMIT = 0
    # A client key, as a client calls (never the admin key): in the
    # throwaway data directory, gone with it.
    client_key = apikeys.create_key("bench-client", "bench")["key"]
    cs._binary_path_cache["agy"] = binary
    if not real:
        spawn = work / "spawn"
        spawn.mkdir(mode=0o700)
        cs._SPAWN_CWD_CACHE = str(spawn)
    tokenizer._get_encoding()  # off the clock: read once from tiktoken's cache

    real_cage_argv = agy_cage.cage_argv if agy_cage else None
    real_begin = agy_store.begin if agy_store else None
    real_purge_shared = agy_store._purge_shared if agy_store else None
    real_reader = cs._DELTA_READERS["agy"]
    real_live_update = live_requests.update
    marks: Dict[str, float] = {}

    def reader(line: bytes) -> str:
        text = real_reader(line)
        if text and "agy_word_at" not in marks:
            marks["agy_word_at"] = time.time()
        return text

    def live_update(*a: Any, **fields: Any) -> None:
        # complete() says "cli_running" right after the spawn.
        if fields.get("phase") == "cli_running" and "spawn_at" not in marks:
            marks["spawn_at"] = time.time()
        real_live_update(*a, **fields)

    cs._DELTA_READERS["agy"] = reader
    live_requests.update = live_update

    def apply(settings: Dict[str, str]) -> None:
        if agy_cage is not None:
            if settings["cage"] == "off":
                agy_cage.cage_argv = lambda b, a, **_kw: [b, *a]
            elif real:
                agy_cage.cage_argv = real_cage_argv
            else:
                agy_cage.cage_argv = functools.partial(
                    real_cage_argv, _extra_ro_for_tests=_SHELL_RO
                )
        if agy_store is not None:
            agy_store._purge_shared = (
                (lambda real_store, failures: None)
                if settings["purge"] == "keep-summaries"
                else real_purge_shared
            )
            agy_store.begin = (
                (lambda env: _NoPurge()) if settings["purge"] == "off" else real_begin
            )

    calls: List[Dict[str, Any]] = []
    load = _load()
    # Round 0 warms each variant up (imports, first route lookup) and is not
    # counted.
    for round_no in range(args.rounds + 1):
        for settings in variants:
            apply(settings)
            marks.clear()
            # purge=off: what the call leaves is removed after it, off the clock.
            after = (
                agy_store.AgyCall(home)
                if agy_store is not None and settings["purge"] == "off"
                else None
            )
            body = {
                "model": f"{PROVIDER_ID}:{model}",
                "messages": [
                    {
                        "role": "user",
                        "content": "Reply with exactly the single word PONG. "
                        f"ref={uuid.uuid4().hex[:8]}",
                    }
                ],
                "max_tokens": 64,
                "cache": False,
                "stream": True,
            }
            t0 = time.time()
            try:
                got = await asyncio.wait_for(
                    asgi_stream(app, client_key, body), timeout=args.timeout
                )
                error = None
            except Exception as exc:  # noqa: BLE001 - reported per call
                got, error = (
                    {"status": None, "first_word_at": None, "text": ""},
                    repr(exc)[:200],
                )
            t_end = time.time()
            if after is not None:
                after.end()
            stamp = _stamps(got["text"]) if not real else {}
            fw = got["first_word_at"]
            spawn_at = marks.get("spawn_at")
            word_at = marks.get("agy_word_at")
            ok = error is None and got["status"] == 200 and fw is not None

            def gap(a: Optional[float], b: Optional[float]) -> Optional[float]:
                return round(b - a, 4) if a is not None and b is not None else None

            if round_no == 0:
                continue
            calls.append(
                {
                    "variant": label(settings),
                    "ok": ok,
                    "status": got["status"],
                    "error": error
                    or (None if ok else (got.get("tail") or got["text"])[:200]),
                    "launch": gap(t0, spawn_at),
                    "startup": gap(spawn_at, stamp.get("start")),
                    "agy_word": gap(spawn_at, word_at),
                    "relay": gap(word_at, fw),
                    "first_word": gap(t0, fw),
                    "total": round(t_end - t0, 4),
                }
            )

    if agy_cage is not None:
        agy_cage.cage_argv = real_cage_argv
    if agy_store is not None:
        agy_store.begin = real_begin
        agy_store._purge_shared = real_purge_shared
    cs._DELTA_READERS["agy"] = real_reader
    live_requests.update = real_live_update
    if any(v["purge"] == "keep-summaries" for v in variants):
        # Leave the store as the gateway does after a call: no shared summary.
        real_store = agy_store._real_store(store)
        if real_store:
            real_purge_shared(real_store, [])

    rows = []
    for settings in variants:
        mine = [c for c in calls if c["variant"] == label(settings)]
        good = [c for c in mine if c["ok"]]
        rows.append(
            {
                "variant": label(settings),
                "calls": len(mine),
                "failed": len(mine) - len(good),
                **{f: stats([c[f] for c in good]) for f in FIELDS},
            }
        )
    agy = "real" if real else "fake"
    print(render(rows, load, agy))
    failed = [c for c in calls if not c["ok"]]
    for c in failed[:5]:
        print(
            f"FAIL {c['variant']}: status={c['status']} {c['error']}", file=sys.stderr
        )
    if args.json_path:
        report = {
            "date_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "agy": agy,
            "model": model,
            "rounds": args.rounds,
            "tree": str(Path(args.root).resolve()),
            "load_before": load,
            "load_after": _load(),
            "fake_timings": None if real else timings,
            "rows": rows,
            "calls": calls,
            "store_after": None if real else store_state(store, args.store_leftovers),
        }
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
