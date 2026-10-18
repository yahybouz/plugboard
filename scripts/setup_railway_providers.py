#!/usr/bin/env python3
"""Configure direct API-key providers on a deployed Plugboard — no terminal needed.

Reads API keys from env vars and POSTs them to /admin/direct-providers/<preset>.
Designed for Railway deploys where the user cannot drop into the container shell
to run interactive OAuth flows (claude login / gemini --auth / codex auth login).

Usage (from your laptop):

    PLUGBOARD_URL=https://plugboard.example.com \
    PLUGBOARD_ADMIN_KEY=cae6d4cff... \
    SETUP_ANTHROPIC_API_KEY=sk-ant-api03-...   \
    SETUP_OPENAI_API_KEY=sk-proj-...           \
    SETUP_GEMINI_API_KEY=AIza...               \
    SETUP_GITHUB_PAT=ghp_...                   \
    python scripts/setup_railway_providers.py

Any SETUP_* var that is empty/unset is skipped. Re-running with a new key
updates the corresponding provider in place.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Optional

import urllib.request
import urllib.error

# Accept environment variable names from before the Plugboard rename.
_OLD = "LLM_" + "ENABLER_"
for _key in list(os.environ):
    if _key.startswith(_OLD):
        os.environ.setdefault("PLUGBOARD_" + _key[len(_OLD) :], os.environ[_key])


PRESETS = [
    ("anthropic", "SETUP_ANTHROPIC_API_KEY", "Anthropic (Claude API direct)"),
    ("openai", "SETUP_OPENAI_API_KEY", "OpenAI (gpt-4o, gpt-5, o1, o3)"),
    ("gemini", "SETUP_GEMINI_API_KEY", "Google Gemini (AI Studio)"),
    ("github-models", "SETUP_GITHUB_PAT", "GitHub Models (41 OSS-model gateway)"),
]


def _post(url: str, admin_key: str, body: dict) -> tuple[int, str]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {admin_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def _get(url: str, admin_key: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {admin_key}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def main() -> int:
    base = (os.environ.get("PLUGBOARD_URL") or "").rstrip("/")
    admin_key = os.environ.get("PLUGBOARD_ADMIN_KEY") or ""

    if not base:
        print(
            "ERROR: set PLUGBOARD_URL (e.g. https://plugboard.example.com)"
        )
        return 2
    if not admin_key:
        print(
            "ERROR: set PLUGBOARD_ADMIN_KEY (the value you used for the ADMIN_KEY env var on Railway)"
        )
        return 2

    # Pre-flight: list current presets so we know what's already there
    code, raw = _get(f"{base}/admin/direct-providers/presets", admin_key)
    if code != 200:
        print(f"ERROR: cannot reach Plugboard admin API ({code}): {raw[:200]}")
        return 3
    print(f"✓ Plugboard admin API reachable at {base}")

    # Existing providers (so we don't duplicate)
    code, raw = _get(f"{base}/providers", admin_key)
    existing_provider_keys = set()
    if code == 200:
        try:
            data = json.loads(raw)
            for p in (data.get("providers") if isinstance(data, dict) else data) or []:
                existing_provider_keys.add(p.get("preset_key") or p.get("key", ""))
        except Exception:
            pass

    print()
    summary: list[tuple[str, str, Optional[str]]] = []
    for preset_key, env_var, label in PRESETS:
        api_key = (os.environ.get(env_var) or "").strip()
        if not api_key:
            print(f"  ⏭  {label:50} — skipped (env var {env_var} empty)")
            summary.append((preset_key, "skipped", None))
            continue
        if preset_key in existing_provider_keys:
            print(
                f"  ↻  {label:50} — already configured (skipping; revoke + re-run if you want to rotate)"
            )
            summary.append((preset_key, "already-configured", None))
            continue

        code, raw = _post(
            f"{base}/admin/direct-providers/{preset_key}",
            admin_key,
            {"api_key": api_key},
        )
        if code in (200, 201):
            try:
                data = json.loads(raw)
                provider_id = data.get("id", "<unknown>")
            except Exception:
                provider_id = "<unparsed>"
            print(f"  ✅ {label:50} — created (provider_id={provider_id})")
            summary.append((preset_key, "created", provider_id))
        else:
            print(f"  ❌ {label:50} — HTTP {code}: {raw[:160]}")
            summary.append((preset_key, f"failed-{code}", None))

    # Confirm state
    print()
    print("=== Final state ===")
    code, raw = _get(f"{base}/v1/models", admin_key)
    if code == 200:
        try:
            data = json.loads(raw)
            models = data.get("data") or []
            print(f"  /v1/models exposes {len(models)} model(s)")
            preview = sorted({m.get("id", "") for m in models})[:8]
            for mid in preview:
                print(f"    • {mid}")
            if len(models) > len(preview):
                print(f"    ... +{len(models) - len(preview)} more")
        except Exception:
            pass
    else:
        print(f"  /v1/models {code}: {raw[:200]}")

    failed = [s for s in summary if s[1].startswith("failed-")]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
