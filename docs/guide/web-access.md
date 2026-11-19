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
| `400` | `web_with_tools_unsupported` | the request also declares `tools` (ADR-040) | no |
| `400` | `web_unavailable` | no claude CLI route serves the model | no |
| `429` | `web_busy` | the web requests in progress are at their limit; `Retry-After: 10` | no |
| `502` | `web_budget_exhausted` | the model used its `web_max_uses` calls without a final answer | yes |

**Web and tools**: a request that asks for the web cannot declare `tools`
(400 `web_with_tools_unsupported`, nothing called), even where tools are
emulated on CLI routes (`PLUGBOARD_CLI_TOOLS_EMULATION=1`, `allow_cli_tools`):
a web run gives the CLI only its two web tools. Send two requests: one with
the web, one with your tools ([ADR-040](../adr/ADR-040-web-et-outils.md)).
The refusals come in this order: `web_bad_request`, `web_not_allowed`,
`web_with_tools_unsupported`, `web_unavailable`, `web_busy`. Contract 3.2.0.

A web answer is never read from nor written to the response cache: it depends
on the live web. Its usage entry is marked (`web`, `web_uses`), and the month's
summaries (`/v1/me`, `/usage/me`) count `web_requests` and `web_uses`.

### What a web request costs

A web run takes several turns, reads pages and calls a helper model for the
page summaries, so it is billed on the tokens claude reports for the run,
not on an estimate of the question and the answer:

- every model of the run is counted, the helper (`claude-sonnet-5-5`)
  included, from the `modelUsage` of claude's `result` event;
- `prompt_tokens` = input + cache writes + cache reads, `completion_tokens` =
  output; the cache reads are also shown as
  `prompt_tokens_details.cached_tokens`;
- the whole is priced at the price of the route that served the model you
  asked for, with this month's grid, as any other call; there is no
  separate web price. Billing has no cache price: cache writes and cache
  reads are billed at the input price.

A run that spends its turns without answering is billed too: it did spend
those tokens. It is final: the request ends with `502 web_budget_exhausted`
and no other claude route runs the same search again (it would be billed a
second time). Its usage entry (`web`, its own `web_uses`, the tokens
reported, at the price of the route that ran it) counts once against the
key's monthly budget like a served answer; the route that ran it is not
counted as failing (no circuit, no error metric, no `provider_down` alert).
A stream that had already sent words when the run stopped is billed once,
on the tokens reported.

On a key with a monthly budget, a web request holds, before the run, its
prompt plus `PLUGBOARD_WEB_HOLD_TOKENS_PER_TURN` input tokens (default
`20000`) for each turn the run may take (`web_max_uses` + 2), priced at the
dearest route as any hold (BUG-161): with `web_max_uses` 3, 100,000 input
tokens on top of the prompt. A web request the budget left cannot hold gets
`429`, nothing run. The billing then settles the hold to what the run really
cost (the tokens reported), so a hold larger than the cost is never charged.

When claude reports no usable counts (no `result` event, a counter that is
not a non-negative integer), the call is billed on the estimate, as before,
and the answer is served; the gateway logs a warning, without any content.
A request without the web is billed as before.

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
address). A web run therefore has **no network but its egress** (since
4.15.2, PLU-162): claude runs in a bubblewrap cage with a network of its own
(`--unshare-user --unshare-net`), holding nothing but its own loopback. Its
`HTTPS_PROXY` and `HTTP_PROXY` name a small relay on that loopback
(`127.0.0.1:3128` of the cage; `ALL_PROXY` and `NO_PROXY` empty), which
forwards each connection to a unix socket of the run, served by the run's
own CONNECT proxy in the gateway. That is the only way out: a client that
ignores `HTTPS_PROXY` (a direct connection to the gateway's port, the
internal network, the metadata address or any public address) finds no route
(`ECONNREFUSED` on the cage's loopback, `ENETUNREACH` elsewhere). Until
4.15.1 the guard was a proxy on the gateway's loopback that held only for a
client honouring `HTTPS_PROXY`.

The cage is a network cage: claude's files are the same as before (the
whole root as it is, `/dev`, `/proc`, `HOME`, `~/.claude`, node), the same
uid, the same command line and environment but the proxy variables, with one
exception: the CLIs' launch directory (claude's working directory, which
holds the private folders of the calls in progress, `web-run-*`, `agy-tmp-*`,
`agy-store-*`) is shown empty but for the run's own folder. A web run cannot
reach the egress socket of another web run (and so pass its own connection
cap) nor that of an agy call, nor read an agy call's store. When that
directory holds what claude needs (an operator's `PLUGBOARD_SUBPROCESS_CWD`
holding its `HOME`, `~/.claude`, its binary or the gateway's Python), it
cannot be hidden and the web run is refused (`503
provider_sandbox_unavailable`); the default launch directory, a private
temporary folder, never holds them. It is not a file sandbox: a web run has
no shell nor file tool, only `WebSearch` and `WebFetch`. **No cage, no web run**: when bwrap is
missing (the image's `/usr/libexec/plugboard/bwrap`, else one on `PATH`), the
run's socket cannot be made, or the cage or its relay cannot start, claude is
not started and the request gets `503 provider_sandbox_unavailable`, as an
agy call without its cage; there is no setting that runs it without. A
request without the web is started exactly as before, outside any cage.

The run's proxy tunnels only to **public** addresses: each destination is resolved
by the proxy, every address is checked (`netguard.ip_is_public`) and the
tunnel connects to the checked address, so a second resolution cannot point
elsewhere. Each tunnel and each refusal is logged with the host and port
asked (`web egress tunnel: host=...`), never a path or a page; the host is
escaped (control characters and ANSI sequences shown as `\x..`) and cut to
253 characters. A refusal is logged once per host, port and minute; the
count of the repeats follows within the next minute (`N more refused`); past
256 hosts and ports in a minute, the others share one `<other hosts>` line
naming a sample of at most 8 (`sample=host:port,...`). Loopback, private ranges, the CGNAT range of the tailnet,
link-local and the metadata address are refused (`403`); a name that resolves
to one non-public address is refused whole. Plain-HTTP proxying is refused too,
and so is a tunnel to any port but `443` (`403`, since 4.15.0): no plain HTTP
goes out, inside a tunnel or not.

The proxy resolves names on threads of its own (at most 8 at once), never on
the gateway's shared worker threads, and gives a name 5 seconds: a name whose
DNS does not answer in time is refused (`403`, logged as `did not resolve in
5 s`), so slow names cannot delay billing writes or the other requests
(since 4.15.2).

A tunnel is closed after 120 seconds without a byte in one way, and in any
case once it is `PLUGBOARD_EGRESS_TUNNEL_MAX_SECONDS` old (default `600`,
since 4.15.2), even with bytes still flowing: a page trickled byte by byte
cannot hold a connection forever. Each web run's proxy serves at most 64
connections at once (`503` beyond): the cap is per run, so a run cannot take
the connections of another (since 4.15.2; until then one proxy served every
run). The proxy on the gateway's loopback of 4.15.1 (`web_egress.ensure_started`)
is no longer used by the web runs.

What the run's proxy reports, tunnels and refusals, is logged as before
(`web egress tunnel: host=...`, `web egress refused: host=...`) and counted
in the same figures (`GET /admin/web`, `/metrics`): the hosts a claude web run
contacts, its own start-up included (`api.anthropic.com`, and whatever
claude reaches for its sign-in or telemetry), show there.

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
| `PLUGBOARD_WEB_HOLD_TOKENS_PER_TURN` | `20000` | Input tokens a budgeted web request holds for each turn of its run, on top of its prompt; `0` holds the prompt's estimate only. A value that is not a whole number from 0 to 1,000,000 gives the default, with one warning. |
