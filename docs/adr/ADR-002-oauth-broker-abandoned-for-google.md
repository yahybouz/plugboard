# ADR-002: Remove the HTTP OAuth broker presets for Gemini and ChatGPT

## Status

Accepted (2026-04-19). Shipped in v2.5.0. Replaces the `google-broker` preset introduced in v2.4.0.

## Context

v2.4.0 added an OAuth broker to the dashboard. With the `google-broker` preset, an administrator signed in with a Google account through a PKCE popup, Plugboard stored the token encrypted, and then sent it as a bearer token to `https://generativelanguage.googleapis.com/v1beta/openai/chat/completions`.

The first live test failed with `401 UNAUTHENTICATED`. That endpoint only accepts API keys created in AI Studio (`?key=...`); it does not accept OAuth bearer tokens. The endpoints that accept a token with the `cloud-platform` scope are Vertex AI, which needs an active GCP project, and Cloud Code Assist, which is proprietary and not OpenAI-compatible.

The OAuth flow itself worked, but the token it produced could not be used against the target endpoint. The preset was broken by design.

The `chatgpt-broker` preset had the same flaw: it targeted a reverse-engineered ChatGPT backend endpoint that does not accept a raw bearer token.

## Decision

Remove the `google-broker` and `chatgpt-broker` presets in v2.5.0.

Gemini users choose instead between:

- `gemini-cli-subprocess`, which spawns the local `gemini` CLI (ADR-001);
- `gemini`, the direct API with an AI Studio API key.

ChatGPT subscribers use `codex-cli-subprocess`.

The `gemini-oauth` preset, which reuses the token stored by a logged-in Gemini CLI, was left in place; see Consequences.

Broker presets whose endpoints really accept OAuth bearer tokens are kept:

- `github-broker` (`https://models.github.ai/inference`);
- `copilot-broker` (`https://api.githubcopilot.com`).

## Consequences

Positive:

- The dashboard only offers broker presets that work end to end.
- Less code, and a single pattern (`subprocess:*`) for Gemini, Codex and Claude subscriptions.

Negative:

- Breaking for anyone who created a provider from `google-broker` in v2.4.0: it must be recreated from `gemini-cli-subprocess`.
- The OAuth broker modules (`plugboard/oauth_manager.py`, `plugboard/oauth_store.py`) now only serve GitHub Models and Copilot. If GitHub stops accepting OAuth bearer tokens, they become dead code.

Open point:

- `gemini-oauth` is still listed as available, and it sends the CLI's OAuth token as a bearer token to the same `generativelanguage.googleapis.com` endpoint. The finding above applies to it as well. This decision relies on `gemini-cli-subprocess` for subscription-based Gemini access.

## Alternatives considered

- **Keep the broker and route to Vertex AI.** Keeps the popup flow, but adds GCP project discovery, Vertex model naming and region handling, and does not generalise to Codex or Claude.
- **Keep the broker and call Cloud Code Assist through an adapter.** Faithful to what the vendor CLI does, but a large and fragile adapter over a proprietary format.
- **Leave the preset in place.** No immediate effort, but a preset that connects and then fails confuses users and hides the real constraint from contributors who might copy the pattern.
