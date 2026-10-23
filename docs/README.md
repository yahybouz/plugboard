# Plugboard documentation

## Guides

| Guide | Read it to |
|---|---|
| [Getting started](guide/getting-started.md) | Install Plugboard, open the dashboard, send a first request |
| [Providers and subscriptions](guide/providers-and-subscriptions.md) | Add API keys, local models, custom endpoints and subscriptions, and understand the risks |
| [Clients and keys](guide/clients-and-keys.md) | Issue `pb_` keys, set budgets and rate limits, read usage |
| [Pricing](guide/pricing.md) | Understand and edit the monthly pricing grid |
| [Monitoring](guide/monitoring.md) | Use the dashboard, health and metrics endpoints, watchdog, audit log and alerts |
| [Configuration](guide/configuration.md) | Look up every environment variable |
| [Deployment](guide/deployment.md) | Run Plugboard on a server, in Docker or behind a reverse proxy |
| [MCP server](guide/mcp.md) | Connect MCP clients and publish updates to client projects |
| [Integrating with Plugboard](api/README.md) | Call the API from an application or an MCP client, against a versioned contract |
| [Troubleshooting](guide/troubleshooting.md) | Diagnose startup, login, client and provider problems |

## Reference

- [Architecture](ARCHITECTURE.md): components, request flow, state and security boundaries.
- [Architecture Decision Records](adr/README.md): why the design is what it is.
- [Runbooks](runbooks/README.md): step-by-step procedures for operators.
- [Client API contract](api/openapi.json) (OpenAPI 3.1) and [MCP contract](api/mcp.json): what integrators may rely on, checked in CI. A running instance serves the first at `/v1/openapi.json`, without a key.
- The full OpenAPI schema, admin routes included, is served by the running instance at `/openapi.json` and `/docs` (admin access required).
