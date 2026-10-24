# Runbooks

Step-by-step procedures for operators running Plugboard. Each runbook helps
you confirm a problem in a few minutes, mitigate it, and know when to
escalate. Commands assume a local instance at `http://localhost:8765`, are
run from the installation directory, and send admin calls with
`Authorization: Bearer $ADMIN_KEY`. When admin MFA is enforced, add a fresh
TOTP code or use a session: see
[admin API with MFA](../guide/monitoring.md#admin-api-with-mfa).

## Index

| Runbook | Symptom | Priority |
|---|---|---|
| [provider-down.md](provider-down.md) | A provider is red in the dashboard, clients get `502`/`503`, watchdog or circuit breaker alerts | P1 or P2 |
| [pricing-scheduler.md](pricing-scheduler.md) | Pricing grid stale or incomplete, scheduler circuit breaker open, billing drift | P2 (P1 if no pricing at all) |
| [rollback-release.md](rollback-release.md) | Errors or failed health checks right after an upgrade | P1 |

For general diagnostics, see the
[troubleshooting guide](../guide/troubleshooting.md) and the
[monitoring guide](../guide/monitoring.md).

## Runbook format

New runbooks use these sections, in this order:

1. **Symptom**: how the problem shows up (dashboard, client errors, logs, alerts).
2. **Priority**: P1, P2 or P3, and what moves it up or down.
3. **Quick diagnosis**: a few commands that confirm the problem.
4. **Common causes**: known root causes, each with its fix.
5. **Mitigation and rollback**: what to do when there is no quick fix.
6. **Escalation**: when to escalate, and what to hand over.

Rules:

- Every command must work as written: copy, paste, replace the `<placeholders>`.
- Only reference endpoints, variables, scripts and files that exist in the code.
- When an incident reveals a cause the runbook did not cover, add it.
