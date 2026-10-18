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
| [Troubleshooting](guide/troubleshooting.md) | Diagnose startup, login, client and provider problems |

## Reference

- [Architecture](ARCHITECTURE.md): components, request flow, state and security boundaries.
- [Architecture Decision Records](adr/README.md): why the design is what it is.
- [Runbooks](runbooks/README.md): step-by-step procedures for operators.
- The live OpenAPI schema is served by the running instance at `/openapi.json` and `/docs` (admin access required).
