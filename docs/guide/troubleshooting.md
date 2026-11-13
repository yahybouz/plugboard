# Troubleshooting

Start with the server log: it is JSON on standard output, and every response carries an `X-Request-ID` that you can search for. Longer procedures are in the [runbooks](../runbooks/README.md).

## The server refuses to start

### `API_KEY holds comment text from .env.example` (or `ADMIN_KEY`)

python-dotenv reads `KEY=   # some note` as the value `# some note`. A key copied unfilled from an older `.env.example` would then hold public text that anyone could use to authenticate, so the server stops. Put a random value in `.env` and restart:

```bash
openssl rand -hex 24
```

A real key may start with `#`; only `#` alone or `#` followed by whitespace is rejected.

If the log says the same about `ENCRYPTION_KEY`, the server starts but logs a critical message: your stored credentials are encrypted with a guessable key. Do not change the value blindly (stored tokens and the MFA secret would become unreadable): protect `data/`, then re-create the providers with a new `ENCRYPTION_KEY`.

### `API_KEY and ADMIN_KEY are both empty: refusing to listen on 0.0.0.0`

With both keys empty (possible only with `PLUGBOARD_DEV=1`), every route is open, the admin API and the dashboard included. `run.py` (and so `make dev`, the Docker image and the systemd unit) then listens on this machine only, and refuses `HOST=0.0.0.0` (the default when `HOST` is unset), `::`, a LAN address or an empty value. Either:

- run `make init`, or set random values for `ADMIN_KEY` and `API_KEY` in `.env` (`openssl rand -hex 24`); a container needs `ADMIN_KEY`, since it listens on `0.0.0.0`;
- or, for throwaway local work, set `HOST=127.0.0.1` (`localhost` and `::1` also work).

Only `API_KEY` and `ADMIN_KEY` count: `PLUGBOARD_API_KEY` and `PLUGBOARD_ADMIN_KEY` are MCP client settings that the server does not read.

Started without `run.py` (`uvicorn plugboard.app:app --host 0.0.0.0`), the server starts, but answers `403` (`API_KEY and ADMIN_KEY are both empty: every route is open, so this server only answers on a loopback interface…`) to any request that does not arrive on a loopback interface, a Unix socket included; the log records the first refusal. Proxy headers and the `Host` header do not change it. The same two fixes apply: set the keys, or listen on `127.0.0.1` (the `uvicorn` default).

### `Invalid configuration: STRICT_TENANT explicitly enabled while API_KEY is empty`

`PLUGBOARD_STRICT_TENANT=1` (or its old name) with an empty `API_KEY` is refused. Set `API_KEY`, or remove the variable.

### The port is already in use

Another process listens on `8765`. Stop it or set `PORT` in `.env`.

## Dashboard auto-login does not happen

On a local install the login page should sign you in without a key. When it does not, the server logs one line per attempt:

```
auth.auto_login refused reason=<reason> peer=<address>
```

| Reason | Fix |
|---|---|
| `not_local_install` | Local install: set `HOST=127.0.0.1` in `.env` and start with `python run.py` or `make dev` (not with `uvicorn` directly), or set `PLUGBOARD_LOCAL_AUTO_LOGIN=1` on a personal workstation. A server exposed to a network keeps `HOST=0.0.0.0` and uses `ADMIN_KEY`. |
| `no_admin_key` | `ADMIN_KEY` is empty in `.env`: set it and restart. |
| `disabled` | `PLUGBOARD_LOCAL_AUTO_LOGIN` is `0` (or an unrecognised value). |
| `proxied` | A proxy header (`X-Forwarded-For`, `Forwarded`, `Via`, ...) is present: access through a proxy, a tunnel or Docker port forwarding. Enter `ADMIN_KEY`. |
| `non_loopback_peer` | The connection does not come from this machine (another host, a container network). Enter `ADMIN_KEY`. |
| `non_loopback_host` | Open `http://localhost:8765`, not `http://0.0.0.0:8765`, a LAN address or a domain name. |
| `not_navigation` | Open the URL directly in the browser's address bar, not through curl, a script or a page served on another port. Safari: use `localhost`, not `127.0.0.1`. |
| `logged_out` | Normal right after clicking **Log out**: open `http://localhost:8765` again. |
| `mfa_required` | Admin MFA is enforced: sign in with `ADMIN_KEY` and the TOTP code. |
| `no_local_secret` | This browser has neither the login token nor a remembered-browser cookie: start with `make dev`, or open `http://localhost:8765/login?token=` followed by the content of `data/.login_token`. |
| `bad_token` | The token is wrong or was already used (it is single-use and replaced after each sign-in): read the current one from `data/.login_token`, or restart with `make dev`. |

`proxied`, `non_loopback_peer` and `bad_token` refusals coming with a loopback `Host` header are also written to the audit log (`auth.auto_login_refused`), at most once per address and reason every ten minutes.

On a deployed instance auto-login is off by design: read `ADMIN_KEY` from the server's `.env` or secret store and type it in the login form.

### Every admin login fails with `MFA code required` and no code works

With `PLUGBOARD_ADMIN_MFA_REQUIRED=1`, an `admin_mfa.json` that exists but cannot be read (corrupted, not JSON, unreadable permissions) refuses admin access instead of turning MFA off; the log says `Failed to read .../admin_mfa.json`. Restore the file from a backup, or remove it (MFA is then not enrolled) and enrol again.

## Dashboard login problems

- **`Admin key required`**: the key does not match `ADMIN_KEY`. Check the value the server actually loaded (restart after editing `.env`).
- **`Too many failed attempts`**: after 5 failures within a minute, the client IP is locked out for 15 minutes.
- **Signed out after a while**: sessions last `ADMIN_SESSION_MAX_AGE` (8 hours). Changing `ADMIN_KEY` invalidates every session.
- **Cookie not kept over plain HTTP on a server**: with `COOKIE_SECURE=auto` the cookie is `Secure` for any host other than localhost, so the browser drops it over `http://`. Serve the dashboard over HTTPS (see [Deployment](deployment.md#behind-a-reverse-proxy)).
- **`403 Cross-site request refused`**: a change was sent with the dashboard session from a page other than the dashboard (another port or host name). If the dashboard itself gets it, it is served over plain HTTP through a proxy that rewrites the `Host` header: keep `Host` at the proxy, or serve over HTTPS. Scripts send the admin key in `Authorization` instead of the cookie ([details](monitoring.md#changes-made-with-a-dashboard-session)).

## Client errors

| Symptom | Cause and fix |
|---|---|
| `401 Invalid API key` | Unknown, revoked or mistyped key. Check it under **API keys**; create a new one if it was lost (keys cannot be recovered). |
| `401 Authentication required: missing client_id` | A chat request without a key. Send `Authorization: Bearer pb_...`. |
| `403 Model '...' not allowed` | The model is outside the key's `allowed_models`. Entries are exact IDs: the bare model name, `<provider-id>:<model>` or the full ID `/v1/models` lists (`Codex:gpt-6.1-sol`). Since 4.3.1 a provider prefix or part of a name no longer allows the models it used to cover, and a display prefix alone (`Codex:`) allows nothing either. The message names the ID that was checked as `<provider-id>:<model>` (`chatgpt-via-codex-cli:gpt-6.1-sol` for the Codex CLI): if it is the provider and model you mean to allow, create a new key that lists it in one of these spellings; otherwise have the client send `<provider-id>:<model>` or the ID `/v1/models` lists, since a name without a provider prefix can be routed to another provider ([allowed models](clients-and-keys.md#allowed-models)). |
| `404` with `"code": "model_not_found"` | No provider knows the model: its prefix is no provider ID and no provider lists it. Check the spelling against `GET /v1/models`, or send `<provider-id>:<model>`. A model pulled with the `ollama` command rather than the dashboard's Ollama page is listed after the next refresh, or at once with **Discover** on the provider ([details](../api/README.md#404-model_not_found)). |
| `429 Rate limit exceeded` | The per-IP limit (`RATE_LIMIT`, 60 per minute). Behind a reverse proxy whose address is not in `FORWARDED_ALLOW_IPS`, all clients share the proxy's address: list it ([reverse proxy](deployment.md#behind-a-reverse-proxy)). |
| `429 Client rate limit exceeded` | The key's RPM limit. |
| `429 Monthly spending limit exceeded` | The key's monthly budget. Raise it or wait for the next month (UTC). |
| `429 The quota or rate limit of the ... provider is exhausted` | The last provider tried answered HTTP 429, or its CLI reported an exhausted quota. A non-streamed request is offered to the other candidate providers first; a streamed request to an HTTP provider is not. `Retry-After` carries the provider's delay, or the time until the reopening hour the CLI printed (gateway local time), capped at 7 days. The admin key and the Playground see the provider's own message. |
| `502` with `HTTP 404` from the provider | The model name does not exist on that provider, or the base URL of a custom provider contains `/v1` (Plugboard appends `/v1/chat/completions` itself). Only the admin key and the Playground see the provider's message; a client key gets an `error_ref`: search the gateway log for `ref=<error_ref>`. |
| `400` with `"code": "unsupported_on_cli"` | Only CLI subscription routes serve the model, and the request asks for what a CLI cannot pass: tools, a forced `tool_choice`, `functions`, a `response_format` other than text, or an image or other non-text content part (`unsupported` lists them). Nothing was called. Send plain text, or pick a model an API or Ollama provider serves; a model name without a provider prefix that such a provider also lists goes to it ([what a CLI route does not do](providers-and-subscriptions.md#what-a-cli-subscription-route-does-not-do)). |
| `400` with `"code": "model_not_supported_by_subscription"` | The account plan a CLI subscription route is signed in with does not serve the model (codex with a ChatGPT account). No other provider is tried: pick another model from `GET /v1/models`, or use a route with an API key. A client key gets an `error_ref`: search the gateway log for `ref=<error_ref>`. |
| `400` with `"code": "context_length_exceeded"` | A CLI subscription route said the messages do not fit the model's context. No other provider is tried: shorten the conversation, start a new session, or pick a model with a larger context. A client key gets an `error_ref`: search the gateway log for `ref=<error_ref>`. |
| `502` from a CLI provider | The CLI failed. The response includes its exit code and error output; run the CLI by hand as the same user to see the prompt it is waiting for (login expired, terms to accept). |
| `503` with `Retry-After` | The concurrency cap (`PLUGBOARD_MAX_INFLIGHT_CHAT`) stayed full for the whole wait (`"code": "server_busy"`), every provider for the model stayed at its cap for `PLUGBOARD_PROVIDER_WAIT_S`, or already had `PLUGBOARD_PROVIDER_MAX_WAITING` requests waiting for it (`"code": "providers_busy"`, see [concurrent requests per provider](providers-and-subscriptions.md#concurrent-requests-per-provider)), the previous turn of the request's session was still being answered at the end of its wait, or `PLUGBOARD_SESSION_MAX_WAITING` turns already waited (`"code": "session_busy"`, *The previous turn of this session is still being answered*), or a CLI provider timed out. A request waits 10 s at most in all by default, a full per-CLI process limit (`PLUGBOARD_SUBPROCESS_LIMIT`) excepted ([one wait per request](clients-and-keys.md#how-limits-are-enforced)). |
| Empty `content` with status 200 | The model returned no text. Check the provider directly; Plugboard passes the answer through unchanged. |

## A model is missing from `/v1/models`

`/v1/models` lists the newest versions of each family (`PLUGBOARD_MODEL_EXPOSE`, `PLUGBOARD_MODEL_EXPOSE_VERSIONS`), at most `PLUGBOARD_TOP_N_PER_PROVIDER` models per provider when that cap is set, and with `PLUGBOARD_MODEL_FILTER=curated` it also hides older OpenAI and Gemini generations, older Claude versions and local Ollama models. The model can still be called with its explicit `<provider-id>:<model>` ID. See [How models are listed](providers-and-subscriptions.md#how-models-are-listed). Also check `PLUGBOARD_MODEL_DENYLIST` and, after adding a provider, click **Discover**.

## Costs show 0

- `GET /admin/coverage/pricing` lists models without a price. **Pricing → Missing price only** shows them; set a price there, or run `POST /admin/pricing/scrape`.
- A new price may be waiting in **Pricing → Needs your review**: changes above `PLUGBOARD_PRICE_DRIFT_PCT` and first prices on which two sources disagree are not billed until applied.
- `GET /admin/pricing/scheduler/status` shows whether the scheduler runs and whether its circuit breaker is open. See the [pricing scheduler runbook](../runbooks/pricing-scheduler.md).
- Cache hits are free by design (`X-Cache: HIT`).

## The catalog is stale or unavailable

**Monitoring → Automatic updates** (or `GET /admin/auto-update/status`) shows the catalog state:

- `unavailable`: no copy was ever downloaded. Check outbound access to `https://models.dev/api.json`, or set `PLUGBOARD_CATALOG_URL` to a mirror. Built-in presets and custom endpoints keep working.
- `stale`: the last download failed (`last_error` says why); the last good copy is used. A download that would lose more than half of the providers or models is refused on purpose.
- `off`: `PLUGBOARD_CATALOG=off` or an empty `PLUGBOARD_CATALOG_URL`.

Providers already created from the catalog are not affected: they keep their base URL and refresh their models from the provider itself.

## Model lists do not change

- Check `auto_discover` on the provider (**Edit** on the Providers page). Providers created before this release have it off.
- A model missing from the provider's answer is removed only after two refreshes; **Monitoring → Automatic updates** and `last_changes` in `GET /admin/models/scheduler/status` show pending removals and errors (a refresh that would drop more than half of a list is not applied).
- CLI providers are refreshed at most every `PLUGBOARD_MODEL_AVAILABILITY_CLI_INTERVAL_H` hours (24). **Refresh now** runs everything at once.

## A provider is down

Check **Monitoring**, then:

```bash
curl -s http://localhost:8765/providers/<provider-id>/health -H "Authorization: Bearer $ADMIN_KEY"
curl -s http://localhost:8765/admin/circuits -H "Authorization: Bearer $ADMIN_KEY"
```

A model whose circuit is open (3 consecutive provider failures: `429`, `5xx`, timeouts, network errors, `401`/`403`, CLI quota or failed run; never a `400`, `413`, `422` or a prompt too long) is retried after 60 seconds; `POST /admin/circuits/{model}/reset` closes it immediately. Full procedure: [provider-down runbook](../runbooks/provider-down.md).

## Ollama is not detected

Ollama is registered at startup only if `OLLAMA_URL` answers with "Ollama is running". Start Ollama, then restart Plugboard. In Docker, `localhost` is the container itself: set `OLLAMA_URL` to an address the container can reach.

## Start from a clean state

Stop the server and back up `data/` first. Removing `data/providers.json` and `data/api_keys.json` deletes all providers and client keys (keys cannot be recovered); keep `data/pricing/` to preserve your pricing history. Then start the server and re-create providers and keys from the dashboard.
