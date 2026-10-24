# Pricing

Plugboard prices every request with a **monthly pricing grid**: one file per month, `data/pricing/YYYY_MM.json`, with prices in USD per million tokens.

```json
{
  "month": "2026_10",
  "models": {
    "gpt-4o-mini": {"input_price": 0.15, "output_price": 0.6},
    "claude-sonnet-4-6": {"input_price": 3.0, "output_price": 15.0}
  },
  "note": "USD per 1M tokens. Updated 2026-10-17 via scraper (litellm)"
}
```

Cost of a request = `prompt_tokens / 1,000,000 × input_price + completion_tokens / 1,000,000 × output_price`.

## How a model finds its price

A call is priced with the grid of the current month and **the provider that actually served it** (after any fallback to another provider). The lookup, first hit wins:

1. **The provider's own prices**, in `models_by_provider` (see [Prices per provider](#prices-per-provider)).
2. **The vendor price**, in `models`, using the model name sent to the provider (without the `<provider-id>:` prefix), with normalisation and suffix matching so that names such as `openai/gpt-4o` or `anthropic--claude-sonnet-4-6` still match. A suffix matches only after `/`, `:` or `--` (`himalaya` never takes `laya`'s price); an id whose prefix is joined by `.`, `_` or `-` (`vendor-claude-sonnet-4-6`) therefore misses the vendor entry, so give that provider its own prices. The built-in table of step 3 still matches suffixes without a boundary. Suffix matching never jumps from one provider's prices to another's. The decision engine entries (`jev`, `laya`, `laya-multilingual`) price [decisions](decisions.md) only, never a chat model.
3. If the current grid cannot price the model, the same lookup in up to twelve previous monthly grids, then the [official price table](#sources) for the month being billed (exact names only), then a small table of built-in prices.
   Ollama models are priced by Ollama's own grid: a cloud model (`gpt-oss:120b-cloud`) takes the price published on [ollama.com/pricing](https://ollama.com/pricing), read at each pricing refresh into the `ollama` price space (its own id, then its family such as `gemma4:cloud`); a local model costs 0. A cloud model Ollama does not list goes through the usual lookup.
   A 0 that prices a local Ollama model by name (`llama3`, `qwen3:8b`), in a grid or in the built-in table, applies only to a self-hosted Ollama provider serving a non-cloud model; elsewhere it is skipped, estimates from it included. Only a 0 an admin set (`source: manual`) is a price for every provider.
   Self-hosted means an Ollama provider whose `base_url` host is `localhost`, a loopback address (`127.0.0.0/8`, `::1`) or a private IP literal (`10/8`, `172.16/12`, `192.168/16`, IPv6 `fc00::/7`). A DNS name (`host.docker.internal`, `ollama`, `localhost.example.com`), the provider id or port 11434 alone never qualifies: for an Ollama reached by name, use its IP, or set a `manual` 0 for the model in the `ollama` price space.
   Ollama model names are priced without regard to case or surrounding spaces (`GPT-OSS:20B-Cloud` is priced, and treated, as `gpt-oss:20b-cloud`); the name is still sent to Ollama as written.
4. **The closest version of the same family.** A new `claude-opus-5-6` with no price yet is billed at `claude-opus-5-5`'s price (effort suffixes such as `-high` and date suffixes are ignored; on a tie the newer version wins; families are never mixed). The price is marked `source: estimate`, logged, sent as a `price.estimated` webhook and queued in **Review** as a suggestion of type `estimate`. Applying it makes the price the model's own; until then each call keeps being billed at the estimate.

When nothing matches, a key with a monthly budget is refused with `402` before any provider call ([which models have a known price](clients-and-keys.md#which-models-have-a-known-price)); a key without a budget has the request recorded with a cost of 0. Each usage entry records the provider that served it and the grid entry that priced it (`price_key`, `price_source`); an admin key also receives that entry in the `X-Price-Key` response header. Client keys never see it.

`GET /admin/coverage/pricing` lists the advertised models that have a price, those that are free (price 0, typically local Ollama models) and those that are missing:

```bash
curl -s http://localhost:8765/admin/coverage/pricing -H "Authorization: Bearer $ADMIN_KEY" | jq '{coverage_pct, missing}'
```

## How the grid is updated today

A background **pricing scheduler** runs inside the server; no cron job is needed.

1. **At startup**, once providers are loaded, it updates the current month's grid.
2. **Every 6 hours** it updates it again (`PLUGBOARD_PRICING_REFRESH_INTERVAL_H`, 1 to 24). With `0` it runs once a day at `PRICING_DAILY_HOUR_UTC` (default 03:00 UTC).

Each update of the current month:

1. **Inherits** every model of the previous month that the current grid does not have yet. This is how a new month starts with a full grid.
2. **Fetches prices from each source** listed in `PLUGBOARD_PRICING_SOURCES` (see [Sources](#sources)).
3. **Merges them model by model**: the highest-priority source that prices a model wins, and each entry records the `source` it came from. A source that does not answer during an update leaves its entries unchanged; a lower-priority source never takes over a model that a higher one priced.
4. **Holds large changes for review**: a price that moves by more than `PLUGBOARD_PRICE_DRIFT_PCT` (default 20 %) is not written; it waits in the [review queue](#prices-held-for-review).
5. **Refuses to save** a grid that shrank to less than 20 % of the previous month's size (a sign of a partial fetch), and logs `SENTINEL REFUSED`.
6. **Never modifies a past month.** Past usage keeps the cost it was recorded with.

The scheduler has its own circuit breaker: after 3 consecutive failures it pauses for `PRICING_CB_COOLDOWN_S` (default one hour) and its state survives restarts in `data/cb_state.json`. Check it with:

```bash
curl -s http://localhost:8765/admin/pricing/scheduler/status -H "Authorization: Bearer $ADMIN_KEY"
```

Set `PRICING_SCHEDULER_DISABLED=1` to turn it off. If the scheduler looks stuck, follow the [pricing scheduler runbook](../runbooks/pricing-scheduler.md).

## Sources

`PLUGBOARD_PRICING_SOURCES` lists the sources to use, highest priority first. The default is:

```
PLUGBOARD_PRICING_SOURCES=external,official,provider_native,models_dev,litellm,known
```

| Source | What it reads |
|---|---|
| `external` | Your own JSON document at `PLUGBOARD_PRICING_SOURCE_URL` (`{"models": {...}}` or a bare model map in the format above), over https (http only for `localhost`). It is your source, so its changes are never held for review. |
| `official` | The vendors' published prices, checked by hand and dated, shipped in `plugboard/official_prices.py` (Claude 5.x, Gemini 4 Argon and 3.8 Flash, `gpt-4o-mini`, the decision engines Jev and Laya). Nothing is fetched, so it applies even when every network source fails. Each refresh writes the prices in force on the **first day of the grid's month** (a vendor change dated mid-month applies from the next month), with `source: official` and the vendor page in `source_url`. Its changes are never held for review and are announced as `pricing.changed` (`applied`). `GET /admin/pricing/scheduler/status` reports its age under `official`; past `PLUGBOARD_OFFICIAL_PRICES_MAX_AGE_DAYS` (35) it reads `stale` and each refresh logs a warning. Updating it: [runbook](../runbooks/pricing-scheduler.md#official-price-table). |
| `provider_native` | Prices a configured provider publishes itself. Today: OpenRouter's model list (`pricing.prompt` / `pricing.completion`, per token, converted to per million), fetched only when an OpenRouter provider is configured. |
| `models_dev` | The [models.dev](https://models.dev) catalog (MIT) that Plugboard already downloads for presets. A bare model id takes the price of its vendor (OpenAI, Anthropic, Google, Mistral, DeepSeek, xAI, Moonshot...), not of a reseller. |
| `litellm` | The public price list of the LiteLLM project (chat models only). |
| `known` | A small built-in table. It only fills models that are advertised by a provider and have no price at all. |

Every price is checked before it is used: between 0 and 1000 USD per million tokens, and an output price between 0.1 and 20 times the input price. A price outside these bounds is ignored and logged.

**Upgrading with `PLUGBOARD_PRICING_SOURCE_URL` (or the older `PRICING_SOURCE_URL`) set and `PLUGBOARD_PRICING_SOURCES` unset**: only `external`, then `official`, then `known` are used, and the server logs *Pricing sources unchanged (external, official, known). Set PLUGBOARD_PRICING_SOURCES to add models.dev and LiteLLM.* An unknown name in `PLUGBOARD_PRICING_SOURCES` is logged as an error and the default order is used. An explicit list without `official` is respected: refreshes do not write official prices, but billing still uses the table for a model the grid does not price.

## Prices per provider

Resellers do not always charge the vendor's price. A grid can carry their own prices next to the vendor prices:

```json
{
  "models": {"gpt-4o": {"input_price": 2.5, "output_price": 10.0, "source": "models_dev"}},
  "models_by_provider": {
    "openrouter": {"openai/gpt-4o-mini": {"input_price": 0.2, "output_price": 0.8, "source": "provider_native"}}
  }
}
```

The key of `models_by_provider` is a price space: a models.dev provider id (`openrouter`, `opencode`, `deepinfra`, `togetherai`, `fireworks-ai` are filled automatically), a preset key, or a Plugboard provider id. A provider created from the `catalog-openrouter` preset uses, in order, the spaces `catalog-openrouter`, `openrouter` and its own id, so you can also price a single provider record. Automatic updates keep a reseller price only when it differs from the vendor's; otherwise the vendor price applies.

Set or revert a provider price with the same endpoints as vendor prices, naming the space:

```bash
curl -s -X PATCH http://localhost:8765/admin/pricing/2026_10 -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" -d '{"models_by_provider": {"my-provider-id": {"gpt-4o": {"input_price": 2, "output_price": 8}}}}'
curl -s -X POST http://localhost:8765/admin/pricing/2026_10/revert -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" -d '{"model": "gpt-4o", "provider": "my-provider-id"}'
```

A `PUT` without `models_by_provider` keeps the provider prices already in the grid.

Before relying on provider prices on an existing install, compare what they would change on last month's usage:

```bash
make billing-reconcile ARGS="--month 2026_09 --fail-above-pct 5"
```

It prints, per client and provider, the cost with the vendor price only and with provider prices, and exits 1 when a group moves by more than the threshold.

## Prices held for review

A structured source can be wrong. When a source moves a price by more than `PLUGBOARD_PRICE_DRIFT_PCT` percent (measured from the current price and from the last reviewed price, so small steps cannot add up unnoticed), or gives a first price that disagrees by more than that with another source, the change is queued as a suggestion of type `drift` instead of being written. **A price waiting in the queue is not billed**: the grid keeps the previous price (or no price) until an admin applies it.

```bash
curl -s http://localhost:8765/admin/pricing/suggestions -H "Authorization: Bearer $ADMIN_KEY"
curl -s -X POST http://localhost:8765/admin/pricing/suggestions/<id>/apply -H "Authorization: Bearer $ADMIN_KEY"
curl -s -X POST http://localhost:8765/admin/pricing/suggestions/<id>/reject -H "Authorization: Bearer $ADMIN_KEY"
# several at once:
curl -s -X POST http://localhost:8765/admin/pricing/suggestions/apply -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" -d '{"ids": ["<id1>", "<id2>"]}'
```

Estimates (step 4 of [How a model finds its price](#how-a-model-finds-its-price)) land in the same queue. Unlike the other types they are already billed, because a model with no price would otherwise cost 0.

Each suggestion has a stable id. A rejected suggestion does not come back unless the source proposes a different price. Applying answers `409` with the reason when the grid changed since the suggestion was made; suggestions of a closed month expire. A rejection is remembered across months. Set `PLUGBOARD_PRICE_DRIFT_ACTION=apply` to write such changes directly. Applying and rejecting are recorded in the audit log.

## Prices read by a model

Some providers publish their prices only on a web page. When `PLUGBOARD_LLM_PRICING=1` (off by default), once a day after the price update Plugboard:

1. lists the advertised models that still have no price;
2. fetches the pricing page of their provider: the URL set in `PLUGBOARD_LLM_PRICING_URLS` (`provider-id=https://...`, entries separated by `;`), otherwise the `doc` link of the provider in the models.dev catalog;
3. sends the page text to a model (`PLUGBOARD_LLM_PRICING_MODEL` on `PLUGBOARD_LLM_PRICING_PROVIDER`, by default the first active HTTP provider and its default model) and asks for a JSON table limited to those models;
4. queues each price found as a suggestion of type `llm` in [the review queue](#prices-held-for-review). **Nothing reaches the grid, and nothing is billed, until an admin applies it.**

```bash
PLUGBOARD_LLM_PRICING=1
PLUGBOARD_LLM_PRICING_URLS=groq=https://groq.com/pricing;my-provider=https://example.com/pricing
PLUGBOARD_LLM_PRICING_PROVIDER=anthropic-direct
PLUGBOARD_LLM_PRICING_MODEL=gpt-4o-mini
PLUGBOARD_LLM_PRICING_MAX_USD=0.50
PLUGBOARD_LLM_PRICING_MAX_MODELS=20
```

Limits and safety:

- **Cost.** Each call is estimated first (page tokens × input price + 1 024 × output price). The pricing model must have a known price, the day's spend stays under `PLUGBOARD_LLM_PRICING_MAX_USD` (0.50, kept across restarts), at most `PLUGBOARD_LLM_PRICING_MAX_MODELS` (20) models are looked up per pass, and a pass runs once every 24 hours. The calls appear in usage under the reserved client `plugboard-internal`.
- **Pages.** https only, on the exact host of the configured or catalog URL; every redirect is checked again; the host name is resolved once and the connection goes to that address only if it is public (private, loopback, link-local, carrier-grade NAT, cloud metadata and IPv4-mapped private addresses are refused); proxies from the environment are ignored; 2 MB at most; the text is cut to 60 % of the model's context.
- **Answers.** The page goes to the model as untrusted data in its own message. Only the requested model ids are kept, with prices above 0 and up to 1 000 USD per million tokens. A refusal or an invalid answer creates nothing. A page that tries to inject instructions can at worst produce a wrong suggestion, which you reject.
- Only public pages and model ids are sent to the pricing model, never client data.

A rejected price is not asked again until the model's catalog entry changes. **Pricing → Prices read by a model** shows the settings, today's spend and, for each model still without a price, why and what to do. `POST /admin/pricing/llm/run` runs a pass now (the daily budget still applies).

## Why was I billed this price?

1. **Find the request.** In **Usage & Billing → Recent requests** (or `GET /admin/usage/{month}`), each entry has `provider_id` (the provider that served the call, after any fallback), `price_key` and `price_source`.
2. **Read the price key.** `gpt-4o` is a vendor price from `models`; `openrouter|openai/gpt-4o` is the price of the `openrouter` space in `models_by_provider`. An empty key means no price matched and the call was recorded at $0 (see `GET /admin/coverage/pricing`).
3. **Read the source.** `manual` is an admin edit; `external`, `official`, `provider_native`, `models_dev`, `litellm` and `known` are the automatic sources above; `llm` is a suggestion applied by an admin.
4. **Check the grid.** `GET /admin/pricing/{month}` shows the entry. A price change waiting in the review queue was not used: the previous price was.

An admin key also gets the price key of each call in the `X-Price-Key` response header.

## Month rollover

There is no separate rollover step. The current month is computed from the UTC date; on the first update of a new month the grid file is created and inherits the previous month's models, then receives live prices. Requests made before that first update are still priced, through the walk-back to the previous month's grid.

## Refresh manually

- Dashboard: **Jobs → Run now** on *Price refresh*, or
- API: `POST /admin/pricing/scrape`, which also passes the advertised models so that new models can be priced from the built-in table:

```bash
curl -s -X POST http://localhost:8765/admin/pricing/scrape -H "Authorization: Bearer $ADMIN_KEY"
# {"month": "2026_10", "added": 3, "updated": 1, "inherited": 0, "total": 2140, "source": "litellm"}
```


## Edit prices yourself

In the dashboard, **Pricing** shows the current month by default (a month picker selects another). Edit a price, add a row with **+ Add** (model name, input and output price per million tokens), then **Save Pricing**.

Over the API, `PATCH /admin/pricing/{month}` sets the price of the models you send and leaves the others alone:

```bash
curl -s -X PATCH http://localhost:8765/admin/pricing/2026_10 -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" -d '{"models": {"my-model": {"input_price": 1.2, "output_price": 4.8}}}'
```

`PUT /admin/pricing/{month}` **replaces the whole grid** of that month, so send the complete model map:

```bash
curl -s http://localhost:8765/admin/pricing/2026_10 -H "Authorization: Bearer $ADMIN_KEY" > grid.json
# edit grid.json, then:
jq '{models: .models, note: .note}' grid.json | curl -s -X PUT http://localhost:8765/admin/pricing/2026_10 \
  -H "Authorization: Bearer $ADMIN_KEY" -H "Content-Type: application/json" -d @-
```

Things to know about manual edits:

- **A price you change is marked `source: manual` and no source replaces it.** Rows you send unchanged (to within 1e-9) keep their source, so a `PUT` of the grid you just read marks nothing.
- To give a model back to the sources, revert it; its price is recomputed at once from the last prices fetched (or the previous month's price):

  ```bash
  curl -s -X POST http://localhost:8765/admin/pricing/2026_10/revert -H "Authorization: Bearer $ADMIN_KEY" \
    -H "Content-Type: application/json" -d '{"model": "my-model"}'
  ```

  Only the current month can be reverted; past months are frozen.
- Manual prices are inherited by the following months.
- Editing a grid changes the cost of future requests only.

## Related settings

| Variable | Default | Effect |
|---|---|---|
| `PRICING_SCHEDULER_DISABLED` | empty | `1` turns the scheduler off |
| `PRICING_DAILY_HOUR_UTC` | `3` | Hour of the daily update |
| `PLUGBOARD_PRICING_REFRESH_INTERVAL_H` | `6` | Hours between updates (1 to 24); `0` = daily at `PRICING_DAILY_HOUR_UTC` |
| `PRICING_UPDATE_TIMEOUT_S` | `120` | Timeout of one update |
| `PRICING_CB_COOLDOWN_S` | `3600` | Circuit breaker pause after 3 failures |
| `PLUGBOARD_PRICING_SOURCES` | `external,official,provider_native,models_dev,litellm,known` | Sources to use, highest priority first (see [Sources](#sources)) |
| `PLUGBOARD_OFFICIAL_PRICES_MAX_AGE_DAYS` | `35` | Days after the official table's last check before it is reported `stale` |
| `PLUGBOARD_PRICING_SOURCE_URL` | empty | Your own JSON price document (the `external` source). `PRICING_SOURCE_URL` is still read |
| `PLUGBOARD_PRICE_DRIFT_PCT` | `20` | Change, in percent, above which a price is held for review |
| `PLUGBOARD_PRICE_DRIFT_ACTION` | `review` | `review` queues large changes; `apply` writes them |

All settings are listed in the [configuration reference](configuration.md).
