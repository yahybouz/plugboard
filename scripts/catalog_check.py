#!/usr/bin/env python3
"""Check the presets Plugboard generates from the live models.dev catalog.

Downloads PLUGBOARD_CATALOG_URL (default https://models.dev/api.json) once,
prints the generated presets and the models excluded from each, and exits
non-zero when a provider Plugboard documents is missing. Not run in CI:
it needs network access.

    make catalog-check
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugboard import models_dev  # noqa: E402

REQUIRED = ("openrouter", "groq", "moonshotai", "opencode")


async def main() -> int:
    url = models_dev.catalog_url() or models_dev.DEFAULT_CATALOG_URL
    code, body, _ = await models_dev._download(url, "")
    providers, invalid = models_dev.parse_catalog(json.loads(body))
    presets = models_dev.generated_presets(providers)
    print(f"{url}: {len(providers)} providers, {invalid} invalid entries, {len(presets)} presets\n")
    for preset in presets:
        provider = providers[preset["catalog_id"]]
        excluded = models_dev.excluded_models(provider)
        print(
            f"{preset['key']:32} {preset['base_url']:48} "
            f"{len(preset['known_models']):4} models  {len(excluded):3} excluded"
        )
    missing = [pid for pid in REQUIRED if f"{models_dev.PRESET_PREFIX}{pid}" not in {p["key"] for p in presets}]
    if missing:
        print(f"\nMissing required presets: {', '.join(missing)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
