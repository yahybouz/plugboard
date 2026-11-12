#!/usr/bin/env python3
"""Read-only check of a data directory before an upgrade (PLU-19).

Run it on a copy of ``data/``, or on the live volume mounted read-only,
before deploying a new version. It reads the JSON stores (providers, client
keys, pricing grids, update registry), never writes anything and never
prints a stored value: findings name a file, a ``key_id`` or a provider ID,
an entry position and a rule.

ERROR (exit 1) when the upgrade would change what a key may do, when a key
policy is invalid (in both versions), or when a store cannot be read by the
server:

  cli-published-id         allowed_models holds a published CLI ID such as
                           ``claude-code:<model>`` and that CLI is registered
                           under its own ID: 4.4.0 refused the entry, this
                           version grants that model
  dead-entry               an entry that names no model ("", ``Codex:``,
                           ``<provider>:``): it grants nothing in this
                           version, while 4.4.0 let "" and "<provider>:" match
                           a route without a model name
  emptied-list             every entry is dead: removing them would leave an
                           empty list, which means "every model"
  allowed-models-null      allowed_models is null: chat, /v1/models and
                           /v1/preflight fail (500), embeddings read it as
                           "every model"
  allowed-models-not-list  a string (read letter by letter) or non-text entries
  decision-policy-invalid  allowed_decision_models neither null nor a list of
                           known decision models
  client-id-refused        an active key whose client_id is blank or "admin":
                           refused (401) since 4.11.3 / 4.11.4, reissue it first
  key-record-invalid, key-record-duplicate, provider-invalid, price-invalid,
  registry-invalid, bad-shape, unreadable

WARN (exit 0, exit 1 with --strict): decision-rights, legacy-key-prefix
(``lle_`` keys), removed-connector, plaintext-token, registry-clients,
pricing-space-ignored, a
published CLI ID whose CLI is not registered (grants nothing yet), missing
stores, and the key faults above on a revoked key. NOTE: active CLI
providers, broker credentials, catalog presets, keys without model
restriction. The registry checked is data/registry.json, not a
PLUGBOARD_REGISTRY_PATH elsewhere.

Standard library only: it runs with the host's python3 or any image's.

Usage:
  python3 scripts/precheck_data.py --data /path/to/data-copy [--strict]

Exit status: 0 ready, 1 not ready, 2 usage error (no data directory).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

# Same tables as plugboard/model_identity.py and plugboard/decisions.py
# (tests/test_precheck_data.py keeps them equal): the script must run
# without the package and its dependencies.
CLI_DISPLAY_ALIASES = {
    "claude-via-claude-cli": "claude-code",
    "chatgpt-via-codex-cli": "Codex",
    "google-gemini-via-gemini-cli": "Gemini",
}
DECISION_MODELS = ("jev", "laya", "laya-multilingual")
REMOVED_CONNECTORS = frozenset({"openclaw", "openclaw-llm-proxy"})

PROMPT_FIELDS = ("title", "impact", "version", "date", "summary", "changes")
CHANGE_FIELDS = ("action", "file", "description")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
PRICING_NAME = re.compile(r"^\d{4}_\d{2}\.json$")
_MISSING = object()


class Report:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str, str, str]] = []

    def add(self, level: str, code: str, where: str, message: str) -> None:
        self.lines.append((level, code, where, message))

    def count(self, level: str) -> int:
        return sum(1 for line in self.lines if line[0] == level)

    def print(self) -> None:
        order = {"ERROR": 0, "WARN": 1, "NOTE": 2}
        for level, code, where, message in sorted(
            self.lines, key=lambda line: order[line[0]]
        ):
            print(f"{level} {code} {where}: {message}")


def _load(path: Path, report: Report, name: str) -> Any:
    """Parsed JSON, or _MISSING when absent, or None after an ERROR."""
    if not path.exists():
        return _MISSING
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        report.add("ERROR", "unreadable", name, f"cannot be opened ({exc.strerror})")
        return None
    except UnicodeDecodeError as exc:
        report.add("ERROR", "unreadable", name, f"not UTF-8 text (byte {exc.start})")
        return None
    try:
        return json.loads(text)
    except ValueError as exc:
        # The message of json carries a position, never the content.
        report.add(
            "ERROR",
            "unreadable",
            name,
            f"not valid JSON ({exc.msg}, line {exc.lineno})",
        )
        return None


# ── providers.json ─────────────────────────────────────────────────────────
def check_providers(data: Path, report: Report) -> set:
    name = "providers.json"
    doc = _load(data / name, report, name)
    if doc is _MISSING:
        report.add(
            "WARN", "missing", name, "absent: the gateway starts with no provider"
        )
        return set()
    if doc is None:
        return set()
    providers = doc.get("providers") if isinstance(doc, dict) else None
    if not isinstance(providers, list):
        report.add("ERROR", "bad-shape", name, 'expected {"providers": [...]}')
        return set()
    ids: set = set()
    for index, provider in enumerate(providers):
        where = f"{name} provider #{index}"
        if not isinstance(provider, dict):
            report.add("ERROR", "provider-invalid", where, "not an object")
            continue
        pid = provider.get("id")
        if not isinstance(pid, str) or not pid.strip():
            report.add("ERROR", "provider-invalid", where, "no id")
            continue
        where = f"{name} provider {pid}"
        if pid in ids:
            report.add("ERROR", "provider-invalid", where, "id used twice")
        ids.add(pid)
        active = provider.get("is_active", True)
        removed = any(
            str(provider.get(field, "") or "").strip().lower().partition(":")[0]
            in REMOVED_CONNECTORS
            for field in ("id", "type", "provider_type", "auth_source", "connector")
        )
        if removed:
            report.add(
                "WARN",
                "removed-connector",
                where,
                "connector removed from Plugboard: never routed",
            )
            continue
        token = provider.get("bearer_token")
        if isinstance(token, str) and token and not token.startswith("enc:"):
            report.add(
                "WARN",
                "plaintext-token",
                where,
                "token stored unencrypted (encrypted on next write)",
            )
        if not active:
            continue
        source = str(provider.get("auth_source", "") or "")
        if source.startswith("subprocess:"):
            report.add(
                "NOTE",
                "cli-provider-active",
                where,
                "active CLI provider: check its sign-in after the upgrade",
            )
        elif source.startswith("broker:"):
            report.add(
                "NOTE", "broker-auth", where, "credentials from the OAuth broker"
            )
        if provider.get("preset_key"):
            report.add(
                "NOTE", "preset-provider", where, "created from a catalog preset"
            )
    return ids


# ── api_keys.json ──────────────────────────────────────────────────────────
def _registered_owner(entry: str, provider_ids: set) -> Optional[str]:
    """Longest registered provider ID prefixing ``entry`` (model_identity)."""
    return max(
        (pid for pid in provider_ids if pid and entry.startswith(pid + ":")),
        key=len,
        default=None,
    )


def _entry_fault(entry: Any, provider_ids: set) -> Optional[tuple[str, str, str]]:
    """(level override or "", code, message) for a faulty entry, else None."""
    if not isinstance(entry, str):
        return (
            "",
            "allowed-models-not-list",
            f"entry is {type(entry).__name__}, not text",
        )
    if not entry.strip() or entry.endswith(":"):
        return (
            "",
            "dead-entry",
            'names no model (empty or prefix only): grants nothing in this version; 4.4.0 let "" and "<provider id>:" match a route without a model name',
        )
    if _registered_owner(entry, provider_ids) is not None:
        return None  # <provider id>:<model>, the same in both versions
    alias = entry.split(":", 1)[0] if ":" in entry else ""
    owners = [o for o, a in CLI_DISPLAY_ALIASES.items() if a == alias]
    if not owners:
        return None
    if owners[0] in provider_ids:
        return (
            "",
            "cli-published-id",
            f"published CLI ID ('{alias}:'): 4.4.0 refused it, this version grants it; write it as {owners[0]}:<model>",
        )
    return (
        "WARN",
        "cli-published-id",
        f"published CLI ID ('{alias}:') of a CLI provider not registered under {owners[0]}: grants nothing now, and would grant once it is",
    )


def _check_allowed_models(
    record: dict, where: str, level: str, report: Report, provider_ids: set
) -> None:
    if "allowed_models" not in record:
        return
    allowed = record["allowed_models"]
    if allowed is None:
        report.add(
            level,
            "allowed-models-null",
            where,
            "allowed_models is null: chat, /v1/models and /v1/preflight fail with 500, and /v1/embeddings reads it as every model (both versions)",
        )
        return
    if not isinstance(allowed, list):
        report.add(
            level,
            "allowed-models-not-list",
            where,
            f"allowed_models is {type(allowed).__name__}, not a list",
        )
        return
    dead = 0
    for position, entry in enumerate(allowed):
        fault = _entry_fault(entry, provider_ids)
        if fault:
            override, code, message = fault
            dead += code == "dead-entry"
            entry_level = "WARN" if override == "WARN" else level
            report.add(
                entry_level, code, f"{where} allowed_models[{position}]", message
            )
    if allowed and dead == len(allowed):
        report.add(
            level,
            "emptied-list",
            where,
            "every entry is dead: this version refuses every model to the key, and removing them leaves [], which allows every model",
        )


def _check_decisions(record: dict, where: str, level: str, report: Report) -> None:
    policy = record.get("allowed_decision_models")
    if policy is None:
        return
    if not isinstance(policy, list) or any(m not in DECISION_MODELS for m in policy):
        report.add(
            level,
            "decision-policy-invalid",
            where,
            f"allowed_decision_models must be null or a list of {', '.join(DECISION_MODELS)}",
        )
    elif policy:
        report.add(
            "WARN",
            "decision-rights",
            where,
            f"may call {len(policy)} decision model(s) on /v1/decisions",
        )


def check_keys(data: Path, report: Report, provider_ids: set) -> None:
    name = "api_keys.json"
    doc = _load(data / name, report, name)
    if doc is _MISSING:
        report.add("WARN", "missing", name, "absent: no client key")
        return
    if doc is None:
        return
    keys = doc.get("keys") if isinstance(doc, dict) else None
    if not isinstance(keys, list):
        report.add("ERROR", "bad-shape", name, 'expected {"keys": [...]}')
        return
    seen_ids: set = set()
    seen_hashes: set = set()
    for index, record in enumerate(keys):
        if not isinstance(record, dict):
            report.add(
                "ERROR", "key-record-invalid", f"{name} key #{index}", "not an object"
            )
            continue
        key_id = record.get("key_id")
        if isinstance(key_id, str) and key_id.strip():
            where = f"{name} key {key_id}"
        else:
            where = f"{name} key #{index}"
            report.add("ERROR", "key-record-invalid", where, "no key_id")
        key_hash = record.get("key_hash")
        if not isinstance(key_hash, str) or not SHA256_HEX.match(key_hash):
            report.add(
                "ERROR",
                "key-record-invalid",
                where,
                "key_hash is not a SHA-256 hex digest",
            )
        elif key_hash in seen_hashes:
            report.add(
                "ERROR", "key-record-duplicate", where, "same key_hash as another key"
            )
        if isinstance(key_id, str) and key_id in seen_ids:
            report.add(
                "ERROR", "key-record-duplicate", where, "same key_id as another key"
            )
        seen_ids.add(key_id)
        seen_hashes.add(key_hash)

        active = bool(record.get("is_active", True))
        level = "ERROR" if active else "WARN"
        if not active:
            where += " (revoked)"
        _check_allowed_models(record, where, level, report, provider_ids)
        _check_decisions(record, where, level, report)
        if not active:
            continue
        client_id = str(record.get("client_id") or "").strip()
        if not client_id or client_id.lower() == "admin":
            report.add(
                "ERROR",
                "client-id-refused",
                where,
                "client_id blank or 'admin': this version refuses the key (401); reissue it under a client ID first",
            )
        if str(record.get("key_prefix") or "").startswith("lle_"):
            report.add(
                "WARN",
                "legacy-key-prefix",
                where,
                "issued before the rename (lle_): still valid, rotate it when convenient",
            )
        if record.get("allowed_models") == [] or "allowed_models" not in record:
            report.add(
                "NOTE", "unrestricted-key", where, "no allowed_models: every model"
            )


# ── pricing/YYYY_MM.json ───────────────────────────────────────────────────
def _bad_price(value: Any) -> bool:
    if value is None:
        return False  # unknown price: refused with 402, by design
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return True
    return math.isnan(value) or math.isinf(value) or value < 0


def check_pricing(data: Path, report: Report) -> None:
    folder = data / "pricing"
    files = sorted(folder.glob("*.json")) if folder.is_dir() else []
    if not files:
        report.add(
            "WARN", "missing", "pricing/", "no pricing grid: prices are fetched again"
        )
        return
    for path in files:
        name = f"pricing/{path.name}"
        if not PRICING_NAME.match(path.name):
            report.add(
                "NOTE",
                "pricing-name",
                name,
                "not named YYYY_MM.json: not read by the server",
            )
            continue
        doc = _load(path, report, name)
        if doc is None or doc is _MISSING:
            continue
        if not isinstance(doc, dict):
            report.add("ERROR", "bad-shape", name, "expected a JSON object")
            continue
        # As billing.match_in_grid reads them: an empty or null `models` or
        # `models_by_provider` is {}; a provider space that is not an object
        # is skipped; any other non-object table breaks the price lookup.
        tables = [("model", doc.get("models") or {})]
        by_provider = doc.get("models_by_provider") or {}
        if not isinstance(by_provider, dict):
            report.add(
                "ERROR", "bad-shape", name, "models_by_provider is not an object"
            )
            by_provider = {}
        for space_index, space in enumerate(by_provider):
            label = f"provider space #{space_index} model"
            if not isinstance(by_provider[space], dict):
                report.add(
                    "WARN",
                    "pricing-space-ignored",
                    f"{name} provider space #{space_index}",
                    "not an object: ignored by the server",
                )
                continue
            tables.append((label, by_provider[space]))
        for label, table in tables:
            if not isinstance(table, dict):
                report.add(
                    "ERROR", "bad-shape", name, f"a {label} table is not an object"
                )
                continue
            for index, entry in enumerate(table.values()):
                where = f"{name} {label} #{index}"
                if not isinstance(entry, dict):
                    report.add(
                        "ERROR", "price-invalid", where, "entry is not an object"
                    )
                    continue
                for field in ("input_price", "output_price"):
                    if field in entry and _bad_price(entry[field]):
                        report.add(
                            "ERROR",
                            "price-invalid",
                            where,
                            f"{field} is not a non-negative number",
                        )


# ── registry.json ──────────────────────────────────────────────────────────
def check_registry(data: Path, report: Report) -> None:
    name = "registry.json"
    doc = _load(data / name, report, name)
    if doc is _MISSING:
        report.add("NOTE", "missing", name, "absent: clients see no updates over MCP")
        return
    if doc is None:
        return
    prompts = doc.get("prompts") if isinstance(doc, dict) else None
    if not isinstance(prompts, list):
        report.add("ERROR", "bad-shape", name, 'expected {"prompts": [...]}')
        return
    # Fields the MCP update tools index directly (mcp_server.py): a missing
    # one fails check_updates / get_update_prompt with a KeyError.
    if "version" not in doc:
        report.add("ERROR", "registry-invalid", name, "no version")
    ids: set = set()
    for index, prompt in enumerate(prompts):
        where = f"{name} prompt #{index}"
        if not isinstance(prompt, dict) or not isinstance(prompt.get("id"), str):
            report.add("ERROR", "registry-invalid", where, "not an object with an id")
            continue
        if prompt["id"] in ids:
            report.add("ERROR", "registry-invalid", where, "id used twice")
        ids.add(prompt["id"])
        missing = [f for f in PROMPT_FIELDS if f not in prompt]
        if missing:
            report.add(
                "ERROR", "registry-invalid", where, f"missing {', '.join(missing)}"
            )
        changes = prompt.get("changes", [])
        if not isinstance(changes, list) or not all(
            isinstance(c, dict) and all(k in c for k in CHANGE_FIELDS) for c in changes
        ):
            report.add(
                "ERROR",
                "registry-invalid",
                where,
                f"changes must be a list of objects with {', '.join(CHANGE_FIELDS)}",
            )
        clients = prompt.get("clients")
        if clients is not None and (
            not isinstance(clients, list)
            or not all(isinstance(c, str) for c in clients)
        ):
            report.add(
                "WARN",
                "registry-clients",
                where,
                "clients is not a list of client IDs: the update reaches nobody",
            )


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--data",
        default=os.environ.get("PLUGBOARD_DATA") or "data",
        help="data directory to read (default: PLUGBOARD_DATA, else ./data)",
    )
    parser.add_argument("--strict", action="store_true", help="warnings fail too")
    args = parser.parse_args(argv)
    data = Path(args.data)
    if not data.is_dir():
        print(f"precheck: {data} is not a directory", file=sys.stderr)
        return 2

    report = Report()
    provider_ids = check_providers(data, report)
    check_keys(data, report, provider_ids)
    check_pricing(data, report)
    check_registry(data, report)

    print(f"precheck {data}")
    report.print()
    errors, warnings = report.count("ERROR"), report.count("WARN")
    failed = errors > 0 or (args.strict and warnings > 0)
    verdict = "NOT READY" if failed else "READY"
    print(
        f"precheck: {errors} error(s), {warnings} warning(s), {report.count('NOTE')} note(s): {verdict}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
