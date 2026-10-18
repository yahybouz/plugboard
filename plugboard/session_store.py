"""Server-side chat session store (PERF-1).

Problem: clients that talk to a subprocess CLI (gemini / codex / claude)
pay a process-spawn tax on every turn, plus the full message history is
re-tokenised on each call. When a caller holds a long conversation, this
inflates cost and latency linearly.

Solution: expose an opt-in session ID. When set (via the
`X-LLM-Session-ID` header or the `session_id` body field), the server
stores the exchange server-side and, on the next call, *prepends the
stored history* to the new message. Callers only send the latest turn.

Scope deliberately small:
- SQLite-backed for durability across restarts.
- TTL: 24h by default (configurable via PLUGBOARD_SESSION_TTL_S).
- Isolated per client_id so Alice's session ID can't clobber Bob's.
- No streaming semantics here — we just manipulate the `messages` array
  before dispatch and record the assistant reply after.

Not in scope (future):
- Prompt caching at provider level.
- Resume flags on the CLI themselves (too fragile, proprietary).
- Cross-session token accounting (usage table already logs per-request).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from threading import Lock
from typing import Any, Dict, List, Optional
from plugboard.storage import data_dir

_DATA_DIR = data_dir()
_DB_PATH = _DATA_DIR / "sessions.db"
_TTL_S = int(os.environ.get("PLUGBOARD_SESSION_TTL_S", str(24 * 3600)))
_MAX_TURNS = int(os.environ.get("PLUGBOARD_SESSION_MAX_TURNS", "50"))

_INIT_LOCK = Lock()
_INITIALISED = False

_CREATE = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    client_id    TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    messages     TEXT NOT NULL,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    PRIMARY KEY (client_id, session_id)
);
"""

_INDEX = "CREATE INDEX IF NOT EXISTS idx_sessions_updated ON chat_sessions(updated_at);"


def _get_conn() -> sqlite3.Connection:
    global _INITIALISED
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    if not _INITIALISED:
        with _INIT_LOCK:
            if not _INITIALISED:
                conn.execute(_CREATE)
                conn.execute(_INDEX)
                conn.commit()
                _INITIALISED = True
    return conn


def load_session(client_id: str, session_id: str) -> List[Dict[str, Any]]:
    """Return the stored message list (empty if unknown/expired)."""
    if not client_id or not session_id:
        return []
    cutoff = time.time() - _TTL_S
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT messages, updated_at FROM chat_sessions "
            "WHERE client_id = ? AND session_id = ?",
            (client_id, session_id),
        ).fetchone()
        if not row:
            return []
        if row["updated_at"] < cutoff:
            # Expired: drop it so the DB stays tidy.
            conn.execute(
                "DELETE FROM chat_sessions WHERE client_id = ? AND session_id = ?",
                (client_id, session_id),
            )
            conn.commit()
            return []
        try:
            return json.loads(row["messages"])
        except json.JSONDecodeError:
            return []
    finally:
        conn.close()


def save_session(
    client_id: str, session_id: str, messages: List[Dict[str, Any]]
) -> None:
    """Upsert the message list, capped at MAX_TURNS."""
    if not client_id or not session_id or not messages:
        return
    # Cap: keep the most recent N turns. A "turn" = one message.
    if len(messages) > _MAX_TURNS:
        messages = messages[-_MAX_TURNS:]
    now = time.time()
    serialised = json.dumps(messages, ensure_ascii=False)
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO chat_sessions (client_id, session_id, messages, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(client_id, session_id) DO UPDATE SET
                 messages = excluded.messages,
                 updated_at = excluded.updated_at""",
            (client_id, session_id, serialised, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def delete_session(client_id: str, session_id: str) -> bool:
    conn = _get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM chat_sessions WHERE client_id = ? AND session_id = ?",
            (client_id, session_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def purge_expired() -> int:
    """Delete sessions older than TTL. Called by the watchdog sweep."""
    cutoff = time.time() - _TTL_S
    conn = _get_conn()
    try:
        cur = conn.execute("DELETE FROM chat_sessions WHERE updated_at < ?", (cutoff,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def merge_history_into_body(
    client_id: str,
    session_id: Optional[str],
    body: Dict[str, Any],
) -> Dict[str, Any]:
    """Return a NEW body with stored history prepended, when session_id set.

    Caller semantics:
      - No session_id → body returned unchanged.
      - Unknown session_id → body returned unchanged (first turn, the reply
        will be stored and future calls pick it up).
      - Known session_id → stored messages are prepended to body["messages"].
    """
    if not session_id:
        return body
    history = load_session(client_id, session_id)
    if not history:
        return body
    merged = dict(body)
    merged["messages"] = history + list(body.get("messages", []))
    return merged


def record_exchange(
    client_id: str,
    session_id: Optional[str],
    request_messages: List[Dict[str, Any]],
    assistant_reply: str,
) -> None:
    """Append the new user turn(s) + assistant reply to the session."""
    if not session_id:
        return
    existing = load_session(client_id, session_id)
    merged: List[Dict[str, Any]] = list(existing)
    # request_messages is what the CALLER sent (pre-merge). Only the tail
    # matters — existing already contains the prior turns. The convention
    # is: caller sends whatever new user/system messages they want appended.
    # We de-dup by length: if the incoming prefix equals history, skip the prefix.
    start = 0
    while (
        start < len(existing)
        and start < len(request_messages)
        and existing[start] == request_messages[start]
    ):
        start += 1
    merged.extend(request_messages[start:])
    if assistant_reply:
        merged.append({"role": "assistant", "content": assistant_reply})
    save_session(client_id, session_id, merged)
