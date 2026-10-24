#!/usr/bin/env python3
"""Explicit public model download; never invoked by the gateway or worker."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Importing Plugboard must not load an operator's .env for this public download.
import os

os.environ["PLUGBOARD_SKIP_DOTENV"] = "1"
from plugboard.decision_assets import (
    AssetError,
    download_assets,
    read_manifest,
    verify_assets,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", choices=read_manifest()["models"])
    parser.add_argument("--assets", required=True, type=Path)
    parser.add_argument(
        "--max-file-mb",
        type=int,
        help="Refuse BEFORE network if any file exceeds this budget",
    )
    parser.add_argument(
        "--check", action="store_true", help="Only verify all hashes; no download"
    )
    args = parser.parse_args()
    try:
        if args.check:
            ready, reason = verify_assets(args.model, args.assets, deep=True)
            print(reason)
            return 0 if ready else 1
        download_assets(
            args.model,
            args.assets,
            max_file_bytes=None
            if args.max_file_mb is None
            else args.max_file_mb * 1024**2,
        )
        print("ready")
        return 0
    except AssetError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
