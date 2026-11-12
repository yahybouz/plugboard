# Providers and subscriptions

A **provider** is a backend that answers chat completions: a cloud API, a local Ollama, any server with a standard chat API, or a subscription reached through an official CLI. Each person who runs Plugboard adds their own providers with their own credentials; nothing is shared or preconfigured.

Provider credentials stay on the gateway host. API keys and tokens are encrypted at rest in `data/providers.json` and `data/credentials.db` (see `ENCRYPTION_KEY` in the [configuration reference](configuration.md)).

## Provider kinds

| Kind | Dashboard presets | How requests are served |
|---|---|---|
| API key | *Anthropic Direct*, *Google Gemini Direct*, *GitHub Models*, *OpenAI Direct* | HTTP call to the vendor with your key |
| Catalog (models.dev) | *OpenRouter · unverified*, *Groq · unverified*, *Moonshot AI (Kimi) · unverified*, *OpenCode Zen · unverified* and every other provider with a standard chat API that the catalog lists | HTTP call with your key |
| Custom endpoint | **+ Add custom provider** | HTTP call to any server with a standard chat API |
| Local models | *Ollama* (automatic) | HTTP call to your Ollama daemon |
| Subscription through an official CLI | *Claude (via claude CLI)*, *ChatGPT (via codex CLI)*, *Antigravity (via agy CLI)* | Plugboard runs the CLI as a subprocess for each request |
| Subscription through CLI credentials | *ChatGPT (via Codex CLI)*, *Gemini (via Gemini CLI)*, *GitHub Copilot (via gh CLI)*, *GitHub Models (via gh CLI)* | HTTP call with the token the CLI stored on the host |
| GitHub sign-in from the dashboard | *GitHub Models (OAuth via dashboard)*, *GitHub Copilot (OAuth via dashboard)* | HTTP call with a token obtained through GitHub's device flow |

Presets marked *roadmap* in the dashboard (for example *Claude Pro (roadmap)*) cannot be selected.

## Model IDs

A model is addressed as `<provider-id>:<model>`, for example `anthropic-direct:claude-haiku-5-5` or `ollama:qwen3:8b`. The provider ID is derived from the provider name when you create it (*OpenAI Direct* becomes `openai-direct`) and is shown on the Providers page.

- With an explicit provider prefix, the request goes to that provider only. Plugboard retries it on `429`, `500`, `502`, `503` and `529` with backoff, but does not switch provider. An error saying the context or the message is too long is not retried: the request would fail the same way, and the client gets `400`.
- Without a matching prefix, Plugboard tries the active providers in turn, ordered by a score that combines latency, error rate and requests in flight. A context too long goes straight to the next provider.
- When no provider knows the model at all (no provider ID prefixes it, no provider lists it, and every active provider has a model list), the client gets `404` with `"code": "model_not_found"` and no provider is called ([details](../api/README.md#404-model_not_found)). A model pulled with the `ollama` command rather than the Ollama page is not listed until the next refresh: click **Discover** on the provider, or send it as `<provider-id>:<model>`.
- `/v1/models` uses short display prefixes for the CLI providers: `claude-code:`, `Codex:` and `Gemini:`. A registered provider ID takes precedence over a display alias; when multiple registered prefixes match, the longest wins. The catalogue checks each display alias and its canonical fallback against **all registered owners**, including inactive and quarantined legacy records. It uses the canonical spelling when an alias is reserved by another owner.
- If neither existing spelling identifies the exact provider and native model, the catalogue omits that model until the configuration is unambiguous. For example, `owner` with native model `qwen3:latest` cannot publish `owner:qwen3:latest` when `owner:qwen3` is also registered: that ID selects the longer owner. This limit uses the existing grammar; no new separator or encoding is introduced. Unaffected models remain listed. Namespace additions, removals, quarantine and activity changes refresh cached publication on the next catalogue request.

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
- Every published model keeps its provider's ID (`anthropic-direct:claude-opus-5-5`), except for safe Claude, Codex and Gemini CLI display aliases described above.
- Models in `PLUGBOARD_MODEL_DENYLIST` are hidden. Quarantined models and models that failed their last connectivity probe stay listed with `"connected": false`.

A model hidden by exposure filters can still be called with its unambiguous explicit `<provider-id>:<model>` ID, for example `ollama:llama3.2` for a locally pulled Ollama model. An identity omitted because of a namespace collision has no guaranteed address until the configuration is unambiguous.

## Add a provider from the dashboard

### API key presets

1. Create a key with the vendor:
   - Anthropic: <https://console.anthropic.com/settings/keys>
   - OpenAI: <https://platform.openai.com/api-keys> (API access is billed separately from a ChatGPT subscription)
   - Google AI Studio: <https://aistudio.google.com/app/apikey>
   - GitHub Models: a personal access token with the `models:read` scope, from <https://github.com/settings/tokens>
2. In the dashboard, open **Providers → + Add direct provider**.
3. Search for the provider, paste the key and click **Test and create**. The key is tested first (see [Test the connection](#test-the-connection)); when the test fails you can still **Save anyway**.
4. Click **Discover** on the new row to load its model list, then **Ping** to check it answers.

### Any provider from the models.dev catalog

[models.dev](https://models.dev) is an open catalog of LLM providers, their models and their prices (MIT licence). Plugboard downloads it with the price updates and turns every provider that speaks the OpenAI chat format into a preset: OpenRouter, OpenCode Zen, Moonshot AI (Kimi), Groq, DeepSeek, Mistral, xAI, Together, Fireworks, DeepInfra, Cerebras and many more. They appear in the selector as **Catalog · unverified (models.dev)**: Plugboard did not test them, so test the key before relying on one.

A provider created from a catalog preset:

- keeps the base URL it was created with; a later catalog change never redirects your key;
- connects to public addresses only, checked at every connection, even if its host name later points inside your network (see [Public addresses only](#public-addresses-only));
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
| Refused: non-public address | A catalog or built-in preset whose host resolves to a private, loopback or link-local address: nothing was sent. These presets are tested as they are used, without a proxy and without following redirects (see [Public addresses only](#public-addresses-only)). |

**Also send one test token** additionally sends a one-token chat completion to the first listed model. It is off by default because the provider bills it.

A failed test names this section as `docs/guide/providers-and-subscriptions.md#test-the-connection`. Set `PLUGBOARD_DOCS_BASE_URL` to where the repository is published (its GitLab `-/blob/main` page, for example) to get it as a link ([configuration](configuration.md#server)).

### Keep model lists up to date

Each provider has two settings, in **Edit** on the Providers page or through `PUT /providers/{id}`:

- **Refresh the model list automatically** (`auto_discover`): every refresh reads `GET /models` and adds new models at once. A model leaves the list after two refreshes in a row without it; a failed or empty answer, or one that would drop more than half of the list, changes nothing.
- **Pinned models** (`pinned_models`): never removed, whatever the provider answers. The default model is never removed either.

**Discover** on a row applies the provider's answer at once, including removals; when it would remove models, the dashboard lists them and asks first. Removals only follow the provider's own model list (`GET /models` or a native catalog API): when Plugboard falls back to a preset's list or asks a model to list itself, it adds what it finds and removes nothing. Providers created before this release keep `auto_discover` off, so their lists only change when you click **Discover** (Anthropic, Gemini, Ollama and the CLI subscriptions are refreshed as before).

### Custom endpoint

Use **+ Add custom provider** for any server that implements `POST /v1/chat/completions` (vLLM, LM Studio, llama.cpp server, another gateway):

| Field | Value |
|---|---|
| Name | A readable name; the provider ID is derived from it |
| Base URL | The server root (`http://localhost:8000`) or its versioned API root (`https://api.example.com/v1`). Plugboard adds `/v1` only to a bare host, then calls `/chat/completions` and `/models`. |
| Bearer token | Optional; sent as `Authorization: Bearer <token>` |
| Default model | Used when a request names this provider without a model |
| Timeout (s) | Request timeout (default 60) |
| Public addresses only | Off by default; see [below](#public-addresses-only). Leave it off for Ollama or another local server. |

Then click **Discover**. Discovery calls `GET <base>/v1/models`; if that fails it falls back to the preset's known models, then to asking the default model for its list.

If `PLUGBOARD_PROVIDER_DOMAINS_ALLOWED` is set, a base URL whose host is not on that list is refused.

### Public addresses only

A protected provider only connects to public addresses, so its key and prompts never reach `127.0.0.1`, `10.x` or `169.254.169.254` because its host name was later pointed there (DNS rebinding).

- **Protected providers**: every catalog preset, every built-in preset with an HTTP(S) URL (the API key presets, the subscriptions through CLI credentials and the GitHub sign-in presets), and any custom endpoint with **Public addresses only** ticked (`egress_guard`). Ollama, the subscriptions through an official CLI and custom endpoints left unticked are not protected.
- **Forced for presets**: on a catalog or built-in preset the box is ticked and greyed out, and `egress_guard: false` is refused with `422`. The Providers page shows **public only** on a protected row; `GET /providers` returns `egress_guard` (protection in effect) and `egress_guard_forced`.
- **Checked at every connection**: each DNS answer (one non-public address refuses the whole name), an IP address written in the URL, and the target of a redirect.
- **A refused connection** sends nothing: the request goes to the next candidate provider, or the client gets `502` when there is none. The provider stays active, the circuit breaker does not count the refusal, and requests work again as soon as the name resolves to public addresses.
- **Reported at most every 5 minutes per provider**: the gateway log names the provider and host; the first refusal of each 5 minutes is also recorded in the audit log as `provider.egress_refused` and sent to `ALERT_WEBHOOK_URL` as `provider_egress_refused` ([alert webhook](monitoring.md#alert-webhook)). The watchdog reports `egress_refused` and never restarts the provider for it.
- **Editing**: a new base URL on a protected provider, or ticking the box, must be a public `http(s)` address, otherwise `POST /providers` or `PUT /providers/{id}` answers `422` (`503` when the name does not resolve) and records `provider.egress.rejected`. A base URL sent back unchanged is not checked again.
- **No proxy**: protected providers ignore `HTTPS_PROXY` and connect directly, so the server needs direct internet access ([outbound network](deployment.md#outbound-network)).

Two cases need a custom endpoint with the box left unticked:

- a preset whose base URL was changed by hand to an internal address before this release is refused from its first request: recreate it as a custom endpoint to the same URL;
- behind a mandatory proxy, protected providers cannot connect: add a custom endpoint to the same URL with the same key, which keeps using the proxy.

### Ollama

Start Ollama on the gateway host. At startup Plugboard checks `OLLAMA_URL` (default `http://localhost:11434`) and registers an `ollama` provider when the daemon answers. `OLLAMA_ENABLED=false` disables this; `OLLAMA_ENABLED=true` logs a warning when Ollama is not reachable.

The **Ollama** page pulls, inspects and deletes models. Ollama is a *sovereign* provider: the watchdog monitors it but never tries to restart it. Ollama cloud models are merged into the provider automatically unless `PLUGBOARD_OLLAMA_CLOUD_AUTOSYNC=0`.

**Ollama switch.** The **Available to the API, MCP and Playground** switch at the top of the Ollama page turns Ollama off or on at once, without a restart or a redeploy (`GET`/`PUT /admin/ollama/switch`, body `{"enabled": false}`). It sets the `ollama` provider's **Active** field, the same one as on the Providers page, so it is saved in `providers.json` and survives restarts. Turned off:

- its models leave `GET /v1/models` and `/v2/models`, so the MCP `list_models` tool and the Playground no longer offer them;
- a chat request for one of them (`ollama:qwen3:8b`, or a name only Ollama lists when every active provider has a model list), from the API, the MCP server or the Playground, gets `503` with `code` `provider_disabled` and nothing is called: no other provider stands in, no cached answer is served ([details](../api/README.md#503-provider_disabled)); `POST /v1/preflight` reports `provider_disabled`;
- `POST /v1/embeddings` answers `503` *Embedding provider unavailable*, and the semantic cache stops embedding through it;
- a request already running on Ollama goes to its end; one waiting for its slot gets the `503` when its turn comes. The switch shows how many are running and waiting;
- nothing in the background calls the daemon: the watchdog, the probes and the startup check skip it. The Ollama page itself still works (status, list, pull, delete), as an admin gesture.

Turned back on, from the switch or the Providers page, the installed model list is synced once and the models come back. Every change made with the switch is in the audit log as `ollama.switch`, with the state before and after and the requests running and waiting; one made on the Providers page is logged as `provider.update`, with the new `is_active` value. `OLLAMA_ENABLED` only decides the registration at startup: `false` does not turn off a provider already registered.

**Local model downloads.** On a small machine, switch **Local model downloads** off on the Ollama page (or set `PLUGBOARD_LOCAL_MODEL_DOWNLOADS=0`): pulls of local models are refused with 403 and only cloud ids (`:cloud`, `-cloud`) of the official library can be added (`user/model:x-cloud` or `hf.co/...` are local models), and a cloud pull whose layers exceed 64 MB is stopped and deleted. When downloads are on, a pull is checked against the free disk space: the size read from the Ollama registry plus `PLUGBOARD_OLLAMA_MIN_FREE_GB` must fit (507 otherwise), and a pull whose size is unknown is stopped, and its partial model deleted, before it crosses the margin. Endpoints: `GET`/`PUT /admin/ollama/settings`.

**Cloud catalogue.** The Ollama page lists the public Ollama Cloud catalogue (`GET /admin/ollama/cloud-catalog`) with an **Add** button that pulls the cloud manifest (a few KB). No inference is run. The Ollama daemon must be signed in to an Ollama account; on the free plan only some cloud models answer, the others return *not included in your free usage*.

### Subscriptions through an official CLI

Read [the risks](#risks-of-using-subscriptions) first. These presets run the vendor's unmodified CLI on the gateway host, as the operating-system user that runs Plugboard.

1. Install the CLI and sign in once, as that user:

   | CLI | Install | Sign in |
   |---|---|---|
   | Claude Code | `npm install -g @anthropic-ai/claude-code` | run `claude` and follow the login prompt |
   | Codex | `npm install -g @openai/codex` | `codex login` |
   | Antigravity CLI | see [antigravity.google](https://antigravity.google/), then `agy install` | run `agy` once and sign in with Google |

2. **Providers → + Add direct provider**, choose *Claude (via claude CLI)*, *ChatGPT (via codex CLI)* or *Antigravity (via agy CLI)*.
3. Click **Check CLI**: Plugboard verifies that the binary is on the `PATH` and runs. Then **Create**.

What to expect:

- Each request starts a CLI process, so the first token takes seconds rather than milliseconds.
- Concurrent processes per CLI are capped by `PLUGBOARD_SUBPROCESS_LIMIT` (default 4).
- The CLI runs in a neutral temporary working directory, so it does not pick up project files or settings; `PLUGBOARD_SUBPROCESS_CWD` overrides this.
- Codex runs with `--sandbox read-only`; `PLUGBOARD_CODEX_SANDBOX` changes the mode.
- Gemini with an **API key** goes through *Google Gemini Direct* (AI Studio key); with a **Google subscription** it goes through *Antigravity (via agy CLI)*. agy ignores `GEMINI_API_KEY`: it only uses its own Google sign-in. In the `full` container image agy is preinstalled; sign in once with `docker exec -it <gateway> agy` (the session is kept in the `/home/app` volume). An unsigned agy returns an explicit "not signed in" error.
- Antigravity runs in print mode with `--sandbox --mode plan`. Headless `agy` cannot ask for a tool permission, so it refuses every tool call unless your agy `settings.json` pre-allows it: keep that file free of allow rules on the gateway host. Its model list is read from `agy models`; the codex list is read from the catalog cache the codex CLI keeps for your account (`~/.codex/models_cache.json`).
- The *Google Gemini (via gemini CLI)* preset is no longer offered (the gemini CLI now refuses personal-account sign-ins); providers created with it keep working while the CLI does.
- Token counts: agy reports them, and Plugboard uses them once validated (non-negative integers, `total_tokens` equal to input + output) for the response `usage`, the final streamed usage chunk and the usage ledger; a reported 0 stays 0. Valid cache-read and thinking counters are exposed as optional `prompt_tokens_details.cached_tokens` and `completion_tokens_details.reasoning_tokens` when they do not exceed their parent counts; they are never added to the total. Nonzero-thinking semantics are not verified for every agy model: inconsistent totals fall back to estimates. When agy's usage is missing, malformed or inconsistent, and for the other CLIs, usage is estimated with a tokenizer as before.
- Cost is the token counts priced at the model's price in the [pricing grid](pricing.md): an estimate, not the vendor's invoice. The remaining subscription quota is not exposed. Usage rows and cached responses recorded before 4.3.2 keep their estimated counts.
- Streaming requests are answered in full and then replayed as server-sent events.
- A daily job checks which models the signed-in account can really use and quarantines the others (see [Monitoring](monitoring.md#model-catalog-and-connectivity)).
- The CLI gets the conversation as plain text: tools, structured output and images are refused, and sampling parameters are ignored (next section).

#### What a CLI subscription route does not do

A CLI subscription route is a provider whose `auth_source` starts with `subprocess:` (the presets above that run a CLI). Plugboard hands it the conversation as text, on the CLI's standard input, so part of the OpenAI request has no way through.

**Refused.** A chat request that asks for any of the following is not sent to a CLI route:

| Field | Refused when |
|---|---|
| `tools` | the list is not empty |
| `tool_choice` | it is anything other than `none` or `auto` |
| `functions`, `function_call` (older OpenAI form) | `functions` is not empty, `function_call` is anything other than `none` or `auto` |
| `response_format` | its `type` is anything other than `text` (`json_object`, `json_schema`) |
| a content part of a message | its `type` is anything other than `text` (`image_url`, `input_audio`, ...) |

When another provider that is not a CLI also serves the model name sent without a provider prefix (an API provider or Ollama), the request reaches that provider with every field intact. A streamed request (`stream: true`) whose first route is a CLI is answered as a replay of that provider's complete answer, tool calls and `finish_reason` included, not as live tokens. When only CLI routes serve the model, or the model names a CLI provider (`<provider-id>:<model>`), the answer is `400` and nothing is called, counted or cached, also for `stream: true` (the `400` is JSON, not a stream):

```json
{
  "detail": "Model route 'claude-via-claude-cli' is a CLI subscription: it cannot serve tools, content:image_url. Nothing was called.",
  "code": "unsupported_on_cli",
  "unsupported": ["tools", "content:image_url"],
  "hint": "Send plain text messages, or pick a model served by an API or Ollama provider from GET /v1/models."
}
```

`unsupported` names each refused field, a content part as `content:<type>` (`content:unknown` for a part without a type). An empty `tools` list, `tool_choice` `none` or `auto` and `response_format` `text` still go to the CLI. The check comes after the key's `403` (`allowed_models`) and the `404` for an unknown model, and before the `402` for a missing price and the key's `rpm_limit` count.

**Prompt too long.** When the CLI says the messages do not fit the model's context, the answer is `400` with `"code": "context_length_exceeded"`, `error_ref`, `provider_name` and `model_id`, also for `stream: true`. No other provider is tried, since the same prompt would not fit there either; shorten the conversation or pick a model with a larger context. A client key gets a sentence naming the provider and a log reference (`ref=<error_ref>` in the gateway log); the admin also gets the CLI's message and error output.

**Ignored.** `temperature`, `top_p`, `max_tokens`, `max_completion_tokens` and `stop` are accepted and not passed to the CLI: the CLI uses its own settings, and the answer can be longer than `max_tokens` or go past a stop sequence.

**Streaming is replayed.** A `stream: true` request is answered as server-sent events, but the CLI's answer is complete before the first chunk is sent: the chunks replay it, and the first token comes as late as the whole answer.

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

## Concurrent requests per provider

Each provider takes a limited number of requests at once:

- A **local** provider takes one request at a time by default (`PLUGBOARD_LOCAL_PROVIDER_MAX_INFLIGHT`, `0` for no cap). A provider is local when it is one of:
  - `provider_type` `sovereign`, whatever its address;
  - any provider whose `base_url` host is `localhost` or a loopback or private IP address written as digits (`127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `::1`, `fc00::/7`), over `http://` or `https://`;
  - an Ollama (its id starts with `ollama`, or its `base_url` uses port `11434`) reached over plain `http://`, whatever its host: `http://ollama:11434` in Docker, but also a Tailscale or CGNAT address (`100.64.0.0/10`) or any other host name.

  Not local: Ollama Cloud (`https://ollama.com`) and any Ollama over `https://` on a host name; any other provider on a CGNAT or Tailscale address or a host name, unless it is `sovereign`; every CLI provider. Set `max_concurrency` to cap one of these, or to lift the cap of a remote Ollama reached over `http://` (`0` for no cap).
- Any other provider has no cap.
- A provider's `max_concurrency` wins over both: a whole number, `0` for no cap. Set it with `PUT /providers/{id}` (`{"max_concurrency": 2}`) or in `providers.json`.
- CLI providers are also capped by `PLUGBOARD_SUBPROCESS_LIMIT` (concurrent processes per CLI).

A full provider passes a non-streamed request for a model without provider prefix to the next candidate; a streamed request has a single route and waits for that provider only, even when another candidate is free. A full provider counts neither against its circuit breaker nor as an error. When every candidate is full, the request waits up to `PLUGBOARD_PROVIDER_WAIT_S` seconds (default 10) for the first freed slot, oldest request first, then gets `503` with `"code": "providers_busy"` and `Retry-After: 5`. Each gateway worker counts its own requests.

## Add providers from `.env` or scripts

- **Existing OpenClaw installations:** the connector and its environment bootstrap have been removed. Its old variables are ignored and recognizable legacy requests are refused without fallback. [Migrate manually to direct providers](openclaw-removal.md); changing an old provider's name is not a migration.
- **Ollama**: `OLLAMA_URL`, `OLLAMA_ENABLED`, `OLLAMA_TIMEOUT`, `PLUGBOARD_OLLAMA_TOKEN` (Ollama behind a proxy that asks for a bearer token).
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

A discovery answers with the `provider`, the models found (`discovered`, `models`), what changed (`added`, `removed`), `removals_allowed` (false when the provider's list is not trusted, so nothing is removed), `dry_run`, and `first_population`: true when the provider had no model yet, in which case `added` and `removed` are empty by design. A `subprocess:` provider also gets `verified` and `dropped` (its model probes). For a public OpenAI-compatible provider, a coding agent can do the same through the MCP tools `add_provider` (created inactive, public addresses only) and `refresh_catalog` ([MCP admin tools](mcp.md#admin-tools-stdio-only)).

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
