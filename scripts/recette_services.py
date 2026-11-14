#!/usr/bin/env python3
"""Services recipe of a Plugboard gateway (INC-2). Read-only: three GET requests.

Checks, in order:

1. ``GET /health``        answers 200 with ``ok`` true (liveness);
2. ``GET /health/ready``  answers 200 with ``ready`` true (readiness);
3. ``GET /admin/jobs``    lists every expected background job, each one running
                          and healthy.

A job fails the recipe when it is absent from the list or its state is
``disabled``, ``not_started``, ``stopped``, ``error`` or ``stale``. ``never_run``
(started, nothing run yet) fails too, unless ``--allow-never-run``; ``--wait S``
polls up to S seconds first, for a gateway that has just started. The expected
jobs are ``--expect`` (default: all five): ``pricing_refresh``, ``model_refresh``,
``provider_watchdog``, ``availability_snapshots``, ``data_retention``.

The gateway URL and the admin key come from the environment
(``PLUGBOARD_RECETTE_URL``, ``PLUGBOARD_RECETTE_ADMIN_KEY``; names set with
``--url-env`` and ``--key-env``). With admin MFA required, a fresh code goes in
the variable named by ``--totp-env`` (default ``PLUGBOARD_RECETTE_TOTP``). The key
is never printed; plain http to another machine needs ``--allow-http``.

Exit codes: 0 every check passed, 1 a check failed, 2 bad usage or the gateway
cannot be reached.

The host-level backup timer of the prod install (deploy/ovh/plugboard-backup.timer)
is not a gateway job: check it on the host with ``systemctl list-timers``.

Example:
  PLUGBOARD_RECETTE_URL=https://gateway.example PLUGBOARD_RECETTE_ADMIN_KEY=... \\
    python scripts/recette_services.py --wait 60
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

EXPECTED_JOBS = (
    "pricing_refresh",
    "model_refresh",
    "provider_watchdog",
    "availability_snapshots",
    "data_retention",
)
HEALTHY = {"success", "running"}
PENDING = {"never_run"}  # settles by itself after a start


def _get(
    url: str, path: str, headers: Dict[str, str], timeout: float
) -> Tuple[int, Any]:
    """GET ``path``: (status, parsed JSON or None). Raises OSError when unreachable."""
    parts = urlsplit(url)
    conn_cls = (
        http.client.HTTPSConnection
        if parts.scheme == "https"
        else http.client.HTTPConnection
    )
    conn = conn_cls(parts.hostname, parts.port, timeout=timeout)
    try:
        conn.request("GET", (parts.path.rstrip("/") or "") + path, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        try:
            return resp.status, json.loads(raw)
        except ValueError:
            return resp.status, None
    except http.client.HTTPException as exc:
        raise OSError(str(exc)) from exc
    finally:
        conn.close()


def _plain_http_allowed(url: str, allow_http: bool) -> bool:
    parts = urlsplit(url)
    if parts.scheme == "https" or allow_http:
        return True
    host = parts.hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def judge_jobs(
    jobs: Any, expected: List[str], allow_never_run: bool
) -> Tuple[List[str], List[str]]:
    """(report lines, problems) for the jobs listed by ``/admin/jobs``."""
    by_id = {j.get("id"): j for j in jobs if isinstance(j, dict)}
    lines: List[str] = []
    problems: List[str] = []
    for job_id in expected:
        job = by_id.get(job_id)
        if job is None:
            problems.append(f"job {job_id}: absent from /admin/jobs")
            lines.append(f"  {job_id:<24} ABSENT")
            continue
        state = str(job.get("last_status"))
        last = job.get("last_run") or "-"
        lines.append(f"  {job_id:<24} {state:<12} last run {last}")
        if state in HEALTHY or (state in PENDING and allow_never_run):
            continue
        problems.append(f"job {job_id}: {state} (last run {last})")
    return lines, problems


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url-env", default="PLUGBOARD_RECETTE_URL")
    ap.add_argument("--key-env", default="PLUGBOARD_RECETTE_ADMIN_KEY")
    ap.add_argument("--totp-env", default="PLUGBOARD_RECETTE_TOTP")
    ap.add_argument("--expect", default=",".join(EXPECTED_JOBS))
    ap.add_argument("--allow-never-run", action="store_true")
    ap.add_argument("--wait", type=float, default=0.0, help="poll up to S seconds")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--allow-http", action="store_true")
    args = ap.parse_args(argv)

    url = os.environ.get(args.url_env, "")
    key = os.environ.get(args.key_env, "")
    expected = [x.strip() for x in args.expect.split(",") if x.strip()]
    if not url or not key or not expected:
        print(
            f"error: set {args.url_env} and {args.key_env}; --expect needs a job",
            file=sys.stderr,
        )
        return 2
    if not _plain_http_allowed(url, args.allow_http):
        print(
            "error: plain http to another machine needs --allow-http", file=sys.stderr
        )
        return 2
    admin = {"Authorization": f"Bearer {key}"}
    totp = os.environ.get(args.totp_env, "")
    if totp:
        admin["X-Plugboard-Admin-TOTP"] = totp

    problems: List[str] = []
    try:
        status, body = _get(url, "/health", {}, args.timeout)
        ok = status == 200 and isinstance(body, dict) and body.get("ok") is True
        version = body.get("version") if isinstance(body, dict) else None
        print(f"{'ok  ' if ok else 'FAIL'} /health  HTTP {status} version {version}")
        if not ok:
            problems.append(f"/health: HTTP {status}")

        status, body = _get(url, "/health/ready", {}, args.timeout)
        ready = status == 200 and isinstance(body, dict) and body.get("ready") is True
        print(f"{'ok  ' if ready else 'FAIL'} /health/ready  HTTP {status}")
        if not ready:
            bad = (
                {k: v for k, v in (body.get("checks") or {}).items() if v != "ok"}
                if isinstance(body, dict)
                else {}
            )
            problems.append(f"/health/ready: HTTP {status} {bad}")

        deadline = time.monotonic() + max(args.wait, 0.0)
        while True:
            status, body = _get(url, "/admin/jobs", admin, args.timeout)
            if status != 200 or not isinstance(body, list):
                lines, job_problems = [], [f"/admin/jobs: HTTP {status}"]
                break
            lines, job_problems = judge_jobs(body, expected, args.allow_never_run)
            settling = any(
                str(j.get("last_status")) in PENDING | {"running"}
                for j in body
                if isinstance(j, dict) and j.get("id") in expected
            )
            if (not job_problems and not settling) or time.monotonic() >= deadline:
                break
            time.sleep(min(5.0, max(deadline - time.monotonic(), 0.1)))
        print("jobs (/admin/jobs):")
        print("\n".join(lines))
        problems.extend(job_problems)
    except OSError as exc:
        print(f"error: gateway not reachable: {type(exc).__name__}", file=sys.stderr)
        return 2

    for problem in problems:
        print(f"FAIL: {problem.replace(key, '***')}", file=sys.stderr)
    print("recette services:", "FAILED" if problems else "OK")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
