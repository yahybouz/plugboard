# ADR-003: Cache replay for streaming subprocess requests

## Status

Accepted (2026-04-19). Shipped in v2.5.0.

## Context

Until v2.4, the response cache was skipped for `stream=true` requests. A live stream from an HTTP provider cannot be replayed from cache: tokens arrive as the provider produces them, and the caller asked for that live stream.

Subprocess providers (ADR-001) behave differently. The CLI returns the complete answer at once, and the stream Plugboard emits is synthetic: the text is split on spaces and sent as server-sent events (SSE) for the user experience. Nothing prevents caching that answer and replaying it.

This ADR concerns the exact-match response cache (`plugboard/cache.py`, SQLite), whose key is a SHA-256 of the model, the messages, the client and the sampling parameters. The paraphrase-tolerant semantic cache added later does not serve streaming requests.

## Decision

Use the response cache for streaming requests to subprocess providers, with one cache entry shared by streaming and non-streaming calls:

1. The cache key is computed even when `stream=true`.
2. Hit on a subprocess provider: replay the cached content as SSE chunks (split on spaces) with the header `X-Cache: HIT`.
3. Hit on an HTTP provider: ignore the cache and stream live from the provider.
4. Miss on a subprocess provider: call the provider without streaming, write the result to the cache, then emit it as SSE. Later identical requests, streaming or not, hit the cache.

A caller forces a fresh call by sending `"cache": false` in the request body.

## Consequences

Positive:

- Repeating an identical prompt drops from about 10 seconds to about 11 milliseconds (measured in the dashboard Playground).
- Tests against subprocess providers can be deterministic; tests that need a real call send `"cache": false`.
- The replay follows the OpenAI streaming format (`delta.content` chunks, a final chunk carrying `usage`, then `[DONE]`), so the Playground and OpenAI SDKs work unchanged.

Negative:

- Chunk boundaries in a replay are synthetic and may differ from what a real stream would produce. The final content is identical, which is what callers rely on.
- Invalidation is manual (`"cache": false`), and the Playground does not expose that option.
- The default time to live is 5 minutes, which can be short for stable system prompts. It is set with `CACHE_TTL`.

## Alternatives considered

- **Do not cache streams.** Simplest, but every subprocess call costs 7 to 16 seconds even when the answer is already known, which makes iterating on a prompt impractical.
- **A separate cache for streams.** Keeps the two modes apart, but duplicates storage and doubles the time-to-live management.
- **One shared cache with replay (chosen).** One copy per answer, and a streaming miss also fills the cache for non-streaming calls. The cost is an extra replay branch in the chat endpoint.
