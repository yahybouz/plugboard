# Embeddings (`POST /v1/embeddings`)

OpenAI-compatible embeddings served by a **local Ollama engine** registered as a
provider. The route has its own admission, separate from chat: it never falls
back to a generation model, never guesses an owner and never contacts an address
supplied by the caller.

## Admission

| Check | Refusal |
|---|---|
| Active **client** bearer key (`pb_…`). The admin key, browser sessions and anonymous calls are refused, even with `API_KEY` empty | 401 |
| `model` is one of the exact `<provider-id>:<native-model>` identities in `PLUGBOARD_EMBEDDING_MODELS` (empty: route closed) | 404 |
| The owner is the registered provider whose ID prefixes `model` (no display alias, no single-provider default) | 404 |
| The native model has no `cloud` tag (Ollama runs those remotely) | 404 |
| The provider is active and supported | 503 |
| The provider is `provider_type: sovereign` with a plain loopback `base_url` (`127.0.0.1`, `::1`, `localhost`; no credentials, path or query) | 503 |
| The key's `allowed_models` matches the identity exactly (empty list: unrestricted) | 403 |
| Monthly budget and `rpm_limit` of the key; per-IP `RATE_LIMIT` as for every route | 429 |
| Body ≤ 2 MiB; `input` is a string or 1–256 strings, each non-blank and ≤ 16 384 characters; `encoding_format` `float` or `base64`; `dimensions` refused; unknown fields refused. Errors never echo the input | 413 / 422 |

Then the gateway reads `GET /api/tags` on the engine (the model must be installed;
its digest is returned) and calls `POST /api/embed` with `truncate: false`: an
input longer than the model context is refused (422), never cut.

## Response

```json
{
  "object": "list",
  "data": [{"object": "embedding", "index": 0, "embedding": [0.0123, "…"]}],
  "model": "ollama:qwen3-embedding:0.6b",
  "usage": {"prompt_tokens": 12, "total_tokens": 12},
  "plugboard": {"provider_id": "ollama", "native_model": "qwen3-embedding:0.6b",
                "digest": "<engine digest>", "dimensions": 1024,
                "cost_usd": 0.0, "usage_recorded": true}
}
```

Before answering, the gateway checks one vector per input, a single dimension
(≤ 8 192), finite numeric values within float range (and within float32 range when
`encoding_format` is `base64`) and a non-zero norm; otherwise 502
`Invalid embedding engine response`, and no usage row is written. Engine errors are bounded and carry no upstream
body: 502 (HTTP error), 503 (unreachable), 504 (timeout after
`PLUGBOARD_EMBEDDING_TIMEOUT_S`, default 60 s), 404 (model not installed).

## Usage, logs, cost

- One usage row per call: `model = embedding:<native>`, `provider_id` of the engine,
  `prompt_tokens` = the engine's `prompt_eval_count` (tokenizer estimate if absent),
  `completion_tokens = 0`, `cost_usd = 0` (local engine, no price grid),
  `price_source = local:embeddings`. The `embedding:` prefix keeps these rows apart
  from chat rows of the same model; client totals (`/usage/me` summary, `/v1/me`)
  still add their tokens to the month's total tokens, at zero cost.
- One log line per call with the request id (`X-Request-ID`), provider, model,
  input count, dimension, tokens, key id and duration. Input text, vectors and keys are
  never logged.
- Embedding calls do not feed the provider latency statistics used by chat load
  balancing, and do not touch the response or semantic caches.

## Provisioning a model

Reuse the existing Ollama chain; do not download a model that is already there.

1. `GET /admin/ollama/models` (admin) lists installed models and their digests.
2. Only if missing: `POST /admin/ollama/models/pull {"name": "qwen3-embedding:0.6b"}`
   (admin, NDJSON progress, then a provider model sync).
3. Allow the identity: `PLUGBOARD_EMBEDDING_MODELS=ollama:qwen3-embedding:0.6b`.
4. Create a dedicated client key, restricted to that identity:
   `POST /admin/keys {"client_id": "<app>", "allowed_models": ["ollama:qwen3-embedding:0.6b"]}`.

## Limits

- Ollama-native engines only (`/api/embed`); OpenAI-compatible remote embedding
  providers are not routed (by design: local only).
- `GET /api/tags` is read on every call to report the digest (one extra local request).
- Vectors are not bit-for-bit reproducible across calls: the engine's result depends
  on batch composition and concurrent requests (measured cosine ≥ 0.9999 between
  batched and single calls of the same text).
- No streaming, no `dimensions` reduction, no caching.
