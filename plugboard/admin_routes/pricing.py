"""Pricing grids, price suggestions, schedulers, catalogue upkeep and coverage (admin).

Routes ``/admin/pricing*`` (LLM-read prices, suggestions, month grids, scrape,
scheduler status), ``/admin/models/scheduler/status``, ``/admin/auto-update*``,
``/admin/catalog/history`` and ``/admin/coverage/*``, moved verbatim from
``plugboard/app.py`` (PLU-36). ``core`` is the ``plugboard.app`` module that
registers them: the names the tests patch there (``_audit``, ``list_models``,
``load_providers``) and the shared state that stays there (``_MONTH_RE``,
``_month_param``, ``_background_tasks``) are read as ``core.<name>`` when a
handler runs; ``app`` is the application itself, read for ``app.state`` (the
pricing and model-availability schedulers).
"""

from __future__ import annotations

import asyncio as _asyncio
import contextvars
import logging
from datetime import date
from types import ModuleType, SimpleNamespace
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from plugboard import billing as _billing

log = logging.getLogger("plugboard.app")


class PricingUpsert(BaseModel):
    models: Dict[str, Any]
    note: str = ""
    # None keeps the provider prices already in the grid.
    models_by_provider: Optional[Dict[str, Dict[str, Any]]] = None


class PricingPatch(BaseModel):
    models: Dict[str, Any] = Field(default_factory=dict)
    models_by_provider: Dict[str, Dict[str, Any]] = Field(default_factory=dict)


class PricingRevert(BaseModel):
    model: str = Field(min_length=1, max_length=300)
    # Price space (provider) of a models_by_provider entry; empty = vendor price.
    provider: str = Field(default="", max_length=200)


class SuggestionIds(BaseModel):
    ids: List[str] = Field(min_length=1, max_length=500)


def _load_grid_for_edit(month: str) -> Dict[str, Any]:
    try:
        return _billing.load_pricing(month, strict=True) or {"models": {}}
    except _billing.PricingFileError as exc:
        raise HTTPException(503, str(exc))


def _manual_rows(
    rows: Dict[str, Any], current: Dict[str, Any]
) -> Dict[str, Dict[str, Any]]:
    """Validate admin rows; mark as manual only those that change a price."""
    from plugboard.pricing_merge import MAX_PRICE, same_price

    out: Dict[str, Dict[str, Any]] = {}
    for model, row in rows.items():
        if not isinstance(row, dict):
            raise HTTPException(422, f"{model}: expected {{input_price, output_price}}")
        try:
            inp = float(row.get("input_price"))
            outp = float(row.get("output_price"))
        except (TypeError, ValueError):
            raise HTTPException(
                422, f"{model}: input_price and output_price must be numbers"
            )
        if not (0 <= inp <= MAX_PRICE and 0 <= outp <= MAX_PRICE):
            raise HTTPException(
                422,
                f"{model}: prices must be between 0 and {MAX_PRICE:g} USD per 1M tokens",
            )
        price = {k: v for k, v in row.items() if k not in ("source", "previous")}
        price.update(input_price=inp, output_price=outp)
        old = current.get(model)
        if old is not None and same_price(old, price):
            out[model] = {**old, **price}  # unchanged: keeps its source
        else:
            out[model] = {**price, "source": "manual"}
            # Keep the automatic price it replaces, so a revert can restore it
            # even before the next update fetches the sources again.
            if old is not None and old.get("source") != "manual":
                out[model]["previous"] = {
                    k: old[k]
                    for k in ("input_price", "output_price", "source")
                    if k in old
                }
            elif old is not None and old.get("previous"):
                out[model]["previous"] = old["previous"]
    return out


def register(app: FastAPI, core: ModuleType) -> SimpleNamespace:
    """Declare this block's routes on ``app``, in their original order."""

    def _check_month(month: str) -> None:
        if not core._MONTH_RE.match(month):
            raise HTTPException(422, "month must look like YYYY_MM")

    @app.get("/admin/pricing/llm/status")
    async def llm_pricing_status(_: None = Depends(core._require_admin)):
        """Prices read by a model: settings, today's spend and why each model has no price."""
        from plugboard import llm_pricing

        return llm_pricing.status()

    @app.post("/admin/pricing/llm/run")
    async def llm_pricing_run(_: None = Depends(core._require_admin)):
        """Run one pass now, ignoring the once-a-day rule (the daily budget still applies)."""
        from plugboard import llm_pricing

        if not llm_pricing.enabled():
            raise HTTPException(409, "LLM pricing is off: set PLUGBOARD_LLM_PRICING=1")
        report = await llm_pricing.run(await llm_pricing.missing_models(), force=True)
        core._audit(
            "pricing.llm.run",
            details=f"calls={report.calls} suggested={len(report.suggested)}",
        )
        return report.as_dict()

    @app.get("/admin/pricing/suggestions")
    async def list_pricing_suggestions(
        status: str = "pending", _: None = Depends(core._require_admin)
    ):
        """Price changes held for review (type drift) and LLM-read prices (type llm)."""
        from plugboard import pricing_suggestions

        return {"items": pricing_suggestions.list_suggestions(status or None)}

    async def _suggestion_action(sid: str, action: str) -> Dict[str, Any]:
        from plugboard import pricing_suggestions

        try:
            item = getattr(pricing_suggestions, action)(sid)
        except KeyError:
            raise HTTPException(404, f"Unknown suggestion '{sid}'")
        except pricing_suggestions.SuggestionConflict as exc:
            raise HTTPException(409, str(exc))
        except _billing.PricingFileError as exc:
            raise HTTPException(503, str(exc))
        core._audit(
            f"pricing.suggestion.{action}",
            target_type="pricing",
            target_id=item.get("model", sid),
            details=f"{item.get('type')} {sid}",
        )
        if action == "apply" and item.get("current"):
            from plugboard.pricing_scraper import price_change_events

            change = {
                "model": item["model"],
                "source": item.get("source", ""),
                "old": item["current"],
                "new": item["proposed"],
                "pct": item.get("pct"),
                "space": item.get("space", ""),
            }
            price_change_events(item["month"], [change], [], -1).flush_in_background()
        return item

    @app.post("/admin/pricing/suggestions/apply")
    async def apply_pricing_suggestions(
        body: SuggestionIds, _: None = Depends(core._require_admin)
    ):
        return await _bulk_suggestions(body.ids, "apply")

    @app.post("/admin/pricing/suggestions/reject")
    async def reject_pricing_suggestions(
        body: SuggestionIds, _: None = Depends(core._require_admin)
    ):
        return await _bulk_suggestions(body.ids, "reject")

    async def _bulk_suggestions(ids: List[str], action: str) -> Dict[str, Any]:
        done, errors = [], []
        for sid in ids:
            try:
                done.append(await _suggestion_action(sid, action))
            except HTTPException as exc:
                errors.append(
                    {"id": sid, "status": exc.status_code, "detail": exc.detail}
                )
        return {"done": done, "errors": errors}

    @app.post("/admin/pricing/suggestions/{sid}/apply")
    async def apply_pricing_suggestion(
        sid: str, _: None = Depends(core._require_admin)
    ):
        return await _suggestion_action(sid, "apply")

    @app.post("/admin/pricing/suggestions/{sid}/reject")
    async def reject_pricing_suggestion(
        sid: str, _: None = Depends(core._require_admin)
    ):
        return await _suggestion_action(sid, "reject")

    @app.patch("/admin/pricing/{month}")
    async def patch_pricing(
        month: str, body: PricingPatch, _: None = Depends(core._require_admin)
    ):
        """Set the price of some models; changed rows are marked ``source=manual``.

        Other rows are left as they are. A manual price is never replaced by a
        source; ``POST /admin/pricing/{month}/revert`` gives the model back to them.
        """
        month = core._month_param(month)
        _check_month(month)
        changed: List[str] = []
        with _billing.pricing_lock(month):
            grid = _load_grid_for_edit(month)
            models = grid.setdefault("models", {})
            rows = _manual_rows(body.models, models)
            models.update(rows)
            changed += [m for m, r in rows.items() if r.get("source") == "manual"]
            for space, space_rows in body.models_by_provider.items():
                table = grid.setdefault("models_by_provider", {}).setdefault(space, {})
                rows = _manual_rows(space_rows, table)
                table.update(rows)
                changed += [
                    f"{space}|{m}"
                    for m, r in rows.items()
                    if r.get("source") == "manual"
                ]
            _billing.save_pricing(month, grid)
        core._audit(
            "pricing.patch",
            target_type="pricing",
            target_id=month,
            details=",".join(changed)[:500],
        )
        return _billing.load_pricing(month)

    @app.post("/admin/pricing/{month}/revert")
    async def revert_pricing(
        month: str, body: PricingRevert, _: None = Depends(core._require_admin)
    ):
        """Drop a manual price and give the model back to the automatic sources.

        The new price comes from the last update's sources, else the automatic
        price the manual edit replaced, else the previous month (see revert_model).
        """
        month = core._month_param(month)
        from plugboard.pricing_scraper import revert_model

        _check_month(month)
        if month != _billing.current_pricing_month():
            raise HTTPException(
                409, "only the current month can be recomputed; past months are frozen"
            )
        try:
            entry = revert_model(month, body.model, body.provider)
        except KeyError:
            raise HTTPException(404, f"No price for '{body.model}' in {month}")
        except _billing.PricingFileError as exc:
            raise HTTPException(503, str(exc))
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        core._audit(
            "pricing.revert", target_type="pricing", target_id=body.model, details=month
        )
        return {"model": body.model, "entry": entry}

    @app.get("/admin/pricing/{month}")
    async def get_pricing(month: str, _: None = Depends(core._require_admin)):
        month = core._month_param(month)
        pricing = _billing.load_pricing(month)
        if not pricing:
            raise HTTPException(404, f"No pricing data for month '{month}'")
        return pricing

    @app.post("/admin/pricing/scrape")
    async def scrape_pricing(_: None = Depends(core._require_admin)):
        """Scrape latest pricing from configured sources and update current month grid."""
        from plugboard.pricing_scraper import update_current_pricing

        # Get all model IDs from providers for discovery
        all_models = []
        try:
            items = await core.list_models()
            all_models = [m.get("id", "") for m in items if m.get("id")]
        except Exception as exc:
            log.warning("Pricing scrape model discovery failed: %s", exc)

        result = await update_current_pricing(provider_models=all_models)
        core._audit(
            "pricing.scrape",
            details=f"added={result['added']} updated={result['updated']}",
        )
        return result

    @app.get("/admin/pricing/scheduler/status")
    async def pricing_scheduler_status(_: None = Depends(core._require_admin)):
        """Expose PricingScheduler state (CB, failure count, next run) for SREs.

        Manifesto §29.7 — observability is a Build → Run requirement so
        operators can diagnose a stuck scheduler without grepping logs.
        """
        from plugboard import models_dev

        sched = getattr(app.state, "pricing_scheduler", None)
        if sched is None:
            return {
                "running": False,
                "error": "scheduler not initialised",
                "catalog": models_dev.status(),
            }
        return {**sched.status_dict, "catalog": models_dev.status()}

    @app.get("/admin/models/scheduler/status")
    async def models_scheduler_status(_: None = Depends(core._require_admin)):
        sched = getattr(app.state, "model_availability_scheduler", None)
        if sched is None:
            return {"running": False, "error": "scheduler not initialised"}
        return sched.status_dict

    @app.get("/admin/auto-update/status")
    async def auto_update_status(
        journal: int = 20, _: None = Depends(core._require_admin)
    ):
        """Prices, catalog and model lists in one view, with the latest changes."""
        from plugboard import change_events, models_dev, pricing_suggestions

        pricing = getattr(app.state, "pricing_scheduler", None)
        models = getattr(app.state, "model_availability_scheduler", None)
        return {
            "pricing": pricing.status_dict
            if pricing
            else {"error": "scheduler not initialised"},
            "models": models.status_dict
            if models
            else {"error": "scheduler not initialised"},
            "catalog": models_dev.status(),
            "pending_suggestions": len(pricing_suggestions.list_suggestions("pending")),
            "recent_changes": change_events.read_journal(max(1, min(journal, 200))),
        }

    @app.get("/admin/catalog/history")
    def admin_catalog_history(
        provider: Optional[str] = Query(None, max_length=200),
        since: Optional[date] = None,
        until: Optional[date] = None,
        model: Optional[str] = Query(None, max_length=200),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        _: None = Depends(core._require_admin),
    ):
        """Models added or removed per provider, newest first (PB-PROV-02).

        A plain ``def``: FastAPI runs it in its thread pool, so reading the
        journal (up to 10 000 lines) never holds the event loop.
        """
        from plugboard import change_events

        if since and until and since > until:
            raise HTTPException(422, "since is after until")
        page = change_events.query_model_history(
            provider=provider or None,
            since=since,
            until=until,
            model=model or None,
            limit=limit,
            offset=offset,
        )
        return {
            "entries": page["entries"],
            "total": page["total"],
            "limit": limit,
            "offset": offset,
            "providers": page["providers"],
            "retention": {
                "days": change_events.retention_days(),
                "max_lines": change_events.JOURNAL_MAX_LINES,
                "oldest": page["oldest"],
            },
        }

    @app.post("/admin/auto-update/run", status_code=202)
    async def auto_update_run(_: None = Depends(core._require_admin)):
        """Start a price update and a model-list refresh now, in the background.

        409 with ``running_since`` while either one is already running.
        """
        pricing = getattr(app.state, "pricing_scheduler", None)
        models = getattr(app.state, "model_availability_scheduler", None)
        for sched in (pricing, models):
            if sched is not None and sched.running_since is not None:
                return JSONResponse(
                    status_code=409,
                    content={
                        "detail": "an update is already running",
                        "running_since": sched.running_since,
                    },
                )

        async def _run() -> None:
            for name, sched in (("pricing", pricing), ("models", models)):
                if sched is None:
                    continue
                try:
                    await sched.run_now("manual")
                except RuntimeError:
                    log.info("auto-update: %s update already running, skipped", name)
                except Exception as exc:  # noqa: BLE001
                    log.warning("auto-update: %s update failed: %s", name, exc)

        # A fresh context: the task outlives the admin request and must not hold
        # its principal (PB-AUTH-01).
        task = _asyncio.create_task(
            _run(), name="auto-update-run", context=contextvars.Context()
        )
        core._background_tasks.add(task)
        task.add_done_callback(core._background_tasks.discard)
        core._audit("auto_update.run")
        return {"started": True}

    @app.get("/admin/coverage/pricing")
    async def pricing_coverage(_: None = Depends(core._require_admin)):
        """Audit pricing coverage for every exposed model.

        PRICE-1: returns which models have a priced entry, which are free
        (local Ollama), and which are MISSING (billing will charge 0, which
        under-reports usage cost). CI-friendly — an empty `missing` list
        means every caller gets an accurate bill.
        """
        items = await core.list_models()
        pricing = _billing.load_pricing(_billing.current_pricing_month())
        providers_by_id = {p.get("id"): p for p in core.load_providers()}
        covered: list = []
        free: list = []
        missing: list = []
        for m in items:
            mid = m.get("id", "")
            real = mid.split(":", 1)[-1] if ":" in mid else mid
            provider = providers_by_id.get(mid.split(":", 1)[0]) if ":" in mid else None
            price = _billing.resolve_model_price(real, pricing, provider=provider)
            if price is None:
                missing.append(mid)
            elif price.get("input_price", 0) == 0 and price.get("output_price", 0) == 0:
                free.append(mid)
            else:
                covered.append(mid)
        return {
            "month": _billing.current_pricing_month(),
            "total": len(items),
            "covered_paid": len(covered),
            "covered_free": len(free),
            "missing": missing,
            "coverage_pct": round(
                ((len(covered) + len(free)) / max(1, len(items))) * 100, 1
            ),
        }

    @app.get("/admin/coverage/benchmarks")
    async def benchmarks_coverage(_: None = Depends(core._require_admin)):
        """Audit benchmark coverage — curated vs derived vs none.

        BENCH-1: "derived" entries ship provider/family/context-window
        guesses so clients always render *something*. Curated entries also
        include MMLU/HumanEval/MATH scores.
        """
        from plugboard.benchmarks import get_model_info

        items = await core.list_models()
        curated: list = []
        derived: list = []
        none: list = []
        for m in items:
            info = get_model_info(m.get("id", ""))
            if not info:
                none.append(m.get("id", ""))
            elif info.get("info_source") == "benchmark_db":
                curated.append(m.get("id", ""))
            else:
                derived.append(m.get("id", ""))
        return {
            "total": len(items),
            "curated": len(curated),
            "derived": len(derived),
            "none": none,
            "coverage_pct": round(
                ((len(curated) + len(derived)) / max(1, len(items))) * 100, 1
            ),
        }

    @app.put("/admin/pricing/{month}", status_code=200)
    async def upsert_pricing(
        month: str, body: PricingUpsert, _: None = Depends(core._require_admin)
    ):
        """
        Replace the pricing grid of a month. Rows whose price changed are marked
        ``source=manual``. Without ``models_by_provider`` the provider prices
        already in the grid are kept.
        """
        month = core._month_param(month)
        _check_month(month)
        with _billing.pricing_lock(month):
            existing = _load_grid_for_edit(month)
            pricing: Dict[str, Any] = {
                "models": _manual_rows(body.models, existing.get("models") or {}),
                "note": body.note,
            }
            spaces = existing.get("models_by_provider") or {}
            if body.models_by_provider is not None:
                spaces = {
                    space: _manual_rows(rows, spaces.get(space) or {})
                    for space, rows in body.models_by_provider.items()
                    if rows
                }
            if spaces:
                pricing["models_by_provider"] = spaces
            _billing.save_pricing(month, pricing)
        return _billing.load_pricing(month)

    return SimpleNamespace(
        _suggestion_action=_suggestion_action,
        admin_catalog_history=admin_catalog_history,
    )
