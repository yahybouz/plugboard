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
    PLUGBOARD_ADMIN_KEY — Admin key for provider status (stdio only)
    (SSE/HTTP keep only the variables in _NETWORK_ENV; other secrets are dropped)
    PLUGBOARD_MCP_TIMEOUT — Timeout for LLM calls in seconds (default: 900)
    PLUGBOARD_MCP_TOKEN — Bearer token required by SSE/HTTP (32+ characters)
    MCP_HOST              — Bind address for SSE/HTTP (default: 127.0.0.1)
    MCP_PORT              — Port for SSE/HTTP transport (default: 8766)
    PLUGBOARD_MCP_ALLOWED_HOSTS   — Comma-separated Host values (default: loopback)
    PLUGBOARD_MCP_ALLOWED_ORIGINS — Comma-separated Origin values (default: loopback)
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


def _csv_env(name: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


# What the SSE/HTTP server process keeps from its environment and .env; the
# gateway's admin and encryption keys, provider keys and the Jev key are
# dropped, so a process exposed to the network never holds them.
_NETWORK_ENV = frozenset(
    {
        "PLUGBOARD_URL",
        "PLUGBOARD_API_KEY",
        "PLUGBOARD_DATA",
        "PLUGBOARD_REGISTRY_PATH",
        "PLUGBOARD_SKIP_DOTENV",
        "MCP_HOST",
        "MCP_PORT",
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "TZ",
        "TMPDIR",
        "TEMP",
        "TMP",
        "VIRTUAL_ENV",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
)
_NETWORK_ENV_PREFIXES = ("PLUGBOARD_MCP_", "LC_", "PYTHON")


def _keep_network_env() -> None:
    for key in list(os.environ):
        if key not in _NETWORK_ENV and not key.startswith(_NETWORK_ENV_PREFIXES):
            del os.environ[key]


def main():
    transport = "stdio"
    if "--sse" in sys.argv:
        transport = "sse"
    elif "--http" in sys.argv:
        transport = "streamable-http"

    if transport != "stdio":
        # A network caller never acts with the gateway's secrets: keep only the
        # MCP variables before the server module reads its configuration. .env
        # was read above; importing plugboard must not load it again.
        os.environ["PLUGBOARD_SKIP_DOTENV"] = "1"
        _keep_network_env()

    from plugboard.mcp_server import (
        MCP_TOKEN_MIN_LENGTH,
        configure_network_transport,
        mcp,
    )

    if transport != "stdio":
        # Legacy names are mapped again on import (envcompat): filter once
        # every loader has run.
        _keep_network_env()

    token = os.environ.get("PLUGBOARD_MCP_TOKEN", "").strip()
    if transport != "stdio" and len(token) < MCP_TOKEN_MIN_LENGTH:
        print(
            f"Refusing to start the {transport} transport: set PLUGBOARD_MCP_TOKEN "
            f"to a random value of at least {MCP_TOKEN_MIN_LENGTH} characters "
            "(clients send it as 'Authorization: Bearer <token>').",
            file=sys.stderr,
        )
        raise SystemExit(2)

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
        # FastMCP.run() takes no host or port: SSE/HTTP read them from the settings.
        configure_network_transport(
            token,
            host=os.environ.get("MCP_HOST", "").strip() or "127.0.0.1",
            port=port,
            allowed_hosts=_csv_env("PLUGBOARD_MCP_ALLOWED_HOSTS"),
            allowed_origins=_csv_env("PLUGBOARD_MCP_ALLOWED_ORIGINS"),
        )
        print(f"  Listening on {mcp.settings.host}:{port}", file=sys.stderr)
        mcp.run(transport=transport)


if __name__ == "__main__":
    main()
