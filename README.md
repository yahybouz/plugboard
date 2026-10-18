# Plugboard

**A self-hosted, OpenAI-compatible gateway that puts all your LLM providers and subscriptions behind one endpoint, with per-client keys, spending limits, caching, fallback, monitoring and an automatically updated pricing grid.**

Point any OpenAI-compatible client at `http://localhost:8765/v1`, give it a `pb_` key, and Plugboard routes the request to the provider behind the model you picked: an API key (OpenAI, Anthropic, Google, GitHub Models), a local Ollama, any OpenAI-compatible server, or a subscription you are already logged into through an official CLI. Every call is metered per client and checked against that client's limits.

Python 3.12 · FastAPI · no external database (JSON files and SQLite under `data/`) · MIT licensed.

---

## Features

- **One OpenAI-compatible API**: `POST /v1/chat/completions` (streaming over SSE), `GET /v1/models`, `GET /health`. Existing OpenAI SDKs, LangChain and curl work unchanged.
- **Many providers, one model namespace**: models are addressed as `<provider>:<model>`. Built-in presets, every OpenAI-compatible provider of the [models.dev](https://models.dev) catalog (OpenRouter, OpenCode, Kimi, Groq, DeepSeek, Mistral...) in one click, custom endpoints, Ollama (auto-detected), and subscription access through the Claude Code, Codex and Antigravity (`agy`) CLIs or the GitHub CLI. Model lists refresh every 6 hours and changes are journaled and sent as webhooks.
- **Per-client keys**: create `pb_` keys from the dashboard, each with a monthly USD budget, a requests-per-minute limit and an optional list of allowed models. Keys are stored as SHA-256 hashes and shown once.
- **Usage and billing**: tokens and cost are recorded for every request, per client, per key and per month. Responses carry `X-Cost-USD`, `X-Prompt-Tokens` and `X-Completion-Tokens`. CSV export and a read-only client portal.
- **Automatic pricing grid**: one grid per month (USD per million tokens), refreshed every 6 hours from several sources (your own JSON, OpenRouter, models.dev, LiteLLM), with the source of every price, reseller prices per provider, large changes held for your review, and manual prices that no source overwrites. Optionally, a model reads provider pricing pages and proposes prices for you to approve.
- **Caching**: an exact-match response cache scoped per client, and an optional semantic cache for near-duplicate prompts.
- **Fallback and resilience**: retries with backoff on 429/5xx, fallback to other providers for unprefixed model names, a per-model circuit breaker, latency- and error-aware load balancing.
- **Monitoring**: admin dashboard, provider health history, a provider watchdog, a JSON and Prometheus metrics endpoint, an admin audit log, and webhook alerts for failing providers and clients nearing their budget.
- **Admin security**: dashboard auto-login only for a local install opened in the local browser; everywhere else an `ADMIN_KEY` login with brute-force lockout and optional TOTP MFA. Provider credentials are encrypted at rest.
- **MCP server**: exposes chat, model listing, usage and provider status to MCP clients, plus an update registry that tells client projects what to change when the gateway changes.

## 5-minute quickstart (local install)

Requirements: Python 3.12 (`python3.12` on `PATH`, or `uv`, which downloads it), `make`, git.

```bash
git clone <repository-url> plugboard
cd plugboard
make setup   # creates .venv (Python 3.12) and installs the dependencies
make init    # creates .env (mode 0600) with a random ADMIN_KEY and API_KEY
make dev     # starts the server on http://localhost:8765 with hot reload
```

Check that `.env` contains `HOST=127.0.0.1` (set by `.env.example`; without it `run.py` listens on `0.0.0.0`). With that setting the server is a **local install**: open <http://localhost:8765> in a browser on the same machine and you land on the dashboard without typing a key.

Then, in the dashboard:

1. **Providers → + Add Direct Provider**: pick a preset (for example *OpenAI Direct*) and paste your API key. If Ollama is running on `http://localhost:11434`, it is registered automatically at startup.
2. **API Keys → + New Key**: enter a client ID (for example `my-app`), optionally a monthly limit and an RPM limit, and copy the `pb_...` key. It is displayed only once.
3. **Playground**: send a test message to any listed model.

Next steps: [Getting started](docs/guide/getting-started.md) · [Providers and subscriptions](docs/guide/providers-and-subscriptions.md) · [Deployment](docs/guide/deployment.md).

## Use it from your app

List the models your key can use:

```bash
curl -s http://localhost:8765/v1/models -H "Authorization: Bearer pb_your_key" | jq '.data[].id'
```

Send a chat completion with curl:

```bash
curl -s http://localhost:8765/v1/chat/completions \
  -H "Authorization: Bearer pb_your_key" \
  -H "Content-Type: application/json" \
  -d '{
        "model": "openai-direct:gpt-4o-mini",
        "messages": [{"role": "user", "content": "Say hello in one sentence."}],
        "max_tokens": 100
      }'
```

With the official OpenAI Python SDK:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8765/v1", api_key="pb_your_key")

response = client.chat.completions.create(
    model="openai-direct:gpt-4o-mini",
    messages=[{"role": "user", "content": "Say hello in one sentence."}],
)
print(response.choices[0].message.content)
```

A model is addressed as `<provider-id>:<model>`. The provider ID is shown on the dashboard's Providers page (here `openai-direct`, created by the *OpenAI Direct* preset); `GET /v1/models` lists the models the gateway advertises (by default the two newest versions of each model family, `PLUGBOARD_MODEL_EXPOSE=all` for every model), while any `<provider-id>:<model>` can still be called (see [How models are listed](docs/guide/providers-and-subscriptions.md#how-models-are-listed)). The response body also contains a `billing` object (tokens, cost, pricing month) and the `X-Cache` header tells you whether the answer came from the cache. Details: [Clients and keys](docs/guide/clients-and-keys.md).

## Providers at a glance

| Kind | Examples | Credentials |
|---|---|---|
| API key presets | Anthropic, OpenAI, Google Gemini, GitHub Models | Your API key or token, pasted in the dashboard |
| Custom endpoint | Any server exposing an OpenAI-compatible `/v1/chat/completions` (vLLM, LM Studio, another gateway) | Optional bearer token |
| Local models | Ollama | None; detected at `OLLAMA_URL` |
| Subscription through an official CLI | Claude Code (`claude`), Codex (`codex`), Antigravity (`agy`) | The CLI's own login on the gateway host |
| Subscription through CLI credentials | ChatGPT via Codex CLI, Gemini via Gemini CLI, GitHub Copilot and GitHub Models via `gh` | Tokens the CLI already stored on the gateway host |
| GitHub device flow | GitHub Models, GitHub Copilot | Sign-in from the dashboard's *Connections* page |

Step-by-step setup for each kind, from the dashboard or from `.env`: [Providers and subscriptions](docs/guide/providers-and-subscriptions.md).

## Using subscriptions (Claude, ChatGPT, Gemini) — read the risks

Plugboard can serve models from a consumer subscription (Claude Pro/Max, ChatGPT Plus/Pro, Gemini, GitHub Copilot) by running the provider's official CLI or by reusing the credentials that CLI stored. **This may violate the provider's terms of service.**

- Anthropic's Claude Code terms reserve subscription (OAuth) sign-in for ordinary use of Claude Code and Anthropic's own apps, and in 2026 Anthropic restricted the use of Free, Pro and Max credentials in third-party tools, including routing requests through them. See [Claude Code legal and compliance](https://code.claude.com/docs/en/legal-and-compliance).
- Google's Gemini CLI FAQ states that using third-party software to piggyback on Gemini CLI's OAuth sign-in violates its terms and may lead to suspension or termination of the account. See the [Gemini CLI FAQ](https://geminicli.com/docs/resources/faq/).
- OpenAI and GitHub publish their own terms for ChatGPT and Copilot plans; read them before relying on these modes.

The feature is available because some people run Plugboard as a personal tool on their own machine. **You decide whether to use it, and you bear the risk**, including the loss of your subscription account. **API keys are the safe option**: they are billed per use, are meant for programmatic access, and are what you should use for anything shared with other people or applications.

## Why not LiteLLM or OpenRouter?

Both are good tools; pick the one that matches your constraints.

| | Plugboard | LiteLLM proxy | OpenRouter |
|---|---|---|---|
| Where it runs | Your machine or server | Your machine or server | Hosted service |
| Who sees your prompts | Only the providers you configure | Only the providers you configure | OpenRouter and the upstream provider |
| Provider coverage | Built-in presets plus the OpenAI-compatible providers of models.dev | Very broad (100+ providers) | Very broad catalog |
| Consumer subscriptions via CLIs | Yes, at your own risk | No | No |
| Per-client keys with monthly USD limits and RPM limits | Yes, built in, with a dashboard | Yes (virtual keys, budgets) | Per-account credits and keys |
| MCP server with an update registry for client projects | Yes | No | No |
| Storage and scaling | JSON + SQLite, single instance | Database-backed, scales horizontally | Managed |

Choose **Plugboard** if you want one small self-hosted gateway for a handful of apps, with a dashboard, per-client budgets, and the option to use subscriptions you already pay for. Choose **LiteLLM** if you need the widest provider coverage, a database-backed deployment or several instances. Choose **OpenRouter** if you prefer a hosted service with one bill and no infrastructure. Plugboard itself uses LiteLLM's public price list as one of its pricing sources.

## Security notes

- **Auto-login is for a local install only.** It applies when the server listens on a loopback address (`HOST=127.0.0.1`), the request comes directly from the same machine through a browser navigation, no proxy header is present, `ADMIN_KEY` is set and MFA is not enforced. Any deployed instance (`HOST=0.0.0.0`, Docker, reverse proxy) asks for `ADMIN_KEY`. Set `PLUGBOARD_LOCAL_AUTO_LOGIN=0` on servers.
- **Known limit of auto-login.** It trusts a connection from the loopback interface. Containers or VMs whose networking ends on the host loopback (Docker Desktop, Podman, Lima/Colima, OrbStack), a port forwarder placed in front of the install, or a local process sending browser-like headers can obtain an admin session. On a shared machine, or when untrusted containers run on it, set `PLUGBOARD_LOCAL_AUTO_LOGIN=0`.
- **CLI providers are isolated.** `claude`, `codex` and `gemini` run a client's prompt with an allowlisted environment (never `ADMIN_KEY`, `API_KEY` or `ENCRYPTION_KEY`) from a private temporary directory; the server logs a warning when `.env` is readable by other users (`make init` creates it with mode `0600`).
- **Set `API_KEY`.** A non-empty `API_KEY` turns on client authentication for `/v1/*`; clients then use `pb_` keys. With an empty `API_KEY` the server logs a critical warning and `/health` reports `auth_disabled: true`.
- **Protect `data/`.** It holds encrypted provider credentials, key hashes, usage records and the encryption key file if you did not set `ENCRYPTION_KEY`.
- The server refuses to start if `API_KEY` or `ADMIN_KEY` contains comment text copied from `.env.example`.
- Report vulnerabilities privately, as described in [SECURITY.md](SECURITY.md).

## Documentation

- [Getting started](docs/guide/getting-started.md)
- [Providers and subscriptions](docs/guide/providers-and-subscriptions.md)
- [Clients and keys](docs/guide/clients-and-keys.md)
- [Pricing](docs/guide/pricing.md)
- [Monitoring](docs/guide/monitoring.md)
- [Configuration reference](docs/guide/configuration.md)
- [Deployment](docs/guide/deployment.md)
- [MCP server](docs/guide/mcp.md)
- [Troubleshooting](docs/guide/troubleshooting.md)
- [Architecture](docs/ARCHITECTURE.md), [decision records](docs/adr/README.md), [runbooks](docs/runbooks/README.md)

## Roadmap

Not built yet:

- **Native adapters** for providers that do not speak the OpenAI chat format (Anthropic Messages through the catalog, Bedrock, Azure deployments).
- **Several instances** sharing one data store.

## Contributing

The canonical repository is on GitLab, with a read-only mirror on GitHub. Please open issues and merge requests on GitLab. See [CONTRIBUTING.md](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE) © Plugboard contributors.
