# Providers and subscriptions

A **provider** is a backend that answers chat completions: a cloud API, a local Ollama, any server with a standard chat API, or a subscription reached through an official CLI. Each person who runs Plugboard adds their own providers with their own credentials; nothing is shared or preconfigured.

Provider credentials stay on the gateway host. API keys and tokens are encrypted at rest in `data/providers.json` and `data/credentials.db` (see `ENCRYPTION_KEY` in the [configuration reference](configuration.md)).

**Scope of this delivery.** The supported path is a subscription reached through its official CLI: [*Claude (via claude CLI)*, *ChatGPT (via codex CLI)*, *Antigravity (via agy CLI)*](#subscriptions-through-an-official-cli), and *Gemini (via Gemini CLI)* through [CLI credentials](#subscriptions-through-cli-credentials). Paid API-key providers (the API key presets, the models.dev catalog and custom endpoints) are still handled by the code, but are out of scope for this delivery. [Ollama](#ollama) is out of scope too and is not a default provider: keep it turned off.

## Provider kinds

| Kind | Dashboard presets | How requests are served |
|---|---|---|
| API key | *Anthropic Direct*, *Google Gemini Direct*, *GitHub Models*, *OpenAI Direct* | HTTP call to the vendor with your key |
| Catalog (models.dev) | *OpenRouter · unverified*, *Groq · unverified*, *Moonshot AI (Kimi) · unverified*, *OpenCode Zen · unverified* and every other provider with a standard chat API that the catalog lists | HTTP call with your key |
| Custom endpoint | **+ Add custom provider** | HTTP call to any server with a standard chat API |
| Local models | *Ollama* (registered at startup when it answers; out of scope for this delivery, keep it off) | HTTP call to your Ollama daemon |
| Subscription through an official CLI | *Claude (via claude CLI)*, *ChatGPT (via codex CLI)*, *Antigravity (via agy CLI)* | Plugboard runs the CLI as a subprocess for each request |
| Subscription through CLI credentials | *ChatGPT (via Codex CLI)*, *Gemini (via Gemini CLI)*, *GitHub Copilot (via gh CLI)*, *GitHub Models (via gh CLI)* | HTTP call with the token the CLI stored on the host |
| GitHub sign-in from the dashboard | *GitHub Models (OAuth via dashboard)*, *GitHub Copilot (OAuth via dashboard)* | HTTP call with a token obtained through GitHub's device flow |

Presets marked *roadmap* in the dashboard (for example *Claude Pro (roadmap)*) cannot be selected.

## Model IDs

A model is addressed as `<provider-id>:<model>`, for example `chatgpt-via-codex-cli:gpt-5.5` or `antigravity-via-agy-cli:gemini-3.8-flash-medium`. The provider ID is derived from the provider name when you create it (*Claude (via claude CLI)* becomes `claude-via-claude-cli`) and is shown on the Providers page.

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
- Every published model keeps its provider's ID (`antigravity-via-agy-cli:gemini-3.8-flash-medium`), except for safe Claude, Codex and Gemini CLI display aliases described above.
- Models in `PLUGBOARD_MODEL_DENYLIST` are hidden. Quarantined models stay listed with `"connected": false`; a quarantine belongs to the provider that refused the model, never to another provider serving the same name. A model whose last connectivity probe failed less than 36 hours ago is left out of the catalogue a client key sees (the admin key sees it, with `"connected": false`); chat still accepts it. A model never probed, or probed longer ago, stays listed: with no inference probe turned on (`PLUGBOARD_MODEL_REFRESH_PROBE_KINDS` empty, the default and the shipped configurations), no CLI model is probed, so none is left out.

A model hidden by exposure filters can still be called with its unambiguous explicit `<provider-id>:<model>` ID, for example `ollama:llama3.2` for a locally pulled Ollama model. An identity omitted because of a namespace collision has no guaranteed address until the configuration is unambiguous.

## Add a provider from the dashboard

### API key presets

Out of scope for this delivery (paid per use); still available in the dashboard.

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

**Ollama is an optional provider, off by default** in the configurations this repository ships (`deploy/uat`, `deploy/ovh`, `.env.example`: `OLLAMA_ENABLED=false`); its epic is deprioritised (decision of 14/11). Nothing was removed: to use it, set `OLLAMA_ENABLED=true` (or `auto`) and start Ollama on the gateway host. At startup Plugboard then checks `OLLAMA_URL` (default `http://localhost:11434`) and registers an `ollama` provider when the daemon answers; `OLLAMA_ENABLED=true` logs a warning when Ollama is not reachable.

**`OLLAMA_ENABLED=false`** turns Ollama off for the whole process, even when an `ollama` provider is already registered: no `ollama:*` model is listed (API, MCP, Playground), a chat to one gets `503` `provider_disabled` naming the configuration, embeddings through it answer *Embedding provider unavailable* (or, with no `PLUGBOARD_EMBEDDING_MODELS`, *no embedding provider is configured*), no background job calls the daemon or ollama.com, and `/health/ready` stays `200` with `ollama: not_configured`. Nothing is written: the stored switch is kept, and setting the variable back to `true` or `auto` and restarting brings Ollama back.

The **Ollama** page pulls, inspects and deletes models. Ollama is a *sovereign* provider: the watchdog monitors it but never tries to restart it. Ollama cloud models are merged into the provider automatically unless `PLUGBOARD_OLLAMA_CLOUD_AUTOSYNC=0`.

**Ollama switch.** The **Available to the API, MCP and Playground** switch at the top of the Ollama page turns Ollama off or on at once, without a restart or a redeploy (`GET`/`PUT /admin/ollama/switch`, body `{"enabled": false}`). It sets the `ollama` provider's **Active** field, the same one as on the Providers page, so it is saved in `providers.json` and survives restarts. Turned off:

- its models leave `GET /v1/models` and `/v2/models`, so the MCP `list_models` tool and the Playground no longer offer them;
- a chat request for one of them (`ollama:qwen3:8b`, or a name only Ollama lists when every active provider has a model list), from the API, the MCP server or the Playground, gets `503` with `code` `provider_disabled` and nothing is called: no other provider stands in, no cached answer is served ([details](../api/README.md#503-provider_disabled)); `POST /v1/preflight` reports `provider_disabled`;
- `POST /v1/embeddings` answers `503` *Embedding provider unavailable*, and the semantic cache stops embedding through it;
- a request already running on Ollama goes to its end; one waiting for its slot gets the `503` when its turn comes. The switch shows how many are running and waiting;
- nothing in the background calls the daemon: the watchdog, the probes and the startup check skip it. The Ollama page itself still works (status, list, pull, delete), as an admin gesture.

Turned back on, from the switch or the Providers page, the installed model list is synced once and the models come back. Every change made with the switch is in the audit log as `ollama.switch`, with the state before and after and the requests running and waiting; one made on the Providers page is logged as `provider.update`, with the new `is_active` value. `OLLAMA_ENABLED=false` overrides the switch in memory (the switch cannot turn Ollama on while it is set) without changing the stored value.

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

   agy also needs Linux and bubblewrap (`bwrap`, at `/usr/libexec/plugboard/bwrap` or on the `PATH`) on the gateway host: without them every agy call is refused ([agy's cage](#agys-cage)).

2. **Providers → + Add direct provider**, choose *Claude (via claude CLI)*, *ChatGPT (via codex CLI)* or *Antigravity (via agy CLI)*.
3. Click **Verify CLI**: Plugboard verifies that the binary is on the `PATH` and runs (`GET /admin/direct-providers/{preset_key}/check`). Then **Create (use local CLI)**.

What to expect:

- Each request starts a CLI process, so the first token takes seconds rather than milliseconds.
- A CLI provider takes one request at a time by default (`PLUGBOARD_CLI_PROVIDER_MAX_INFLIGHT`, or its `max_concurrency`): the next requests wait for it, and get `503 providers_busy` if it is still busy when their wait budget ends ([concurrent requests per provider](#concurrent-requests-per-provider)). Concurrent processes per CLI are also capped by `PLUGBOARD_SUBPROCESS_LIMIT` (default 4).
- The CLI runs in a neutral temporary working directory, so it does not pick up project files or settings; `PLUGBOARD_SUBPROCESS_CWD` overrides this.
- Codex runs with `--sandbox read-only`; `PLUGBOARD_CODEX_SANDBOX` changes the mode.
- The `full` image ships Claude Code 2.1.325. The *Claude (via claude CLI)* preset lists Claude Fable 5.2 (`claude-fable-5-2`, since 4.14.3) next to Fable 5.1; the official price table bills it 8/40 USD per million tokens. A claude provider created earlier keeps its saved list, and Discover adds the model only when `PLUGBOARD_MODEL_REFRESH_PROBE_KINDS` names `claude` (each model of the preset is then probed by inference): otherwise add `claude-fable-5-2` to the provider's `models` with `PUT /providers/{id}`, keeping the saved ones ([how](../runbooks/production-instance.md#add-claude-fable-52-to-the-claude-provider)).
- A CLI gets the model as a command-line value, so it only gets a name it serves (BUG-120, since 4.14.3): for a route that lists models, its listed models, its default model, the family names of its dated models and the CLI's own aliases (`sonnet`, `opus`, `haiku` and `fable` for claude, `auto` for agy), spelled exactly. Any other name (another Claude Code alias such as `opusplan`, a `[1m]` suffix, another case, a vendor spelling such as `openai:<model>` for codex), or a name that starts with `-` or holds a blank or a control character, is answered `404 model_not_found` before the CLI starts, for every caller ([details](../api/README.md#404-model_not_found)).
- Gemini with an **API key** goes through *Google Gemini Direct* (AI Studio key); with a **Google subscription** it goes through *Antigravity (via agy CLI)*. agy ignores `GEMINI_API_KEY`: it only uses its own Google sign-in. In the `full` container image agy is preinstalled (amd64 only: agy ships no other Linux build); sign in once with `docker exec -it <gateway> agy` (the session is kept in the `/home/app` volume). An unsigned agy returns an explicit "not signed in" error.
- Antigravity runs in print mode with `--sandbox --disable-slash-commands`, inside a bubblewrap cage ([agy's cage](#agys-cage)). `--mode plan` is no longer passed since 4.14.2: agy 1.4.4 says it has no effect then. Headless `agy` cannot ask for a tool permission: it refuses every read, every command and every write, except a write inside its temporary directory (`TMPDIR`), which it does without asking. Plugboard therefore gives each agy request its own private `TMPDIR`, a fresh directory under the directory the CLIs are launched from, removed when the request ends, never the shared `/tmp`. Keep agy's `settings.json` free of allow rules on the gateway host: one such rule applies to every prompt. This bounds the writes a prompt can make through agy's file tool, not what the program itself keeps: agy records every conversation under its own home, `~/.gemini/antigravity-cli`: the prompt in clear (found as written in five of its files on the UAT) and, by all appearances, the answer; about 8 files and 450 KB per call, no limit observed, outside Plugboard's retention. In Docker that directory is in the container's `cli-home` volume (`/home/app`), not on the host account. It also holds agy's sign-in: never delete `~/.gemini/antigravity-cli` itself nor its `antigravity-oauth-token`, or agy is signed out until someone signs in again by hand. After each agy call, answered, failed, timed out or cancelled, streamed or not, Plugboard removes what that call created once the agy process has ended (BUG-97): in `conversations/`, `brain/`, `annotations/` and `presence/` only, the entries named after the call's conversation id (`<id>.db` with its `-wal` and `-shm`, `brain/<id>/`, `<id>.pbtxt`, `<id>.lock`). The id is read from agy's own output and checked before any path is built. A call cut before agy gave it removes what appeared in those four folders during its run: at once when no other agy call of the gateway ran meanwhile, otherwise once no agy call is running, so a call never removes the files of another one still running. The shared summaries (`conversation_summaries.db` with its `-wal` and `-shm`, `jetbox_summaries_proto.pb`) cover every conversation and are removed whole, only when no agy call is running; agy answered normally after they were removed on the UAT and in production. Plugboard never touches the directory itself, the four folders themselves, `antigravity-oauth-token`, `installation_id`, `jetski_state.pbtxt`, `bin/`, `builtin/` nor `cli.log`, and never follows a link out of the directory. What stays: `implicit/`, `log/`, `cache/` and `crashes/`. They do not carry the call's id, so their entries cannot be attributed to one call; two known prompts looked for in the whole directory were found only in `conversations/` and `brain/`, which is no proof for every content; and the logs serve diagnosis. Keep watching the size of the `cli-home` volume. A purge that fails is logged as a warning (error type and number of entries, never the prompt) and the answer is served as usual. Do not run agy by hand with the gateway's home while it serves requests: a conversation started that way during a cut call can be removed with it. None of this is a sandbox: agy's sandbox is its cage ([agy's cage](#agys-cage), BUG-86). Its model list is read from `agy models`; the codex list is read from the catalog cache the codex CLI keeps for your account (`~/.codex/models_cache.json`). That cache also lists models a ChatGPT sign-in refuses: when codex is signed in with ChatGPT (`auth_mode` `chatgpt` in `auth.json`, and no `CODEX_API_KEY` in the gateway's environment), the codex route leaves out `gpt-6.1-sol`, `gpt-6.1-terra`, `gpt-6-sol` and `gpt-6-astra` from discovery, the model refresh and `/v1/models`, saved lists included, and a model name without a provider prefix is not routed to it, not even when it is the route's saved default model or the family name of a dated model it lists (another provider that serves the name answers; `Codex:gpt-6.1-sol` still reaches codex and gets the `400` above). With an API key they stay listed.
- The *Google Gemini (via gemini CLI)* preset is no longer offered (the gemini CLI now refuses personal-account sign-ins); providers created with it keep working while the CLI does.
- Token counts: agy reports them, and Plugboard uses them once validated (non-negative integers, `total_tokens` equal to input + output) for the response `usage`, the final streamed usage chunk and the usage ledger; a reported 0 stays 0. Valid cache-read and thinking counters are exposed as optional `prompt_tokens_details.cached_tokens` and `completion_tokens_details.reasoning_tokens` when they do not exceed their parent counts; they are never added to the total. Nonzero-thinking semantics are not verified for every agy model: inconsistent totals fall back to estimates. When agy's usage is missing, malformed or inconsistent, and for the other CLIs, usage is estimated with a tokenizer as before.
- Cost is the token counts priced at the model's price in the [pricing grid](pricing.md): an estimate, not the vendor's invoice. The remaining subscription quota is not exposed. Usage rows and cached responses recorded before 4.3.2 keep their estimated counts.
- Streaming: `claude` and `agy` send their answer as they write it; `codex` and `gemini` answer in full, and the answer is then replayed as server-sent events.
- A daily job checks which models the signed-in account can really use and quarantines the others (see [Monitoring](monitoring.md#model-catalog-and-connectivity)).
- The CLI gets the conversation as plain text: tools, structured output and images are refused, and sampling parameters are ignored (next section).

#### agy's cage

agy runs a client's prompt as the user that runs Plugboard. Since 4.14.2 (BUG-86), every agy launch by the gateway runs inside a bubblewrap (`bwrap`) cage: a chat, `agy models` and `agy --version` (**Verify CLI**, the provider health check). In the gateway, `plugboard/agy_cage.py` builds the only command line that starts agy, and no setting, option or fallback starts agy without it. Outside the gateway, one operator tool starts agy without it: `scripts/bench_latence.py` (the latency bench, run by hand) starts agy bare, outside the cage and without the purge, with its fixed prompt (BUG-110); do not run it with the gateway's home on a production host.

What agy sees inside the cage:

- its binary, by its real path, **read-only**;
- its store `~/.gemini/antigravity-cli`, **writable**: agy rewrites its sign-in token there (`antigravity-oauth-token`, kept writable) and writes its conversations, their summaries, `cache/`, `log/`, `cli.log`, `implicit/`, `crashes/`, `mcp/` and `updater/` at each call. The store must be a real directory, reached through no symbolic link; it is created (mode `0700`) when missing;
- in the store, **read-only** over it since 4.14.4 (BUG-108), each at its own path when it exists: agy's settings `settings.json` (an allow rule written there would apply to every later prompt), its identity `installation_id` and `jetski_state.pbtxt` (written at its first run, unchanged since on the UAT and on a host), and what it unpacks from its own binary, `bin/` (programs it runs) and `builtin/` (skills it loads). agy cannot change, replace, remove or hard-link them. Each must be a real file or folder reached through no symbolic link, and a file must have no other hard link; otherwise agy is refused (see **Never without the cage** below). A missing one is skipped, never created;
- its own configuration beside the store, **read-only**, each at its own path: the folder `~/.gemini/config` (`.migrated`, `mcp_config.json`, `projects/`) and the file `~/.gemini/projects.json`. agy 1.4.4 cannot start without them: it then tries to create `~/.gemini/config` and fails (`mkdir /home/app/.gemini/config: read-only file system`: every call answered `502` on the UAT, 15/11). `config/mcp_config.json` declares the MCP servers agy uses: agy can read it, never change it. They must be a real folder and a real regular file, reached through no symbolic link. Plugboard does **not create** them: agy writes them itself the first time it is run in a shell of the gateway user, which its sign-in needs anyway; Plugboard does not make up agy's own file formats and never writes in `~/.gemini` outside the store. When one is missing, agy is refused (see **Never without the cage** below);
- the request's private `TMPDIR`, **writable**; it is also agy's working directory;
- read-only: the system libraries (`/usr/lib`, `/usr/lib64`, `/lib`, `/lib64`, `/lib32`), the `/etc` files the loader, name resolution and the user lookup need (`ld.so.cache`, `resolv.conf`, `hosts`, `nsswitch.conf`, `host.conf`, `gai.conf`, `passwd`, `group`, `localtime`), the CA certificates (`/etc/ssl`, `/etc/ca-certificates`, `/etc/pki`, `/usr/share/ca-certificates`, and the paths `SSL_CERT_FILE` and `SSL_CERT_DIR` name, when they are absolute, go through no symbolic link and lie under `/etc/`, `/usr/share/` or `/usr/lib/ssl/`; any other value is ignored, with a warning in the gateway log naming the variable, never its value) and the time zones (`/usr/share/zoneinfo`);
- a minimal `/dev` and, where the host allows it, the `/proc` of its own pid namespace (in a Docker container, none: see **`/proc` in a container** below).

Nothing else. agy does not see the gateway's data or logs (`/app`), `~/.claude`, `~/.codex` or the rest of the home (the rest of `~/.gemini` included: gemini's `settings.json` and `oauth_creds.json`, agy's leftover `projects.json.<uuid>.tmp`), `/usr/bin`, `/usr/local`, `/tmp` or `/sys`. Apart from the store, the `TMPDIR` and the cage's own `/dev` (and `/proc`, when it has one), the root is read-only. agy has its own user, pid, ipc and uts namespaces: it does not see the gateway's processes and cannot read their environment (`/proc/<pid>/environ`). Its own environment is built from the same allowlist as the other CLIs' and reaches it through `execve`, never the command line: no `ADMIN_KEY`, `API_KEY`, `ENCRYPTION_KEY`, nor any `PLUGBOARD_*` variable. The network is shared: agy needs it to reach Google. The cage stops with the gateway (`--die-with-parent`), and a timeout or a cancelled request stops it with agy.

**Never without the cage.** When the cage cannot be built (no `bwrap` at `/usr/libexec/plugboard/bwrap` nor on the `PATH`, a host that is not Linux, a store, `~/.gemini/config` or `~/.gemini/projects.json` path that goes through a symbolic link, a read-only entry of the store above that is a link, of the wrong type or a file with another hard link, or agy never run on the gateway's account so that `~/.gemini/config` or `~/.gemini/projects.json` is missing), or when `bwrap` starts but cannot set it up, agy is not run. A chat then gets `503` with `"code": "provider_sandbox_unavailable"` and no `Retry-After`. A client key gets this `detail`:

```text
The agy provider is unavailable: its sandbox (bubblewrap) could not start, so agy was not run (reference <ref> in the gateway log).
```

The admin key gets the cause instead, for example `bwrap not found on PATH nor at /usr/libexec/plugboard/bwrap`, `agy store path goes through a symbolic link`, `agy sandbox needs Linux`, `agy is not set up for this server account: ~/.gemini/config is missing; ~/.gemini/config and ~/.gemini/projects.json must exist (run agy once in a shell of the gateway user, as for its sign-in)`, or `bwrap`'s first error line. For the one before last, run `agy` once in a shell of the user that runs Plugboard (in Docker: `docker compose -f deploy/ovh/compose.yml exec -it gateway agy`, as for its sign-in), then retry. The router handles it like a timeout: the next provider of the model is tried, if there is one, and the model is never quarantined, neither by a chat, nor by the refresh's probes (`PLUGBOARD_MODEL_REFRESH_PROBE_KINDS`: a cage failure is not counted against the model; a saved model stays, a new one waits for a real probe), nor by the internet check (`"status": "sandbox_unavailable"`). `agy models` returns nothing, so the scheduled refresh keeps the saved models, and **Verify CLI** and the provider health check report the cause.

**Prerequisites.** agy runs only on Linux, with `bwrap` and user and mount namespaces allowed. The gateway takes `/usr/libexec/plugboard/bwrap` when it exists (a path written in its code, never a setting), else the `bwrap` on the `PATH` (a host install). The `full` image installs `bubblewrap` and moves its `bwrap` there, **off the `PATH`**, so as not to change the `bwrap` codex chooses: codex 0.163.0 prefers a `bwrap` found on the `PATH` to the one it ships, and Debian's cannot mount `/proc` in a container. The first 4.14.2 image left it at `/usr/bin/bwrap`, and every codex call failed (`502`, `bwrap: Can't mount proc on /proc: Operation not permitted`, 15/11, INC-4). The image build checks `/usr/libexec/plugboard/bwrap --version` and fails when a `bwrap` is on the `PATH`; so do the CI and `verify-agy-sandbox.py` (`"bwrap_on_path": false`). On a host install, keep in mind that codex then uses the host's `bwrap` from the `PATH`. Docker's default seccomp and AppArmor profiles refuse the namespaces `bwrap` needs: start the container with the profiles that codex's sandbox already needs (BUG-43: `plugboard-codex` and `codex-seccomp.json` in [`deploy/ovh/`](../../deploy/ovh/README.md#codex-linux-sandbox), `plugboard-userns` in [`deploy/uat/`](../../deploy/uat/README.md)). On macOS, and on any host without `bwrap`, agy is refused: every agy call answers `503 provider_sandbox_unavailable`. To check the cage in a running container without a model request, run [`deploy/ovh/verify-agy-sandbox.py`](../../deploy/ovh/README.md#agy-cage).

**`/proc` in a container.** Docker masks some paths of a container's `/proc` (`/proc/kcore`, `/proc/keys`...) by mounting over them, and the kernel then refuses a new `/proc` inside a user namespace (`bwrap: Can't mount proc on /newroot/proc: Operation not permitted`); the compose files of `deploy/` do not change that (no `privileged`, no `systempaths=unconfined`). Like codex's own sandbox, the gateway checks once, at its first agy launch: it runs `bwrap` with the cage's namespaces and system files and a new `/proc`, with `bwrap --version` as the command (agy is never run for this). When the new `/proc` is refused, every cage of that process is built without any `/proc`, and the gateway logs one warning: `agy's sandbox runs without /proc: this container forbids a new procfs`. Any other failure of that check keeps `/proc` (the call then fails with the `503` above) and is checked again at the next call. This is safe: without `/proc`, agy has no `/proc/<pid>/environ` to read at all, so the gateway's environment stays out of reach; the cage keeps its own pid namespace, so what agy leaves running still ends with the call; and the host's `/proc` is never bound into the cage (it would show the gateway's environment, same user). `verify-agy-sandbox.py` reports which case applies: `"proc": "mounted (own pid namespace)"` (a host, or a container that allows a new `/proc`) or `"proc": "not mounted (the container forbids a new procfs)"` (a standard Docker container); both pass. How the real agy behaves without `/proc` is not verified yet.

**agy is the cage's pid 1.** The cage runs agy as pid 1 of its own pid namespace (`--as-pid-1`), and so does the `/proc` check. Without it, `bwrap` starts an init of its own there, and the outer `bwrap` exits without reaping it: on the UAT (15/11), where the gateway is PID 1 of its container with no init, each agy call and each `agy --version` probe of the watchdog left one zombie `bwrap` to the gateway (68 of them), until the container's process limit would forbid any fork. When agy exits, the kernel still kills whatever is left in its pid namespace. A gateway with no init in its container remains a risk for its other subprocesses (codex's own inner `bwrap`, for one) and for an agy call cut by a timeout or a cancellation, which still leaves one zombie: tracked by BUG-113 (an init in the container).

**Known limits.** The cage does not separate agy's calls from each other, nor agy from the container's network:

- agy still sees and can write the rest of its store, not only the files of its own call: the conversations of the other agy calls running at the same time (their prompts, until the purge after each call) and its sign-in token, which it must refresh. Its settings are read-only since 4.14.4 (above), but only those that exist when the call starts: where the store holds no `settings.json` (the usual case), a call can create one, read-only for the calls after it, which then apply its rules. Hiding the other calls' conversations (a private `conversations/`, `brain/`... per call) needs a real agy run to prove that agy still answers, and is not done. BUG-108.
- the network is the container's: agy reaches its loopback (`127.0.0.1`, the gateway's own port included) and the abstract unix sockets of the container's network namespace, as well as the internet. Tracked as BUG-109.

**Self-update.** agy updates itself by replacing its own executable, from a program it leaves running in the background. Three layers stop it:

1. Every agy launch gets `AGY_CLI_DISABLE_AUTO_UPDATE=1`. This variable name is in the agy 1.4.4 binary; its effect is known from static analysis only, not from a real run.
2. The binary is read-only and its folder is on the read-only root: renaming it to `.old` or writing a new binary next to it fails. Tested with a fake agy: the binary outside the cage stays intact.
3. When agy exits, the cage's pid namespace ends and every process left in it is killed, the background program included. No process stays after the call.

The cage lets agy write its update state in its store (`updater/`, `last_check.timestamp`). What agy does when its update fails (go on or stop) is not verified yet. To change agy's version, change the `AGY_URL` and `AGY_SHA512` lines of the `Dockerfile` and build a new image ([deployment](deployment.md)).

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

When another provider that is not a CLI also serves the model name sent without a provider prefix (an API provider or Ollama), the request reaches that provider with every field intact. A streamed request (`stream: true`) whose first route is a CLI is answered, for a claude or agy CLI, live (the text as the CLI writes it, then a last chunk with `finish_reason` and `usage`), and for a codex CLI as a replay of its complete answer, tool calls and `finish_reason` included, not as live tokens. When only CLI routes serve the model, or the model names a CLI provider (`<provider-id>:<model>`), the answer is `400` and nothing is called, counted or cached, also for `stream: true` (the `400` is JSON, not a stream):

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

**Model the subscription does not serve.** codex signed in with a ChatGPT account refuses some models it lists in its own catalog cache ("The 'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT account"). The answer is `400` with `"code": "model_not_supported_by_subscription"`, a `hint`, `error_ref`, `provider_name` and `model_id`, also for `stream: true`. No other provider is tried, and the model is neither quarantined nor counted against the route's circuit: it stays valid on a route with an API key. Pick another model from `GET /v1/models`.

**Ignored.** `temperature`, `top_p`, `max_tokens`, `max_completion_tokens` and `stop` are accepted and not passed to the CLI: the CLI uses its own settings, and the answer can be longer than `max_tokens` or go past a stop sequence.

**Streaming.** With `claude` and `agy`, a `stream: true` request gets the text as the CLI writes it. Nothing is sent before the first word, so a failure before it still goes to the next provider; a failure after it ends the stream with the `stream_interrupted` event, and what was sent is billed. This path does not fill the response cache. With `codex` and `gemini`, the CLI's answer is complete before the first chunk is sent: the chunks replay it, and the first token comes as late as the whole answer.

### Subscriptions through CLI credentials

Read [the risks](#risks-of-using-subscriptions) first. These presets do not run the CLI; they read the token it stored and call the vendor's HTTP endpoint with it:

| Preset | Credential source |
|---|---|
| *ChatGPT (via Codex CLI)* | `~/.codex/auth.json` written by `codex login` |
| *Gemini (via Gemini CLI)* | `~/.gemini/oauth_creds.json` written by the Gemini CLI |
| *GitHub Models (via gh CLI)* | `gh auth token` |
| *GitHub Copilot (via gh CLI)* | `gh auth token`, exchanged for a Copilot token (requires a Copilot subscription) |

Refreshing an expired Gemini CLI token, and signing in to Gemini from the dashboard, need a Google OAuth desktop client in `GEMINI_OAUTH_CLIENT_ID` / `GEMINI_OAUTH_CLIENT_SECRET`; Plugboard does not ship one. Without it, run `gemini` once to refresh the token. Some vendor endpoints reject tokens obtained this way (see [ADR-002](../adr/ADR-002-oauth-broker-abandoned-for-google.md)). For Claude and ChatGPT subscriptions, prefer the subprocess presets above; for a Google subscription, *Antigravity (via agy CLI)*.

### GitHub sign-in from the dashboard

The presets *GitHub Models (OAuth via dashboard)* and *GitHub Copilot (OAuth via dashboard)* use GitHub's device flow: the dashboard shows a code, you open the GitHub page, enter the code and approve. The token is stored encrypted in `data/credentials.db`. When you pick the same preset again, the provider form shows the connected account with **Reconnect** and **Disconnect**; `GET /admin/oauth/connections` lists every connected account.

## Concurrent requests per provider

Each provider takes a limited number of requests at once:

- A **local** provider takes one request at a time by default (`PLUGBOARD_LOCAL_PROVIDER_MAX_INFLIGHT`, `0` for no cap). A provider is local when it is one of:
  - `provider_type` `sovereign`, whatever its address;
  - any provider whose `base_url` host is `localhost` or a loopback or private IP address written as digits (`127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `::1`, `fc00::/7`), over `http://` or `https://`;
  - an Ollama (its id starts with `ollama`, or its `base_url` uses port `11434`) reached over plain `http://`, whatever its host: `http://ollama:11434` in Docker, but also a Tailscale or CGNAT address (`100.64.0.0/10`) or any other host name.

  Not local: Ollama Cloud (`https://ollama.com`) and any Ollama over `https://` on a host name; any other provider on a CGNAT or Tailscale address or a host name, unless it is `sovereign`; every CLI provider (capped by the next rule). Set `max_concurrency` to cap one of these, or to lift the cap of a remote Ollama reached over `http://` (`0` for no cap).
- A **CLI** provider (`auth_source` `subprocess:`, such as Claude Code, Codex, Gemini CLI or agy) takes one request at a time by default (`PLUGBOARD_CLI_PROVIDER_MAX_INFLIGHT`, `0` for no cap). Its next requests wait in its queue, within the request's wait budget, instead of waiting without limit for a CLI process.
- Any other provider has no cap.
- A provider's `max_concurrency` wins over these defaults: a whole number, `0` for no cap. Set it with `PUT /providers/{id}` (`{"max_concurrency": 2}`) or in `providers.json`.
- CLI providers are also capped by `PLUGBOARD_SUBPROCESS_LIMIT` (concurrent processes per CLI, shared by the providers of one CLI and the internal model checks). A request that reaches this limit waits for a process without the budget's bound: keep it at or above the sum of the caps of the providers that use a CLI.

A full provider passes a request for a model without provider prefix, streamed or not, to the next candidate; a streamed request whose provider fails before its first chunk goes to the next candidate too. A full provider counts neither against its circuit breaker nor as an error. When every candidate is full, the request waits up to `PLUGBOARD_PROVIDER_WAIT_S` seconds (default 10) for the first freed slot, oldest request first, then gets `503` with `"code": "providers_busy"` and `Retry-After: 5`. A chat request that already waited for a global slot or for its session's turn only gets what is left of its wait budget ([one wait per request](clients-and-keys.md#how-limits-are-enforced)). Each gateway worker counts its own requests.

## Add providers from `.env` or scripts

- **Existing OpenClaw installations:** the connector and its environment bootstrap have been removed. Its old variables are ignored and recognizable legacy requests are refused without fallback. [Migrate manually to direct providers](openclaw-removal.md); changing an old provider's name is not a migration.
- **Ollama**: `OLLAMA_URL`, `OLLAMA_ENABLED`, `OLLAMA_TIMEOUT`, `PLUGBOARD_OLLAMA_TOKEN` (Ollama behind a proxy that asks for a bearer token).
- **API key presets on a remote instance** (out of scope for this delivery): `scripts/setup_railway_providers.py` reads `SETUP_ANTHROPIC_API_KEY`, `SETUP_OPENAI_API_KEY`, `SETUP_GEMINI_API_KEY` and `SETUP_GITHUB_PAT` and creates the matching presets through the admin API (`PLUGBOARD_URL` and `PLUGBOARD_ADMIN_KEY` point it at the instance). Unset variables are skipped, and presets that already exist are left untouched.
- **Admin API**: everything the dashboard does is available over HTTP with `Authorization: Bearer $ADMIN_KEY`:

```bash
# list presets, then check a subscription CLI and create its provider (no key: the CLI's sign-in is used)
curl -s http://localhost:8765/admin/direct-providers/presets -H "Authorization: Bearer $ADMIN_KEY"
curl -s http://localhost:8765/admin/direct-providers/claude-cli-subprocess/check -H "Authorization: Bearer $ADMIN_KEY"
curl -s -X POST http://localhost:8765/admin/direct-providers/claude-cli-subprocess \
  -H "Authorization: Bearer $ADMIN_KEY" -H "Content-Type: application/json" -d '{}'

# API key presets (out of scope for this delivery): create one from a preset
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

Plugboard ships these modes because some people run it as a personal tool on their own machine. **You decide whether to use them and you bear the consequences**, up to the loss of the account. Never expose a subscription-backed provider to other people. **API keys are the safe option**: they are designed for programmatic access and are billed per use (paid API-key providers are out of scope for this delivery).
