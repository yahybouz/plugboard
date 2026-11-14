# MCP server

Plugboard ships a [Model Context Protocol](https://modelcontextprotocol.io) server named `plugboard`. It lets MCP clients (Claude Code, Claude Desktop, Cursor, agent frameworks) chat through the gateway, list models, check usage and provider health, and receive **update instructions** when the gateway changes in a way that affects them.

The MCP server is a separate process. It does not access providers itself: it calls a running Plugboard instance over HTTP, with a client key, so budgets, limits and usage tracking apply as for any other client. Only `provider_status` and the [admin tools](#admin-tools-stdio-only) use the admin key, over stdio.

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
| `PLUGBOARD_ADMIN_KEY` | empty | Admin key, only needed for `provider_status` and the [admin tools](#admin-tools-stdio-only); read in stdio mode only |
| `PLUGBOARD_MCP_ADMIN_TOOLS` | empty | `1` turns on `add_provider` and `refresh_catalog` (stdio only, with `PLUGBOARD_ADMIN_KEY`) |
| `PLUGBOARD_MCP_TIMEOUT` | `900` | Timeout of chat calls, in seconds |
| `PLUGBOARD_MCP_TOKEN` | empty | Bearer token required in SSE or HTTP mode, 32 characters or more |
| `MCP_HOST` | `127.0.0.1` | Bind address in SSE or HTTP mode |
| `MCP_PORT` | `8766` | Port in SSE or HTTP mode |
| `PLUGBOARD_MCP_ALLOWED_HOSTS` | loopback | Comma-separated `Host` values accepted in SSE or HTTP mode (`host:port`, `host:*`) |
| `PLUGBOARD_MCP_ALLOWED_ORIGINS` | loopback | Comma-separated `Origin` values accepted in SSE or HTTP mode |

`scripts/run_mcp.py` also reads `.env` from the repository root, without overriding variables that are already set. Give the MCP server its own client key with its own budget, and only pass the admin key if you need `provider_status` or the admin tools.

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
- `PLUGBOARD_ADMIN_KEY` is never loaded: `provider_status` (and `monitoring://status`), `add_provider` and `refresh_catalog` answer that they are available over stdio only.
- Every caller holding the token acts as the client of `PLUGBOARD_API_KEY`: run one MCP server, with its own key and token, per client.
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

## Use it from a client

The supported way to reach the models through MCP is **stdio**: your MCP client starts the server as a child process, and the server calls the gateway over HTTP(S) with your **client key** (`pb_...`). Nothing listens on a port, so this opens no new network exposure. Do not use the admin key for this.

You need two things: how to start the process, and the three variables.

| Variable | Value |
|---|---|
| `PLUGBOARD_URL` | The gateway's address as the client reaches it, for example `https://plugboard.example.com` |
| `PLUGBOARD_API_KEY` | Your client key (`pb_...`) |
| `PLUGBOARD_MCP_TIMEOUT` | Optional: seconds allowed to a chat call (default 900) |

**A. On the client's machine** (a checkout of the repository and its MCP environment, `make setup-mcp`): this is the configuration shown above, with `command` set to the `.venv-mcp` interpreter and `args` to `scripts/run_mcp.py`.

**B. Inside the gateway's container** (the `full` image ships the MCP environment in `/opt/venv-mcp`): the client runs the command through Docker, locally or over SSH, and `PLUGBOARD_URL` is the gateway as seen from the container (`http://127.0.0.1:8765`):

```json
{
  "mcpServers": {
    "plugboard": {
      "command": "docker",
      "args": [
        "exec", "-i",
        "-e", "PLUGBOARD_URL=http://127.0.0.1:8765",
        "-e", "PLUGBOARD_API_KEY",
        "plugboard",
        "/opt/venv-mcp/bin/python", "/app/scripts/run_mcp.py"
      ],
      "env": { "PLUGBOARD_API_KEY": "pb_your_key" }
    }
  }
}
```

Replace `plugboard` with the container's name. `-e PLUGBOARD_API_KEY` without a value passes the variable from the client's `env`, so the key is not written in the arguments (visible in a process list). Over SSH, run the same `docker exec -i ...` command on the server (`"command": "ssh", "args": ["host", "docker", "exec", ...]`) and put the key in that remote command's environment.

**Check it works** with one command, from a checkout, with the same variables (`PLUGBOARD_KEY` is the same client key):

```bash
PLUGBOARD_URL=https://plugboard.example.com PLUGBOARD_KEY=pb_your_key \
  python scripts/recette_acces.py results.json
```

It lists the models your key sees, calls one per active provider through the API (plain and streamed) and through this MCP server, prints a table and exits non-zero if a surface fails.

**What the tools show.** `list_models` shows exactly the model IDs of `GET /v1/models` for your key: only models you may call, none from a turned-off provider. A refused call comes back as `Error: <code>: <detail>`, the same code and message the API gives: `quota_exhausted` (a subscription's quota is used up, retry later), `providers_busy` (every provider of the model is at its cap, retry in a few seconds), `forbidden_model` (your key may not use the model), `model_not_supported_by_subscription` (the account plan behind the route does not serve it), `provider_timeout`.

## Tools

| Tool | Arguments | What it does |
|---|---|---|
| `chat` | `model`, `message`, `max_tokens` (4096), `temperature` (0.7), `system_prompt`, `session_id` | One chat completion through `/v1/chat/completions` |
| `chat_stream` | same as `chat` | The same with `stream: true` |
| `list_models` | none | Model IDs from `/v1/models` |
| `embed` | `input` (string or list, ≤ 256), `model`, `include_vectors` (true) | Embeddings through `/v1/embeddings`: model, count, dimensions, usage and vectors |
| `list_embedding_models` | none | Embedding model IDs the key can use (`/v1/embeddings/models`) |
| `list_decision_models` | none | Permitted decision engines, availability, capabilities and confidence semantics |
| `decide` | `model`, `state`, `questions` | Named typed questions through `/v1/decisions`; no chat fallback |
| `check_usage` | none | Usage, spend and limits of the key (`/v1/me`) |
| `inflight_requests` | none | Chat calls of the key's client running now (model, time, phase, "slow" flag) and their last results: tells a slow call from a broken one (`/v1/inflight`); refused with the admin key |
| `provider_status` | none | Watchdog state and provider health (admin key required) |
| `add_provider` | `name`, `base_url`, `api_key`, `default_model` (optional) | Add an OpenAI-compatible provider, inactive, public addresses only ([admin tools](#admin-tools-stdio-only)) |
| `refresh_catalog` | `provider_id`, `dry_run` (false) | Refresh a provider's model list, like the dashboard's Discover button ([admin tools](#admin-tools-stdio-only)) |
| `check_updates` | `client_id` (optional) | Pending updates for the key's client |
| `get_update_prompt` | `prompt_id` | Full instructions of one update for the key's client |
| `mark_applied` | `prompt_id`, `notes`, `client_id` (optional) | Record that the key's client applied an update |
| `list_applied` | `client_id` (optional) | Updates already applied by the key's client |

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

## Admin tools (stdio only)

`add_provider` and `refresh_catalog` let a coding agent add a provider and refresh its model list without the dashboard. They change the gateway for every client, so they are off by default and guarded:

- **Turning them on.** Set `PLUGBOARD_MCP_ADMIN_TOOLS=1` and `PLUGBOARD_ADMIN_KEY`, and run the server over stdio. Over SSE or HTTP they refuse, like `provider_status`: the admin key is never loaded there. An admin key set only for `provider_status` does not turn them on.
- **Admin MFA.** When the gateway enforces admin MFA, the admin key alone is refused, so both tools refuse and send you to the dashboard. The MCP server never holds a TOTP secret.
- **Same checks as the admin API.** The tools call `POST /providers`, `GET /providers` and `POST /providers/{id}/discover` with the admin key: the gateway validates, writes and sends its webhooks as for the dashboard, and audits as for it with the actor `mcp` (see Audit). A refused input gets the gateway's reason (`Error: HTTP <status>: <detail>`, cut at 300 characters); a refused admin key or enforced MFA gets its own message.
- **`add_provider`** creates the provider **inactive**, with **Public addresses only** on: an internal or private address (`127.0.0.1`, `10.x`, `169.254.169.254`...) is refused, and every later connection is checked again. An inactive provider receives no client traffic, so an agent misled by a text it read cannot divert other clients' calls to a host it chose: an admin activates the provider in the dashboard. A local provider (Ollama, vLLM on `localhost`) is added from the dashboard. The answer gives the provider's id (a taken id gets a suffix such as `-1`). If it says the provider may have been created (timeout, answer cut off, `500`), check the dashboard before retrying.
- **`refresh_catalog`** is the Discover button: the gateway lists the provider's models and writes them, and models it no longer lists are removed at once unless its list is not trusted; `default_model` and `pinned_models` are kept. Run it with `dry_run: true` first to see what would change. CLI providers (`subprocess:`, `cli:`) are refused: they depend on a local CLI sign-in, and a `subprocess:` refresh runs model probes, so refresh them from the dashboard. The answer shows at most 20 model IDs per list (models, added, removed); since they come from the provider's own API, control characters are replaced by spaces and each ID is cut at 120 characters.
- **Limits** (per MCP server process, in memory): 5 `add_provider` calls per hour that reach the gateway, refused ones included; per provider, one `refresh_catalog` preview and one write per minute; one refresh at a time. They stop an agent that loops; anyone with the admin key can still call the API directly.
- **Audit.** The gateway records these calls with the actor `mcp` (from the `X-Plugboard-Surface: mcp` header the MCP server sends): `provider.create` and `provider.discover`; a refused address as `provider.egress.rejected` (internal or private address) or `provider.domain.rejected` (host outside `PLUGBOARD_PROVIDER_DOMAINS_ALLOWED`); and, for this actor only, the other refusals `provider.create.refused` and `provider.discover.refused` and the previews `provider.discover.preview` ([admin audit log](monitoring.md#admin-audit-log)). The header tells MCP calls apart; it proves nothing, since any admin can send it.
- **The provider key.** `api_key` goes through the agent's context, then to the gateway, which stores it encrypted. The tool never shows it back, in an answer or an error, and the gateway never writes it to the audit. If the MCP SDK rejects the arguments before the tool runs (a missing field, a wrong type), its error to the caller quotes them in part. The MCP server drops the SDK's DEBUG records that hold whole messages (`mcp.server.lowlevel.server`, `mcp.server.sse`, `mcp.server.streamable_http`). The SDK still logs a request that fails its JSON-RPC validation through the root logger, the message at DEBUG and part of it (about 25 characters) at WARNING: keep the root logger above WARNING while these tools are on.
- **Not available** over MCP: editing or deleting a provider (a new `base_url` would send the stored key elsewhere), and catalog presets (use the dashboard).

## Update registry

The update registry lets the gateway operator push integration changes to the projects that use it. An AI coding agent working in a client project calls `check_updates` (no argument) at the start of a session, reads each pending item with `get_update_prompt`, applies it, and calls `mark_applied` with its ID.

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
- `clients` lists the client IDs the update targets: a list holding the client IDs of your keys (a single string targets no client).
- Each change has `file`, `action` and `description`, and optionally `detail` (free text shown as a code block) or `old` and `new` (lists of lines). An optional `mcp_server` object is rendered as an MCP client configuration snippet.
- Applied updates are recorded in `data/client_updates.db`.

The registry tools act for the client of `PLUGBOARD_API_KEY`, which the server asks the gateway for (`GET /v1/me`) at each call: a caller only sees, reads and marks its own client's updates, and `updates://pending` lists only those. `client_id` is optional; a value that names another client is refused, and another client's update answers *not found*. Without a client key (no key, the admin key, or a key the gateway refuses) the registry tools answer an error. Keep the registry free of secrets.
