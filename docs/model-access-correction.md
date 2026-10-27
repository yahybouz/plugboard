# Effective model access correction

Status: released in 4.3.1.

An empty `allowed_models` list remains unrestricted. Otherwise an entry must
equal the effective native model ID or its registered owner-qualified ID.
Substring, prefix and implicit family matching are no longer authorization.
Existing policies using partial names must be changed to explicit native or
owner-qualified identities before deployment; do not silently broaden them.
Display and legacy request aliases resolve before policy evaluation. Native
colons, tags and Hugging Face namespaces remain opaque to the CLI layer.

Chat admission checks the effective route before any response-cache lookup.
Trusted handler callbacks check each actual dispatch candidate, including
provider failover, for both streaming and non-streaming. These callbacks are
never sourced from a request body. Model-list compatibility and preflight
share the same exact policy checks. Restricted keys bypass the response cache
because historical cache entries do not bind the effective serving identity.

Coverage: `tests/test_model_access_security.py` (substring, prefix, case,
cross-provider, policy snapshot, unresolvable model, failover and streaming
candidates refused before any provider call) and
`tests/test_model_authorization_codex.py`.
