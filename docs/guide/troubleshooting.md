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
| `logged_out` | Normal right after clicking **Logout**: open `http://localhost:8765` again. |
| `mfa_required` | Admin MFA is enforced: sign in with `ADMIN_KEY` and the TOTP code. |

`proxied` and `non_loopback_peer` refusals coming with a loopback `Host` header are also written to the audit log (`auth.auto_login_refused`), at most once per address and reason every ten minutes.

On a deployed instance auto-login is off by design: read `ADMIN_KEY` from the server's `.env` or secret store and type it in the login form.

### Every admin login fails with `MFA code required` and no code works

With `PLUGBOARD_ADMIN_MFA_REQUIRED=1`, an `admin_mfa.json` that exists but cannot be read (corrupted, not JSON, unreadable permissions) refuses admin access instead of turning MFA off; the log says `Failed to read .../admin_mfa.json`. Restore the file from a backup, or remove it (MFA is then not enrolled) and enrol again.

## Dashboard login problems

- **`Admin key required`**: the key does not match `ADMIN_KEY`. Check the value the server actually loaded (restart after editing `.env`).
- **`Too many failed attempts`**: after 5 failures within a minute, the client IP is locked out for 15 minutes.
- **Signed out after a while**: sessions last `ADMIN_SESSION_MAX_AGE` (8 hours). Changing `ADMIN_KEY` invalidates every session.
- **Cookie not kept over plain HTTP on a server**: with `COOKIE_SECURE=auto` the cookie is `Secure` for any host other than localhost, so the browser drops it over `http://`. Serve the dashboard over HTTPS (see [Deployment](deployment.md#behind-a-reverse-proxy)).

## Client errors

| Symptom | Cause and fix |
|---|---|
| `401 Invalid API key` | Unknown, revoked or mistyped key. Check it under **API Keys**; create a new one if it was lost (keys cannot be recovered). |
| `401 Authentication required: missing client_id` | A chat request without a key. Send `Authorization: Bearer pb_...`. |
| `403 Model '...' not allowed` | The model is outside the key's `allowed_models`. |
| `429 Rate limit exceeded` | The per-IP limit (`RATE_LIMIT`, 60 per minute). Behind a reverse proxy all clients share the proxy's address: raise `RATE_LIMIT` or set it to `0` and limit traffic at the proxy. |
| `429 Client rate limit exceeded` | The key's RPM limit. |
| `429 Monthly spending limit exceeded` | The key's monthly budget. Raise it or wait for the next month (UTC). |
| `502` with `HTTP 404` from the provider | The model name does not exist on that provider, or the base URL of a custom provider contains `/v1` (Plugboard appends `/v1/chat/completions` itself). |
| `502` from a CLI provider | The CLI failed. The response includes its exit code and error output; run the CLI by hand as the same user to see the prompt it is waiting for (login expired, terms to accept). |
| `503` with `Retry-After` | The concurrency cap (`PLUGBOARD_MAX_INFLIGHT_CHAT`) is reached, or a CLI provider timed out. |
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

A model whose circuit is open (3 consecutive failures) is retried after 60 seconds; `POST /admin/circuits/{model}/reset` closes it immediately. Full procedure: [provider-down runbook](../runbooks/provider-down.md).

## Ollama is not detected

Ollama is registered at startup only if `OLLAMA_URL` answers with "Ollama is running". Start Ollama, then restart Plugboard. In Docker, `localhost` is the container itself: set `OLLAMA_URL` to an address the container can reach.

## Start from a clean state

Stop the server and back up `data/` first. Removing `data/providers.json` and `data/api_keys.json` deletes all providers and client keys (keys cannot be recovered); keep `data/pricing/` to preserve your pricing history. Then start the server and re-create providers and keys from the dashboard.
