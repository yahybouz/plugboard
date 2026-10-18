#!/usr/bin/env python3
"""
Launch the Plugboard MCP server.

Usage:
    python scripts/run_mcp.py              # stdio (for Claude Desktop / IDE)
    python scripts/run_mcp.py --sse        # SSE transport (for web clients)
    python scripts/run_mcp.py --http       # Streamable HTTP (for agents)

Environment variables:
    PLUGBOARD_URL       — Plugboard base URL (default: http://localhost:8765)
    PLUGBOARD_API_KEY   — Client API key for chat/usage
    PLUGBOARD_ADMIN_KEY — Admin key for monitoring/provider status
    PLUGBOARD_MCP_TIMEOUT — Timeout for LLM calls in seconds (default: 900)
    MCP_PORT              — Port for SSE/HTTP transport (default: 8766)
"""

from __future__ import annotations

import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env
_env_file = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
)
if os.path.exists(_env_file):
    with open(_env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

# Accept variable names from before the Plugboard rename (process env or .env).
# Keys are never copied from API_KEY/ADMIN_KEY: chat runs with the client key
# in PLUGBOARD_API_KEY, the admin key only when PLUGBOARD_ADMIN_KEY is set.
_OLD = "LLM_" + "ENABLER_"
for _key in list(os.environ):
    if _key.startswith(_OLD):
        os.environ.setdefault("PLUGBOARD_" + _key[len(_OLD) :], os.environ[_key])


def main():
    from plugboard.mcp_server import mcp

    transport = "stdio"
    if "--sse" in sys.argv:
        transport = "sse"
    elif "--http" in sys.argv:
        transport = "streamable-http"

    port = int(os.environ.get("MCP_PORT", "8766"))

    print(f"Starting Plugboard MCP server (transport={transport})", file=sys.stderr)
    print(
        f"  Plugboard: {os.environ.get('PLUGBOARD_URL', 'http://localhost:8765')}",
        file=sys.stderr,
    )
    print(
        f"  API key: {'set' if os.environ.get('PLUGBOARD_API_KEY') else 'not set'}",
        file=sys.stderr,
    )
    print(
        f"  Admin key: {'set' if os.environ.get('PLUGBOARD_ADMIN_KEY') else 'not set'}",
        file=sys.stderr,
    )

    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        # FastMCP.run() takes no port: SSE/HTTP read it from the settings.
        mcp.settings.port = port
        mcp.run(transport=transport)


if __name__ == "__main__":
    main()
