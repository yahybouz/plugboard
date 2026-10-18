"""Tests for PERF-1 server-side chat session store."""

from __future__ import annotations

import time

import pytest


@pytest.fixture(autouse=True)
def _isolated_session_db(tmp_path, monkeypatch):
    """Point session_store to a throwaway SQLite file."""
    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path))
    # Re-import after env patch so the module-level paths resolve correctly.
    import importlib

    from plugboard import session_store

    importlib.reload(session_store)
    # Also reset the initialised flag so each test creates its own schema.
    session_store._INITIALISED = False
    return session_store


def test_empty_session_returns_empty_list(_isolated_session_db):
    ss = _isolated_session_db
    assert ss.load_session("alice", "conv1") == []


def test_save_then_load_roundtrip(_isolated_session_db):
    ss = _isolated_session_db
    msgs = [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello"},
    ]
    ss.save_session("alice", "conv1", msgs)
    assert ss.load_session("alice", "conv1") == msgs


def test_sessions_are_isolated_per_client(_isolated_session_db):
    """Alice and Bob using the same session_id must not see each other."""
    ss = _isolated_session_db
    ss.save_session("alice", "shared", [{"role": "user", "content": "alice-data"}])
    ss.save_session("bob", "shared", [{"role": "user", "content": "bob-data"}])
    assert ss.load_session("alice", "shared")[0]["content"] == "alice-data"
    assert ss.load_session("bob", "shared")[0]["content"] == "bob-data"


def test_expired_session_is_purged_on_read(_isolated_session_db, monkeypatch):
    ss = _isolated_session_db
    monkeypatch.setattr(ss, "_TTL_S", 1)
    ss.save_session("alice", "old", [{"role": "user", "content": "ancient"}])
    time.sleep(1.1)
    # Reading drops the expired row
    assert ss.load_session("alice", "old") == []


def test_purge_expired_deletes_old_rows(_isolated_session_db, monkeypatch):
    ss = _isolated_session_db
    monkeypatch.setattr(ss, "_TTL_S", 1)
    ss.save_session("alice", "s1", [{"role": "user", "content": "x"}])
    ss.save_session("alice", "s2", [{"role": "user", "content": "y"}])
    time.sleep(1.1)
    assert ss.purge_expired() == 2
    assert ss.load_session("alice", "s1") == []


def test_max_turns_cap(_isolated_session_db, monkeypatch):
    ss = _isolated_session_db
    monkeypatch.setattr(ss, "_MAX_TURNS", 4)
    msgs = [{"role": "user", "content": f"msg{i}"} for i in range(10)]
    ss.save_session("alice", "long", msgs)
    reloaded = ss.load_session("alice", "long")
    assert len(reloaded) == 4
    # Most recent turns preserved
    assert reloaded[-1]["content"] == "msg9"
    assert reloaded[0]["content"] == "msg6"


def test_delete_session_returns_true_when_present(_isolated_session_db):
    ss = _isolated_session_db
    ss.save_session("alice", "x", [{"role": "user", "content": "hi"}])
    assert ss.delete_session("alice", "x") is True
    assert ss.delete_session("alice", "x") is False


def test_merge_history_prepends_stored_messages(_isolated_session_db):
    ss = _isolated_session_db
    ss.save_session(
        "alice",
        "c",
        [
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
        ],
    )
    new_body = {
        "model": "gemini:gemini-2.5-flash",
        "messages": [{"role": "user", "content": "Q2"}],
    }
    merged = ss.merge_history_into_body("alice", "c", new_body)
    assert len(merged["messages"]) == 3
    assert merged["messages"][0]["content"] == "Q1"
    assert merged["messages"][-1]["content"] == "Q2"
    # Caller's body must not be mutated in place.
    assert len(new_body["messages"]) == 1


def test_merge_without_session_id_returns_body_unchanged(_isolated_session_db):
    ss = _isolated_session_db
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert ss.merge_history_into_body("alice", None, body) is body


def test_record_exchange_appends_new_turn_and_reply(_isolated_session_db):
    ss = _isolated_session_db
    # Existing history
    ss.save_session(
        "alice",
        "c",
        [
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
        ],
    )
    # Caller sends merged messages (history + new turn) — convention that
    # `merge_history_into_body` produces.
    merged_messages = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "Q2"},
    ]
    ss.record_exchange("alice", "c", merged_messages, "A2")
    history = ss.load_session("alice", "c")
    assert [m["content"] for m in history] == ["Q1", "A1", "Q2", "A2"]
