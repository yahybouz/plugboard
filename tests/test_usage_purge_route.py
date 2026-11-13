"""POST /admin/usage/purge with a malformed month in the store (BUG-69)."""

from __future__ import annotations

ADMIN = {"Authorization": "Bearer adminkey"}


def _months(path) -> list:
    import sqlite3

    conn = sqlite3.connect(str(path))
    try:
        return sorted(r[0] for r in conn.execute("SELECT month FROM usage_entries"))
    finally:
        conn.close()


async def test_a_malformed_month_is_skipped_not_a_500(
    admin_client, monkeypatch, tmp_path
):
    from datetime import datetime, timezone

    from plugboard import usage

    monkeypatch.setattr(usage, "_DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(usage, "_schema_ready", {})
    current = datetime.now(timezone.utc).strftime("%Y_%m")
    conn = usage._get_conn()
    try:
        for row_id, month in (
            ("old", "2001_01"),
            ("bad", "xx_yy"),
            # Fullwidth digits: \\d matched them and int() read 2001 (review M-9).
            ("wide", "\uff12\uff10\uff10\uff11_\uff10\uff11"),
            ("now", current),
        ):
            conn.execute(
                "INSERT INTO usage_entries (id, ts, month, client_id, key_id, model, "
                "provider_id, prompt_tokens, completion_tokens, total_tokens, "
                "pricing_month, cost_usd) VALUES (?, '', ?, 'c', '', 'm', 'p', 0, 0, 0, ?, 0)",
                (row_id, month, month),
            )
        conn.commit()
    finally:
        conn.close()

    r = await admin_client.post("/admin/usage/purge?keep_months=1", headers=ADMIN)

    assert r.status_code == 200, r.text
    assert r.json()["purged"] == ["2001_01"]
    # Left as it is, like the automatic retention pass does.
    assert _months(usage._DB_PATH) == sorted(
        [current, "xx_yy", "\uff12\uff10\uff10\uff11_\uff10\uff11"]
    )
