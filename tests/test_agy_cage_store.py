"""Each agy call gets a store of its own, thrown away afterwards (BUG-157).

Up to 4.15.1 the cage bound agy's store ``~/.gemini/antigravity-cli``
writable, shared by every call (P0-5 replay, 18/11): a prompt could read the
sign-in token (R8), rewrite it with the token of another Google account (W6:
the next calls of every tenant then ran on that account), leave a program in
``bin/`` that a later call runs (W5), and read the conversation of another
call in progress (X3).

Now the gateway makes a private folder per call (``agy-store-*``, 0700,
beside the call's ``TMPDIR``), fills it with a copy of the token and of
``bin/`` and ``builtin/``, and binds it at the store's path instead of the
store: whatever agy writes there, the copy of the token included, is removed
with it once the call is over. agy's settings and identity
(``STORE_READ_ONLY``) are still bound read-only from the shared store. R8
remains: agy must read its token (a limit, see agy_cage).

The integration tests run the host's real ``bwrap`` around a fake agy (a
shell script); what it sees inside the cage comes back as its answer, the
only thing that leaves a cage now (the shared store being out of its reach).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import stat
import time
import uuid
from pathlib import Path

import pytest

from plugboard import agy_cage
from plugboard import cli_subprocess as cs
from tests import test_agy_cage as _cage
from tests.test_agy_cage import (
    _BODY,
    _answer,
    _argv,
    _content,
    _install,
    _pairs,
    _processes_with,
)

# The fixtures of tests/test_agy_cage.py: a fake agy installation, the
# host's bwrap, the cage with what a shell needs.
fake_binary = _cage.fake_binary
real_bwrap = _cage.real_bwrap
setup = _cage.setup
shell_in_cage = _cage.shell_in_cage

_TOKEN = "antigravity-oauth-token"
# What the fake store holds once agy ran and was signed in.
_SHARED = {
    _TOKEN: "old-token\n",
    "settings.json": "{}\n",
    "installation_id": "00000000-test-install\n",
    "jetski_state.pbtxt": "state: 1\n",
    "bin/agentapi": "#!/bin/sh\necho agentapi\n",
    "builtin/.checksum": "builtin:abc\n",
    "builtin/skills/a/SKILL.md": "skill\n",
}
# Another session's conversation, left in the shared store (as before 4.15.2).
_OTHER = "conversations/p0-session-B.pb"


def _fill_shared(setup) -> Path:
    store = setup["store"]
    for rel, content in _SHARED.items():
        path = store / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (store / "bin" / "agentapi").chmod(0o755)
    (store / _OTHER).parent.mkdir(parents=True, exist_ok=True)
    (store / _OTHER).write_text("FAKE-CONVERSATION-B prompt in clear\n")
    return store


def _snapshot(folder: Path) -> dict:
    """Every path under ``folder``: content, mode and inode (a rewrite by
    rename changes the inode)."""
    out = {}
    for path in sorted(folder.rglob("*")):
        info = path.lstat()
        rel = path.relative_to(folder).as_posix()
        # Only regular files are read (a fifo would block, a link is not
        # followed).
        content = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
        out[rel] = (content, stat.S_IMODE(info.st_mode), info.st_ino)
    return out


def _private(setup) -> Path:
    path = setup["spawn"] / "agy-store-x"
    path.mkdir(mode=0o700)
    return path


def _report(text: str) -> dict:
    return dict(word.split("=", 1) for word in text.split())


# ── The command line ────────────────────────────────────────────────────────


def test_the_private_store_is_bound_at_the_stores_path(setup, fake_binary):
    """Writable: the call's own store and its TMPDIR; the shared store never."""
    store = _fill_shared(setup)
    private = _private(setup)
    argv, tmpdir = _argv(setup, str(fake_binary), private_store=str(private))

    assert _pairs(argv, "--bind") == [(str(private), str(store)), (tmpdir, tmpdir)]
    sep = argv.index("--")
    # agy's settings and identity: read-only, from the shared store, at
    # their path in the store agy sees, right after it.
    i = argv.index("--bind") + 3
    expected = []
    for name in agy_cage.STORE_READ_ONLY:
        expected += ["--ro-bind", str(store / name), str(store / name)]
    assert argv[i : i + len(expected)] == expected
    # Nothing else of the shared store is bound: neither the token, nor
    # bin/, builtin/ or the conversations (they are copied, or start empty).
    for rel in (_TOKEN, "bin", "builtin", "conversations"):
        assert str(store / rel) not in argv[:sep], rel


def test_the_private_store_is_required():
    """No default: a caller that names no private store gets no cage."""
    with pytest.raises(TypeError):
        agy_cage.cage_argv("/opt/agy", [], env={"HOME": "/x"}, tmpdir="/x/t")


def test_the_private_store_gets_copies_never_the_shared_files(setup, fake_binary):
    store = _fill_shared(setup)
    os.mkfifo(store / "bin" / "a-fifo")
    (store / "bin" / "a-link").symlink_to(setup["app_data"] / "providers.json")
    before = _snapshot(store)
    private = _private(setup)

    _argv(setup, str(fake_binary), private_store=str(private))
    # cage_argv only checks; the copy is made apart (in a thread, BUG-157).
    assert os.listdir(private) == []
    agy_cage.fill_private_store({"HOME": str(setup["home"])}, str(private))

    # The shared store is unchanged.
    assert _snapshot(store) == before
    # The token, bin/ and builtin/: copies (same content, other inodes).
    copied = {
        rel: (private / rel).read_text()
        for rel in _SHARED
        if (private / rel).is_file() and (private / rel).stat().st_size
    }
    assert copied == {
        rel: content
        for rel, content in _SHARED.items()
        if rel.startswith((_TOKEN, "bin/", "builtin/"))
    }
    for rel in copied:
        assert (private / rel).stat().st_ino != (store / rel).stat().st_ino, rel
    assert stat.S_IMODE((private / _TOKEN).stat().st_mode) == 0o600
    assert os.access(private / "bin" / "agentapi", os.X_OK)
    # No link is copied nor followed, no special file either.
    assert sorted(os.listdir(private / "bin")) == ["agentapi"]
    # Nothing else of the shared store: no conversation of another call.
    assert not (private / "conversations").exists()
    assert sorted(os.listdir(private)) == [_TOKEN, "bin", "builtin"]


def test_without_a_token_nothing_is_copied_and_the_cage_is_built(setup, fake_binary):
    """agy not signed in: agy itself says so, as before (no crash here)."""
    (setup["store"] / _TOKEN).unlink()
    private = _private(setup)
    _argv(setup, str(fake_binary), private_store=str(private))
    agy_cage.fill_private_store({"HOME": str(setup["home"])}, str(private))
    assert os.listdir(private) == []


@pytest.mark.parametrize("name", [_TOKEN, "bin", "builtin"])
def test_a_copied_entry_behind_a_link_is_refused(setup, fake_binary, tmp_path, name):
    store = _fill_shared(setup)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (store / name).rename(elsewhere / name)
    (store / name).symlink_to(elsewhere / name)

    with pytest.raises(agy_cage.CageUnavailable) as info:
        _argv(setup, str(fake_binary), private_store=str(_private(setup)))

    assert str(info.value) == f"agy's store entry {name} goes through a symbolic link"


@pytest.mark.parametrize(
    ("name", "expected"),
    [(_TOKEN, "is not a regular file"), ("bin", "is not a directory")],
)
def test_a_copied_entry_of_the_wrong_type_is_refused(
    setup, fake_binary, name, expected
):
    store = _fill_shared(setup)
    path = store / name
    if path.is_dir():
        for child in sorted(path.rglob("*"), reverse=True):
            child.unlink() if not child.is_dir() else child.rmdir()
        path.rmdir()
        path.write_text("not a folder")
    else:
        path.unlink()
        path.mkdir()

    with pytest.raises(agy_cage.CageUnavailable) as info:
        _argv(setup, str(fake_binary), private_store=str(_private(setup)))

    assert str(info.value) == f"agy's store entry {name} {expected}"


@pytest.mark.parametrize("case", ["link", "not-empty", "open", "missing", "shared"])
def test_a_private_store_that_is_not_private_is_refused(
    setup, fake_binary, tmp_path, case
):
    _fill_shared(setup)
    private = setup["spawn"] / "agy-store-x"
    if case == "link":
        (tmp_path / "real").mkdir(mode=0o700)
        private.symlink_to(tmp_path / "real")
    elif case == "not-empty":
        private.mkdir(mode=0o700)
        (private / "planted").write_text("x")
    elif case == "open":
        private.mkdir()
        private.chmod(0o755)
    elif case == "shared":
        private = setup["store"]

    with pytest.raises(agy_cage.CageUnavailable) as info:
        _argv(setup, str(fake_binary), private_store=str(private))

    assert str(info.value).startswith("agy private store "), str(info.value)


# ── The real cage: R8, W5, W6, X3 of the P0-5 replay ────────────────────────


def _attacks(store: str) -> str:
    """Shell run as agy: each attack of the P0-5 replay on agy's store, one
    ``name=yes|no`` word each, in its answer."""
    checks = {
        # R8: agy must read its token (a limit, documented).
        "R8_read_token": f"grep -q old-token {store}/{_TOKEN}",
        # W6: put another account's token in place (by rename, as agy does).
        "W6_rewrite_token": (
            f"printf 'forged-token\\n' > {store}/t.tmp"
            f" && mv -f {store}/t.tmp {store}/{_TOKEN}"
            f" && grep -q forged {store}/{_TOKEN}"
        ),
        "W6_new_file": f"echo x > {store}/{_TOKEN}.p0",
        # W5: leave a program in bin/ for a later call.
        "W5_plant_bin": (
            f"printf '#!/bin/sh\\n' > {store}/bin/p0-planted"
            f" && chmod +x {store}/bin/p0-planted"
        ),
        "W5_overwrite_bin": f"echo x >> {store}/bin/agentapi",
        "W5_builtin": f"echo x > {store}/builtin/.checksum",
        # X3: another session's conversation.
        "X3_read_other": f"cat {store}/{_OTHER}",
        # agy's settings: still read-only (BUG-108).
        "write_settings": f"echo x >> {store}/settings.json",
        # What agy does at each call: still possible, in its own store.
        "write_conversation": (
            f"mkdir -p {store}/conversations && echo c > {store}/conversations/c.db"
        ),
    }
    lines = ["cat >/dev/null", "R="]
    for name, cmd in checks.items():
        lines.append(
            f'if ( {cmd} ) >/dev/null 2>&1; then R="$R {name}=yes"; '
            f'else R="$R {name}=no"; fi'
        )
    return "\n".join(lines) + "\n" + _answer("$R")


def _no_private_store_left(setup) -> None:
    assert list(setup["calls"].glob("agy-store-*")) == []
    assert os.listdir(setup["spawn"]) == []
    assert os.listdir(setup["calls"]) == []


async def test_the_p0_5_attacks_on_the_store_are_blocked(
    setup, real_bwrap, shell_in_cage
):
    store = _fill_shared(setup)
    before = _snapshot(store)
    _install(setup["bin"], "agy", _attacks(str(store)))

    result = await cs.complete("agy", dict(_BODY))

    assert _report(_content(result)) == {
        "R8_read_token": "yes",
        # Rewritten in the call's copy only.
        "W6_rewrite_token": "yes",
        "W6_new_file": "yes",
        "W5_plant_bin": "yes",
        "W5_overwrite_bin": "yes",
        "W5_builtin": "yes",
        "X3_read_other": "no",
        "write_settings": "no",
        "write_conversation": "yes",
    }
    # Blocked: nothing of it reached the shared store, byte for byte.
    assert _snapshot(store) == before
    _no_private_store_left(setup)


async def test_a_token_rewritten_by_a_call_never_reaches_the_next(
    setup, real_bwrap, shell_in_cage
):
    """W6 across calls: what the first call puts in place of the token (a
    refresh, or another account's token) is gone; the next call reads the
    shared token again."""
    store = _fill_shared(setup)
    script = (
        "cat >/dev/null\n"
        f"T={store}/{_TOKEN}\n"
        'R="token=$(cat "$T")"\n'
        'printf \'forged-token\\n\' > "$T.tmp" && mv -f "$T.tmp" "$T"\n'
        "if [ -e "
        f'{store}/bin/p0-planted ]; then R="$R planted=seen"; '
        'else R="$R planted=none"; fi\n'
        f"printf '#!/bin/sh\\n' > {store}/bin/p0-planted\n" + _answer("$R")
    )
    _install(setup["bin"], "agy", script)

    first = await cs.complete("agy", dict(_BODY))
    second = await cs.complete("agy", dict(_BODY))

    assert _content(first) == "token=old-token planted=none"
    assert _content(second) == "token=old-token planted=none"
    assert (store / _TOKEN).read_text() == "old-token\n"
    assert not (store / "bin" / "p0-planted").exists()
    _no_private_store_left(setup)


async def test_a_call_never_sees_the_conversation_of_a_call_in_progress(
    setup, real_bwrap, shell_in_cage
):
    """X3 between two calls running at once: A writes its conversation and
    waits; B, started meanwhile, looks for it where agy keeps it."""
    store = _fill_shared(setup)
    marker = f"{uuid.uuid4().int % 10**9:09d}"
    script = (
        "PROMPT=$(cat)\n"
        f"C={store}/conversations\n"
        'case "$PROMPT" in\n'
        '  *session-A*) mkdir -p "$C" && echo "prompt of A" > "$C/sess-A.db"\n'
        f"    sleep 4.{marker}\n" + "    " + _answer("A=done") + "    ;;\n"
        '  *) if cat "$C/sess-A.db" >/dev/null 2>&1; then R=seen; else R=hidden; fi\n'
        "    " + _answer("B=$R") + "    ;;\n"
        "esac\n"
    )
    _install(setup["bin"], "agy", script)
    body_a = {**_BODY, "messages": [{"role": "user", "content": "session-A"}]}
    task_a = asyncio.ensure_future(cs.complete("agy", body_a))
    try:
        deadline = time.monotonic() + 15
        while not list(setup["calls"].glob("agy-store-*/conversations/sess-A.db")):
            assert time.monotonic() < deadline, "A never wrote its conversation"
            await asyncio.sleep(0.05)

        result_b = await cs.complete("agy", dict(_BODY))
        result_a = await task_a
    finally:
        if not task_a.done():
            task_a.cancel()
            with contextlib.suppress(BaseException):
                await task_a
        for pid in _processes_with(f"4.{marker}".encode()):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)

    assert _content(result_b) == "B=hidden"
    assert _content(result_a) == "A=done"
    assert not (store / "conversations" / "sess-A.db").exists()
    _no_private_store_left(setup)


@pytest.mark.parametrize("end", ["failed", "timeout"])
async def test_the_private_store_is_removed_however_the_call_ends(
    setup, real_bwrap, shell_in_cage, end
):
    store = _fill_shared(setup)
    before = _snapshot(store)
    marker = f"{uuid.uuid4().int % 10**9:09d}"
    tail = "exit 3\n" if end == "failed" else f"sleep 60.{marker}\n"
    _install(
        setup["bin"],
        "agy",
        "cat >/dev/null\n"
        f"echo forged > {store}/{_TOKEN}\n"
        f"mkdir -p {store}/brain/x && echo prompt > {store}/brain/x/t\n"
        # Something agy cannot remove later: the gateway still can.
        f"mkdir -p {store}/cache/locked && echo x > {store}/cache/locked/f"
        f" && chmod 500 {store}/cache/locked\n" + tail,
    )
    try:
        with pytest.raises(cs.CliSubprocessError) as info:
            await cs.complete(
                "agy", dict(_BODY), timeout_s=3.0 if end == "timeout" else 30.0
            )
    finally:
        for pid in _processes_with(f"60.{marker}".encode()):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)

    assert info.value.timed_out is (end == "timeout")
    assert _snapshot(store) == before
    _no_private_store_left(setup)


async def test_a_cage_refused_after_the_store_was_made_leaves_nothing(
    setup, monkeypatch
):
    """The private store is made before the cage's checks: when one of them
    refuses, nothing is launched and the store is removed all the same."""
    _fill_shared(setup)
    ran = setup["tmp"] / "agy-ran"
    _install(setup["bin"], "agy", f"touch {ran}\n")
    monkeypatch.setattr(agy_cage, "_relay_python", _refuse)

    with pytest.raises(cs.CliSubprocessError) as info:
        await cs.complete("agy", dict(_BODY))

    assert info.value.sandbox_failed is True
    assert not ran.exists()
    _no_private_store_left(setup)


def _refuse():
    raise agy_cage.CageUnavailable("the relay's Python interpreter is missing")


async def test_a_private_store_that_cannot_be_made_launches_nothing(setup, monkeypatch):
    _fill_shared(setup)
    ran = setup["tmp"] / "agy-ran"
    _install(setup["bin"], "agy", f"touch {ran}\n")
    real_mkdtemp = cs.tempfile.mkdtemp

    def mkdtemp(*args, prefix=None, **kwargs):
        if prefix == "agy-store-":
            raise OSError(28, "No space left on device")
        return real_mkdtemp(*args, prefix=prefix, **kwargs)

    monkeypatch.setattr(cs.tempfile, "mkdtemp", mkdtemp)

    with pytest.raises(cs.CliSubprocessError) as info:
        await cs.complete("agy", dict(_BODY))

    assert info.value.sandbox_failed is True
    assert "agy private store cannot be created (OSError)" in str(info.value)
    assert not ran.exists()
    assert os.listdir(setup["spawn"]) == []
    assert os.listdir(setup["calls"]) == []


# ── The copy never holds the event loop, and waits for the call's slot ──────


def _slow(real, seconds: float, calls: list):
    def slow(*args, **kwargs):
        calls.append(time.monotonic())
        time.sleep(seconds)
        return real(*args, **kwargs)

    return slow


async def test_filling_and_removing_the_store_never_block_the_event_loop(
    setup, real_bwrap, shell_in_cage, monkeypatch
):
    """A slow copy (a large bin/, a slow disk) and a slow removal run in a
    thread: the gateway keeps serving meanwhile."""
    from plugboard import agy_store

    _fill_shared(setup)
    _install(setup["bin"], "agy", "cat >/dev/null\n" + _answer("OK"))
    fills, removals = [], []
    monkeypatch.setattr(
        agy_cage, "_fill_private_store", _slow(agy_cage._fill_private_store, 0.8, fills)
    )
    monkeypatch.setattr(agy_store, "_remove", _slow(agy_store._remove, 0.8, removals))
    # The cage's once-per-process /proc check (it runs bwrap, held on the
    # loop by design, under 5 s): done before the measure.
    agy_cage._proc_mountable(agy_cage._bwrap(), {})
    gaps = []

    async def ticker():
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    tick = asyncio.ensure_future(ticker())
    try:
        result = await cs.complete("agy", dict(_BODY))
    finally:
        tick.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tick

    assert _content(result) == "OK"
    assert len(fills) == 1 and len(removals) == 1
    assert max(gaps) < 0.4, f"the event loop was held {max(gaps):.2f}s"
    _no_private_store_left(setup)


async def test_the_store_is_filled_only_once_the_call_has_its_slot(
    setup, real_bwrap, shell_in_cage, monkeypatch
):
    """With one CLI slot, a second call waits for it with an empty store of
    its own: nothing is copied for a call still queued."""
    store = _fill_shared(setup)
    monkeypatch.setattr(cs, "_CONCURRENCY_LIMIT", 1)
    monkeypatch.setattr(cs, "_semaphores", {})
    fills = []
    monkeypatch.setattr(
        agy_cage, "_fill_private_store", _slow(agy_cage._fill_private_store, 0, fills)
    )
    marker = f"{uuid.uuid4().int % 10**9:09d}"
    _install(
        setup["bin"],
        "agy",
        "PROMPT=$(cat)\n"
        f'case "$PROMPT" in *first*) sleep 2.{marker} ;; esac\n' + _answer("OK"),
    )
    first = {**_BODY, "messages": [{"role": "user", "content": "first"}]}
    task_a = asyncio.ensure_future(cs.complete("agy", first))
    task_b = None
    try:
        deadline = time.monotonic() + 15
        while not _processes_with(f"2.{marker}".encode()):
            assert time.monotonic() < deadline, "the first call never started"
            await asyncio.sleep(0.02)
        task_b = asyncio.ensure_future(cs.complete("agy", dict(_BODY)))
        while len(list(setup["calls"].glob("agy-store-*"))) < 2:
            assert time.monotonic() < deadline, "the second call never queued"
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.3)

        # The first call has its copy; the second, queued, an empty store.
        assert len(fills) == 1
        stores = sorted(
            setup["calls"].glob("agy-store-*"), key=lambda p: len(os.listdir(p))
        )
        assert os.listdir(stores[0]) == []
        assert (stores[1] / _TOKEN).read_text() == (store / _TOKEN).read_text()

        results = await asyncio.gather(task_a, task_b)
    finally:
        for task in (task_a, task_b):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task

    assert [_content(r) for r in results] == ["OK", "OK"]
    assert len(fills) == 2
    _no_private_store_left(setup)


# ── Whatever agy leaves in its store, the store is removed ──────────────────


@pytest.mark.parametrize("mode", ["000", "100", "500"])
async def test_the_store_is_removed_even_when_agy_locks_its_root(
    setup, real_bwrap, shell_in_cage, mode
):
    _fill_shared(setup)
    _install(
        setup["bin"],
        "agy",
        "cat >/dev/null\n"
        'S="$HOME/.gemini/antigravity-cli"\n'
        'mkdir -p "$S/brain/x" && echo prompt > "$S/brain/x/t"\n'
        f'chmod {mode} "$S/brain/x" "$S"\n' + _answer("OK"),
    )

    result = await cs.complete("agy", dict(_BODY))

    assert _content(result) == "OK"
    _no_private_store_left(setup)


async def test_a_very_deep_tree_left_by_agy_is_removed(
    setup, real_bwrap, shell_in_cage
):
    """Deeper than Python's recursion limit (shutil.rmtree recurses)."""
    _fill_shared(setup)
    _install(
        setup["bin"],
        "agy",
        "cat >/dev/null\n"
        'cd "$HOME/.gemini/antigravity-cli" && i=0\n'
        "while [ $i -lt 1200 ]; do mkdir a && cd a || break; i=$((i+1)); done\n"
        "echo x > f\n" + _answer("depth=$i"),
    )

    result = await cs.complete("agy", dict(_BODY))

    assert _content(result) == "depth=1200"
    _no_private_store_left(setup)


async def test_links_left_by_agy_are_removed_never_their_targets(
    setup, real_bwrap, shell_in_cage
):
    """As the purge of BUG-97 did: a link is removed as a link."""
    _fill_shared(setup)
    target_file = setup["app_data"] / "providers.json"
    target_dir = setup["app_data"]
    _install(
        setup["bin"],
        "agy",
        "cat >/dev/null\n"
        'S="$HOME/.gemini/antigravity-cli"\n'
        f'ln -s {target_file} "$S/file-link" && ln -s {target_dir} "$S/dir-link"\n'
        f'mkdir -p "$S/brain" && ln -s {target_dir} "$S/brain/inner-link"\n'
        + _answer("OK"),
    )

    result = await cs.complete("agy", dict(_BODY))

    assert _content(result) == "OK"
    assert target_file.read_text() == "app-data-secret"
    assert sorted(os.listdir(target_dir)) == ["providers.json"]
    _no_private_store_left(setup)


async def test_a_failed_removal_is_logged_and_the_answer_still_served(
    setup, real_bwrap, shell_in_cage, monkeypatch, caplog
):
    from plugboard import agy_store

    _fill_shared(setup)
    _install(setup["bin"], "agy", "cat >/dev/null\n" + _answer("OK"))
    prefix = str(setup["calls"] / agy_store.PRIVATE_PREFIX)
    real_rmdir = os.rmdir

    def rmdir(path, *args, **kwargs):
        if str(path).startswith(prefix):
            raise OSError(16, "Device or resource busy")
        return real_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "rmdir", rmdir)
    caplog.set_level("WARNING", logger="plugboard.agy_store")

    result = await cs.complete("agy", dict(_BODY))

    monkeypatch.setattr(os, "rmdir", real_rmdir)
    assert _content(result) == "OK"
    messages = [
        r.getMessage() for r in caplog.records if r.name == "plugboard.agy_store"
    ]
    assert messages == ["agy private store left in place after the call (OSError)"]
    assert str(setup["calls"]) not in " ".join(messages)
    (left,) = setup["calls"].glob("agy-store-*")
    agy_store.remove_private_store(str(left))
    assert not left.exists()


async def test_a_copy_that_fails_refuses_the_call_and_leaves_nothing(
    setup, monkeypatch
):
    import errno

    _fill_shared(setup)
    ran = setup["tmp"] / "agy-ran"
    _install(setup["bin"], "agy", f"touch {ran}\n")

    def full(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(agy_cage, "_copy_file", full)

    with pytest.raises(cs.CliSubprocessError) as info:
        await cs.complete("agy", dict(_BODY))

    assert info.value.sandbox_failed is True
    assert "agy private store cannot be filled (OSError)" in str(info.value)
    assert not ran.exists()
    _no_private_store_left(setup)


def _deep_tree(root: Path, depth: int) -> None:
    """``depth`` nested folders ``a/a/…`` under ``root``, made through
    descriptors: never a path longer than one name, whatever ``root``."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for _ in range(depth):
            os.mkdir("a", dir_fd=fd)
            child = os.open("a", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
    finally:
        os.close(fd)


@contextlib.contextmanager
def _recursion_limit(limit: int):
    """Python's recursion limit set to ``limit``, restored on exit. The
    garbage left by earlier tests is collected first and the collector held
    meanwhile: a finalizer run at the bottom of the lowered stack would fail
    on it (an unraisable RecursionError) and not be the test's own."""
    import gc
    import sys

    gc.collect()
    collecting = gc.isenabled()
    gc.disable()
    before = sys.getrecursionlimit()
    sys.setrecursionlimit(limit)
    try:
        yield
    finally:
        sys.setrecursionlimit(before)
        if collecting:
            gc.enable()


async def test_a_too_deep_shared_bin_refuses_the_call_and_leaves_nothing(setup):
    """A 1200-level tree in bin/: the copy recurses (one call and two open
    descriptors per level), so it stops on the first limit reached:
    Python's stack (RecursionError) or, under a soft limit of 1024 open
    files as in the CI, EMFILE (OSError). Either way the call is refused
    before agy runs and nothing is left (BUG-157)."""
    from plugboard import agy_store

    store = _fill_shared(setup)
    ran = setup["tmp"] / "agy-ran"
    _install(setup["bin"], "agy", f"touch {ran}\n")
    deep = store / "bin" / "deep"
    deep.mkdir()
    _deep_tree(deep, 1200)
    try:
        with pytest.raises(cs.CliSubprocessError) as info:
            await cs.complete("agy", dict(_BODY))

        assert info.value.sandbox_failed is True
        assert any(
            f"agy private store cannot be filled ({kind})" in str(info.value)
            for kind in ("RecursionError", "OSError")
        ), str(info.value)
        assert not ran.exists()
        _no_private_store_left(setup)
    finally:
        agy_store.remove_private_store(str(deep))


async def test_a_shared_bin_deeper_than_the_stack_refuses_the_call(setup):
    """RecursionError itself, whatever the run's limits: the recursion limit
    is lowered to a known margin above the current depth, so that a short
    tree (far from any limit on open files or path length) is deeper than
    the stack. The call is refused before agy runs, nothing left (BUG-157)."""
    import inspect

    from plugboard import agy_store

    store = _fill_shared(setup)
    ran = setup["tmp"] / "agy-ran"
    _install(setup["bin"], "agy", f"touch {ran}\n")
    deep = store / "bin" / "deep"
    deep.mkdir()
    _deep_tree(deep, 300)
    try:
        with _recursion_limit(len(inspect.stack(0)) + 150):
            with pytest.raises(cs.CliSubprocessError) as info:
                await cs.complete("agy", dict(_BODY))

        assert info.value.sandbox_failed is True
        assert "agy private store cannot be filled (RecursionError)" in str(info.value)
        assert not ran.exists()
        _no_private_store_left(setup)
    finally:
        agy_store.remove_private_store(str(deep))
