"""models.dev catalog: providers, models and prices from https://models.dev.

models.dev (MIT licensed) publishes one JSON document listing AI providers,
their models, context limits and per-million-token prices. Plugboard uses it
to offer a one-click preset for every OpenAI-compatible provider it lists.

Flow::

    PLUGBOARD_CATALOG_URL ──fetch (ETag, timeout, size cap)──► validate entries
        │ network/JSON/size error                                │ shrink > 50 %
        ▼                                                        ▼
    keep data/catalog/models_dev.json (last good copy)      keep the last good copy
        │
        ▼
    generated_presets() ──► direct_providers.list_presets() (catalog-<id>, unverified)

Nothing here changes billing. A refresh never touches providers that were
already created from a preset: their base_url is frozen at creation.
"""

from __future__ import annotations

import ipaddress
import json
import re
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import aiohttp
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from plugboard.storage import atomic_write_json, data_dir

log = logging.getLogger(__name__)

DEFAULT_CATALOG_URL = "https://models.dev/api.json"
PRESET_PREFIX = "catalog-"
OPENAI_COMPATIBLE_NPM = "@ai-sdk/openai-compatible"

# models.dev ids known to speak the OpenAI chat-completions protocol. The
# coded base_url always wins over the catalog's `api` field for these ids, so
# a changed or compromised catalog entry cannot redirect their API keys.
KNOWN_BASE_URLS: Dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "moonshotai": "https://api.moonshot.ai/v1",
    "opencode": "https://opencode.ai/zen/v1",
    "xai": "https://api.x.ai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "togetherai": "https://api.together.xyz/v1",
    "fireworks-ai": "https://api.fireworks.ai/inference/v1",
    "deepinfra": "https://api.deepinfra.com/v1/openai",
    "cerebras": "https://api.cerebras.ai/v1",
}

# A whole catalog that lost more than this share of its providers or models
# compared with the previous good copy is treated as a bad upstream push.
_MAX_SHRINK = 0.5
_MAX_PRICE = 1000.0  # USD per 1M tokens


def catalog_url() -> str:
    """Configured catalog URL; empty when the catalog is turned off."""
    if os.environ.get("PLUGBOARD_CATALOG", "").strip().lower() in ("off", "0", "false"):
        return ""
    if "PLUGBOARD_CATALOG_URL" in os.environ:
        return os.environ["PLUGBOARD_CATALOG_URL"].strip()
    return DEFAULT_CATALOG_URL


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        log.error("%s is not a number; using %s", name, default)
        return default
    return value if value > 0 else default


def _catalog_dir() -> Path:
    return data_dir() / "catalog"


def _catalog_file() -> Path:
    return _catalog_dir() / "models_dev.json"


def _meta_file() -> Path:
    return _catalog_dir() / "models_dev.meta.json"


# -- Validation -----------------------------------------------------------------


class CatalogCost(BaseModel):
    input: Optional[float] = Field(default=None, ge=0, le=_MAX_PRICE)
    output: Optional[float] = Field(default=None, ge=0, le=_MAX_PRICE)
    cache_read: Optional[float] = Field(default=None, ge=0, le=_MAX_PRICE)
    cache_write: Optional[float] = Field(default=None, ge=0, le=_MAX_PRICE)


class CatalogLimit(BaseModel):
    context: Optional[int] = Field(default=None, ge=0)
    output: Optional[int] = Field(default=None, ge=0)


class CatalogModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=200)
    name: str = ""
    cost: Optional[CatalogCost] = None
    limit: Optional[CatalogLimit] = None
    tool_call: Optional[bool] = None
    # Some aggregators route a model through another SDK (Anthropic, OpenAI
    # Responses...). Such models are not callable with chat completions.
    provider: Optional[Dict[str, Any]] = None


class CatalogProvider(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=100)
    name: str = ""
    npm: str = ""
    api: str = ""
    doc: str = ""
    env: List[str] = Field(default_factory=list)
    models: Dict[str, CatalogModel] = Field(default_factory=dict)


def parse_catalog(raw: Any) -> Tuple[Dict[str, CatalogProvider], int]:
    """Validate a raw api.json document.

    Returns the valid providers keyed by id and the number of invalid
    entries (providers or models) that were skipped.
    """
    if not isinstance(raw, dict):
        raise ValueError("catalog is not a JSON object")
    providers: Dict[str, CatalogProvider] = {}
    invalid = 0
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            invalid += 1
            continue
        raw_models = entry.get("models")
        models_raw: Dict[str, Any] = raw_models if isinstance(raw_models, dict) else {}
        models: Dict[str, CatalogModel] = {}
        for model_key, model in models_raw.items():
            if not isinstance(model, dict):
                invalid += 1
                continue
            try:
                models[model_key] = CatalogModel.model_validate({"id": model_key, **model})
            except ValidationError:
                invalid += 1
        try:
            provider = CatalogProvider.model_validate(
                {**{k: v for k, v in entry.items() if k != "models"}, "id": entry.get("id") or key}
            )
        except ValidationError:
            invalid += 1
            continue
        provider.models = models
        providers[provider.id] = provider
    return providers, invalid


def _model_count(providers: Dict[str, CatalogProvider]) -> int:
    return sum(len(p.models) for p in providers.values())


def shrank_too_much(
    new: Dict[str, CatalogProvider], previous: Dict[str, CatalogProvider]
) -> bool:
    """True when `new` lost more than half of the providers or models of `previous`."""
    if not previous:
        return False  # first fetch: nothing to compare with
    old_p, old_m = len(previous), _model_count(previous)
    if old_p and len(new) < old_p * (1 - _MAX_SHRINK):
        return True
    return bool(old_m and _model_count(new) < old_m * (1 - _MAX_SHRINK))


# -- Storage --------------------------------------------------------------------

_cache: Dict[str, Any] = {"mtime": None, "providers": {}}


def load_catalog() -> Dict[str, CatalogProvider]:
    """Last good catalog from disk (empty when none was ever fetched)."""
    path = _catalog_file()
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return {}
    if _cache["mtime"] == mtime:
        return _cache["providers"]
    try:
        providers, _ = parse_catalog(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        log.error("Stored catalog %s is unreadable: %s", path, exc)
        return {}
    _cache.update(mtime=mtime, providers=providers)
    return providers


def _read_meta() -> Dict[str, Any]:
    try:
        return json.loads(_meta_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_meta(meta: Dict[str, Any]) -> None:
    _catalog_dir().mkdir(parents=True, exist_ok=True)
    atomic_write_json(_meta_file(), meta)


def status() -> Dict[str, Any]:
    """Catalog state for /admin/pricing/scheduler/status and the dashboard."""
    meta = _read_meta()
    providers = load_catalog()
    if not catalog_url():
        state = "off"
    elif not providers:
        state = "unavailable"
    elif meta.get("last_error"):
        state = "stale"
    else:
        state = "ok"
    return {
        "state": state,
        "url": catalog_url(),
        "fetched_at": meta.get("fetched_at"),
        "checked_at": meta.get("checked_at"),
        "last_error": meta.get("last_error"),
        "invalid_entries": meta.get("invalid_entries", 0),
        "providers": len(providers),
        "models": _model_count(providers),
        "presets": len(generated_presets(providers)),
    }


# -- Fetch ----------------------------------------------------------------------


class CatalogTooLarge(ValueError):
    pass


async def _download(url: str, etag: str) -> Tuple[int, Optional[bytes], str]:
    timeout = aiohttp.ClientTimeout(total=_env_float("PLUGBOARD_CATALOG_TIMEOUT_S", 20))
    max_bytes = int(_env_float("PLUGBOARD_CATALOG_MAX_MB", 20) * 1024 * 1024)
    headers = {"Accept": "application/json"}
    if etag:
        headers["If-None-Match"] = etag
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 304:
                return 304, None, etag
            if resp.status != 200:
                raise ValueError(f"HTTP {resp.status}")
            # content.read(n) returns as soon as one chunk arrives: loop until
            # EOF, stopping as soon as the size cap is passed.
            chunks: List[bytes] = []
            size = 0
            async for chunk in resp.content.iter_chunked(64 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise CatalogTooLarge(f"catalog larger than {max_bytes} bytes")
                chunks.append(chunk)
            return 200, b"".join(chunks), resp.headers.get("ETag", "")


async def refresh_catalog() -> Dict[str, Any]:
    """Fetch the catalog and keep the last good copy on any failure.

    Never raises: the result says what happened, and the failure is
    recorded in the catalog status.
    """
    url = catalog_url()
    if not url:
        return {"state": "off"}
    meta = _read_meta()
    now = datetime.now(timezone.utc).isoformat()
    meta["checked_at"] = now
    try:
        code, body, etag = await _download(url, meta.get("etag", "") if _catalog_file().exists() else "")
        if code == 304:
            meta["last_error"] = None
            _write_meta(meta)
            return {"state": "unchanged"}
        if body is None:
            raise ValueError("empty catalog response")
        raw = json.loads(body)
        providers, invalid = parse_catalog(raw)
        previous = load_catalog()
        old_p = len(previous) or int(meta.get("providers") or 0)
        old_m = _model_count(previous) or int(meta.get("models") or 0)
        # Compare with the counts of the last good copy, even when that copy
        # can no longer be read, so a damaged file does not open the gate.
        if (old_p and len(providers) < old_p * (1 - _MAX_SHRINK)) or (
            old_m and _model_count(providers) < old_m * (1 - _MAX_SHRINK)
        ):
            raise ValueError(
                f"catalog shrank from {old_p} to {len(providers)} providers "
                f"({old_m} to {_model_count(providers)} models); kept the previous copy"
            )
        _catalog_dir().mkdir(parents=True, exist_ok=True)
        atomic_write_json(_catalog_file(), raw)
        meta.update(
            etag=etag, fetched_at=now, last_error=None, invalid_entries=invalid,
            providers=len(providers), models=_model_count(providers),
        )
        _write_meta(meta)
        log.info(
            "models.dev catalog: %d providers, %d models, %d invalid entries skipped",
            len(providers),
            _model_count(providers),
            invalid,
        )
        return {"state": "updated", "providers": len(providers), "invalid_entries": invalid}
    except Exception as exc:  # noqa: BLE001 — every failure keeps the old copy
        meta["last_error"] = f"{type(exc).__name__}: {exc}"
        try:
            _write_meta(meta)
        except OSError as write_exc:
            log.error("Cannot record catalog error: %s", write_exc)
        log.warning("models.dev catalog refresh failed (%s); keeping the last good copy", meta["last_error"])
        return {"state": "error", "error": meta["last_error"]}


# -- Presets --------------------------------------------------------------------


def is_public_https(url: str) -> bool:
    """https, default port, no credentials, and not a private or local host."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https" or not parts.hostname:
        return False
    if parts.username or parts.password or parts.port not in (None, 443):
        return False
    host = parts.hostname.lower()
    if not re.fullmatch(r"[a-z0-9.-]+", host):
        return False  # templated ("${ACCOUNT}.example.com") or malformed
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # a name; resolution is checked when the provider is tested
    return ip.is_global


def _excluded_ids() -> set:
    raw = os.environ.get("PLUGBOARD_CATALOG_EXCLUDE", "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def resolve_base_url(provider: CatalogProvider) -> str:
    """OpenAI-compatible base URL for a catalog provider, or "" when unusable."""
    if provider.id in KNOWN_BASE_URLS:
        return KNOWN_BASE_URLS[provider.id]
    if provider.npm == OPENAI_COMPATIBLE_NPM and provider.api and is_public_https(provider.api):
        return provider.api.rstrip("/")
    return ""


def _chat_compatible(model: CatalogModel, provider: CatalogProvider) -> bool:
    """False when the model overrides the provider SDK with another protocol."""
    npm = (model.provider or {}).get("npm")
    return not npm or npm in (OPENAI_COMPATIBLE_NPM, provider.npm)


def compatible_models(provider: CatalogProvider) -> List[str]:
    return sorted(m.id for m in provider.models.values() if _chat_compatible(m, provider))


def excluded_models(provider: CatalogProvider) -> List[str]:
    return sorted(m.id for m in provider.models.values() if not _chat_compatible(m, provider))


def _preset(provider: CatalogProvider, base_url: str) -> Dict[str, Any]:
    models = compatible_models(provider)
    priced = any(
        m.cost and m.cost.input is not None and m.cost.output is not None
        for m in provider.models.values()
    )
    name = provider.name or provider.id
    return {
        "key": f"{PRESET_PREFIX}{provider.id}",
        "name": name,
        "status": "available",
        "base_url": base_url,
        "default_model": models[0] if models else "",
        "provider_type": "cloud",
        "auth_header_name": "Authorization",
        "auth_header_prefix": "Bearer",
        "signup_url": provider.doc,
        "token_hint": ", ".join(provider.env),
        "known_models": models,
        "catalog_id": provider.id,
        "catalog": True,
        "verified": False,
        "prices_known": priced,
        "setup_steps": [
            {
                "title": f"Create an API key at {name}",
                "link": provider.doc,
                "description": "Sign in to the provider and create an API key.",
            },
            {
                "title": "Paste the key and test the connection",
                "description": "Plugboard lists the models the key can use before you save.",
            },
        ],
    }


def generated_presets(
    providers: Optional[Dict[str, CatalogProvider]] = None,
) -> List[Dict[str, Any]]:
    """One preset per OpenAI-compatible catalog provider, sorted by name."""
    if providers is None:
        providers = load_catalog()
    excluded = _excluded_ids()
    presets = []
    for provider in providers.values():
        if provider.id in excluded:
            continue
        base_url = resolve_base_url(provider)
        if base_url and compatible_models(provider):
            presets.append(_preset(provider, base_url))
    return sorted(presets, key=lambda p: p["name"].lower())


def get_generated_preset(key: str) -> Optional[Dict[str, Any]]:
    if not key.startswith(PRESET_PREFIX):
        return None
    for preset in generated_presets():
        if preset["key"] == key:
            return preset
    return None


def model_entry(catalog_id: str, model_id: str) -> Optional[CatalogModel]:
    provider = load_catalog().get(catalog_id)
    return provider.models.get(model_id) if provider else None
