# Effective model access correction

Status: released in 4.3.1.

An empty `allowed_models` list remains unrestricted. Otherwise an entry must
equal the effective native model ID or its registered owner-qualified ID
(or, since the section below, the ID `/v1/models` publishes for it).
Substring, prefix and implicit family matching are no longer authorization.
Existing policies using partial names must be changed to explicit native or
owner-qualified identities before deployment; do not silently broaden them.
Display and legacy request aliases resolve before policy evaluation. Native
colons, tags and Hugging Face namespaces remain opaque to the CLI layer.

## Published model ID as a third spelling

Status: unreleased, after 4.5.0.

`/v1/models` shows the three CLI providers under a display alias:
`claude-code:<model>`, `Codex:<model>`, `Gemini:<model>`. An `allowed_models`
entry may now be that published ID. For one model, three spellings are
accepted, each in strict equality:

| Spelling | Example |
| --- | --- |
| native model ID | `gpt-6.1-sol` |
| owner-qualified ID | `chatgpt-via-codex-cli:gpt-6.1-sol` |
| ID published by `/v1/models` | `Codex:gpt-6.1-sol` |

The rule lives in one place, `model_access_matches` in
`plugboard/model_identity.py`: the entry must equal what `published_model_id`
returns for the effective route, computed against every registered provider
ID, inactive ones included, exactly as `/v1/models` computes it. Chat, each
failover candidate, the model lists, `/v1/models/{model_id}/info` and
`/v1/preflight` share it. Embeddings do not pass the registry and keep the
first two spellings only.

What it does not open:

- A bare alias (`Codex:`, `claude-code:`), a prefix or suffix of a model name,
  a wildcard (`Codex:*`), another case (`codex:`) or added whitespace grant
  nothing, as before. This holds for a CLI registered without a default
  model too: a request `Codex:` or `chatgpt-via-codex-cli:` then resolves to
  an empty model name, which no entry grants, whatever its spelling: not the
  alias alone, not the provider ID alone (`chatgpt-via-codex-cli:`), not an
  empty entry (`""`). Before, those last two let the request through, and
  Plugboard then ran the CLI with `--model` set to the default model it
  hard-codes for that kind of CLI (`plugboard/cli_subprocess.py`), a model
  the key never named. Chat, streamed or not, now answers `403` and
  `/v1/preflight` `forbidden_model`, without running the CLI.
- The legacy request prefixes `claude-cli:`, `codex-cli:` and `gemini-cli:`
  (and their `-subprocess` forms) are still resolved in a request, but an
  entry written with them grants nothing: `/v1/models` never publishes them.
- A provider registered under the alias owns the spelling. With a provider
  whose ID is `Codex`, active or not, `Codex:<model>` designates that provider
  and never the Codex CLI, which `/v1/models` then publishes as
  `chatgpt-via-codex-cli:<model>`. The same holds for a nested owner such as
  `claude-code:team`. The comparison is on the exact provider ID, case
  included. A provider created through the API gets a lower-case ID made from
  its name (`Codex` gives `codex`, `Gemini` gives `gemini`), which is not the
  alias: `Codex:<model>` then still designates the CLI. IDs such as `Codex`,
  `Gemini` or `claude-code:team` only come from editing the registry by hand;
  through the API only `claude-code` can be taken (a provider named
  `Claude Code`, for instance).
- A CLI registered under another ID (for instance `my-codex`) is published as
  `my-codex:<model>`; `Codex:<model>` does not cover it.

Two consequences to know:

- Keys whose entries are published IDs granted nothing since 4.3.1. They call
  their models again as soon as this version is deployed, without migration.
  Review such keys before deploying.
- An entry is compared to native IDs too, as it always was. `Codex:gpt-6.1-sol`
  therefore also covers a provider whose native model is literally named
  `Codex:gpt-6.1-sol` (an upstream gateway registered as a provider). And if a
  provider is later registered under an alias, entries written with that alias
  move from the CLI to that provider; through the API, this can only happen to
  `claude-code` (see above).

Chat admission checks the effective route before any response-cache lookup.
Trusted handler callbacks check each actual dispatch candidate, including
provider failover, for both streaming and non-streaming. These callbacks are
never sourced from a request body. Model-list compatibility and preflight
share the same exact policy checks. Restricted keys bypass the response cache
because historical cache entries do not bind the effective serving identity.

Coverage: `tests/test_model_access_security.py` (substring, prefix, case,
cross-provider, policy snapshot, unresolvable model, failover and streaming
candidates refused before any provider call),
`tests/test_model_authorization_codex.py` and
`tests/test_allowed_models_published_id.py` (published IDs through the real
registry: lists, chat, info, preflight, alias collisions, failover).
