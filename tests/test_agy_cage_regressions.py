"""What agy's calls already guaranteed still holds inside the cage (BUG-86).

The purge of agy's store (BUG-97), the private ``TMPDIR``, the answer read as
it is written, a call cut by a timeout or a cancellation, and the usage agy
reports were tested with a fake agy run outside bwrap (``agy_without_cage``,
see conftest). Here the same behaviours go through ``cli_subprocess.complete``
with the host's real ``bwrap`` around a fake agy (a shell script): agy's store
and ``TMPDIR`` are bound into the cage at their real paths, so what the fake
agy writes there is what the gateway finds, purges and removes afterwards.

The shell needs ``/usr/bin`` and the ``/bin`` link, passed through
``cage_argv``'s test-only parameter (see tests/test_agy_cage.py). Skipped,
with the reason, where bwrap cannot create namespaces.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import os
import signal
import time
import uuid
from pathlib import Path

import pytest

from plugboard import agy_cage, agy_store
from plugboard import cli_subprocess as cs
from tests.test_agy_cage import (
    _SHELL_RO,
    _bwrap_unusable_reason,
    _install,
    _processes_with,
)

# The real one, before conftest turns the purge off for the other tests.
_REAL_STORE_DIR = agy_store.store_dir

_OLD = "0b7c3a1e-5f00-4d2e-9a11-000000000000"  # a conversation already there
_A = "6c1d2e3f-0a4b-4c5d-8e9f-a0b1c2d3e4f5"
_BODY = {
    "model": "gemini-test",
    "messages": [{"role": "user", "content": "prompt-in-clear-91c2"}],
}
# Per conversation, where agy 1.4.4 writes (see agy_store).
_ENTRIES = (
    "conversations/{cid}.db",
    "conversations/{cid}.db-wal",
    "brain/{cid}/.system_generated/logs/transcript.jsonl",
    "annotations/{cid}.pbtxt",
    "presence/{cid}.lock",
)
_SHARED = ("conversation_summaries.db", "conversation_summaries.db-wal")
_USAGE = (
    '{"input_tokens":1234,"output_tokens":56,"thinking_tokens":7,'
    '"cache_read_tokens":1000,"total_tokens":1290}'
)
_EXPECTED_USAGE = {
    "prompt_tokens": 1234,
    "completion_tokens": 56,
    "total_tokens": 1290,
    "prompt_tokens_details": {"cached_tokens": 1000},
    "completion_tokens_details": {"reasoning_tokens": 7},
}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """The real bwrap around a fake agy, agy's real store lookup, a throwaway
    HOME whose store holds a sign-in token and an older conversation."""
    reason = _bwrap_unusable_reason()
    if reason:
        pytest.skip(f"bwrap unusable: {reason}")
    monkeypatch.setattr(agy_store, "store_dir", _REAL_STORE_DIR)
    monkeypatch.setattr(
        agy_cage,
        "cage_argv",
        functools.partial(agy_cage.cage_argv, _extra_ro_for_tests=_SHELL_RO),
    )
    home = tmp_path / "home"
    store = home / ".gemini" / "antigravity-cli"
    store.mkdir(parents=True)
    (home / ".gemini" / "config").mkdir()
    (home / ".gemini" / "projects.json").write_text("{}\n")
    (store / "antigravity-oauth-token").write_text("old-token\n")
    (store / "installation_id").write_text("install")
    for rel in _ENTRIES:
        path = store / rel.format(cid=_OLD)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("old")
    spawn = tmp_path / "spawn"
    spawn.mkdir(mode=0o700)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setattr(cs, "_SPAWN_CWD_CACHE", str(spawn))
    monkeypatch.delenv("PLUGBOARD_SUBPROCESS_CWD", raising=False)
    monkeypatch.setattr(cs, "_binary_path_cache", {})
    return {"store": store, "spawn": spawn, "bin": bin_dir}


def _fake_agy(setup, body: str = "") -> Path:
    """A fake agy that writes one conversation ``_A`` in its store the way agy
    does (prompt in clear), the shared summaries, refreshes its token by a
    temporary file and a rename, runs ``body`` (which sets ``ANSWER`` and may
    write deltas with ``delta``), then writes agy's ``json`` payload or, with
    ``stream-json``, the init event first and the result event last."""
    entries = " ".join(rel.format(cid="$CID") for rel in _ENTRIES)
    script = f"""PROMPT=$(cat)
S="$HOME/.gemini/antigravity-cli"
CID='{_A}'
STREAM=no
case " $* " in *" stream-json "*) STREAM=yes ;; esac
delta() {{
  printf '{{"event":"step_update","step_update":{{"conversation_id":"%s","step_index":1,"state":"ACTIVE","step_type":"agent_response","text_delta":"%s"}}}}\\n' "$CID" "$1"
}}
mkdir -p "$S/conversations" "$S/brain/$CID/.system_generated/logs" "$S/annotations" "$S/presence"
for f in {entries}; do printf '%s' "$PROMPT" > "$S/$f"; done
for f in {" ".join(_SHARED)}; do printf '%s' "$CID" >> "$S/$f"; done
printf 'refreshed-token\\n' > "$S/antigravity-oauth-token.tmp"
mv -f "$S/antigravity-oauth-token.tmp" "$S/antigravity-oauth-token"
if [ "$STREAM" = yes ]; then
  printf '{{"event":"init","conversation_id":"%s","init":{{}}}}\\n' "$CID"
fi
FILES=yes
for f in {entries}; do [ -e "$S/$f" ] || FILES=no; done
ANSWER="files=$FILES"
{body}
PAYLOAD=$(printf '{{"conversation_id":"%s","status":"SUCCESS","response":"%s","usage":%s}}' "$CID" "$ANSWER" '{_USAGE}')
if [ "$STREAM" = yes ]; then
  printf '{{"event":"result","result":%s}}\\n' "$PAYLOAD"
else
  printf '%s\\n' "$PAYLOAD"
fi
"""
    return _install(setup["bin"], "agy", script)


def _entries_of(store: Path, cid: str) -> list:
    return sorted(
        f"{sub}/{name}"
        for sub in agy_store.CONVERSATION_DIRS
        if (store / sub).is_dir()
        for name in os.listdir(store / sub)
        if name == cid or name.startswith(cid + ".")
    )


def _assert_purged(setup) -> None:
    """``_A`` and the shared summaries gone; the token refreshed by agy, the
    older conversation and agy's own files left as they were."""
    store = setup["store"]
    assert _entries_of(store, _A) == []
    assert [n for n in _SHARED if (store / n).exists()] == []
    assert len(_entries_of(store, _OLD)) == len(_ENTRIES)
    for rel in _ENTRIES:
        assert (store / rel.format(cid=_OLD)).read_text() == "old"
    assert (store / "antigravity-oauth-token").read_text() == "refreshed-token\n"
    assert (store / "installation_id").read_text() == "install"


def _content(result) -> str:
    return result["choices"][0]["message"]["content"]


async def _until(check, what: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} never happened")
        await asyncio.sleep(0.02)


# ── 1. Purge of agy's store (BUG-97) ────────────────────────────────────────


async def test_the_store_is_purged_under_the_cage(setup):
    _fake_agy(setup)
    token = setup["store"] / "antigravity-oauth-token"
    token_inode = token.stat().st_ino

    result = await cs.complete("agy", dict(_BODY))

    # Written through the cage into the real store, there until agy ended.
    assert _content(result) == "files=yes"
    _assert_purged(setup)
    assert token.stat().st_ino != token_inode, "rewritten by rename"


# ── 2. Private TMPDIR ───────────────────────────────────────────────────────


async def test_each_call_gets_a_private_tmpdir_removed_after_it(setup):
    _fake_agy(
        setup,
        'touch "$TMPDIR/written-by-agy"\n'
        'ANSWER="tmpdir=$TMPDIR cwd=$(pwd -P) mode=$(stat -c %a "$TMPDIR")"',
    )
    spawn = str(setup["spawn"])

    seen = []
    for _ in range(2):
        result = await cs.complete("agy", dict(_BODY))
        seen.append(dict(part.split("=", 1) for part in _content(result).split()))

    for one in seen:
        assert os.path.dirname(one["tmpdir"]) == spawn
        assert os.path.basename(one["tmpdir"]).startswith("agy-tmp-")
        assert one["cwd"] == one["tmpdir"], "the cage starts agy in its TMPDIR"
        assert one["mode"] == "700"
        assert not os.path.exists(one["tmpdir"]), "removed after the call"
    assert seen[0]["tmpdir"] != seen[1]["tmpdir"]
    assert os.listdir(spawn) == []


# ── 3. The answer read as it is written ─────────────────────────────────────

_PIECES = ("Bon", "jour", " tout", " le monde")


async def test_the_answer_streams_through_the_cage(setup):
    _fake_agy(
        setup,
        'ANSWER="Bonjour tout le monde"\n'
        'if [ "$STREAM" = yes ]; then\n'
        f'  delta "{_PIECES[0]}"; sleep 0.6\n'
        + "".join(f'  delta "{piece}"\n' for piece in _PIECES[1:])
        + "fi",
    )

    plain = await cs.complete("agy", dict(_BODY))
    _assert_purged(setup)

    pieces: list = []
    t0 = time.perf_counter()
    streamed = await cs.complete(
        "agy",
        dict(_BODY),
        on_delta=lambda text: pieces.append((time.perf_counter() - t0, text)),
    )
    total = time.perf_counter() - t0

    assert [text for _, text in pieces] == list(_PIECES)
    assert pieces[0][0] < total - 0.4, "first piece long before the end"
    assert _content(streamed) == _content(plain) == "Bonjour tout le monde"
    assert streamed["usage"] == plain["usage"] == _EXPECTED_USAGE
    _assert_purged(setup)
    assert os.listdir(setup["spawn"]) == []


# ── 4. A call cut by a timeout or a cancellation ────────────────────────────


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
@pytest.mark.parametrize("cut", ["cancel", "timeout"])
async def test_a_cut_call_leaves_no_process_no_entry_no_tmpdir(setup, stream, cut):
    """agy writes its conversation (and, streamed, its first event naming it),
    leaves a program in the background, then sleeps: cut, nothing of it is
    left, neither in /proc, nor in its store, nor its TMPDIR."""
    marker = f"{uuid.uuid4().int % 10**9:09d}"
    binary = _fake_agy(
        setup,
        '[ "$STREAM" = yes ] && delta "started"\n'
        f"sleep 300.{marker} </dev/null >/dev/null 2>&1 &\n"
        'touch "$TMPDIR/ready"\n'
        f"sleep 301.{marker}\n",
    )
    markers = (f".{marker}".encode(), str(binary).encode())
    pieces: list = []
    task = asyncio.ensure_future(
        cs.complete(
            "agy",
            dict(_BODY),
            timeout_s=3.0 if cut == "timeout" else 60.0,
            on_delta=pieces.append if stream else None,
        )
    )
    try:
        ready = []
        await _until(
            lambda: ready.extend(setup["spawn"].glob("agy-tmp-*/ready")) or ready,
            "agy ready",
        )
        tmpdir = ready[0].parent
        if stream:
            await _until(lambda: pieces == ["started"], "first event read")
        assert len(_entries_of(setup["store"], _A)) == len(_ENTRIES)
        assert all(_processes_with(m) for m in markers), "agy runs in its cage"

        if cut == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(cs.CliSubprocessError) as info:
                await task
            assert info.value.timed_out is True
            assert info.value.sandbox_failed is False

        left = {m: _processes_with(m) for m in markers}
        deadline = time.monotonic() + 5.0
        while any(left.values()) and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            left = {m: _processes_with(m) for m in markers}
        assert left == {m: [] for m in markers}, "no process of agy left"
        _assert_purged(setup)
        assert not tmpdir.exists()
        assert os.listdir(setup["spawn"]) == []
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        for m in markers:
            for pid in _processes_with(m):
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)


# ── 5. The usage agy reports, billed the same ───────────────────────────────


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
async def test_reported_usage_is_the_same_with_and_without_the_cage(
    setup, request, stream
):
    _fake_agy(
        setup,
        "CAGED=no\n"
        "grep -q bwrap /proc/1/cmdline 2>/dev/null && CAGED=yes\n"
        'ANSWER="caged=$CAGED"\n'
        '[ "$STREAM" = yes ] && delta "$ANSWER"',
    )
    on_delta = (lambda text: None) if stream else None

    caged = await cs.complete("agy", dict(_BODY), on_delta=on_delta)
    request.getfixturevalue("agy_without_cage")  # from here on, no bwrap
    bare = await cs.complete("agy", dict(_BODY), on_delta=on_delta)

    assert (_content(caged), _content(bare)) == ("caged=yes", "caged=no")
    assert caged["usage"] == bare["usage"] == _EXPECTED_USAGE
    _assert_purged(setup)
