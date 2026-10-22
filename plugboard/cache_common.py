"""Shared helpers for cache modules (closes ARCH-DEBT-023).

Both ``cache.py`` (exact response cache) and ``semantic_cache.py``
(MinHash + embedding cache) need the same SEC-001 strict-tenant gate
when scoping a row by ``client_id``. Until 2026-05-04 the gate lived
only in ``cache.py``; ``semantic_cache.py`` had a partial reimplementation
(``cid = client_id or ""`` without the strict-mode raise). Centralising
the helper here ensures every present and future cache backend honours
the same multi-tenant contract (manifesto §11.4 + ADR-032).

Importers:
- ``plugboard.cache``                 (response_cache table)
- ``plugboard.semantic_cache``        (semantic_cache + semantic_bands)
- any 3rd cache added later — register its adapter in
  ``tests/test_tnr_sec001_cache_isolation.py`` so the parametrised
  TNR exercises it.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


# ARCH-DEBT-038 closed: the canonical name is now PLUGBOARD_ALLOW_PERMISSIVE
# (opt-OUT semantic — strict is the default). The legacy PLUGBOARD_STRICT_TENANT
# is still honoured for rollback compat (a deployment can be downgraded without
# rebuilding code), but emits a deprecation warning at first read so operators
# notice the rename.
_DEPRECATION_LOGGED = False


def _truthy(value: str) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def strict_tenant_mode() -> bool:
    """SEC-001 strict mode flag (default ON since v3.0.0 / ADR-032).

    Resolution order (first match wins):

    1. ``PLUGBOARD_ALLOW_PERMISSIVE`` (new, opt-out semantic, ARCH-DEBT-038).
       If truthy, returns False (= permissive). Otherwise strict.
    2. ``PLUGBOARD_STRICT_TENANT`` (legacy, kept for backwards compat).
       If set explicitly to a falsy value, returns False (= permissive).
       Reading this var emits a deprecation warning once per process.
    3. Default: True (= strict).
    """
    global _DEPRECATION_LOGGED

    new_var = os.environ.get("PLUGBOARD_ALLOW_PERMISSIVE", "").strip()
    if new_var:
        return not _truthy(new_var)

    legacy = os.environ.get("PLUGBOARD_STRICT_TENANT", "").strip().lower()
    if legacy:
        if not _DEPRECATION_LOGGED:
            log.warning(
                "PLUGBOARD_STRICT_TENANT is deprecated (ARCH-DEBT-038). "
                "Use PLUGBOARD_ALLOW_PERMISSIVE=1 for permissive (opt-out) "
                "instead. Legacy var still honoured for now."
            )
            _DEPRECATION_LOGGED = True
        return legacy not in {"0", "false", "no", "off"}

    return True  # strict unless explicitly turned off


def check_client_id(client_id: str) -> str:
    """Normalise and gate ``client_id`` for any cache backend.

    - Strict mode (default): raise ``ValueError`` on empty/None — a caller
      without ``client_id`` is either a bug or a breach attempt.
    - Permissive mode: log WARNING and accept '' for legacy backwards
      compat (pre-v2.8.2 rows).
    """
    cid = client_id or ""
    if not cid:
        msg = (
            "cache call with empty client_id — pre-v2.8.2 legacy row scope. "
            "All callers must propagate client_id (SEC-001)."
        )
        if strict_tenant_mode():
            raise ValueError(msg)
        log.warning(msg)
    return cid
