# ADR-001: CLI subprocess pattern for OAuth-only providers

## Status

Accepted (2026-04-19). Shipped in v2.5.0.

## Context

Plugboard forwards OpenAI-compatible requests to several LLM providers. Some users wanted to use a subscription they already pay for (a Google account for Gemini, a ChatGPT plan through Codex, a Claude plan) instead of a paid API key. These subscriptions authenticate with OAuth, and the providers' public OpenAI-compatible endpoints do not accept the tokens they issue.

Two earlier attempts did not solve it:

1. Reusing the OAuth token stored by the Gemini CLI and calling the HTTP endpoint directly.
2. An OAuth broker in the dashboard (PKCE popup, device flow, encrypted token storage). The OAuth flow worked, but the Gemini endpoint answered `401 UNAUTHENTICATED`: it accepts API keys, not OAuth bearer tokens (see ADR-002).

The Google endpoints that do accept an OAuth token with the `cloud-platform` scope are Vertex AI, which requires an active GCP project, and Cloud Code Assist, which is internal, not OpenAI-compatible and uses a proprietary payload. Supporting either one takes significant adapter work.

The open-source OpenClaw project takes a different route. It does not call these providers over HTTP at all: it spawns the vendor's own CLI (`gemini`, `codex`, `claude`) as a subprocess and parses its JSON output. The CLI keeps track of the vendor's internal API, so the caller does not have to.

## Decision

Adopt the subprocess pattern for providers whose OAuth flow needs a proprietary endpoint or a complex adapter.

- A provider is declared with `auth_source=subprocess:<kind>`. The supported kinds are `gemini`, `codex` and `claude`, exposed as the presets `gemini-cli-subprocess`, `codex-cli-subprocess` and `claude-cli-subprocess`.
- `plugboard/cli_subprocess.py` holds one backend definition per kind (command, arguments, input mode, output format). It spawns the CLI with `asyncio.create_subprocess_exec`, without a shell, and translates OpenAI messages into a CLI prompt and the CLI output back into an OpenAI response.
- `plugboard/router.py` skips HTTP forwarding for `subprocess:*` providers, in both the regular and the streaming path.
- The CLIs do not report token counts reliably, so billing estimates usage with tiktoken.
- At startup, every installed CLI is warmed up in the background (`<cli> --version`) to reduce the cold-start cost of the first request.
- Repeated identical requests are served from the response cache, including streaming requests (ADR-003).

## Consequences

Positive:

- The code stays small and data-driven: supporting another CLI means adding one backend entry.
- No endpoint maintenance: when a vendor changes its internal API, its CLI absorbs the change.
- One pattern covers three vendors.

Negative:

- High latency: about 7 to 16 seconds per call (process start, Node.js runtime, OAuth, the vendor's HTTP call, parsing). Warm-up and caching reduce it.
- The CLIs must be installed and logged in on the host that runs Plugboard. The Docker image now ships them, but they still have to be signed in inside the container.
- Spawning external binaries widens the attack surface. The code limits it: no shell, stdin closed for CLIs that take the prompt as an argument, binary paths resolved once and cached (against PATH hijacking), a dedicated working directory, a concurrency cap per CLI (`PLUGBOARD_SUBPROCESS_LIMIT`, default 4), Codex run in a read-only sandbox by default (`PLUGBOARD_CODEX_SANDBOX`), and Claude run with no tools, no MCP servers and no user settings.
- Calls are stateless: the whole conversation is sent to the CLI on every turn. CLI session resume is not used.

## Alternatives considered

- **Vertex AI over HTTP.** Native OpenAI-compatible route that accepts OAuth bearer tokens, with about 2 seconds of latency instead of about 10. Rejected: the user must provision a GCP project, the code is specific to Google, and it does not help with the other vendors.
- **Cloud Code Assist with an OpenAI adapter.** This is what the Gemini CLI does internally. Rejected: a large adapter (messages, tools, streaming, token counting) over a proprietary format that can change without notice.
- **Require API keys.** No code needed. Rejected: it forces paid API usage on users who already have a subscription, and it loses preview models that are only available to personal accounts.
