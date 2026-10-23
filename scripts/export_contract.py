#!/usr/bin/env python3
"""Write, or check, the integration contracts under docs/api/.

    python scripts/export_contract.py              # docs/api/openapi.json
    python scripts/export_contract.py --mcp        # docs/api/mcp.json
    python scripts/export_contract.py --check      # exit 1 when the file is stale
    python scripts/export_contract.py --mcp --check

The OpenAPI document comes from plugboard.api_contract. The MCP contract is
read from the MCP server itself (its tools, with their JSON input schemas,
its resources and its prompts), so it needs the MCP environment:
`make setup-mcp`, then run this script with .venv-mcp/bin/python.

    make contract          # both files
    make contract-check    # both checks (CI runs them)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OPENAPI_PATH = ROOT / "docs" / "api" / "openapi.json"
MCP_PATH = ROOT / "docs" / "api" / "mcp.json"


def render(document: Dict[str, Any]) -> str:
    """Stable text form: the committed file is compared byte for byte."""
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def openapi_document() -> Dict[str, Any]:
    from plugboard.api_contract import build_openapi

    return build_openapi()


async def _mcp_document() -> Dict[str, Any]:
    from plugboard.api_contract import CONTRACT_VERSION
    from plugboard.mcp_server import mcp

    tools = await mcp.list_tools()
    resources = await mcp.list_resources()
    prompts = await mcp.list_prompts()
    return {
        "server": mcp.name,
        "contract_version": CONTRACT_VERSION,
        "transport": ["stdio", "sse", "streamable-http"],
        "tools": [
            {
                "name": t.name,
                "description": (t.description or "").strip(),
                "inputSchema": t.inputSchema,
            }
            for t in sorted(tools, key=lambda t: t.name)
        ],
        "resources": [
            {
                "uri": str(r.uri),
                "name": r.name,
                "description": (r.description or "").strip(),
                "mimeType": r.mimeType,
            }
            for r in sorted(resources, key=lambda r: str(r.uri))
        ],
        "prompts": [
            {
                "name": p.name,
                "description": (p.description or "").strip(),
                "arguments": [
                    {"name": a.name, "required": bool(a.required)}
                    for a in (p.arguments or [])
                ],
            }
            for p in sorted(prompts, key=lambda p: p.name)
        ],
    }


def mcp_document() -> Dict[str, Any]:
    try:
        return asyncio.run(_mcp_document())
    except ModuleNotFoundError as exc:
        sys.exit(
            f"{exc}: the MCP contract needs the MCP environment. Run "
            "`make setup-mcp`, then "
            ".venv-mcp/bin/python scripts/export_contract.py --mcp"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mcp", action="store_true", help="the MCP contract")
    parser.add_argument(
        "--check", action="store_true", help="compare instead of writing"
    )
    args = parser.parse_args()

    path = MCP_PATH if args.mcp else OPENAPI_PATH
    text = render(mcp_document() if args.mcp else openapi_document())
    rel = path.relative_to(ROOT)
    if args.check:
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current != text:
            print(
                f"{rel} is out of date with the code. Run `make contract`, review "
                "the diff, and bump CONTRACT_VERSION in plugboard/api_contract.py "
                "when the change is not backward compatible.",
                file=sys.stderr,
            )
            return 1
        print(f"{rel}: up to date")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"Wrote {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
