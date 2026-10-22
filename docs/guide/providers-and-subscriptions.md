# Providers and subscriptions

A **provider** is a backend that answers chat completions: a cloud API, a local Ollama, any server with a standard chat API, or a subscription reached through an official CLI. Each person who runs Plugboard adds their own providers with their own credentials; nothing is shared or preconfigured.

Provider credentials stay on the gateway host. API keys and tokens are encrypted at rest in `data/providers.json` and `data/credentials.db` (see `ENCRYPTION_KEY` in the [configuration reference](configuration.md)).

## Provider kinds

| Kind | Dashboard presets | How requests are served |
|---|---|---|
| API key | *Anthropic Direct*, *Google Gemini Direct*, *GitHub Models*, *OpenAI Direct* | HTTP call to the vendor with your key |
| Catalog (models.dev) | *OpenRouter · unverified*, *Groq · unverified*, *Moonshot AI (Kimi) · unverified*, *OpenCode Zen · unverified* and every other provider with a standard chat API that the catalog lists | HTTP call with your key |
| Custom endpoint | **+ Add Custom Provider** | HTTP call to any server with a standard chat API |
| Local models | *Ollama* (automatic) | HTTP call to your Ollama daemon |
| Subscription through an official CLI | *Claude (via claude CLI)*, *ChatGPT (via codex CLI)*, *Antigravity (via agy CLI)* | Plugboard runs the CLI as a subprocess for each request |
| Subscription through CLI credentials | *ChatGPT (via Codex CLI)*, *Gemini (via Gemini CLI)*, *GitHub Copilot (via gh CLI)*, *GitHub Models (via gh CLI)* | HTTP call with the token the CLI stored on the host |
| GitHub sign-in from the dashboard | *GitHub Models (OAuth via dashboard)*, *GitHub Copilot (OAuth via dashboard)* | HTTP call with a token obtained through GitHub's device flow |

Presets marked *roadmap* in the dashboard (for example *Claude Pro (roadmap)*) cannot be selected.

## Model IDs

A model is addressed as `<provider-id>:<model>`, for example `anthropic-direct:claude-haiku-5-5` or `ollama:qwen3:8b`. The provider ID is derived from the provider name when you create it (*OpenAI Direct* becomes `openai-direct`) and is shown on the Providers page.

- With an explicit provider prefix, the request goes to that provider only. Plugboard retries it on `429`, `500`, `502`, `503` and `529` with backoff, but does not switch provider.
- Without a matching prefix, Plugboard tries the active providers in turn, ordered by a score that combines latency, error rate and requests in flight.
- `/v1/models` shows short display prefixes for the CLI providers: `claude-code:` for the Claude CLI, `Codex:` for the Codex CLI, `Gemini:` for the Gemini CLI, and `ollama:` for Ollama. Every other provider is listed under its own ID. These prefixes are accepted in requests, but they are not provider IDs, so such a request goes through the fallback path. To target a provider directly, use its ID, for example `claude-via-claude-cli:`, `chatgpt-via-codex-cli:` or `antigravity-via-agy-cli:` for the CLI presets.

### How models are listed

By default `GET /v1/models` lists every discovered model, within the limits below. Setting `PLUGBOARD_MODEL_FILTER=curated` keeps a short list per built-in provider instead:

- Claude: Opus, Sonnet and Haiku only, and only the `PLUGBOARD_CLAUDE_LATEST_N` most recent of each family (default 2).
- OpenAI and Codex: GPT-5 and newer.
- Gemini: Gemini 3 and newer.
- Ollama: only cloud models (`:cloud` or `thinking` variants) of the families deepseek, kimi, qwen, glm, gemma, nemotron, minimax, gpt-oss and gemini. Locally pulled models are not listed.
- Other providers (custom endpoints and their models) are listed as they are, in both modes.

In every mode:

- Per model family, only the `PLUGBOARD_MODEL_EXPOSE_VERSIONS` newest versions are listed (default 2); `PLUGBOARD_MODEL_EXPOSE=all` lists every model. A provider's default model and models already used stay listed.
- `PLUGBOARD_TOP_N_PER_PROVIDER` can also cap each provider (default `0`, no cap), ranked by benchmark score and recency. Ollama models are grouped by family and not truncated.
- Every model keeps its provider's ID (`anthropic-direct:claude-opus-5-5`). Only the Claude, Codex and Gemini CLI providers use the short `claude-code:`, `Codex:` and `Gemini:` prefixes.
- Models in `PLUGBOARD_MODEL_DENYLIST` are hidden. Quarantined models and models that failed their last connectivity probe stay listed with `"connected": false`.

A model hidden from the list can still be called with its explicit `<provider-id>:<model>` ID, for example `ollama:llama3.2` for a locally pulled Ollama model.

## Add a provider from the dashboard

### API key presets

1. Create a key with the vendor:
   - Anthropic: <https://console.anthropic.com/settings/keys>
   - OpenAI: <https://platform.openai.com/api-keys> (API access is billed separately from a ChatGPT subscription)
   - Google AI Studio: <https://aistudio.google.com/app/apikey>
   - GitHub Models: a personal access token with the `models:read` scope, from <https://github.com/settings/tokens>
2. In the dashboard, open **Providers → + Add Direct Provider**.
3. Search for the provider, paste the key and click **Test and create**. The key is tested first (see [Test the connection](#test-the-connection)); when the test fails you can still **Save anyway**.
4. Click **Discover** on the new row to load its model list, then **Ping** to check it answers.

### Any provider from the models.dev catalog

[models.dev](https://models.dev) is an open catalog of LLM providers, their models and their prices (MIT licence). Plugboard downloads it with the price updates and turns every provider that speaks the OpenAI chat format into a preset: OpenRouter, OpenCode Zen, Moonshot AI (Kimi), Groq, DeepSeek, Mistral, xAI, Together, Fireworks, DeepInfra, Cerebras and many more. They appear in the selector as **Catalog · unverified (models.dev)**: Plugboard did not test them, so test the key before relying on one.

A provider created from a catalog preset:

- keeps the base URL it was created with; a later catalog change never redirects your key;
- starts with the catalog's model list, then discovers what your key can really use in the background (the row shows **Discovering…**);
- has `auto_discover` on: its list is refreshed every 6 hours (see [Monitoring](monitoring.md#model-catalog-and-connectivity));
- is priced with its own prices when they differ from the vendor's (see [Prices per provider](pricing.md#prices-per-provider)).

Models that the catalog marks as needing another API (Anthropic Messages, OpenAI Responses) are left out of a catalog preset. `PLUGBOARD_CATALOG_EXCLUDE` hides whole providers, `PLUGBOARD_CATALOG=off` stops the download (only the built-in presets remain), and `PLUGBOARD_CATALOG_URL` points to a mirror.

### Test the connection

**Test connection** (or **Test and create**) calls `GET /models` on the provider with your key, without creating anything:

| Result | Meaning and what to do |
|---|---|
| Connected (N models) | The key works and the provider lists N models. |
| Key valid, but the provider lists no model | Authenticated, but nothing to route yet. Check the account (credits, region) or add a model by hand. |
| Invalid key (401/403) | The key is wrong, revoked, or belongs to another region or project of the same vendor. |
| Not a standard chat API | The host did not answer `GET /models` in the standard format. Check the base URL, or add it as a custom endpoint. |
| Unreachable | No answer: network, proxy, firewall or a wrong host. |

**Also send one test token** additionally sends a one-token chat completion to the first listed model. It is off by default because the provider bills it.

### Keep model lists up to date

Each provider has two settings, in **Edit** on the Providers page or through `PUT /providers/{id}`:

- **Refresh the model list automatically** (`auto_discover`): every refresh reads `GET /models` and adds new models at once. A model leaves the list after two refreshes in a row without it; a failed or empty answer, or one that would drop more than half of the list, changes nothing.
- **Pinned models** (`pinned_models`): never removed, whatever the provider answers. The default model is never removed either.

**Discover** on a row applies the provider's answer at once, including removals; when it would remove models, the dashboard lists them and asks first. Removals only follow the provider's own model list (`GET /models` or a native catalog API): when Plugboard falls back to a preset's list or asks a model to list itself, it adds what it finds and removes nothing. Providers created before this release keep `auto_discover` off, so their lists only change when you click **Discover** (Anthropic, Gemini, Ollama and the CLI subscriptions are refreshed as before).

### Custom endpoint

Use **+ Add Custom Provider** for any server that implements `POST /v1/chat/completions` (vLLM, LM Studio, llama.cpp server, another gateway):

| Field | Value |
|---|---|
| Name | A readable name; the provider ID is derived from it |
| Base URL | The server root (`http://localhost:8000`) or its versioned API root (`https://api.example.com/v1`). Plugboard adds `/v1` only to a bare host, then calls `/chat/completions` and `/models`. |
| Bearer Token | Optional; sent as `Authorization: Bearer <token>` |
| Default Model | Used when a request names this provider without a model |
| Timeout (s) | Request timeout (default 60) |

Then click **Discover**. Discovery calls `GET <base>/v1/models`; if that fails it falls back to the preset's known models, then to asking the default model for its list.

If `PLUGBOARD_PROVIDER_DOMAINS_ALLOWED` is set, a base URL whose host is not on that list is refused.

### Ollama

Start Ollama on the gateway host. At startup Plugboard checks `OLLAMA_URL` (default `http://localhost:11434`) and registers an `ollama` provider when the daemon answers. `OLLAMA_ENABLED=false` disables this; `OLLAMA_ENABLED=true` logs a warning when Ollama is not reachable.

The **Ollama** page pulls, inspects and deletes models. Ollama is a *sovereign* provider: the watchdog monitors it but never tries to restart it. Ollama cloud models are merged into the provider automatically unless `PLUGBOARD_OLLAMA_CLOUD_AUTOSYNC=0`.

### Subscriptions through an official CLI

Read [the risks](#risks-of-using-subscriptions) first. These presets run the vendor's unmodified CLI on the gateway host, as the operating-system user that runs Plugboard.

1. Install the CLI and sign in once, as that user:

   | CLI | Install | Sign in |
   |---|---|---|
   | Claude Code | `npm install -g @anthropic-ai/claude-code` | run `claude` and follow the login prompt |
   | Codex | `npm install -g @openai/codex` | `codex login` |
   | Antigravity CLI | see [antigravity.google](https://antigravity.google/), then `agy install` | run `agy` once and sign in with Google |

2. **Providers → + Add Direct Provider**, choose *Claude (via claude CLI)*, *ChatGPT (via codex CLI)* or *Antigravity (via agy CLI)*.
3. Click **Check CLI**: Plugboard verifies that the binary is on the `PATH` and runs. Then **Create**.

What to expect:

- Each request starts a CLI process, so the first token takes seconds rather than milliseconds.
- Concurrent processes per CLI are capped by `PLUGBOARD_SUBPROCESS_LIMIT` (default 4).
- The CLI runs in a neutral temporary working directory, so it does not pick up project files or settings; `PLUGBOARD_SUBPROCESS_CWD` overrides this.
- Codex runs with `--sandbox read-only`; `PLUGBOARD_CODEX_SANDBOX` changes the mode.
- Antigravity runs in print mode with `--sandbox --mode plan`. Headless `agy` cannot ask for a tool permission, so it refuses every tool call unless your agy `settings.json` pre-allows it: keep that file free of allow rules on the gateway host. Its model list is read from `agy models`; the codex list is read from the catalog cache the codex CLI keeps for your account (`~/.codex/models_cache.json`).
- The *Google Gemini (via gemini CLI)* preset is no longer offered (the gemini CLI now refuses personal-account sign-ins); providers created with it keep working while the CLI does.
- The CLIs do not report token counts, so usage is estimated with a tokenizer and billed at the model's price in the [pricing grid](pricing.md).
- Streaming requests are answered in full and then replayed as server-sent events.
- A daily job checks which models the signed-in account can really use and quarantines the others (see [Monitoring](monitoring.md#model-catalog-and-connectivity)).

### Subscriptions through CLI credentials

Read [the risks](#risks-of-using-subscriptions) first. These presets do not run the CLI; they read the token it stored and call the vendor's HTTP endpoint with it:

| Preset | Credential source |
|---|---|
| *ChatGPT (via Codex CLI)* | `~/.codex/auth.json` written by `codex login` |
| *Gemini (via Gemini CLI)* | `~/.gemini/oauth_creds.json` written by the Gemini CLI |
| *GitHub Models (via gh CLI)* | `gh auth token` |
| *GitHub Copilot (via gh CLI)* | `gh auth token`, exchanged for a Copilot token (requires a Copilot subscription) |

Refreshing an expired Gemini CLI token, and signing in to Gemini from the dashboard, need a Google OAuth desktop client in `GEMINI_OAUTH_CLIENT_ID` / `GEMINI_OAUTH_CLIENT_SECRET`; Plugboard does not ship one. Without it, run `gemini` once to refresh the token. Some vendor endpoints reject tokens obtained this way (see [ADR-002](../adr/ADR-002-oauth-broker-abandoned-for-google.md)). For Claude, ChatGPT and Gemini subscriptions, prefer the subprocess presets above.

### GitHub sign-in from the dashboard

The presets *GitHub Models (OAuth via dashboard)* and *GitHub Copilot (OAuth via dashboard)* use GitHub's device flow: the dashboard shows a code, you open the GitHub page, enter the code and approve. The token is stored encrypted in `data/credentials.db`. When you pick the same preset again, the provider form shows the connected account with **Reconnect** and **Disconnect**; `GET /admin/oauth/connections` lists every connected account.

## Add providers from `.env` or scripts

- **Bootstrap one provider at startup.** When `data/providers.json` contains no provider at startup, Plugboard creates one from `OPENCLAW_URL` and `OPENCLAW_KEY` (both required), with `OPENCLAW_MODEL` as default model and `OPENCLAW_NAME` as name. The variables are named after the OpenClaw gateway, but any OpenAI-compatible endpoint works. If the provider ID contains `openclaw`, unprefixed model names are rewritten to OpenClaw's `openclaw/<vendor>--<model>` format, so pick another name for other gateways.
- **Ollama**: `OLLAMA_URL`, `OLLAMA_ENABLED`, `OLLAMA_TIMEOUT`.
- **API key presets on a remote instance**: `scripts/setup_railway_providers.py` reads `SETUP_ANTHROPIC_API_KEY`, `SETUP_OPENAI_API_KEY`, `SETUP_GEMINI_API_KEY` and `SETUP_GITHUB_PAT` and creates the matching presets through the admin API (`PLUGBOARD_URL` and `PLUGBOARD_ADMIN_KEY` point it at the instance). Unset variables are skipped, and presets that already exist are left untouched.
- **Admin API**: everything the dashboard does is available over HTTP with `Authorization: Bearer $ADMIN_KEY`:

```bash
# list presets, then create one from a preset
curl -s http://localhost:8765/admin/direct-providers/presets -H "Authorization: Bearer $ADMIN_KEY"
curl -s -X POST http://localhost:8765/admin/direct-providers/openai \
  -H "Authorization: Bearer $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"api_key": "sk-..."}'

# test a key against a catalog preset, then create the provider
curl -s -X POST http://localhost:8765/admin/direct-providers/test \
  -H "Authorization: Bearer $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"preset_key": "catalog-openrouter", "api_key": "sk-or-..."}'
curl -s -X POST http://localhost:8765/admin/direct-providers/catalog-openrouter \
  -H "Authorization: Bearer $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"api_key": "sk-or-..."}'

# test any standard chat endpoint before adding it as a custom provider
curl -s -X POST http://localhost:8765/admin/direct-providers/test \
  -H "Authorization: Bearer $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"base_url": "https://api.example.com/v1", "api_key": "..."}'

# turn on automatic model refresh and pin a model
curl -s -X PUT http://localhost:8765/providers/openrouter \
  -H "Authorization: Bearer $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"auto_discover": true, "pinned_models": ["openai/gpt-4o-mini"]}'

# preview what a discovery would add and remove, then apply it
curl -s -X POST "http://localhost:8765/providers/openrouter/discover?dry_run=true" -H "Authorization: Bearer $ADMIN_KEY"
curl -s -X POST http://localhost:8765/providers/openrouter/discover -H "Authorization: Bearer $ADMIN_KEY"

# create a custom provider, then discover its models
curl -s -X POST http://localhost:8765/providers \
  -H "Authorization: Bearer $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"name": "Local vLLM", "base_url": "http://localhost:8000", "bearer_token": "", "default_model": "my-model"}'
curl -s -X POST http://localhost:8765/providers/local-vllm/discover -H "Authorization: Bearer $ADMIN_KEY"
```

`PUT /providers/{id}` updates a provider (base URL, token, default model, active flag, timeout, `auto_discover`, `pinned_models`) and `DELETE /providers/{id}` removes it. Catalog presets are keyed `catalog-<models.dev id>` (`catalog-openrouter`, `catalog-groq`, `catalog-moonshotai`...); `GET /admin/direct-providers/presets` lists them with `"catalog": true`.

The catalog settings in `.env`:

```bash
# Turn the models.dev download off (only built-in presets remain)
PLUGBOARD_CATALOG=off
# Or use a mirror, and hide some providers
PLUGBOARD_CATALOG_URL=https://mirror.example.com/models.dev/api.json
PLUGBOARD_CATALOG_EXCLUDE=some-provider,another-provider
```

## Risks of using subscriptions

Using a consumer subscription (Claude Pro/Max, ChatGPT Plus/Pro, Gemini, GitHub Copilot) through a gateway **may violate the provider's terms of service**:

- Anthropic reserves subscription sign-in for ordinary use of Claude Code and its own applications, and since 2026 does not permit routing requests through Free, Pro or Max credentials in third-party tools. See [Claude Code legal and compliance](https://code.claude.com/docs/en/legal-and-compliance).
- Google's Gemini CLI FAQ states that using third-party software to piggyback on Gemini CLI's OAuth sign-in violates its terms and may lead to account suspension or termination. See the [Gemini CLI FAQ](https://geminicli.com/docs/resources/faq/).
- OpenAI and GitHub publish their own terms for ChatGPT and Copilot plans. Heavy automated use through `gh` has also been answered with rate limits.

Plugboard ships these modes because some people run it as a personal tool on their own machine. **You decide whether to use them and you bear the consequences**, up to the loss of the account. Never expose a subscription-backed provider to other people. **API keys are the safe option**: they are designed for programmatic access and are billed per use.
