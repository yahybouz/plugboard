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

Hardening measures in place include: hashed client keys with constant-time comparison, encrypted provider credentials, brute-force lockout and optional TOTP on admin login, a strict Content Security Policy and security headers, request body size limits, per-IP and per-key rate limits, an optional provider domain allowlist, strict tenant isolation of caches, and CLI subprocesses started without a shell, with an allowlisted environment, from a private directory, with concurrency limits.
