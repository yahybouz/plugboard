# MCP server

Plugboard ships a [Model Context Protocol](https://modelcontextprotocol.io) server named `plugboard`. It lets MCP clients (Claude Code, Claude Desktop, Cursor, agent frameworks) chat through the gateway, list models, check usage and provider health, and receive **update instructions** when the gateway changes in a way that affects them.

The MCP server is a separate process. It does not access providers itself: it calls a running Plugboard instance over HTTP, with a client key, so budgets, limits and usage tracking apply as for any other client.

## Install

The MCP server runs in its own virtual environment, `.venv-mcp`, installed from the hash-checked lock:

```bash
make setup-mcp
```

## Configure

| Variable | Default | Purpose |
|---|---|---|
| `PLUGBOARD_URL` | `http://localhost:8765` | Plugboard instance to call |
| `PLUGBOARD_API_KEY` | empty | Client `pb_` key used for chat and usage |
| `PLUGBOARD_ADMIN_KEY` | empty | Admin key, only needed for `provider_status`; read in stdio mode only |
| `PLUGBOARD_MCP_TIMEOUT` | `900` | Timeout of chat calls, in seconds |
| `PLUGBOARD_MCP_TOKEN` | empty | Bearer token required in SSE or HTTP mode, 32 characters or more |
| `MCP_HOST` | `127.0.0.1` | Bind address in SSE or HTTP mode |
| `MCP_PORT` | `8766` | Port in SSE or HTTP mode |
| `PLUGBOARD_MCP_ALLOWED_HOSTS` | loopback | Comma-separated `Host` values accepted in SSE or HTTP mode (`host:port`, `host:*`) |
| `PLUGBOARD_MCP_ALLOWED_ORIGINS` | loopback | Comma-separated `Origin` values accepted in SSE or HTTP mode |

`scripts/run_mcp.py` also reads `.env` from the repository root, without overriding variables that are already set. Give the MCP server its own client key with its own budget, and only pass the admin key if you need `provider_status`.

## Run

```bash
make mcp         # stdio, for desktop and IDE clients
make mcp-sse     # SSE transport on MCP_PORT
make mcp-http    # streamable HTTP transport on MCP_PORT
```

These targets run `.venv-mcp/bin/python scripts/run_mcp.py` with no flag, `--sse` or `--http`.

### Network transports (SSE, HTTP)

Anyone who reaches the port acts with the server's client key, so SSE and HTTP are authenticated:

- The server refuses to start without `PLUGBOARD_MCP_TOKEN` (32 characters or more, for example `openssl rand -hex 32`). Clients send `Authorization: Bearer <token>`; any other request gets 401. The comparison is constant-time.
- It listens on `127.0.0.1` unless `MCP_HOST` says otherwise, and refuses a `Host` (421) or `Origin` (403) outside `PLUGBOARD_MCP_ALLOWED_HOSTS` / `PLUGBOARD_MCP_ALLOWED_ORIGINS` (loopback by default) against DNS rebinding. Behind a proxy, list the public name there, for example `mcp.example.com:*` and `https://mcp.example.com`.
- `PLUGBOARD_ADMIN_KEY` is never loaded: `provider_status` (and `monitoring://status`) answer that they are available over stdio only.
- The process keeps only the variables it needs, whether they come from the environment or `.env`: `PLUGBOARD_URL`, `PLUGBOARD_API_KEY`, `PLUGBOARD_MCP_*`, `MCP_HOST`, `MCP_PORT`, `PLUGBOARD_DATA`, `PLUGBOARD_REGISTRY_PATH`, and system variables (`PATH`, `HOME`, locale, `TMPDIR`, `PYTHON*`, `SSL_CERT_*`). `ADMIN_KEY`, `ENCRYPTION_KEY`, provider keys and `PLUGBOARD_JEV_API_KEY` are dropped before the server starts.

stdio is unchanged and needs no token: only the process that spawned the server can reach it.

Example client configuration (stdio):

```json
{
  "mcpServers": {
    "plugboard": {
      "command": "/path/to/plugboard/.venv-mcp/bin/python",
      "args": ["/path/to/plugboard/scripts/run_mcp.py"],
      "env": {
        "PLUGBOARD_URL": "http://localhost:8765",
        "PLUGBOARD_API_KEY": "pb_your_key"
      }
    }
  }
}
```

## Tools

| Tool | Arguments | What it does |
|---|---|---|
| `chat` | `model`, `message`, `max_tokens` (4096), `temperature` (0.7), `system_prompt`, `session_id` | One chat completion through `/v1/chat/completions` |
| `chat_stream` | same as `chat` | The same with `stream: true` |
| `list_models` | none | Model IDs from `/v1/models` |
| `list_decision_models` | none | Permitted decision engines, availability, capabilities and confidence semantics |
| `decide` | `model`, `state`, `questions` | Named typed questions through `/v1/decisions`; no chat fallback |
| `check_usage` | none | Usage, spend and limits of the key (`/v1/me`) |
| `provider_status` | none | Watchdog state and provider health (admin key required) |
| `check_updates` | `client_id` | Pending updates for a client project |
| `get_update_prompt` | `prompt_id` | Full instructions of one update |
| `mark_applied` | `client_id`, `prompt_id`, `notes` | Record that a client applied an update |
| `list_applied` | `client_id` | Updates already applied by a client |

Resources: `models://list`, `usage://current`, `monitoring://status`, `updates://pending`.

The active decision catalogue contains only `jev` (TypeSafe API), `laya`
(English) and `laya-multilingual` (the locally hostable Laya family). Kev was
removed by decision 72 on 24 October 2026. Legacy Kev IDs are rejected in chat
without a generative fallback; they grant no decision capability.

The decision tools require `PLUGBOARD_API_KEY` with explicit decision permissions.
They never substitute `PLUGBOARD_ADMIN_KEY`, even when the client key is absent.
`questions` is a mapping of question IDs to objects containing `type` and
`instructions`: `choice` adds a `criteria` mapping, `score` an ordered `criteria`
list, and `noul` has no criteria. See [typed decision examples](../api/README.md#typed-decisions).
The transport timeout is `PLUGBOARD_MCP_TIMEOUT`, clamped to 1–45 seconds for
decisions; successful bodies are capped at 512 KiB. Redirects are refused and
failures return a JSON `error` object with a static `code` and `message`, without
upstream bodies or credentials. Availability does not establish real inference
verification or production hosting compatibility. Jev without a deliberately
configured server-side key returns 503; no real Jev call is claimed. The optional
Laya runtime is qualified only for macOS arm64 ≥14, not Linux/OVH. Historical
multilingual smoke remains 2/3; the Torch pt2 reserve and final reviews remain open.

Prompts: `quick_chat`, `deep_analysis`, `code_review`. Their default model IDs come from the original deployment and are unlikely to exist on yours; pass a model from `list_models` explicitly.

## Update registry

The update registry lets the gateway operator push integration changes to the projects that use it. An AI coding agent working in a client project calls `check_updates` at the start of a session, reads each pending item with `get_update_prompt`, applies it, and calls `mark_applied`.

The registry is operator data kept outside the repository: `data/registry.json`, or the file named by `PLUGBOARD_REGISTRY_PATH`. Without it the registry is empty. `plugboard/registry.example.json` shows the format:

```json
{
  "version": "1.0.0",
  "last_updated": "2026-10-17",
  "prompts": [
    {
      "id": "CP-001",
      "version": "1.0.0",
      "date": "2026-10-17",
      "impact": "IMPORTANT",
      "clients": ["my-app"],
      "title": "Use the provider prefix in model names",
      "summary": "Model IDs must now be sent as <provider-id>:<model>.",
      "changes": [
        {
          "file": ".env",
          "action": "replace",
          "description": "Prefix the model name with its provider ID.",
          "old": ["LLM_MODEL=gpt-4o-mini"],
          "new": ["LLM_MODEL=anthropic-direct:claude-haiku-5-5"]
        }
      ],
      "verification": "Send one request and check that it returns 200."
    }
  ]
}
```

- `impact` is one of `CRITIQUE` (apply immediately), `IMPORTANT` or `EVOLUTION` (apply when convenient); `check_updates` groups pending items by these values.
- `clients` lists the client IDs the update targets; it should match the client IDs of your keys.
- Each change has `file`, `action` and `description`, and optionally `detail` (free text shown as a code block) or `old` and `new` (lists of lines). An optional `mcp_server` object is rendered as an MCP client configuration snippet.
- Applied updates are recorded in `data/client_updates.db`.

The registry tools are not tied to the caller's key: anyone who can run this MCP server can read the registry and mark updates as applied for any client ID. Keep the registry free of secrets.
