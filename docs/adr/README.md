# Architecture Decision Records

An Architecture Decision Record (ADR) captures one significant design decision in Plugboard: the problem it answered, what was decided, what it costs, and which options were set aside. ADRs explain why the code looks the way it does; they are not user documentation.

Each record follows the same format:

- **Status**: Accepted, Superseded or Rejected, with the date of the decision.
- **Context**: the situation and the constraints that forced a choice.
- **Decision**: what was decided.
- **Consequences**: positive and negative effects, and any later change that supersedes part of the decision.
- **Alternatives considered**: the options that were rejected, and why.

An accepted ADR is not rewritten to follow the code. When the code later departs from a decision, the record says so in its Consequences section, or a new ADR supersedes it.

Numbers are not contiguous: gaps correspond to records that were never published.

## Index

| ID | Title | Status | Date |
|----|-------|--------|------|
| [ADR-001](ADR-001-cli-subprocess-pattern.md) | CLI subprocess pattern for OAuth-only providers | Accepted | 2026-04-19 |
| [ADR-002](ADR-002-oauth-broker-abandoned-for-google.md) | Remove the HTTP OAuth broker presets for Gemini and ChatGPT | Accepted | 2026-04-19 |
| [ADR-003](ADR-003-semantic-cache-for-stream.md) | Cache replay for streaming subprocess requests | Accepted | 2026-04-19 |
| [ADR-032](ADR-032-strict-tenant-default.md) | Strict tenant isolation by default | Accepted | 2026-04-26 |
| [ADR-033](ADR-033-remove-legacy-admin-cookie.md) | Remove the legacy admin-key cookie | Accepted | 2026-04-26 |
| [ADR-034](ADR-034-remove-legacy-api-key-fallback.md) | Remove the synthetic client key built from API_KEY | Accepted | 2026-04-26 |
| [ADR-036](ADR-036-auth-mode-derived-constant.md) | AUTH_MODE as a derived constant | Accepted | 2026-05-04 |
| [ADR-037](ADR-037-openai-direct-provider.md) | Direct OpenAI provider with an API key | Accepted | 2026-05-04 |

## When to write an ADR

Write one when a change:

- introduces a new technology, provider type or integration pattern;
- adds a critical external dependency;
- changes the data model or the storage layout in a structural way;
- exposes a new external endpoint or changes how callers authenticate;
- affects security, tenant isolation or compliance;
- removes or breaks behaviour that existing deployments rely on;
- has a significant running cost.

A bug fix, a rename or a refactor that keeps behaviour unchanged does not need one.

To add a record, copy the format above into `ADR-NNN-short-title.md` with the next free number, and add it to the index.

Versions before 4.0.0 cited in these records refer to the unpublished development history.
