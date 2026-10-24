#!/usr/bin/env python3
"""Opt-in REAL local inference: short synthetic positive/negative/order fixtures."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import resource
import sys
import time

os.environ["PLUGBOARD_SKIP_DOTENV"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plugboard.decision_runtime import available, infer, RuntimeFailure
from plugboard.decisions import validate_answers


async def smoke(model: str) -> int:
    ready, reason = available(model)
    if not ready:
        print(json.dumps({"model": model, "status": "unavailable", "reason": reason}))
        return 2
    records = []
    for label, approved, reverse in [
        ("positive", True, False),
        ("negative", False, False),
        ("option_order", True, True),
    ]:
        options = {"approved": "approved", "rejected": "rejected"}
        if reverse:
            options = dict(reversed(options.items()))
        request = {
            "model": model,
            "state": f"Status: {'approved' if approved else 'rejected'}. Priority: {'high' if approved else 'low'}.",
            "questions": {
                "status": {
                    "type": "choice",
                    "instructions": "What is the stated status?",
                    "criteria": options,
                },
                "approved": {"type": "noul", "instructions": "The status is approved."},
                "priority": {
                    "type": "score",
                    "instructions": "What is the stated priority?",
                    "criteria": ["low", "medium", "high"],
                },
            },
        }
        start = time.perf_counter()
        try:
            result = await infer(model, request)
            answers = validate_answers(request, result)
            passed = answers["status"]["choice"] == (
                "approved" if approved else "rejected"
            )
            passed &= (answers["approved"]["noul"] >= 0.5) == approved
            passed &= (answers["priority"]["score"] >= 1) == approved
        except RuntimeFailure as exc:
            print(
                json.dumps(
                    {"model": model, "status": "runtime_failed", "reason": str(exc)}
                )
            )
            return 1
        except Exception:
            print(json.dumps({"model": model, "status": "invalid_output"}))
            return 1
        rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        records.append(
            {
                "fixture": label,
                "passed": bool(passed),
                "answers": answers,
                "latency_ms": round((time.perf_counter() - start) * 1000, 2),
                "peak_child_rss_bytes": rss if sys.platform == "darwin" else rss * 1024,
            }
        )
    print(
        json.dumps(
            {
                "model": model,
                "kind": "real_official_local_inference",
                "fixtures": records,
            },
            indent=2,
        )
    )
    return 0 if all(r["passed"] for r in records) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", choices=["laya", "laya-multilingual"])
    raise SystemExit(asyncio.run(smoke(parser.parse_args().model)))
