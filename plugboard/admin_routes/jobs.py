"""Background jobs and crons (admin).

Routes ``/admin/jobs``, ``/admin/jobs/{job_id}/log`` and
``/admin/jobs/{job_id}/run``, moved verbatim from ``plugboard/app.py``
(PLU-36). ``core`` is the ``plugboard.app`` module that registers them:
``_require_admin`` and ``_audit`` are read as ``core.<name>`` when a handler
runs; ``app`` is the application itself, read for ``app.state``.
"""

from __future__ import annotations

from types import ModuleType, SimpleNamespace

from fastapi import Depends, FastAPI, HTTPException


def register(app: FastAPI, core: ModuleType) -> SimpleNamespace:
    """Declare this block's routes on ``app``, in their original order."""

    @app.get("/admin/jobs")
    async def list_jobs(_: None = Depends(core._require_admin)):
        """Background jobs (price refresh, model refresh, watchdog) and their state."""
        from plugboard.jobs import get_jobs_status

        return get_jobs_status(app.state)

    @app.get("/admin/jobs/{job_id}/log")
    async def job_log(
        job_id: str, lines: int = 50, _: None = Depends(core._require_admin)
    ):
        """Last result of a background job."""
        from plugboard.jobs import get_job_log

        result = get_job_log(job_id, lines=lines, app_state=app.state)
        if "error" in result:
            raise HTTPException(404, result["error"])
        return result

    @app.post("/admin/jobs/{job_id}/run")
    async def run_job_endpoint(job_id: str, _: None = Depends(core._require_admin)):
        """Start a background job now; it runs on its own and the call returns at once."""
        from plugboard.jobs import run_job

        result = await run_job(job_id, app.state)
        # run_job returns a bare {"error": ...} (no "status") ONLY for genuine
        # not-found cases (unknown job id / missing script) -> 404. Every other
        # outcome (success / error / timeout / continuous) carries a "status" and is
        # returned as 200 so the dashboard can render the real message instead of a
        # generic 404 (which the frontend used to display as "undefined — Output").
        if "status" not in result and "error" in result:
            raise HTTPException(404, result["error"])
        core._audit(
            "job.run",
            target_type="job",
            target_id=job_id,
            details=result.get("status", ""),
        )
        return result

    return SimpleNamespace()
