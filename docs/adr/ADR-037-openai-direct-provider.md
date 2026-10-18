# ADR-037: Direct OpenAI provider with an API key

## Status

Accepted (2026-05-04).

## Context

A cloud benchmark run hit a blocking problem on a provider that reached OpenAI models through GitHub Models with the `gh` CLI token (the `github-models-oauth` preset): 46 HTTP 429 responses on `gpt-5-chat` in 21 minutes, from a single tenant. The error message referred to scraping and to GitHub's terms of service, which suggests GitHub treats that volume as unsupported use.

Without another route, the benchmark lacked OpenAI's top models (GPT-5, o3-pro, GPT-4.1) and could not cover a reasonable matrix of cloud providers.

## Decision

Use the direct OpenAI preset: `openai` in `plugboard/direct_providers.py`, base URL `https://api.openai.com/v1`, API key sent as a bearer token.

- The infrastructure already existed. The preset had been in the catalog since v1.2.0, so no routing code was needed. The work was to extend its `known_models` list with the GPT-5, GPT-4.1 and o3-pro families and to add a test on the preset's shape.
- It covers the top tier without the GitHub rate limit, and its cost is tracked like any other provider (`X-Cost-USD` response header, daily pricing refresh).
- It complements the subscription route instead of replacing it. The Codex CLI preset (`codex-cli-subprocess`, a ChatGPT plan through OAuth) stays available; the `openai` preset is for paid API-key usage.

Callers select it through the provider prefix of the model id, which is the id the provider was given when it was configured. For example, with providers configured as `github-models-via-gh-cli` and `openai`:

```python
# Before (rate-limited)
model = "github-models-via-gh-cli:openai/gpt-5-chat"

# After
model = "openai:gpt-5-chat"
```

The OpenAI-compatible request surface does not change.

## Consequences

Positive:

- The benchmark matrix is complete and no longer hits the GitHub rate limit.

Negative:

- An OpenAI API key is billed per token, separately from any ChatGPT subscription.

Neutral:

- The subscription route (Codex CLI) and the API-key route coexist. Routing tells them apart by the provider prefix of the model id.
- The preset's model list has grown since: it now also lists the GPT-5.5 family and more o-series models. For o-series models, the router translates `max_tokens` into `max_completion_tokens`, which those models require.

## Alternatives considered

- **Remove the `gh` CLI route from the catalog.** Simple, and ends false expectations, but loses access to GPT-5 and o3.
- **Aggressive backoff on the `gh` CLI route.** Little effort, but a benchmark run becomes too long to be usable.
