# Security policy

## Reporting a vulnerability

Please **do not report security problems in public issues, merge requests or discussions.**

Report them privately by opening a **confidential issue** in the project's GitLab repository (check *This issue is confidential* when you create it). Confidential issues are visible only to you and the project maintainers.

Include as much of the following as you can:

- the affected version or commit;
- the component (client API, admin API, dashboard, MCP server, CLI providers, Docker image);
- the steps to reproduce, or a proof of concept;
- the impact you expect (data exposure, authentication bypass, remote code execution, cost abuse, ...);
- whether the issue is already public.

What to expect:

- an acknowledgement within a few days;
- an assessment and, if the report is confirmed, a fix and a coordinated disclosure date agreed with you;
- credit in the release notes if you wish.

Please give us reasonable time to release a fix before any public disclosure, and do not access data that is not yours or degrade a service you do not own while testing.

## Supported versions

Security fixes are made on the latest release. Upgrade before reporting if you can.

## Scope notes for operators

Some behaviours are by design; please read them before reporting:

- **Dashboard auto-login** is enabled only for a local install: the server must listen on a loopback address (or `PLUGBOARD_LOCAL_AUTO_LOGIN=1` must be set), the request must come directly from the same machine as a browser navigation to `localhost`, carry no proxy header, `ADMIN_KEY` must be set and MFA must not be enforced. Known and accepted: same-machine containers or VMs routed to the host loopback, and local processes forging browser headers, pass these checks (see the README's security notes; set `PLUGBOARD_LOCAL_AUTO_LOGIN=0` on shared machines). Any other way to obtain an admin session is a vulnerability.
- **An empty `API_KEY`** disables client authentication on `/v1/*`. The server warns about it and `/health` reports it; this is a configuration choice, not a vulnerability.
- **Subscription-backed providers** reuse the operator's own CLI sign-in. Their use may breach the provider's terms (see the README); that is a policy risk the operator accepts, not a security vulnerability in Plugboard.
- **Outbound requests for prices and catalogs.** The server fetches public price lists and the models.dev catalog (`PLUGBOARD_PRICING_SOURCES`, `PLUGBOARD_CATALOG`), and, only when `PLUGBOARD_LLM_PRICING=1`, provider pricing pages that a model then reads. These pages are untrusted: what a model reads from them only becomes a suggestion an admin must apply, and the page fetch refuses non-https URLs, other hosts, redirects off the host and non-public addresses. A way to make Plugboard fetch an internal address, or to bill a price nobody applied, is a vulnerability.
- `data/` contains encrypted credentials, the generated encryption key (when `ENCRYPTION_KEY` is not set), key hashes and usage data. Protecting that directory is the operator's responsibility.

Hardening measures in place include: hashed client keys with constant-time comparison, encrypted provider credentials, brute-force lockout and optional TOTP on admin login, a strict Content Security Policy and security headers, request body size limits, per-IP and per-key rate limits, an optional provider domain allowlist, strict tenant isolation of caches, and CLI subprocesses started without a shell, with an allowlisted environment, from a private directory, with concurrency limits, in their own process group (killed with their descendants and reaped on a timeout, a cancellation or an error, before their slot is freed) and without the prompt in the logs.

### Admin, MCP and CLI-home boundaries

- **Admin MFA** (`PLUGBOARD_ADMIN_MFA_REQUIRED=1` with an enrolled secret): the `ADMIN_KEY` alone is refused on admin routes; it needs a session opened with a TOTP code or a fresh code in `X-Plugboard-Admin-TOTP`. Codes are single-use (no step at or before the last accepted one). With the flag set, an `admin_mfa.json` that exists but cannot be read fails closed: admin routes and login are refused until the file is repaired or removed. Admin sessions are recorded server-side: logout revokes one, any MFA enrolment, rotation or disable revokes all. Known limit: rotating or disabling MFA from a valid session does not ask for a new code ([TODOS](TODOS.md)); the client plane (`/v1/*`) still accepts the admin key as a bearer.
- **MCP over SSE/HTTP** requires `PLUGBOARD_MCP_TOKEN`, listens on loopback by default with DNS-rebinding protection, and keeps only its own variables: the admin, encryption, provider and Jev keys are not in its environment, even when `.env` holds them ([MCP server](docs/guide/mcp.md#network-transports-sse-http)). Reaching it without the token is a vulnerability.
- **OVH deployment**: the gateway keeps CLI logins in its own `cli-home` volume and never mounts the host account's CLI homes ([deploy/ovh](deploy/ovh/README.md#cli-homes)).

### MFA changes and accepted limits

- Each admin session records the MFA generation it was opened under. Any enrolment, rotation or disable changes the generation, so older sessions are refused even when `admin_sessions.json` cannot be rewritten; that write failure is still raised to the caller. The code that confirms a new secret on `/admin/mfa/setup` is consumed.
- MCP over SSE/HTTP keeps an allowlist of variables (`PLUGBOARD_URL`, `PLUGBOARD_API_KEY`, `PLUGBOARD_MCP_*`, `MCP_HOST`, `MCP_PORT`, the data paths and system variables) after `.env` is loaded, not only in the server module.
- A Copilot token derived from a GitHub connection that was saved or removed during the exchange is not cached.
- Accepted, not addressed: the client plane (`/v1/*`) accepts the admin key as a bearer; `/admin/mfa*` asks for no fresh code; the TOTP replay lock is per process (workers sharing one data directory rely on the file record alone); `scripts/setup_railway_providers.py` and the stdio MCP `provider_status` tool send the admin key without a TOTP code, so they are refused on admin routes once MFA is enforced.

### Budgets and accepted limits

- A budgeted key never runs a model whose price is unknown (402). The 0 of a local Ollama model applies only to an Ollama provider whose `base_url` is `localhost`, a loopback or a private (RFC 1918, IPv6 unique local) IP literal; a DNS name, the provider id or port 11434 alone never qualifies. Ollama model names are priced case- and space-insensitively and sent upstream as written. Decision engine prices (`jev`, `laya`) never price a chat model, and a grid price key matches the end of a model id only after `/`, `:` or `--`. Accepted, not addressed: the small built-in price table still matches suffixes without that boundary, and an id whose vendor prefix is joined by `.`, `_` or `-` (`vendor-claude-sonnet-4-6`) misses its grid entry, admin `manual` prices included, and takes the built-in price or 402; give such a provider its own prices (`models_by_provider`).
- A Jev call TypeSafe answered (200) but that is rejected, or that timed out after it was sent, is billed: the reported usage, else the 64,000-token ceiling. A connection error or an error status is not billed. Accepted, not addressed: when the API deadline (45 s, queue wait included) ends a call before TypeSafe answers, that 504 is not recorded (at most the ceiling, about $0.0027, per occurrence).
- If a decision's usage cannot be written, decisions of budgeted keys are refused (503) until a write succeeds: a decision recorded for any key, or the one probe call allowed every 60 seconds. The state is per process. Accepted, not addressed: a call refused afterwards by the budget or rpm check still uses that probe, so budgeted keys can stay refused until an unbudgeted key records a decision.
- Accepted, not addressed: the input tokens Jev reports are billed as reported (`input_tokens: 0` costs 0); the endpoint is fixed, TLS, without redirects. A CLI's descendants that outlive a normal exit are not killed (only a timeout, cancellation or error kills the process group). Decision queue waits are bounded at 15 s while a Laya run may take 30 s, so one key chaining slow decisions can make others time out (429); it is bounded by the per-key limit of one decision in flight and the rpm limit.
- An OAuth refresh in flight is saved only if the stored credential did not change meanwhile, so a disconnect or a new account during a refresh is kept.

Known limit: the Gemini, Codex and Antigravity CLIs take the prompt as a command-line argument, so it is visible in the process list (`/proc/<pid>/cmdline`) to every process that shares its PID namespace, and from the host. Only Claude Code reads it from stdin. Run Plugboard in its own container or user when other workloads share the host.
