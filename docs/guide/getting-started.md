# Getting started

This guide takes you from a fresh clone to a first metered request on your own machine.

## Requirements

- Python 3.12 (`python3.12` on `PATH`, or `uv`, which downloads it), `make` and git. The dependency locks target 3.12; `make setup PYTHON=python3.13` uses another interpreter, unsupported.
- Optional: [Ollama](https://ollama.com) for local models, and the official CLIs (`claude`, `codex`, `gemini`, `gh`) if you want to use subscriptions (read the [risks](providers-and-subscriptions.md#risks-of-using-subscriptions) first).

## Install

```bash
git clone <repository-url> plugboard
cd plugboard
make setup   # creates .venv with Python 3.12 and installs requirements-dev.lock (hash-checked)
make init    # creates .env from .env.example with a random ADMIN_KEY and API_KEY
make dev     # runs python run.py with hot reload (RELOAD=true)
```

Without `make`, the equivalent is:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env              # then set ADMIN_KEY and API_KEY to random values
chmod 600 .env                    # it holds ADMIN_KEY and ENCRYPTION_KEY
.venv/bin/python run.py
```

Generate random values with `openssl rand -hex 24`. The server refuses to start if `ADMIN_KEY` or `API_KEY` still holds comment text copied from `.env.example`.

`run.py` loads `.env`, listens on `HOST` (default `0.0.0.0` when unset; `.env.example` sets `127.0.0.1`) and `PORT` (default `8765`).

## What `.env` controls

| Variable | Why it matters on day one |
|---|---|
| `ADMIN_KEY` | Admin secret for the dashboard and every `/admin/*` endpoint. Required for auto-login too. |
| `API_KEY` | Any non-empty value turns on client authentication for `/v1/*`. Clients then use `pb_` keys created in the dashboard; the `API_KEY` value itself is not a client key. |
| `HOST` | `127.0.0.1` makes this a local install (dashboard auto-login). Use `0.0.0.0` on a server. |
| `ENCRYPTION_KEY` | Encrypts provider credentials at rest. If empty, a key is generated once in `data/.encryption_key`. |

The full list is in the [configuration reference](configuration.md).

## Open the dashboard

Open <http://localhost:8765> in a browser on the same machine. On a local install you are signed in automatically and redirected to `/dashboard`. Auto-login requires all of the following:

- the server was started with `python run.py` or `make dev` and listens on a loopback address (`HOST=127.0.0.1`), or `PLUGBOARD_LOCAL_AUTO_LOGIN=1` is set;
- `ADMIN_KEY` is not empty;
- the browser runs on the same machine and opens the URL directly (address bar or bookmark), through `localhost`, `127.0.0.1` or `::1`;
- no proxy sits in front of the server;
- admin MFA is not enforced.

Otherwise the login page asks for `ADMIN_KEY`. If you expected auto-login, the server log says why it was refused: see [Troubleshooting](troubleshooting.md#dashboard-auto-login-does-not-happen).

The dashboard has these pages:

| Page | What it is for |
|---|---|
| Overview | Request counts, errors and latency per provider |
| API Keys | Create, revoke, rotate and delete client keys |
| Usage & Billing | Monthly usage per client, CSV export |
| Providers | Add API-key presets, CLI-based providers and custom endpoints; discover models |
| Connections | GitHub device-flow sign-in for GitHub Models and Copilot |
| Pricing | View and edit the current month's prices |
| Playground | Chat with any model, check which models have internet access |
| Ollama | Pull, inspect and delete local Ollama models |
| Monitoring | Provider health, uptime and recent history |
| Jobs | The price refresh, the model catalog refresh and the provider watchdog: state, next run, **Run now** |

## Add a first provider

The fastest options:

- **Ollama**: if Ollama answers on `http://localhost:11434` when the server starts, it is registered automatically as the `ollama` provider.
- **API key**: Providers → **+ Add Direct Provider** → choose *OpenAI Direct*, *Anthropic Direct*, *Google Gemini Direct* or *GitHub Models* → paste the key → **Create**.

All options are described in [Providers and subscriptions](providers-and-subscriptions.md).

## Create a client key

API Keys → **+ New Key**:

- **Client ID**: a slug for the application, for example `my-app`. Usage is aggregated per client ID.
- **Label**: free text, for example `production`.
- **Monthly Limit ($)** and **RPM Limit**: leave empty to apply the server defaults (`PLUGBOARD_DEFAULT_MONTHLY_LIMIT_USD`, 10 USD, and `PLUGBOARD_DEFAULT_RPM_LIMIT`, 60 requests per minute), or enter `0` for unlimited.

Copy the `pb_...` key immediately: only its hash is stored. More in [Clients and keys](clients-and-keys.md).

## Send a request

```bash
export PB_KEY=pb_your_key

curl -s http://localhost:8765/v1/chat/completions \
  -H "Authorization: Bearer $PB_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "openai-direct:gpt-4o-mini", "messages": [{"role": "user", "content": "Hello"}]}'
```

Replace the model with `<provider-id>:<model>` for a provider you configured. Then open **Usage & Billing** to see the request and its cost.

## Where your data lives

Everything is stored under `data/` (or `PLUGBOARD_DATA`):

| Path | Content |
|---|---|
| `providers.json` | Provider definitions; credentials encrypted |
| `api_keys.json` | Client key records (hashes, limits) |
| `pricing/YYYY_MM.json` | One pricing grid per month |
| `usage.db` | Usage entries (SQLite) |
| `cache.db`, `semantic_cache.db` | Response caches |
| `audit.db`, `monitoring.db`, `watchdog.db` | Admin audit trail and health history |
| `credentials.db` | Credentials from the dashboard OAuth connections |
| `sessions.db` | Optional server-side chat sessions |
| `admin_mfa.json` | Encrypted TOTP secret when MFA is enrolled |
| `.encryption_key` | Generated encryption key when `ENCRYPTION_KEY` is empty |
| `model_status.json`, `quarantined_models.json` | Model connectivity status and quarantine list |

Back up this directory and keep it private.

## Run the tests

```bash
make test         # pytest with coverage
make secret-scan  # look for committed secrets before you push
```

## Next steps

- [Providers and subscriptions](providers-and-subscriptions.md)
- [Clients and keys](clients-and-keys.md)
- [Deployment](deployment.md) when you move to a server
