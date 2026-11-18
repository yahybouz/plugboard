"""Circuit breakers and internet-access probes of the models (admin).

Routes ``/admin/circuits*`` and ``/admin/models/check-internet*``, moved
verbatim from ``plugboard/app.py`` (PLU-36). ``core`` is the ``plugboard.app``
module that registers them: the names the tests patch there, and the bulk-check
state that stays there (``_BULK_INTERNET_*``), are read as ``core.<name>`` when
a handler runs.
"""

from __future__ import annotations

import asyncio as _asyncio
import logging
from contextlib import nullcontext as _nullcontext
from types import ModuleType, SimpleNamespace
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException

from plugboard import provider_admission as _provider_admission
from plugboard.router import ProvidersBusyError, ProviderTimeoutError

log = logging.getLogger("plugboard.app")


def register(app: FastAPI, core: ModuleType) -> SimpleNamespace:
    """Declare this block's routes on ``app``, in their original order."""

    @app.get("/admin/circuits")
    async def get_circuits(_: None = Depends(core._require_admin)):
        """View circuit breaker status for all tracked models."""
        from plugboard.circuit_breaker import (
            get_all_circuits,
            TIER_TIMEOUTS,
        )

        return {
            "circuits": get_all_circuits(),
            "tier_timeouts": TIER_TIMEOUTS,
        }

    @app.post("/admin/circuits/{model}/reset")
    async def reset_circuit_endpoint(
        model: str, _: None = Depends(core._require_admin)
    ):
        """Manually reset a circuit breaker to closed."""
        from plugboard.circuit_breaker import reset_circuit

        reset_circuit(model)
        core._audit("circuit.reset", target_type="model", target_id=model)
        return {"model": model, "state": "closed"}

    @app.post("/admin/models/check-internet")
    async def check_model_internet_access(
        body: Dict[str, Any], _: None = Depends(core._require_admin)
    ):
        """Probe a model to check for live internet/web-search access.

        Detection uses a nonce-based approach: a unique token is embedded in the
        prompt and the model must echo it back.  Only a model with real-time web
        access can retrieve today's date from an external source AND return the
        exact nonce — a model that merely repeats the prompt cannot satisfy both
        constraints simultaneously.

        By default the result is **not** persisted to providers.json.  Pass
        ``"persist": true`` in the request body to save ``internet_access`` for
        the model.  Name-based heuristics (kimi, glm …) are intentionally absent;
        the decision is driven solely by the actual response.
        """
        import datetime as _dt

        model_id = body.get("model_id", "")
        if not model_id:
            raise HTTPException(400, "Missing model_id")

        persist: bool = bool(body.get("persist", False))

        # 1. Resolve provider and real model name
        try:
            provider, real_model = core.resolve_provider_and_model(model_id)
        except Exception as exc:
            raise HTTPException(404, f"Model '{model_id}' not found: {exc}")

        has_internet = False
        status_str = "offline"
        error_str = None
        confidence = (
            "low"  # low | medium | high — persisted as internet_access_confidence
        )

        # ── PROBE METHODOLOGY v2 (2026-05-31) ────────────────────────────────
        # Core matcher logic now lives in plugboard.probes.internet (extracted
        # 2026-05-31 P0-02 so the live-test framework and scripts/probe_all_models
        # share a single source of truth). This endpoint only wires together the
        # HTTP call + retry-on-fail + persistence policy.
        from plugboard.probes.internet import (
            build_internet_probe_body,
            evaluate_probe_response,
        )

        probe_body = build_internet_probe_body(real_model or model_id)
        # The provider being checked, and no other: a bare model name went to
        # every active provider, so another one (a CLI) could answer, or be held
        # by the probe, and its result be recorded for this model (D-43).
        probe_body["model"] = f"{provider['id']}:{real_model or model_id}"

        try:
            # Retry-on-fail guard (2026-05-31 — user observed claude-opus-4-6
            # flapping between online/offline on consecutive probes even with
            # temperature=0). One LLM call is not enough to confidently mark a
            # model as offline: a single truncated or evasive reply produces a
            # false negative that then sticks via persist=True.
            PROBE_MAX_ATTEMPTS = 2
            last_result = None
            for attempt in range(1, PROBE_MAX_ATTEMPTS + 1):
                response = await core.forward(probe_body, timeout=30.0)
                last_result = evaluate_probe_response(response)
                if last_result.has_internet:
                    break
                log.info(
                    "probe_attempt %d/%d failed for %s: missing %s",
                    attempt,
                    PROBE_MAX_ATTEMPTS,
                    model_id,
                    ", ".join(last_result.attempts_missing),
                )

            has_internet = last_result.has_internet
            status_str = last_result.status
            confidence = last_result.confidence
            if not has_internet:
                error_str = f"after {PROBE_MAX_ATTEMPTS} attempts — " + (
                    last_result.error or "no signal"
                )

        except ProvidersBusyError as exc:
            # Every provider of the model is full, not offline (D-23): nothing is
            # learnt, so nothing is quarantined or persisted.
            return {
                "model_id": model_id,
                "internet_access": False,
                "internet_access_confidence": "low",
                "status": "busy",
                "error": str(exc),
            }
        except Exception as exc:
            reason = str(exc)
            if getattr(exc, "sandbox_failed", False):
                # agy's cage could not start, so agy was not run (BUG-86):
                # nothing was checked, so nothing is quarantined or persisted,
                # whatever bwrap's quoted line reads like ("not found").
                return {
                    "model_id": model_id,
                    "internet_access": False,
                    "internet_access_confidence": "low",
                    "status": "sandbox_unavailable",
                    "error": reason,
                }
            from plugboard.model_quarantine import (
                is_refused_model_error,
                record_terminal_unavailable,
            )

            refused_model = is_refused_model_error(reason, getattr(exc, "status", None))
            timed_out = (
                isinstance(exc, ProviderTimeoutError)
                or getattr(exc, "timed_out", False)
                or "timeout" in reason.lower()
                or "timed out" in reason.lower()
            )
            if timed_out and not refused_model:
                # A timeout says nothing of the model (BUG-105): a slow answer,
                # a busy CLI. Nothing is quarantined, counted or persisted; the
                # bulk check counts it failed, as its own per-model timeout.
                return {
                    "model_id": model_id,
                    "internet_access": False,
                    "internet_access_confidence": "low",
                    "status": "timeout",
                    "error": reason,
                }

            if refused_model:
                record_terminal_unavailable(
                    real_model, reason=reason[:200], provider_id=provider["id"]
                )
                core.invalidate_models_cache()
                return {
                    "model_id": model_id,
                    "internet_access": False,
                    "status": "quarantined",
                    "error": reason,
                }

            # Any other failure (a 401 or 403 of the account, a key error, a CLI
            # that exited in error, a 5xx) says nothing of the model (BUG-127):
            # the quarantine stays the router's, which counts the failed call
            # once in forward(). Nothing was checked, so nothing is persisted.
            return {
                "model_id": model_id,
                "internet_access": False,
                "internet_access_confidence": "low",
                "status": "error",
                "error": reason,
            }

        # 3. Optionally persist the result back into providers.json.
        if persist:
            models_list = provider.get("models", []) or []
            updated = False
            now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
            for m in models_list:
                if isinstance(m, dict) and m.get("id") == real_model:
                    m["internet_access"] = has_internet
                    m["internet_access_confidence"] = confidence
                    m["last_internet_probe_at"] = now_iso
                    if error_str:
                        m["internet_access_reason"] = error_str
                    else:
                        m.pop("internet_access_reason", None)
                    updated = True
                    break

            if updated:
                core.update_provider(provider["id"], {"models": models_list})
                core.invalidate_models_cache()
                status_str = "updated"

        return {
            "model_id": model_id,
            "internet_access": has_internet,
            "internet_access_confidence": confidence,
            "status": status_str,
            "error": error_str,
        }

    def _without_local_ollama_models(
        items: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """``items`` minus the models a local Ollama runs on this host.

        Ollama Cloud models (``<name>:cloud``, a tag ending in ``-cloud``) are
        served remotely and kept.
        """
        from plugboard.model_identity import catalogue_native_model
        from plugboard.ollama_admin import is_cloud_id
        from plugboard.router import _is_ollama_provider

        ollama_ids = {p["id"] for p in core.load_providers() if _is_ollama_provider(p)}

        def _local(m: Dict[str, Any]) -> bool:
            owner = m.get("provider") or m.get("owned_by")
            return owner in ollama_ids and not is_cloud_id(catalogue_native_model(m))

        return [m for m in items if not _local(m)]

    @app.post("/admin/models/check-internet/all")
    async def check_all_models_internet_access(
        body: Optional[Dict[str, Any]] = None,
        _: None = Depends(core._require_admin),
    ):
        """Bulk-probe every active model for live internet access.

        Iterates over every model returned by ``list_models()`` and calls
        :func:`check_model_internet_access` with bounded concurrency
        (``PLUGBOARD_BULK_INTERNET_CONCURRENCY``, default 3). A single global
        lock prevents two bulk runs from overlapping — a second concurrent call
        returns 409.

        Pass ``"persist": true`` (default ``false``) to write ``internet_access``
        back into ``providers.json`` for each tested model. Pass ``"provider":
        "<provider_id>"`` to restrict the scan to one provider. Pass
        ``"skip_local": true`` to leave local Ollama models out (the boot probe
        does): they have no web access, and probing one makes the host load and
        run it. Ollama Cloud models are still probed.
        """
        import time as _time

        body = body or {}
        persist: bool = bool(body.get("persist", False))
        provider_filter: Optional[str] = body.get("provider")
        skip_local: bool = body.get("skip_local") is True

        if core._BULK_INTERNET_LOCK.locked():
            raise HTTPException(
                status_code=409,
                detail="A bulk internet check is already running. Retry once it completes.",
            )

        async with core._BULK_INTERNET_LOCK:
            started_at = _time.monotonic()
            items = await core.list_models()
            if provider_filter:
                items = [
                    m
                    for m in items
                    if (
                        m.get("owned_by") == provider_filter
                        or m.get("provider") == provider_filter
                    )
                ]
            if skip_local:
                before = len(items)
                items = _without_local_ollama_models(items)
                if before != len(items):
                    log.info(
                        "bulk_internet_check: %d local Ollama model(s) skipped",
                        before - len(items),
                    )

            semaphore = _asyncio.Semaphore(core._BULK_INTERNET_CONCURRENCY)
            results: List[Dict[str, Any]] = []
            # D-43: a provider with a concurrency cap (a CLI, a local one) has its
            # models checked within that cap, so the bulk check never fills its
            # queue with its own probes, which would answer each other "busy".
            records = {p.get("id"): p for p in core.load_providers()}
            provider_slots: Dict[str, _asyncio.Semaphore] = {}
            for m in items:
                pid = m.get("provider") or m.get("owned_by")
                cap = (
                    _provider_admission.max_inflight(records[pid])
                    if pid in records
                    else 0
                )
                if cap and pid not in provider_slots:
                    provider_slots[pid] = _asyncio.Semaphore(cap)

            async def _probe_one(m: Dict[str, Any]) -> Dict[str, Any]:
                mid = m.get("id", "")
                slot = provider_slots.get(m.get("provider") or m.get("owned_by"))
                # The provider's turn first: waiting for it holds no bulk slot.
                async with slot or _nullcontext(), semaphore:
                    try:
                        coro = core.check_model_internet_access(
                            {"model_id": mid, "persist": persist},
                            _=None,
                        )
                        return await _asyncio.wait_for(
                            coro, timeout=core._BULK_INTERNET_PER_MODEL_TIMEOUT_S
                        )
                    except _asyncio.TimeoutError:
                        return {
                            "model_id": mid,
                            "internet_access": False,
                            "status": "timeout",
                            "error": f"per-model timeout {core._BULK_INTERNET_PER_MODEL_TIMEOUT_S}s",
                        }
                    except HTTPException as exc:
                        return {
                            "model_id": mid,
                            "internet_access": False,
                            "status": "error",
                            "error": f"HTTP {exc.status_code}: {exc.detail}",
                        }
                    except Exception as exc:  # noqa: BLE001
                        return {
                            "model_id": mid,
                            "internet_access": False,
                            "status": "error",
                            "error": str(exc)[:300],
                        }

            gathered = await _asyncio.gather(*(_probe_one(m) for m in items))
            results.extend(gathered)

            elapsed = _time.monotonic() - started_at
            online = sum(1 for r in results if r.get("internet_access"))
            failed = sum(
                1
                for r in results
                if r.get("status")
                in ("error", "timeout", "busy", "sandbox_unavailable")
            )
            offline = len(results) - online - failed

            # Invalidate models cache so the UI immediately sees the persisted flag.
            if persist:
                try:
                    from plugboard.router import invalidate_models_cache

                    invalidate_models_cache()
                except Exception:
                    pass

            log.info(
                "bulk_internet_check completed: total=%d online=%d offline=%d failed=%d elapsed=%.1fs persist=%s",
                len(results),
                online,
                offline,
                failed,
                elapsed,
                persist,
            )

            return {
                "total": len(results),
                "online_count": online,
                "offline_count": offline,
                "failed_count": failed,
                "elapsed_s": elapsed,
                "persisted": persist,
                "results": results,
            }

    return SimpleNamespace(
        check_model_internet_access=check_model_internet_access,
        check_all_models_internet_access=check_all_models_internet_access,
    )
