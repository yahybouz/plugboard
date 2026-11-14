"""``docs/guide/configuration.md`` lists every environment variable the code reads.

The code is scanned with ``ast`` (``plugboard/``, ``scripts/*.py``, ``run.py``):

* every ``PLUGBOARD_*`` string literal, since that prefix is reserved for
  environment variables;
* the literal or module-constant name passed to an environment read
  (``os.environ.get/pop/setdefault``, ``os.getenv``, ``os.environ[...]``,
  ``... in os.environ``, ``envcompat.get``, a helper whose name contains
  ``env``), and the older names passed next to a ``PLUGBOARD_*`` name.

A name is documented when the guide shows it in backticks. Names built at run
time (``scripts/setup_railway_providers.py`` reads its keys from a table) are
not seen by the scan.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "guide" / "configuration.md"

ENV_NAME = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$|^[A-Z]{4,}$")
PREFIXED = re.compile(r"^PLUGBOARD_[A-Z0-9_]*[A-Z0-9]$")

# Read by the code but not operator configuration.
NOT_CONFIGURATION = {
    # Set by plugboard/decision_runtime.py for the worker it starts.
    "TMPDIR": "scripts/decision_worker.py",
    # The key the load bench sends; read by the operator's script, not the
    # server (PLU-17).
    "PLUGBOARD_BENCH_KEY": "scripts/bench_concurrency.py",
    # The gateway the latency bench measures; also the operator's script (INC-2).
    "PLUGBOARD_BENCH_URL": "scripts/bench_latence.py",
    # The recipe's own gateway, admin key and TOTP code (scripts/recette_services.py).
    "PLUGBOARD_RECETTE_URL": "scripts/recette_services.py",
    "PLUGBOARD_RECETTE_ADMIN_KEY": "scripts/recette_services.py",
    "PLUGBOARD_RECETTE_TOTP": "scripts/recette_services.py",
}


def _source_files() -> list[Path]:
    return sorted(
        [
            *(ROOT / "plugboard").rglob("*.py"),
            *(ROOT / "scripts").glob("*.py"),
            ROOT / "run.py",
        ]
    )


def _attr_name(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _strings(node: ast.AST, constants: dict) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Name) and node.id in constants:
        return [constants[node.id]]
    if isinstance(node, (ast.Tuple, ast.List)):
        return [s for item in node.elts for s in _strings(item, constants)]
    return []


def _is_environ(node: ast.AST) -> bool:
    return _attr_name(node).endswith("environ")


def variables_read(source: str) -> set[str]:
    tree = ast.parse(source)
    constants = {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if PREFIXED.match(node.value):
                names.add(node.value)
        elif isinstance(node, ast.Call):
            func = node.func
            owner = _attr_name(func.value) if isinstance(func, ast.Attribute) else ""
            called = _attr_name(func)
            args = [s for arg in node.args for s in _strings(arg, constants)]
            if (
                (owner.endswith("environ") and called in {"get", "pop", "setdefault"})
                or called == "getenv"
                or (owner == "envcompat" and called in {"get", "get_int", "get_float"})
                or "env" in called.lower()
                or any(PREFIXED.match(arg) for arg in args)
            ):
                names.update(arg for arg in args if ENV_NAME.match(arg))
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Load)
            and _is_environ(node.value)
        ):
            names.update(
                s for s in _strings(node.slice, constants) if ENV_NAME.match(s)
            )
        elif isinstance(node, ast.Compare) and any(
            isinstance(op, (ast.In, ast.NotIn)) and _is_environ(right)
            for op, right in zip(node.ops, node.comparators)
        ):
            names.update(s for s in _strings(node.left, constants) if ENV_NAME.match(s))
    return names


def test_scan_sees_each_way_the_code_reads_a_variable():
    source = """
import os
from plugboard import envcompat
NAMED_ENV = "SOME_CONSTANT_NAME"
os.environ.get("VIA_GET")
os.getenv("VIA_GETENV")
os.environ["VIA_SUBSCRIPT"]
"VIA_MEMBERSHIP" in os.environ
os.environ.get(NAMED_ENV)
envcompat.get("PLUGBOARD_NEW", "OLD_NAME")
_hours("PLUGBOARD_HOURS", ("OLDER_HOURS",), 6)
_env_int("VIA_HELPER", 3)
envcompat.get_int("VIA_GET_INT", 1)
envcompat.get_float("VIA_GET_FLOAT", 0.5)
LABEL = "not a variable"
"""
    assert variables_read(source) == {
        "VIA_GET",
        "VIA_GETENV",
        "VIA_SUBSCRIPT",
        "VIA_MEMBERSHIP",
        "SOME_CONSTANT_NAME",
        "PLUGBOARD_NEW",
        "OLD_NAME",
        "PLUGBOARD_HOURS",
        "OLDER_HOURS",
        "VIA_HELPER",
        "VIA_GET_INT",
        "VIA_GET_FLOAT",
    }


def test_configuration_guide_lists_every_variable_the_code_reads():
    guide = GUIDE.read_text(encoding="utf-8")
    read_by: dict[str, list[str]] = {}
    for path in _source_files():
        for name in variables_read(path.read_text(encoding="utf-8")):
            read_by.setdefault(name, []).append(str(path.relative_to(ROOT)))

    missing = {
        name: files
        for name, files in sorted(read_by.items())
        if f"`{name}`" not in guide and name not in NOT_CONFIGURATION
    }

    assert not missing, (
        f"Add these variables to {GUIDE.relative_to(ROOT)} "
        f"(or to NOT_CONFIGURATION with the reason): {missing}"
    )


def test_exemptions_are_still_read_by_the_code():
    for name, script in NOT_CONFIGURATION.items():
        assert name in variables_read((ROOT / script).read_text(encoding="utf-8"))
