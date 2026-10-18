"""Model catalog enrichment + deduplication.

The raw `/v1/models` list is noisy: it contains dated snapshots AND
`-latest` aliases that resolve to the same thing (Anthropic), plus
IDs with two colons (`ollama:gemma4:latest`) that confuse naive
clients who split on `:`.

This module adds three fields to every model entry:

* ``display_name`` — human-friendly label safe to render in any
  dropdown, never empty, never loses a segment after naive splits.
* ``family`` — base name without version/date/variant suffix. Used to
  group related snapshots so a UI can collapse them.
* ``variant`` — what distinguishes this entry inside its family
  (``"latest"``, a date ``"20241022"``, or a size tag like ``"7b"``).

And one method:

* :func:`dedupe_family_snapshots` drops dated snapshots when a
  ``-latest`` alias of the same family already exists — keeping the
  catalog compact and giving the user one canonical pointer per
  family. See user request: *"je garde le dernier"* (we keep the
  latest pointer, not each dated snapshot).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Tuple

log = logging.getLogger(__name__)

# ISO-ish date suffix: `-YYYYMMDD` at the end of the name.
_DATED_RE = re.compile(r"-(20\d{6})$")
# Version tag at the end: `-latest` or just `latest`.
_LATEST_RE = re.compile(r"-?latest$", re.IGNORECASE)

# Per-subprocess-kind discovery profile.
# - ``prefix``/``prefixes``: model IDs in the LiteLLM pricing snapshot must
#   start with one of these to belong to the CLI's vendor (e.g. ``claude-``
#   for the Anthropic CLI).
# - ``vendor_namespace_prefixes``: substrings that mark a vendor-namespaced
#   variant we want to skip (e.g. ``azure_ai/claude-opus-4-7`` is the same
#   model as ``claude-opus-4-7`` but reached through a different gateway —
#   the bare name is canonical for the CLI).
# - ``regional_prefixes``: pricing rows like ``us.anthropic.claude-opus-4-7``
#   are AWS Bedrock regional copies of the same model; the CLI does not
#   accept those names, so drop them.
_SUBPROCESS_DISCOVERY: Dict[str, Dict[str, Any]] = {
    "claude": {
        "prefix": "claude-",
        "vendor_namespace_prefixes": (
            "azure_ai/",
            "bedrock/",
            "vertex_ai/",
            "anthropic/",
        ),
        "regional_prefixes": (
            "anthropic.",
            "us.anthropic.",
            "eu.anthropic.",
            "apac.anthropic.",
            "au.anthropic.",
            "global.anthropic.",
        ),
    },
    "codex": {
        "prefixes": ("gpt-", "o"),
        "allow_re": (
            r"^(?:gpt-[5-9]\d*(?:\.\d+)?"
            r"(?:-(?:mini|nano|pro|latest|chat|codex))?"
            r"|gpt-4o|gpt-4-turbo|gpt-4|o[134](?:-(?:mini|pro))?)$"
        ),
        "vendor_namespace_prefixes": ("openai/", "azure_ai/", "github/"),
        "regional_prefixes": (),
    },
    "gemini": {
        "prefix": "gemini-",
        "allow_re": (
            r"^gemini-(?:"
            r"(?:2\.0|2\.5)-(?:pro|flash|flash-lite)(?:-\d{3})?"
            r"|3(?:[.-]1)?-(?:pro|flash|flash-lite)(?:-preview)?"
            r"|(?:pro|flash|flash-lite)-latest"
            r")$"
        ),
        "vendor_namespace_prefixes": ("vertex_ai/", "google/"),
        "regional_prefixes": (),
    },
}


def discover_subprocess_models_from_pricing(kind: str) -> List[str]:
    """Return the up-to-date model IDs for a subprocess CLI by harvesting
    the current month's pricing snapshot.

    The :class:`PricingScheduler` refreshes ``data/pricing/YYYY_MM.json``
    once a day from the LiteLLM public catalogue, which tracks every
    Anthropic/OpenAI/Google model on release. By reusing that snapshot
    we get a free, zero-config "new model auto-discovery" pipeline:
    the day LiteLLM adds ``claude-opus-4-7`` to its JSON, it lands in
    our pricing file at 03:00 UTC, and the next call to this function
    returns it — no preset edit, no manual sync click.

    Returns an empty list (not an error) when:
      - ``kind`` is unknown
      - the pricing file does not exist yet (fresh deployment)
      - the file is unreadable

    Subprocess providers must fall back to their static ``known_models``
    in those cases — the goal is *enrichment*, never *regression*.
    """
    profile = _SUBPROCESS_DISCOVERY.get(kind)
    if profile is None:
        return []
    if profile.get("enabled") is False:
        return []

    # Lazy import: model_catalog is loaded before the FastAPI app wires up
    # the billing module, so a top-level import would create a cycle.
    try:
        from plugboard.billing import current_pricing_month, load_pricing
    except Exception as exc:  # noqa: BLE001
        log.debug(
            "discover_subprocess_models_from_pricing: billing unavailable (%s)", exc
        )
        return []

    # Try the current month, then fall back to up to 11 previous months.
    # Reason: a fresh deployment on the 1st of the month has a near-empty
    # current snapshot, and a transient pricing-scheduler hiccup (LiteLLM
    # unreachable, partial write) can also leave the current file with
    # only a sentinel row. The vendor catalogue evolves slowly enough
    # that last month's snapshot is a perfectly serviceable baseline.
    months_to_try: List[str] = []
    try:
        month = current_pricing_month()
        months_to_try.append(month)
        year_s, mon_s = month.split("_")
        y, m = int(year_s), int(mon_s)
        for _ in range(11):
            m -= 1
            if m == 0:
                m = 12
                y -= 1
            months_to_try.append(f"{y:04d}_{m:02d}")
    except Exception:  # noqa: BLE001
        months_to_try = []

    prefixes: Tuple[str, ...] = tuple(
        profile.get("prefixes") or (profile.get("prefix"),)
    )
    prefixes = tuple(p for p in prefixes if p)
    models: Dict[str, Any] = {}
    for candidate in months_to_try:
        try:
            pricing = load_pricing(candidate)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "discover_subprocess_models_from_pricing: load_pricing(%s) failed (%s)",
                candidate,
                exc,
            )
            continue
        candidate_models = pricing.get("models") or {}
        if not isinstance(candidate_models, dict):
            continue
        # "Usable" snapshot: contains at least one row matching the kind
        # we are asked for. A snapshot with no claude-* row at all is
        # almost certainly a sentinel left behind by a pricing-scheduler
        # failure, and we should walk back to the previous month.
        if any(
            isinstance(k, str) and any(k.startswith(prefix) for prefix in prefixes)
            for k in candidate_models
        ):
            models = candidate_models
            if candidate != months_to_try[0]:
                log.info(
                    "discover_subprocess_models_from_pricing: using %s fallback "
                    "for kind=%s (current month snapshot has no %s rows)",
                    candidate,
                    kind,
                    "/".join(f"{p}*" for p in prefixes),
                )
            break
    if not models:
        return []

    vendor_ns: Tuple[str, ...] = tuple(profile.get("vendor_namespace_prefixes") or ())
    regional: Tuple[str, ...] = tuple(profile.get("regional_prefixes") or ())
    allow_re = profile.get("allow_re")
    allow_pattern = re.compile(str(allow_re)) if allow_re else None

    discovered: List[str] = []
    for model_id in models.keys():
        if not isinstance(model_id, str):
            continue
        # Skip vendor-namespaced rows: ``azure_ai/claude-opus-4-7`` is the
        # same model as ``claude-opus-4-7`` but reached through a different
        # gateway. The CLI does not accept the namespaced form.
        if any(model_id.startswith(ns) for ns in vendor_ns):
            continue
        # Skip Bedrock-regional / Vertex-regional copies.
        if any(model_id.startswith(rp) for rp in regional):
            continue
        # Skip Bedrock-style suffix ``-v1:0`` / ``-v2:0`` even when LiteLLM
        # tracks them without the regional prefix (e.g. an unscoped
        # ``claude-sonnet-4-5-20250929-v1:0`` row). These names are not
        # accepted by the vendor CLIs.
        if re.search(r"-v\d+:\d+$", model_id):
            continue
        if any(model_id.startswith(prefix) for prefix in prefixes):
            if allow_pattern and not allow_pattern.match(model_id):
                continue
            discovered.append(model_id)

    # Stable sort: lexicographic. Tests rely on this order, and the dashboard
    # renders the list as-is so a deterministic order is user-friendly.
    return sorted(set(discovered))


def _strip_provider_prefix(model_id: str) -> Tuple[str, str]:
    """Return (provider_prefix, rest). `rest` never contains a colon unless
    it came from an Ollama-style ``name:tag`` suffix."""
    if ":" not in model_id:
        return "", model_id
    prefix, rest = model_id.split(":", 1)
    return prefix, rest


def _ollama_split(rest: str) -> Tuple[str, str]:
    """For an Ollama-style ``name:tag`` (e.g. ``gemma4:latest``), return
    (name, tag). If no colon, return (rest, ``""``)."""
    if ":" not in rest:
        return rest, ""
    name, tag = rest.split(":", 1)
    return name, tag


def classify(model_id: str, owned_by: str = "") -> Dict[str, Any]:
    """Extract display_name / family / variant from a full model ID.

    The inputs we care about:
      - ``ollama:deepseek-v3.1:671b-cloud`` → display ``deepseek-v3.1:671b-cloud``,
        family ``deepseek-v3.1``, variant ``671b-cloud``.
      - ``claude-via-claude-cli:claude-3-5-haiku-20241022`` → display
        ``claude-3-5-haiku-20241022``, family ``claude-3-5-haiku``, variant
        ``20241022``.
      - ``claude-via-claude-cli:claude-3-5-haiku-latest`` → display
        ``claude-3-5-haiku-latest``, family ``claude-3-5-haiku``, variant
        ``latest``.
      - ``github-models-via-gh-cli:openai/gpt-4o`` → display ``openai/gpt-4o``,
        family ``openai/gpt-4o``, variant ``""``.
    """
    _prefix, rest = _strip_provider_prefix(model_id)
    display = rest or model_id

    # Ollama-shaped: name:tag. Family = name, variant = tag.
    if owned_by.lower().startswith("ollama") or ":" in rest:
        name, tag = _ollama_split(rest)
        return {
            "display_name": display,
            "family": name,
            "variant": tag,
        }

    # Dated snapshot: foo-20241022 → family=foo, variant=20241022
    m = _DATED_RE.search(rest)
    if m:
        return {
            "display_name": display,
            "family": rest[: m.start()],
            "variant": m.group(1),
        }

    # -latest suffix
    m = _LATEST_RE.search(rest)
    if m:
        return {
            "display_name": display,
            "family": rest[: m.start()].rstrip("-"),
            "variant": "latest",
        }

    # Vanilla: whole rest is the family, no variant.
    return {
        "display_name": display,
        "family": rest,
        "variant": "",
    }


# Provider prefixes that run pure inference with NO live web, regardless of the
# model name. Matched on the prefix before the first ":" in the (provider-tagged)
# model id, e.g. "ollama:gpt-oss:120b-cloud" -> provider "ollama".
_OFFLINE_PROVIDERS = frozenset({"ollama", "sovereign"})

# Vendor tokens that mark a web-capable agent CLI / vendor API. Matched as a
# substring anywhere in the id so real provider ids ("claude-via-claude-cli",
# "chatgpt-via-codex-cli", "google-gemini-via-gemini-cli") and bare model names
# ("claude-opus-4-7", "gpt-5.5") are both covered.
_WEB_VENDOR_TOKENS = (
    "claude", "anthropic", "gpt", "codex", "openai", "chatgpt", "gemini", "google",
)


def has_internet_access_default(model_id: str) -> bool:
    """Conservative DEFAULT ``internet_access`` for an UNPROBED model.

    A real probe (``check_model_internet_access``) overrides this per-model and
    is the authority. This default exists only for the window before any probe
    has run, so it must NOT over-claim connectivity.

    PROVIDER first, then vendor token: a model is online-by-default only when it
    is NOT served by an offline-inference provider (ollama / sovereign) AND its
    id carries a web-vendor token. Ollama-hosted models — cloud OR local — are
    pure inference with NO live web, *even when their NAME contains* "gpt"/
    "gemini" (e.g. ``ollama:gpt-oss:cloud``, ``ollama:gemini-3-flash:cloud``) or
    a ``:cloud`` tag. Cloud-HOSTING is not web-ACCESS.

    Evidence (2026-06-01): the live D3 internet probe across the 3 surfaces shows
    every Ollama ``:cloud`` model (kimi, qwen, glm, deepseek, gpt-oss) answering
    fresh-web questions as OFFLINE, and ``model_status.json`` agrees. This
    reverses the earlier ``"cloud" in id -> True`` default, which rested on the
    unverified assumption that the Ollama Cloud proxy reaches the internet.
    """
    lower_id = model_id.lower()
    provider = lower_id.split(":", 1)[0] if ":" in lower_id else ""

    # Offline-inference providers: never online by default — short-circuit
    # BEFORE the vendor-token match so an Ollama-hosted "gpt-oss"/"gemini-*" is
    # not mistaken for the web-capable vendor CLI.
    if provider in _OFFLINE_PROVIDERS:
        return False

    # Web-capable agent CLIs / vendor APIs (real provider ids contain the vendor
    # token; bare ids are the vendor name itself).
    if any(tok in lower_id for tok in _WEB_VENDOR_TOKENS):
        return True

    # Unknown / unprefixed local model: conservative — offline by default.
    return False


def enrich(models: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add display_name / family / variant / internet_access to every entry, preserving order."""
    out = []
    for m in models:
        enriched = dict(m)
        enriched.update(classify(m.get("id", ""), m.get("owned_by", "")))
        if enriched.get("internet_access") is None:
            enriched["internet_access"] = has_internet_access_default(m.get("id", ""))
        out.append(enriched)
    return out


def _is_dated_variant(variant: str) -> bool:
    """True if ``variant`` is a YYYYMMDD-style date tag."""
    return bool(_DATED_RE.fullmatch(f"-{variant}"))


def dedupe_family_snapshots(models: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse every ``(owned_by, family)`` group to a single canonical entry
    whenever possible, so the picker never shows dated snapshots.

    Four rules, applied in order:

    1. If a group contains a ``variant="latest"`` entry, drop every other
       variant in the same group (bare + dated).
    2. Else, if a bare entry (``variant=""``) exists, drop the dated
       snapshots in the same group.
    3. Else, if **only dated** entries remain (no bare, no latest),
       pick the most recent date as canonical and rewrite it to look
       undated: ``display_name``/``variant`` are rebuilt as if the
       entry were bare, and the original dated token is exposed as
       an ``alias`` (router then accepts both the clean and the dated
       name for API calls). The full dated ``id`` stays intact so
       routing to subprocess CLIs keeps working.
    4. Cross-family — a family ending in ``-0`` is the same model as the
       shorter family without the ``-0`` suffix (``claude-opus-4-0``
       == ``claude-opus-4``). Drop the ``-0`` form when both coexist.

    Size tags like ``"7b"`` or ``"671b-cloud"`` have non-empty variants
    that do not match ``_DATED_RE`` so they are always preserved.
    """
    models_list = list(models)
    # Group by (owned_by, family)
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for m in models_list:
        key = (m.get("owned_by", ""), m.get("family", ""))
        groups.setdefault(key, []).append(m)

    kept: List[Dict[str, Any]] = []
    for _key, entries in groups.items():
        has_latest = any(e.get("variant") == "latest" for e in entries)
        has_bare = any((e.get("variant") or "") == "" for e in entries)
        dated_entries = [e for e in entries if _is_dated_variant(e.get("variant", ""))]
        non_dated = [e for e in entries if not _is_dated_variant(e.get("variant", ""))]

        if has_latest:
            # Rule 1: latest beats bare + dated. Non-dated, non-bare variants
            # (size tags like "7b", "671b-cloud") still coexist because they
            # describe different hardware targets, not snapshots of the same
            # thing.
            for e in entries:
                v = e.get("variant", "") or ""
                if v == "latest":
                    kept.append(e)
                elif v and not _is_dated_variant(v):
                    kept.append(e)
        elif has_bare:
            # Rule 2: bare beats dated; non-dated variants (size tags)
            # still coexist because they are not duplicates.
            for e in non_dated:
                kept.append(e)
        elif dated_entries and not non_dated:
            # Rule 3: promote newest dated snapshot to canonical.
            newest = max(dated_entries, key=lambda e: e.get("variant", ""))
            promoted = dict(newest)
            family = promoted.get("family", "") or ""
            original_variant = promoted.get("variant", "")
            # display_name: strip the `-YYYYMMDD` suffix if present.
            original_display = promoted.get("display_name", "") or ""
            promoted["display_name"] = (
                _DATED_RE.sub("", original_display) or family or original_display
            )
            promoted["variant"] = ""
            existing_aliases = list(promoted.get("aliases") or [])
            if family and family not in existing_aliases:
                existing_aliases.append(family)
            promoted["aliases"] = existing_aliases
            if original_variant:
                promoted["promoted_from"] = original_variant
            kept.append(promoted)
        else:
            # No canonical hint and no dated-only collapse — keep every
            # non-dated entry (size tags) as is.
            for e in non_dated:
                kept.append(e)

    # Rule 4 — drop `family-0` when the bare `family` is also present
    # under the same owner. The shorter name is canonical.
    families_by_owner: Dict[str, set] = {}
    for m in kept:
        families_by_owner.setdefault(m.get("owned_by", ""), set()).add(
            m.get("family", "")
        )
    kept = [
        m
        for m in kept
        if not (
            (m.get("family", "") or "").endswith("-0")
            and (m.get("family", "") or "")[:-2]
            in families_by_owner.get(m.get("owned_by", ""), set())
        )
    ]

    # Preserve original order
    order = {id(m): i for i, m in enumerate(models_list)}
    kept.sort(key=lambda m: order.get(id(m), 0))
    return kept


def build_alias_map(models: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    """Return ``{"<owner>:<alias>": "<owner>:<canonical_model_id>"}`` for every
    promoted entry that exposes an ``aliases`` list.

    Used by the router to accept both the clean name (``claude-3-opus``)
    and the canonical dated name (``claude-3-opus-20240229``) for API
    requests, while ``/v1/models`` still returns the dated ID as the
    source of truth.
    """
    alias_map: Dict[str, str] = {}
    for m in models:
        owner = m.get("owned_by", "") or ""
        model_id = m.get("id", "") or ""
        if not owner or not model_id:
            continue
        # Strip the owner prefix from the ID to rebuild per-owner alias keys.
        if model_id.startswith(owner + ":"):
            bare_id = model_id[len(owner) + 1 :]
        else:
            bare_id = model_id
        for alias in m.get("aliases") or []:
            if not alias or alias == bare_id:
                continue
            alias_map[f"{owner}:{alias}"] = model_id
    return alias_map
