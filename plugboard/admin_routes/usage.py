"""Usage reporting and retention (admin).

Routes ``/admin/usage/{month}`` (read and delete),
``/admin/usage/{month}/export``, ``/admin/usage/{month}/key/{key_id}``,
``/admin/usage/purge``, ``/admin/retention``, ``/admin/usage/migrate`` and
``/admin/usage/{month}/{client_id}``, moved from ``plugboard/app.py`` (PLU-36),
behaviour unchanged. ``core`` is the ``plugboard.app`` module that registers
them: ``_require_admin``, ``_month_param`` and ``_usage`` are read as
``core.<name>`` when a handler runs.
"""

from __future__ import annotations

from types import ModuleType, SimpleNamespace

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import Response


def register(app: FastAPI, core: ModuleType) -> SimpleNamespace:
    """Declare this block's routes on ``app``, in their original order."""

    @app.get("/admin/usage/{month}")
    async def admin_usage_month(
        month: str,
        page: int = 1,
        limit: int = 100,
        _: None = Depends(core._require_admin),
    ):
        month = core._month_param(month)
        entries, total = core._usage.get_usage_paginated(month, page, limit)
        return {
            "month": month,
            "page": page,
            "limit": limit,
            "total": total,
            "entries": entries,
            "summary": core._usage.summarize_usage_month(
                month
            ),  # summary is always on ALL entries
        }

    @app.delete("/admin/usage/{month}", status_code=204)
    async def delete_usage_month(month: str, _: None = Depends(core._require_admin)):
        month = core._month_param(month)
        if not core._usage.delete_usage(month):
            raise HTTPException(404, f"No usage data for month '{month}'")

    @app.get("/admin/usage/{month}/export")
    async def export_usage(
        month: str, format: str = "json", _: None = Depends(core._require_admin)
    ):
        """Export usage data as CSV or JSON."""
        month = core._month_param(month)
        entries = core._usage.get_usage(month)
        if format == "csv":
            import csv
            import io

            output = io.StringIO()
            if entries:
                writer = csv.DictWriter(output, fieldnames=entries[0].keys())
                writer.writeheader()
                writer.writerows(entries)
            return Response(
                content=output.getvalue(),
                media_type="text/csv",
                headers={
                    "Content-Disposition": f"attachment; filename=usage_{month}.csv"
                },
            )
        return {
            "month": month,
            "entries": entries,
            "summary": core._usage.summarize_usage(entries),
        }

    @app.get("/admin/usage/{month}/key/{key_id}")
    async def admin_usage_by_key(
        month: str, key_id: str, _: None = Depends(core._require_admin)
    ):
        month = core._month_param(month)
        entries = core._usage.get_usage_by_key(month, key_id)
        return {
            "month": month,
            "key_id": key_id,
            "entries": entries,
            "summary": core._usage.summarize_usage(entries),
        }

    @app.post("/admin/usage/purge")
    async def purge_old_usage(
        keep_months: int = Query(6, ge=1), _: None = Depends(core._require_admin)
    ):
        """Delete usage files older than `keep_months` months."""
        deleted = core._usage.purge_old_usage(keep_months)
        return {"purged": deleted, "keep_months": keep_months}

    @app.get("/admin/retention")
    async def admin_retention(_: None = Depends(core._require_admin)):
        """Retention settings and the report of the last retention pass."""
        from plugboard import retention

        return {
            "settings": retention.settings(),
            "last_run": retention.last_report(),
            "held": retention.held(),
        }

    @app.post("/admin/usage/migrate")
    async def migrate_usage(_: None = Depends(core._require_admin)):
        """Import existing usage_*.json files into SQLite (idempotent)."""
        count = core._usage.migrate_json_to_sqlite()
        return {"migrated_entries": count}

    @app.get("/admin/usage/{month}/{client_id}")
    async def admin_usage_client(
        month: str, client_id: str, _: None = Depends(core._require_admin)
    ):
        month = core._month_param(month)
        entries = core._usage.get_usage_by_client(month, client_id)
        return {
            "month": month,
            "client_id": client_id,
            "entries": entries,
            "summary": core._usage.summarize_usage(entries),
        }

    return SimpleNamespace()
