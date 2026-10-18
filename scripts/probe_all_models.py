import sys
from pathlib import Path
import asyncio
import logging

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Set logging level to WARNING to keep output clean
logging.basicConfig(level=logging.WARNING)

# ruff: noqa: E402 — imports must follow sys.path.insert above
from plugboard.router import (
    list_models,
    resolve_provider_and_model,
    forward,
    invalidate_models_cache,
)
from plugboard.config import update_provider
from plugboard.model_quarantine import (
    record_terminal_unavailable,
    is_account_unsupported_error,
    is_terminal_model_unavailable_error,
)


async def probe_all():
    print("Fetching active models from catalog...")
    models = await list_models()
    print(f"Found {len(models)} active models to test.\n")

    results = []

    for m in models:
        model_id = m.get("id")
        if not model_id:
            continue

        try:
            provider, real_model = resolve_provider_and_model(model_id)
        except Exception as exc:
            print(f"Testing {model_id}... [ERROR] Could not resolve: {exc}")
            continue

        print(f"Testing {model_id}...", end="", flush=True)

        has_internet = False
        status_str = "offline"

        try:
            # Ask the model to perform a real-time lookup of today's exact date.
            # The expected date is computed dynamically from system clock so the
            # script never goes stale.
            import datetime as _dt

            today = _dt.datetime.now(_dt.timezone.utc)
            _MONTHS_EN = [
                "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December",
            ]
            _MONTHS_FR = [
                "janvier", "février", "mars", "avril", "mai", "juin",
                "juillet", "août", "septembre", "octobre", "novembre", "décembre",
            ]
            d, mo, y = today.day, today.month, today.year
            month_en, month_fr = _MONTHS_EN[mo - 1], _MONTHS_FR[mo - 1]
            expected_date_variants = [
                f"{y}-{mo:02d}-{d:02d}",
                f"{d:02d}/{mo:02d}/{y}",
                f"{d}/{mo}/{y}",
                f"{mo:02d}/{d:02d}/{y}",
                f"{d} {month_en} {y}",
                f"{month_en} {d}, {y}",
                f"{month_en} {d} {y}",
                f"{d} {month_fr} {y}",
            ]

            probe_body = {
                "model": model_id,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Perform a real-time web search RIGHT NOW to obtain today's exact date. "
                            "Reply with today's full date in ISO format 'YYYY-MM-DD' on its own line. "
                            "Do NOT guess. If you cannot perform a live lookup, reply exactly: NO-WEB-ACCESS."
                        ),
                    }
                ],
                "max_tokens": 150,
                "temperature": 0.0,
            }

            # Using 15 seconds timeout: fast enough to prevent long hangs,
            # but plenty of time for functional, warmed-up CLIs to respond.
            result = await forward(probe_body, timeout=15.0)
            content = (
                (
                    ((result.get("choices") or [{}])[0].get("message") or {}).get(
                        "content"
                    )
                    or ""
                )
                .strip()
                .lower()
            )

            # Strict: must contain today's full date in one of the accepted forms.
            # Name-based fallbacks (kimi/glm) are intentionally removed — only the
            # actual response decides, matching the server-side probe semantics.
            has_internet = any(v.lower() in content for v in expected_date_variants)
            status_str = "online (Web Search)" if has_internet else "offline"

            # Update provider configuration with verified internet access
            models_list = provider.get("models", []) or []
            updated = False
            for pm in models_list:
                if isinstance(pm, dict) and pm.get("id") == real_model:
                    pm["internet_access"] = has_internet
                    updated = True
                    break
            if updated:
                update_provider(provider["id"], {"models": models_list})

            print(f" [{status_str.upper()}]")
            results.append({"model_id": model_id, "status": status_str})

        except Exception as exc:
            reason = str(exc)

            # Treat auth errors, 404, or model unavailable as terminal failures and quarantine them
            is_terminal = (
                "404" in reason
                or "not found" in reason.lower()
                or "model_not_found" in reason
                or "401" in reason
                or "403" in reason
                or "unauthorized" in reason.lower()
                or "forbidden" in reason.lower()
                or "key" in reason.lower()
                or "timeout" in reason.lower()
                or "timed out" in reason.lower()
                or "exited" in reason.lower()
                or is_account_unsupported_error(reason)
                or is_terminal_model_unavailable_error(reason)
            )

            if is_terminal:
                record_terminal_unavailable(real_model, reason=reason[:200])
                print(f" [QUARANTINED] (Reason: {reason[:80]}...)")
                results.append(
                    {"model_id": model_id, "status": "quarantined", "reason": reason}
                )
            else:
                print(f" [OFFLINE] (Transient error: {reason[:80]}...)")
                results.append(
                    {"model_id": model_id, "status": "offline", "reason": reason}
                )

    # Invalidate cache so changes take effect
    invalidate_models_cache()

    print("\n" + "=" * 50)
    print("PROBE RUN SUMMARY:")
    print("=" * 50)
    for r in results:
        status_part = r["status"].upper()
        if r["status"] == "quarantined":
            print(f"- {r['model_id']}: {status_part} ({r.get('reason', '')[:60]}...)")
        else:
            print(f"- {r['model_id']}: {status_part}")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(probe_all())
