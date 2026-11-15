"""agy keeps every conversation, prompt in clear, under
``$HOME/.gemini/antigravity-cli`` (real store of agy 1.4.4, UAT, 14/11): about
8 files and 450 KB per call, next to its sign-in (``antigravity-oauth-token``).
After each agy call the gateway removes what that call created, and nothing
else (BUG-97).

A real process is spawned (a Python script named ``agy``) with a throwaway
``HOME``: it writes the files of one conversation the way agy does, prints
agy's real JSON (or ``stream-json`` events) and, at its end, says whether its
own files were still there.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import sys
from pathlib import Path

import pytest

from plugboard import agy_store
from plugboard import cli_subprocess as cs

# The real one, before conftest turns the purge off for the other tests.
_REAL_STORE_DIR = agy_store.store_dir

_OLD = "0b7c3a1e-5f00-4d2e-9a11-000000000000"  # a conversation already there
_A = "f5afb0a0-22e8-41fc-9bb6-73f6e1d65261"
_B = "01de9500-7c8c-46bc-900e-f2a21cea4ba5"

_FAKE_AGY = r"""
import json, os, re, sys, time

stream = "stream-json" in sys.argv[1:]
prompt = sys.stdin.read()
found = re.search(r"CMD(\{.*?\})END", prompt, re.S)
cmd = json.loads(found.group(1)) if found else {}
store = os.path.join(os.environ["HOME"], ".gemini", "antigravity-cli")
cid = cmd.get("id")
mine = []
if cid:
    mine = [
        f"conversations/{cid}.db",
        f"conversations/{cid}.db-wal",
        f"brain/{cid}/.system_generated/logs/transcript.jsonl",
        f"annotations/{cid}.pbtxt",
        f"presence/{cid}.lock",
    ]
    for rel in mine:
        path = os.path.join(store, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(prompt)  # agy keeps the prompt in clear
    for name in ("conversation_summaries.db", "jetbox_summaries_proto.pb"):
        with open(os.path.join(store, name), "a") as fh:
            fh.write(cid)
for rel, target in cmd.get("links", {}).items():
    os.symlink(target, os.path.join(store, rel))
reported = cmd.get("report", cid or "")


def emit(obj):
    print(json.dumps(obj), flush=True)


if stream:
    emit({"event": "init", "conversation_id": reported, "init": {"cwd": "/tmp"}})
if cmd.get("started"):
    open(cmd["started"], "w").close()
if cmd.get("hang"):
    time.sleep(60)
wait = cmd.get("wait_for")
while wait and not os.path.exists(wait):
    time.sleep(0.02)
intact = all(os.path.exists(os.path.join(store, rel)) for rel in mine)
result = {
    "conversation_id": reported,
    "status": "SUCCESS",
    "response": ("intact" if intact else "missing") + "\n",
    "duration_seconds": 1.5,
    "num_turns": 1,
    "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
}
if cmd.get("denied"):
    result["response"] = ""
    result["denied_actions"] = [{"action": "write_file", "display_name": "WriteFile"}]
if stream:
    emit({
        "event": "step_update",
        "step_update": {
            "conversation_id": reported,
            "step_index": 1,
            "state": "DONE",
            "step_type": "agent_response",
            "text_delta": result["response"],
        },
    })
    emit({"event": "result", "result": result})
else:
    emit(result)
"""


def _tree(root: Path) -> dict:
    """Every entry under ``root``: file content, link target or ``<dir>``."""
    out = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            out[rel] = "-> " + os.readlink(path)
        elif path.is_dir():
            out[rel] = "<dir>"
        else:
            out[rel] = path.read_text(encoding="utf-8")
    return out


def _entries_of(store: Path, cid: str) -> list:
    return sorted(
        f"{sub}/{name}"
        for sub in agy_store.CONVERSATION_DIRS
        if (store / sub).is_dir()
        for name in os.listdir(store / sub)
        if name == cid or name.startswith(cid + ".")
    )


class Env:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.store = tmp_path / "home" / ".gemini" / "antigravity-cli"

    def body(self, text: str = "", **cmd) -> dict:
        return {
            "model": "gemini-3.8-flash-low",
            "messages": [
                {"role": "user", "content": f"{text} CMD{json.dumps(cmd)}END"}
            ],
        }

    def flag(self, name: str) -> str:
        return str(self.tmp / name)

    def kept(self) -> dict:
        """What no purge may touch: sign-in, agy's own files, the old talk."""
        return {
            rel: value
            for rel, value in _tree(self.store).items()
            if rel not in agy_store.SHARED_FILES
            and (
                not rel.startswith(tuple(agy_store.CONVERSATION_DIRS))
                or _OLD in rel
                or rel in agy_store.CONVERSATION_DIRS
            )
        }


@pytest.fixture
def env(tmp_path, monkeypatch, agy_without_cage):  # outside bwrap, see conftest
    monkeypatch.setattr(agy_store, "store_dir", _REAL_STORE_DIR)
    spawn = tmp_path / "spawn"
    spawn.mkdir(mode=0o700)
    monkeypatch.setattr(cs, "_SPAWN_CWD_CACHE", str(spawn))
    monkeypatch.delenv("PLUGBOARD_SUBPROCESS_CWD", raising=False)
    monkeypatch.setattr(cs, "_binary_path_cache", {})
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "agy"
    script.write_text(f"#!{sys.executable} -I\n{_FAKE_AGY}", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    e = Env(tmp_path)
    store = e.store
    for sub in (*agy_store.CONVERSATION_DIRS, "bin", "builtin", "cache", "implicit"):
        (store / sub).mkdir(parents=True)
    for sub in ("log", "crashes"):
        (store / sub).mkdir()
    (store / "antigravity-oauth-token").write_text("TOKEN", encoding="utf-8")
    (store / "installation_id").write_text("install", encoding="utf-8")
    (store / "jetski_state.pbtxt").write_text("state", encoding="utf-8")
    (store / "bin" / "helper").write_text("bin", encoding="utf-8")
    (store / "builtin" / "skill.md").write_text("builtin", encoding="utf-8")
    (store / "cache" / "last_conversations.json").write_text(_OLD, encoding="utf-8")
    (store / "implicit" / "1234.pb").write_text("implicit", encoding="utf-8")
    (store / "log" / "agy.log").write_text("log", encoding="utf-8")
    (store / "crashes" / "c1.txt").write_text("crash", encoding="utf-8")
    (store / "cli.log").symlink_to("log/agy.log")
    (store / "conversations" / f"{_OLD}.db").write_text("old", encoding="utf-8")
    (store / "brain" / _OLD).mkdir()
    (store / "brain" / _OLD / "transcript.jsonl").write_text("old", encoding="utf-8")
    (store / "annotations" / f"{_OLD}.pbtxt").write_text("old", encoding="utf-8")
    (store / "presence" / f"{_OLD}.lock").write_text("old", encoding="utf-8")
    return e


async def _until(path: str) -> None:
    for _ in range(500):
        if os.path.exists(path):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{path} never appeared")


def _shared(store: Path) -> list:
    return sorted(n for n in agy_store.SHARED_FILES if (store / n).exists())


# ── one call ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
async def test_a_successful_call_leaves_nothing_of_its_conversation(env, stream):
    kept = env.kept()
    pieces: list = []

    result = await cs.complete(
        "agy",
        env.body(id=_A),
        timeout_s=20,
        on_delta=pieces.append if stream else None,
    )

    assert result["choices"][0]["message"]["content"] == "intact\n"
    assert _entries_of(env.store, _A) == []
    assert _shared(env.store) == [], "no agy call in flight: summaries go too"
    assert env.kept() == kept


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
async def test_a_call_whose_tool_was_denied_is_purged_too(env, stream):
    kept = env.kept()

    with pytest.raises(cs.CliSubprocessError):
        await cs.complete(
            "agy",
            env.body(id=_A, denied=True),
            timeout_s=20,
            on_delta=(lambda t: None) if stream else None,
        )

    assert _entries_of(env.store, _A) == []
    assert env.kept() == kept


async def test_a_timed_out_call_without_an_id_is_purged(env):
    kept = env.kept()

    with pytest.raises(cs.CliSubprocessError) as info:
        await cs.complete("agy", env.body(id=_A, hang=True), timeout_s=1.5)

    assert info.value.timed_out is True
    assert _entries_of(env.store, _A) == []
    assert env.kept() == kept


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
async def test_a_cancelled_call_is_purged(env, stream):
    kept = env.kept()
    started = env.flag("started")
    task = asyncio.ensure_future(
        cs.complete(
            "agy",
            env.body(id=_A, hang=True, started=started),
            timeout_s=30,
            on_delta=(lambda t: None) if stream else None,
        )
    )
    await _until(started)
    assert _entries_of(env.store, _A) != [], "there while agy runs"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _entries_of(env.store, _A) == []
    assert env.kept() == kept


# ── calls at the same time ───────────────────────────────────────────────────


async def test_two_calls_never_purge_each_other(env):
    kept = env.kept()
    release_a = env.flag("release-a")
    started_a = env.flag("started-a")
    task_a = asyncio.ensure_future(
        cs.complete(
            "agy",
            env.body(id=_A, started=started_a, wait_for=release_a),
            timeout_s=20,
        )
    )
    await _until(started_a)

    result_b = await cs.complete("agy", env.body(id=_B), timeout_s=20)

    assert result_b["choices"][0]["message"]["content"] == "intact\n"
    assert _entries_of(env.store, _B) == []
    assert len(_entries_of(env.store, _A)) == 5, "A still runs: untouched"
    assert _shared(env.store) != [], "A still runs: summaries stay"

    Path(release_a).touch()
    result_a = await task_a

    assert result_a["choices"][0]["message"]["content"] == "intact\n"
    assert _entries_of(env.store, _A) == []
    assert _shared(env.store) == []
    assert env.kept() == kept


async def test_a_cut_call_never_purges_a_neighbour_still_running(env):
    """A is cut without an id while B, started after it, still runs: what
    appeared during A's run (A's files and B's) waits until B is done."""
    kept = env.kept()
    started_a, started_b = env.flag("started-a"), env.flag("started-b")
    release_b = env.flag("release-b")
    task_a = asyncio.ensure_future(
        cs.complete("agy", env.body(id=_A, hang=True, started=started_a), timeout_s=2)
    )
    await _until(started_a)
    task_b = asyncio.ensure_future(
        cs.complete(
            "agy",
            env.body(id=_B, started=started_b, wait_for=release_b),
            timeout_s=20,
        )
    )
    await _until(started_b)

    with pytest.raises(cs.CliSubprocessError) as info:
        await task_a
    assert info.value.timed_out is True
    assert len(_entries_of(env.store, _B)) == 5, "B still runs: untouched"
    assert _shared(env.store) != []

    Path(release_b).touch()
    result_b = await task_b

    assert result_b["choices"][0]["message"]["content"] == "intact\n"
    assert _entries_of(env.store, _A) == []
    assert _entries_of(env.store, _B) == []
    assert _shared(env.store) == []
    assert env.kept() == kept


async def test_a_streamed_call_known_by_its_first_event_is_purged_at_once(env):
    """The id comes with agy's first event: a streamed call cancelled while a
    neighbour runs removes its own files right away, and only those."""
    started_b, release_b = env.flag("started-b"), env.flag("release-b")
    task_b = asyncio.ensure_future(
        cs.complete(
            "agy",
            env.body(id=_B, started=started_b, wait_for=release_b),
            timeout_s=20,
        )
    )
    await _until(started_b)
    started_a = env.flag("started-a")
    task_a = asyncio.ensure_future(
        cs.complete(
            "agy",
            env.body(id=_A, hang=True, started=started_a),
            timeout_s=30,
            on_delta=lambda t: None,
        )
    )
    await _until(started_a)
    await asyncio.sleep(0.2)  # the init event is read

    task_a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task_a

    assert _entries_of(env.store, _A) == []
    assert len(_entries_of(env.store, _B)) == 5

    Path(release_b).touch()
    result_b = await task_b
    assert result_b["choices"][0]["message"]["content"] == "intact\n"
    assert _entries_of(env.store, _B) == []


# ── what is never removed ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "forged",
    [
        "../antigravity-oauth-token",
        "TOKEN_PATH",
        "..",
        ".",
        "",
        "a/b",
        ".hidden",
        "x" * 200,
        "antigravity-oauth-token",
        "conversations",
    ],
)
async def test_a_forged_id_removes_nothing(env, forged):
    if forged == "TOKEN_PATH":
        forged = str(env.store / "antigravity-oauth-token")
    before = _tree(env.store)

    await cs.complete("agy", env.body(report=forged), timeout_s=20)

    assert _tree(env.store) == before
    assert (env.store / "antigravity-oauth-token").read_text() == "TOKEN"


@pytest.mark.parametrize(
    "value",
    [
        "../antigravity-oauth-token",
        "/etc/passwd",
        "..",
        ".",
        "",
        "a/b",
        ".x",
        "x" * 200,
        None,
        7,
    ],
)
def test_forged_ids_are_rejected(value):
    assert agy_store.valid_conversation_id(value) is None


def test_ids_are_read_where_agy_writes_them():
    """Shapes captured on the UAT (agy 1.4.4): ``json`` payload, ``stream-json``
    init and result events; an agy error carries an empty id."""
    payload = json.dumps({"conversation_id": _A, "status": "SUCCESS", "response": ""})
    assert agy_store.conversation_id_from_output(payload) == _A
    init = json.dumps({"event": "init", "conversation_id": _B, "init": {}})
    assert agy_store.conversation_id_from_line(init.encode()) == _B
    result = json.dumps({"event": "result", "result": {"conversation_id": _A}})
    assert agy_store.conversation_id_from_output("noise\n" + result) == _A
    error = json.dumps({"conversation_id": "", "status": "ERROR", "error": "x"})
    assert agy_store.conversation_id_from_output(error) is None


async def test_links_are_removed_never_their_targets(env):
    outside = env.tmp / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    token = env.store / "antigravity-oauth-token"
    kept = env.kept()

    await cs.complete(
        "agy",
        env.body(
            id=_A,
            links={
                f"brain/{_A}/escape": str(outside),
                f"conversations/{_A}.db-shm": str(token),
            },
        ),
        timeout_s=20,
    )

    assert outside.read_text() == "outside"
    assert token.read_text() == "TOKEN"
    assert _entries_of(env.store, _A) == []
    assert env.kept() == kept


async def test_a_store_link_to_elsewhere_is_never_followed(env, tmp_path):
    """A conversation folder that is a link leads out of the store: skipped."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / f"{_A}.lock").write_text("keep", encoding="utf-8")
    presence = env.store / "presence"
    for child in presence.iterdir():
        child.unlink()
    presence.rmdir()
    presence.symlink_to(elsewhere)

    await cs.complete("agy", env.body(report=_A), timeout_s=20)

    assert (elsewhere / f"{_A}.lock").read_text() == "keep"


async def test_a_failed_purge_is_logged_and_the_answer_still_served(
    env, monkeypatch, caplog
):
    def refuse(path):
        raise PermissionError(13, "Permission denied", path)

    monkeypatch.setattr(agy_store, "_remove", refuse)
    caplog.set_level(logging.WARNING, logger="plugboard.agy_store")

    result = await cs.complete(
        "agy", env.body("SECRET-PROMPT-7f3a", id=_A), timeout_s=20
    )

    assert result["choices"][0]["message"]["content"] == "intact\n"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("PermissionError" in w for w in warnings), warnings
    assert all("SECRET-PROMPT-7f3a" not in w for w in warnings)
    assert all(_A not in w for w in warnings)


def test_the_rest_of_the_suite_never_reaches_a_real_store():
    """conftest: fake agy binaries elsewhere run with the developer's HOME."""
    assert agy_store.store_dir(os.environ.get("HOME")) is None


async def test_no_store_means_nothing_to_do(env):
    import shutil

    shutil.rmtree(env.store.parent)

    result = await cs.complete("agy", env.body(), timeout_s=20)

    assert result["choices"][0]["message"]["content"] == "intact\n"
    assert not env.store.exists()
