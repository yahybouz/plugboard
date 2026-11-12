"""scripts/precheck_data.py: read-only data check before an upgrade (PLU-19).

The script reads a copy (or a read-only mount) of ``data/`` and fails on the
key policies whose meaning changes between 4.4.0 (the production version)
and this one, and on stores the server could not read. Each faulty set below
must fail; the clean set must pass. Nothing it prints may carry a secret or a
stored value.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "precheck_data.py"

TOKEN = "enc:gAAAAABsecret-provider-token-value"
HASH = "a" * 64
CLIENT = "client-name-that-must-not-print"


def _key(n: int, **fields) -> dict:
    record = {
        "key_id": f"{n:032x}",
        "key_hash": f"{n:064x}",
        "key_prefix": "pb_1234a",
        "client_id": CLIENT,
        "label": "label that must not print",
        "is_active": True,
        "monthly_limit_usd": 10.0,
        "allowed_models": ["ollama:qwen3:0.6b", "claude-via-claude-cli:sonnet"],
        "rpm_limit": 60,
        "allowed_decision_models": None,
    }
    record.update(fields)
    return record


def _clean() -> dict:
    return {
        "providers.json": {
            "providers": [
                {
                    "id": "ollama",
                    "name": "Ollama",
                    "base_url": "http://127.0.0.1:11434",
                    "bearer_token": TOKEN,
                    "is_active": True,
                },
                {
                    "id": "claude-via-claude-cli",
                    "name": "Claude CLI",
                    "base_url": "",
                    "auth_source": "subprocess:claude",
                    "is_active": True,
                },
            ]
        },
        "api_keys.json": {
            "keys": [
                _key(1, key_hash=HASH),
                _key(2, allowed_models=[]),
                # A revoked key keeps its old policy: it cannot authenticate.
                _key(3, is_active=False, allowed_models=["Codex:gpt-6"], client_id=""),
            ]
        },
        "pricing/2026_11.json": {
            "month": "2026_11",
            "models": {
                "claude-sonnet": {"input_price": 3.0, "output_price": 15.0},
                "qwen3:0.6b": {"input_price": 0, "output_price": 0},
                "unknown-yet": {"input_price": None, "output_price": None},
            },
        },
        "registry.json": {
            "version": "1.0.0",
            "prompts": [
                {
                    "id": "CP-001",
                    "version": "1.0.0",
                    "date": "2026-01-01",
                    "impact": "EVOLUTION",
                    "clients": [CLIENT],
                    "title": "t",
                    "summary": "s",
                    "changes": [
                        {"action": "replace", "file": ".env", "description": "d"}
                    ],
                }
            ],
        },
    }


def _write(root: Path, files: dict) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        elif isinstance(content, (dict, list)):
            path.write_text(json.dumps(content), encoding="utf-8")
        else:
            path.write_text(content, encoding="utf-8")
    return root


def _precheck(data: Path, *args):
    env = {k: v for k, v in os.environ.items() if k != "PLUGBOARD_DATA"}
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--data", str(data), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _codes(proc, level):
    return sorted(
        {
            line.split()[1]
            for line in proc.stdout.splitlines()
            if line.startswith(level + " ")
        }
    )


def _with_key(**fields) -> dict:
    files = _clean()
    files["api_keys.json"]["keys"].append(_key(9, **fields))
    return files


def test_the_clean_set_passes(tmp_path):
    proc = _precheck(_write(tmp_path, _clean()))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _codes(proc, "ERROR") == []
    assert "READY" in proc.stdout.splitlines()[-1]


# The seven faulty sets of PLU-19 (audit 2026-11-12, lot 1).
FAULTY = {
    "published CLI ID": (
        {"allowed_models": ["claude-code:sonnet"]},
        "cli-published-id",
    ),
    "prefix only": ({"allowed_models": ["Codex:", "ollama:x"]}, "dead-entry"),
    "empty string": ({"allowed_models": ["", "ollama:x"]}, "dead-entry"),
    "emptied list": (
        {"allowed_models": ["", "chatgpt-via-codex-cli:"]},
        "emptied-list",
    ),
    "null": ({"allowed_models": None}, "allowed-models-null"),
    "string": ({"allowed_models": "ollama:qwen3:0.6b"}, "allowed-models-not-list"),
    "decision policy": (
        {"allowed_decision_models": "jev"},
        "decision-policy-invalid",
    ),
}


@pytest.mark.parametrize("case", sorted(FAULTY))
def test_each_faulty_set_fails(tmp_path, case):
    fields, code = FAULTY[case]

    proc = _precheck(_write(tmp_path, _with_key(**fields)))

    assert proc.returncode == 1, proc.stdout
    assert code in _codes(proc, "ERROR")
    assert f"{9:032x}" in proc.stdout  # the key is named by its key_id
    assert "NOT READY" in proc.stdout.splitlines()[-1]


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        (
            {"allowed_decision_models": ["jev", "unknown-model"]},
            "decision-policy-invalid",
        ),
        ({"allowed_models": ["ollama:x", 3]}, "allowed-models-not-list"),
        ({"client_id": ""}, "client-id-refused"),
        ({"client_id": "  "}, "client-id-refused"),
        ({"client_id": " Admin "}, "client-id-refused"),
        ({"key_hash": "not-a-sha256"}, "key-record-invalid"),
        ({"key_hash": HASH}, "key-record-duplicate"),
        ({"key_id": f"{1:032x}"}, "key-record-duplicate"),
    ],
)
def test_other_key_faults_fail(tmp_path, fields, code):
    proc = _precheck(_write(tmp_path, _with_key(**fields)))

    assert proc.returncode == 1, proc.stdout
    assert code in _codes(proc, "ERROR")


def test_a_provider_named_like_a_cli_alias_is_an_ordinary_owner(tmp_path):
    files = _with_key(allowed_models=["Codex:gpt-6"])
    files["providers.json"]["providers"].append({"id": "Codex", "base_url": "http://h"})

    proc = _precheck(_write(tmp_path, files))

    assert proc.returncode == 0, proc.stdout


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"allowed_decision_models": ["jev"]}, "decision-rights"),
        ({"key_prefix": "lle_abcd"}, "legacy-key-prefix"),
    ],
)
def test_reported_but_not_blocking(tmp_path, fields, code):
    files = _clean()
    del files["api_keys.json"]["keys"][2]  # the revoked key warns too
    assert _precheck(_write(tmp_path / "a", files), "--strict").returncode == 0

    files["api_keys.json"]["keys"].append(_key(9, **fields))
    data = _write(tmp_path / "b", files)

    proc = _precheck(data)
    assert proc.returncode == 0, proc.stdout
    assert _codes(proc, "WARN") == [code]

    strict = _precheck(data, "--strict")
    assert strict.returncode == 1
    assert "NOT READY" in strict.stdout


def test_faults_of_a_revoked_key_are_warnings(tmp_path):
    proc = _precheck(_write(tmp_path, _with_key(is_active=False, allowed_models=None)))

    assert proc.returncode == 0, proc.stdout
    assert "allowed-models-null" in _codes(proc, "WARN")


@pytest.mark.parametrize(
    ("name", "content", "code"),
    [
        ("api_keys.json", "{not json", "unreadable"),
        ("api_keys.json", {"keys": {}}, "bad-shape"),
        ("api_keys.json", [], "bad-shape"),
        ("providers.json", "", "unreadable"),
        ("providers.json", {"providers": [{"name": "no id"}]}, "provider-invalid"),
        (
            "providers.json",
            {"providers": [{"id": "ollama"}, {"id": "ollama"}]},
            "provider-invalid",
        ),
        ("pricing/2026_11.json", "[1, 2", "unreadable"),
        ("pricing/2026_11.json", ["not", "an", "object"], "bad-shape"),
        ("pricing/2026_11.json", {"models": [1]}, "bad-shape"),
        (
            "pricing/2026_11.json",
            {"models": {"m": {"input_price": "3.0", "output_price": 1}}},
            "price-invalid",
        ),
        (
            "pricing/2026_11.json",
            {"models": {"m": {"input_price": -1, "output_price": 1}}},
            "price-invalid",
        ),
        ("registry.json", "{", "unreadable"),
        ("registry.json", {"version": "1", "prompts": {}}, "bad-shape"),
        ("registry.json", {"prompts": []}, "registry-invalid"),
        (
            "registry.json",
            {"version": "1", "prompts": [{"id": "CP-1", "clients": ["a"]}]},
            "registry-invalid",
        ),
        ("api_keys.json", b"\xff\xfe{}", "unreadable"),
        (
            "pricing/2026_11.json",
            {"models_by_provider": {"p": {"m": {"input_price": "3.0"}}}},
            "price-invalid",
        ),
        ("pricing/2026_11.json", {"models_by_provider": [1]}, "bad-shape"),
    ],
)
def test_unreadable_or_incoherent_stores_fail(tmp_path, name, content, code):
    files = _clean()
    files[name] = content

    proc = _precheck(_write(tmp_path, files))

    assert proc.returncode == 1, proc.stdout
    assert code in _codes(proc, "ERROR")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_a_file_the_reader_cannot_open_fails(tmp_path):
    data = _write(tmp_path, _clean())
    keys = data / "api_keys.json"
    keys.chmod(0)
    try:
        proc = _precheck(data)
    finally:
        keys.chmod(stat.S_IRUSR | stat.S_IWUSR)

    assert proc.returncode == 1
    assert "unreadable" in _codes(proc, "ERROR")


def test_missing_stores_are_reported_not_failed(tmp_path):
    proc = _precheck(tmp_path)

    assert proc.returncode == 0, proc.stdout
    assert "missing" in _codes(proc, "WARN")


def test_notes_name_cli_and_broker_providers(tmp_path):
    files = _clean()
    files["providers.json"]["providers"] += [
        {"id": "gh", "auth_source": "broker:github", "is_active": True},
        {"id": "cat", "preset_key": "groq", "is_active": True},
        {"id": "old", "type": "openclaw", "is_active": True},
        {"id": "plain", "bearer_token": "plaintext-token-value", "is_active": True},
    ]

    proc = _precheck(_write(tmp_path, files))

    assert proc.returncode == 0, proc.stdout
    notes = _codes(proc, "NOTE")
    assert {"cli-provider-active", "broker-auth", "preset-provider"} <= set(notes)
    warns = _codes(proc, "WARN")
    assert {"removed-connector", "plaintext-token"} <= set(warns)
    assert "plaintext-token-value" not in proc.stdout


def test_nothing_secret_or_stored_is_printed(tmp_path):
    files = _with_key(allowed_models=["claude-code:sonnet", ""], client_id="admin")
    files["api_keys.json"]["keys"].append(_key(10, allowed_decision_models=["jev"]))

    proc = _precheck(_write(tmp_path, files))
    out = proc.stdout + proc.stderr

    for value in (
        TOKEN,
        HASH,
        f"{1:064x}",
        CLIENT,
        "label that must not print",
        "pb_1234a",
        "sonnet",
        "qwen3",
        "claude-sonnet",
    ):
        assert value not in out, value


def test_the_data_directory_is_never_written(tmp_path):
    data = _write(tmp_path, _with_key(allowed_models=None))
    before = {
        p: (p.stat().st_mtime_ns, p.read_bytes())
        for p in data.rglob("*")
        if p.is_file()
    }

    _precheck(data)

    after = {
        p: (p.stat().st_mtime_ns, p.read_bytes())
        for p in data.rglob("*")
        if p.is_file()
    }
    assert after == before


def test_a_missing_data_directory_is_a_usage_error(tmp_path):
    proc = _precheck(tmp_path / "nope")

    assert proc.returncode == 2
    assert "nope" in proc.stderr


def test_cli_aliases_match_the_server(tmp_path):
    """The script runs without the package; its alias table must not drift."""
    from plugboard.model_identity import _DISPLAY_OWNERS

    import importlib.util

    spec = importlib.util.spec_from_file_location("precheck_data", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.CLI_DISPLAY_ALIASES == _DISPLAY_OWNERS
    from plugboard.decisions import DECISION_MODELS
    from plugboard.legacy_connectors import _REMOVED_IDS

    assert module.DECISION_MODELS == DECISION_MODELS
    assert module.REMOVED_CONNECTORS == _REMOVED_IDS


def test_a_published_id_of_an_unregistered_cli_is_a_warning(tmp_path):
    proc = _precheck(_write(tmp_path, _with_key(allowed_models=["Codex:gpt-6"])))

    assert proc.returncode == 0, proc.stdout
    assert "cli-published-id" in _codes(proc, "WARN")
    assert "cli-published-id" not in _codes(proc, "ERROR")


def test_an_owner_registered_under_a_longer_id_is_not_an_alias(tmp_path):
    files = _with_key(allowed_models=["claude-code:team:m"])
    files["providers.json"]["providers"].append({"id": "claude-code:team"})

    proc = _precheck(_write(tmp_path, files))

    assert proc.returncode == 0, proc.stdout


def test_registry_clients_that_reach_nobody_are_a_warning(tmp_path):
    files = _clean()
    files["registry.json"]["prompts"][0]["clients"] = "my-app"

    proc = _precheck(_write(tmp_path, files))

    assert proc.returncode == 0, proc.stdout
    assert "registry-clients" in _codes(proc, "WARN")


@pytest.mark.parametrize(
    "grid",
    [
        {"models": None},
        {"models": []},
        {"models": {}, "models_by_provider": None},
        {"models_by_provider": []},
    ],
)
def test_empty_tables_read_as_empty_like_the_server(tmp_path, grid):
    files = _clean()
    files["pricing/2026_11.json"] = {"month": "2026_11", **grid}

    proc = _precheck(_write(tmp_path, files))

    assert proc.returncode == 0, proc.stdout


def test_a_provider_space_the_server_skips_is_a_warning(tmp_path):
    files = _clean()
    files["pricing/2026_11.json"] = {"models_by_provider": {"p": ["x"]}}

    proc = _precheck(_write(tmp_path, files))

    assert proc.returncode == 0, proc.stdout
    assert "pricing-space-ignored" in _codes(proc, "WARN")


# ── The meaning changes the script reports, checked against the code ─────


def _matches_4_4_0(allowed, owner, native):
    """plugboard/model_identity.py model_access_matches as tagged v4.4.0."""
    allowed = tuple(allowed)
    return not allowed or native in allowed or f"{owner}:{native}" in allowed


@pytest.mark.parametrize(
    ("entry", "owner", "native", "before", "now"),
    [
        # cli-published-id: refused by 4.4.0, granted now.
        ("claude-code:sonnet", "claude-via-claude-cli", "sonnet", False, True),
        ("Codex:gpt-6", "chatgpt-via-codex-cli", "gpt-6", False, True),
        # dead-entry: "" and "<provider id>:" matched a route without a model.
        ("", "chatgpt-via-codex-cli", "", True, False),
        ("chatgpt-via-codex-cli:", "chatgpt-via-codex-cli", "", True, False),
        # An alias prefix alone granted nothing then and grants nothing now.
        ("Codex:", "chatgpt-via-codex-cli", "", False, False),
        # <provider id>:<model> is the same in both versions.
        ("ollama:qwen3:0.6b", "ollama", "qwen3:0.6b", True, True),
    ],
)
def test_reported_meaning_changes_match_the_code(entry, owner, native, before, now):
    from plugboard.model_identity import model_access_matches

    registered = ("claude-via-claude-cli", "chatgpt-via-codex-cli", "ollama")

    assert _matches_4_4_0([entry, "other:x"], owner, native) is before
    assert model_access_matches([entry, "other:x"], owner, native, registered) is now
