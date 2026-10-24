# Migrating from the removed OpenClaw connector

Decision YB #78 (24 October 2026) removes the dedicated connector. Direct
Claude Code, Codex, Ollama and compatible HTTP providers remain available.
Jev through TypeSafe API and hosted Laya English/multilingual remain separate
typed-decision paths; Kev has no active capability.

`OPENCLAW_URL`, `OPENCLAW_KEY`, `OPENCLAW_MODEL` and `OPENCLAW_NAME` are ignored.
They neither bootstrap nor persist a provider. The old model encoder, wrapper
dispatch, connector aliases and watchdog launcher execution are removed.

Recognizable legacy IDs (`openclaw`, `openclaw-llm-proxy`), explicit legacy
type/auth-source markers and legacy wrapper requests return an
`unsupported/removed legacy connector` error before transport. They do not
fall back to another provider or trigger discovery, inference, health probes or
a restart. Such stored records require migration and are excluded from active
selection and catalogues; loading them does not rewrite the provider store.

## Manual migration

1. Keep a protected backup of the existing configuration and recorded usage
   using your normal administrative process. Do not paste provider tokens,
   `.env` content or encrypted stores into diagnostics, reports or chat.
2. Review known provider IDs and explicit type/auth-source markers locally,
   without probing endpoints. Treat a recognizable legacy record as inactive
   and migration-required. Review separately any custom names or URLs whose
   implementation you know depends on the removed bridge.
3. Create a new direct provider using the appropriate dashboard preset or
   admin API. Configure vendor credentials deliberately through the normal
   secure provider form or the vendor CLI login. No automatic secret extraction,
   decryption/reuse script, in-place rewrite or ID rename is provided.
4. Select the new provider's advertised native model IDs. Update each caller's
   model/default and its client ACL explicitly; an old ID is never silently
   redirected. Native tags/revisions remain opaque. A generic provider named
   `team-openclaw-notes` remains generic and receives its native model ID without
   connector encoding.
5. Validate the new direct connection under your own operational authorization
   before switching traffic. Keep the old record and historical billing data
   for audit or archive it through your normal process; this change does not
   delete or rewrite them.

The service cannot identify the implementation behind every arbitrary URL or
custom name. Generic OpenAI-compatible endpoints remain supported; a substring
in a user-chosen name is not proof of the removed connector. Actual OVH usage
has not been checked: no SSH, production inspection or migration was performed.

Historical usage, invoice/price lookup and CLI-pattern attribution notices are
preserved. Historical ID normalization for prices is read-only compatibility,
not active model routing. Removal alone establishes no measured RAM reduction
or deployment qualification. Existing limits remain: Jev without its key is
503/unverified remotely; multilingual quality is 2/3; the Torch pt2 reserve is
open; the optional Laya runtime is qualified only on macOS arm64 ≥14, with
Linux/OVH unqualified.

## Identity and health follow-up

The registry selects the longest exact registered owner ID. Its native model ID is opaque: tags, namespaces and revisions are passed unchanged. The catalogue constructs public IDs from that pair rather than cleaning the full string. Complete canonical CLI owners retain their established short aliases when no registered owner shadows the alias. Owners containing `:` and generic owners retain their full registered IDs. For example:

| Registered owner | Native model | Published ID |
|---|---|---|
| `claude-via-claude-cli` | `claude-sonnet-4-6` | `claude-code:claude-sonnet-4-6` |
| `claude-via-claude-cli:lane` | `qwen3:jev` | `claude-via-claude-cli:lane:qwen3:jev` |
| `claude-via-claude-cli:openclaw` | `qwen3:jev` | `claude-via-claude-cli:openclaw:qwen3:jev` |
| `openai` | `gpt-5` | `openai:gpt-5` |

If `claude-code` is itself registered, the canonical CLI account publishes `claude-via-claude-cli:claude-sonnet-4-6` so the two accounts remain distinct. Dated model promotion stays within the selected owner. Catalogue consumers classify the actual owner and native model, including after caching.

MCP has no provider registry: it rejects unambiguous retired IDs locally and lets the authenticated API decide ambiguous qualified names. The API and final provider transport retain their rejection checks. Scheduled discovery validates a legacy default model before credentials or transport, just as manual discovery does. Unknown connectivity is nonpositive, without a fabricated probe or timestamp, and does not disable first use of a direct provider.

