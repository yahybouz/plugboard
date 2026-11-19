# Web access for the models

A model served by Plugboard has no tool by default: no web, no file, no shell,
no MCP. Web access (search and page fetch) can be granted **per key**; a
request of that key then asks for it explicitly. Everything else is unchanged:
a request that does not ask is served exactly as before, with no tool and no
extra cost.

## Turn it on for a key

Web access is off for every key, old and new. The administrator turns it on
for one key, without recreating or rotating it:

```bash
curl -s -X PUT http://localhost:8765/admin/keys/<key_id>/web \
  -H "Authorization: Bearer <admin key>" -H "X-Plugboard-Admin-TOTP: <code>" \
  -H "Content-Type: application/json" \
  -d '{"web_access": true, "web_max_uses": 3}'
```

- `web_access`: a JSON boolean; only `true` grants it.
- `web_max_uses`: the web tool calls (a search or a page fetch each) one
  request may make, from 1 to 10. Optional: unchanged when absent, `3` for a
  key that never had one.
- `{"web_access": false}` turns it off again at once.

The dashboard shows a **Web** column on the **API keys** page: `off`, or
`on · <web_max_uses>`; a click switches it, after a confirmation. A key can
also be created with web access (`POST /admin/keys` with `web_access` and
`web_max_uses`). A rotated key keeps its web settings. The admin key itself
never has web access.

`GET /v1/me` tells the key's holder whether it has the right
(`web_access`, `web_max_uses`).

## Ask for the web in a request

Either the header or the body field, on `POST /v1/chat/completions` (and v2):

```bash
curl -s http://localhost:8765/v1/chat/completions \
  -H "Authorization: Bearer pb_..." -H "X-Plugboard-Web: 1" \
  -H "Content-Type: application/json" \
  -d '{"model": "claude-code:claude-sonnet-5-5",
       "messages": [{"role": "user", "content": "Fuel prices in Morocco today? Cite your sources."}]}'
```

```json
{"model": "claude-code:claude-opus-5-5", "messages": [...], "plugboard": {"web": true}}
```

`plugboard` must be an object whose only field is `web`, a JSON boolean; the
header reads `1` or `0`. The field is removed before the provider is called.
Streaming (`"stream": true`) works the same way.

| Answer | Code | When | Something called? |
|---|---|---|---|
| `400` | `web_bad_request` | the header or the `plugboard` field is malformed | no |
| `403` | `web_not_allowed` | the key does not hold `web_access` | no |
| `400` | `web_unavailable` | no claude CLI route serves the model | no |
| `429` | `web_busy` | the web requests in progress are at their limit; `Retry-After: 10` | no |
| `502` | `web_budget_exhausted` | the model used its `web_max_uses` calls without a final answer | yes |

A web answer is never read from nor written to the response cache: it depends
on the live web. Its usage entry is marked (`web`, `web_uses`), and the month's
summaries (`/v1/me`, `/usage/me`) count `web_requests` and `web_uses`.

## Providers that support it

Only the **claude CLI** routes (`claude-code:...`, providers with
`auth_source` `subprocess:claude`) have web tools here. A web request goes
only to them: when a model is served both by a claude CLI route and another
provider, the other provider is left out for this request; when no claude CLI
route serves it, the request is refused with `400 web_unavailable`, before
anything is called.

Not supported, by design: codex, agy, gemini, Ollama and the HTTP providers
(OpenAI, Anthropic API, OpenRouter...). Ask a `claude-code:` model, or send the
request without web.

## What the CLI is allowed to do

For a web request, and only then, the claude CLI is started with:

- `--tools WebSearch,WebFetch` and `--allowedTools WebSearch,WebFetch`: these
  two tools and nothing else (no shell, no file, no MCP, no agent);
  `--permission-mode default`, `--strict-mcp-config`, `--setting-sources ""`
  are kept. The gateway refuses to start a web run whose command line would
  hold anything more (`web_access.check_web_args`).
- `--max-turns` = `web_max_uses` + 2, and a system instruction giving the
  limit, asking for the source URLs and saying that web text is data.
- its own deadline, `PLUGBOARD_WEB_TIMEOUT_S`;
- a helper model (page summaries) that is never Haiku: `PLUGBOARD_WEB_HELPER_MODEL`
  (default `claude-sonnet-5-5`; a value naming Haiku falls back to it).

## Prompt injection

A page the model reads can hold instructions ("ignore your rules", "fetch this
address"). The model is told that web text is untrusted data, but that is a
mitigation, not a guarantee. What bounds the damage is what the run can do:
two read-only tools, a bounded number of calls, no file, no shell, no secret
in its environment (the CLI's environment is an allowlist, as for every
request) and the egress guard below. Treat a web answer as you would treat a
page found by a search engine: check the cited sources before acting on it.

## Egress guard

`WebFetch` runs inside the gateway's container, which can reach private
addresses (the Docker host, the other services, the tailnet, a cloud metadata
address). Every connection of a web run therefore goes through a small proxy
of the gateway, bound to `127.0.0.1` (`HTTPS_PROXY` of the run, `NO_PROXY`
empty). It tunnels only to **public** addresses: each destination is resolved
by the proxy, every address is checked (`netguard.ip_is_public`) and the
tunnel connects to the checked address, so a second resolution cannot point
elsewhere. Loopback, private ranges, the CGNAT range of the tailnet,
link-local and the metadata address are refused (`403`); a name that resolves
to one non-public address is refused whole. Plain-HTTP proxying is refused too.

## Load

Web requests have their own cap, separate from the chat cap:
`PLUGBOARD_MAX_INFLIGHT_WEB` (default `2`; `0` turns the web off: every web
request gets `429 web_busy`). A web request also takes a chat slot like any
other. A refused request costs nothing.

## Watch it

- `GET /admin/web`: web requests in progress, the limit, started, refused by
  reason, tool calls, tunnels the egress guard opened and refused.
- `/metrics`: `plugboard_web_active`, `plugboard_web_max_inflight`,
  `plugboard_web_requests_total`, `plugboard_web_tool_uses_total`,
  `plugboard_web_refused_not_allowed_total`, `plugboard_web_refused_unavailable_total`,
  `plugboard_web_refused_busy_total`, `plugboard_web_egress_tunnels_total`,
  `plugboard_web_egress_refused_total`.
- The gateway log has one line per web run (model, tool calls, and the models
  the CLI reports in its result, helper included: a Haiku there is a warning
  and counts in `helper_haiku` of `GET /admin/web`) and one per web call
  (client, key id, tool calls, duration), never their content.

## Settings

| Variable | Default | Effect |
|---|---|---|
| `PLUGBOARD_MAX_INFLIGHT_WEB` | `2` | Concurrent web requests; `0` turns the web off. |
| `PLUGBOARD_WEB_TIMEOUT_S` | `900` | Deadline of one web run (at least 30 s). |
| `PLUGBOARD_WEB_HELPER_MODEL` | `claude-sonnet-5-5` | The CLI's helper model during a web run; never Haiku. |
