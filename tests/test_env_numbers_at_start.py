"""A numeric setting that is not a number never stops the gateway.

Every numeric setting goes through the shared reader (``envcompat.get_int`` /
``get_float``): a value that is not a finite number gives the default, with
one warning. Settings read when their module is imported are checked in a
fresh interpreter, which is where a bad value used to stop the start.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from plugboard import envcompat

ROOT = Path(__file__).resolve().parents[1]

_PROBE = """
import importlib, json, sys, traceback
from plugboard import envcompat
values, errors = {}, {}
for wanted in sys.argv[1:]:
    module, attr = wanted.split(":")
    try:
        values[wanted] = getattr(importlib.import_module(module), attr)
    except Exception:
        errors[module] = traceback.format_exc(limit=-4)
reported = sorted({name for name, _ in envcompat._bad_numbers})
print("PROBE " + json.dumps({"values": values, "errors": errors, "reported": reported}))
"""


def _start(tmp_path: Path, settings: dict, wanted: list) -> dict:
    """Import the modules of ``wanted`` (``module:attr``) with ``settings``."""
    env = {k: v for k, v in os.environ.items() if k not in settings}
    env.update(PLUGBOARD_SKIP_DOTENV="1", PLUGBOARD_DATA=str(tmp_path), **settings)
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, *wanted],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    line = next(x for x in proc.stdout.splitlines() if x.startswith("PROBE "))
    return json.loads(line[len("PROBE ") :])


# Settings read at import: module -> {variable: (attribute, default)}.
AT_IMPORT = {
    "plugboard.http_pool": {
        "PLUGBOARD_HTTP_POOL_SIZE": ("_CONN_LIMIT", 100),
        "PLUGBOARD_HTTP_POOL_PER_HOST": ("_CONN_PER_HOST", 20),
        "PLUGBOARD_HTTP_DNS_TTL": ("_DNS_TTL", 300),
    },
    "plugboard.cache": {
        "CACHE_TTL": ("_DEFAULT_TTL", 300),
        "CACHE_MAX_SIZE": ("_MAX_CACHE_SIZE", 10000),
    },
    "plugboard.semantic_cache": {
        "SEMANTIC_CACHE_TTL": ("_DEFAULT_TTL", 3600),
        "SEMANTIC_CACHE_MAX_SIZE": ("_MAX_CACHE_SIZE", 5000),
        "SEMANTIC_CACHE_THRESHOLD": ("_DEFAULT_THRESHOLD", 0.85),
        "PLUGBOARD_EMBED_SIMILARITY_THRESHOLD": ("_EMBED_THRESHOLD", 0.92),
        "PLUGBOARD_EMBED_TIMEOUT": ("_EMBED_TIMEOUT", 10.0),
    },
    "plugboard.provider_watchdog": {
        "WATCHDOG_INTERVAL": ("_CHECK_INTERVAL", 30),
        "WATCHDOG_FAILURES": ("_FAILURE_THRESHOLD", 3),
        "WATCHDOG_RESTART_COOLDOWN": ("_RESTART_COOLDOWN", 120),
        "WATCHDOG_MAX_RESTARTS": ("_MAX_RESTARTS", 5),
        "WATCHDOG_HEALTH_TIMEOUT": ("_HEALTH_TIMEOUT", 10),
        "WATCHDOG_DEEP_TIMEOUT": ("_DEEP_CHECK_TIMEOUT", 60),
        "WATCHDOG_DEEP_INTERVAL": ("_DEEP_CHECK_INTERVAL", 3),
    },
    "plugboard.alerts": {"ALERT_FAILURE_THRESHOLD": ("_ALERT_THRESHOLD", 3)},
    "plugboard.ollama": {"OLLAMA_TIMEOUT": ("_OLLAMA_TIMEOUT", 30)},
    "plugboard.app": {
        "PLUGBOARD_CACHE_RESPONSE_FLOOR_MS": ("_CACHE_RESPONSE_FLOOR_MS", 0),
        "ADMIN_SESSION_MAX_AGE": ("_ADMIN_SESSION_MAX_AGE", 28800),
        "MAX_BODY_BYTES": ("_MAX_BODY_BYTES", 10 * 1024 * 1024),
        "PLUGBOARD_BULK_INTERNET_CONCURRENCY": ("_BULK_INTERNET_CONCURRENCY", 3),
        "PLUGBOARD_BULK_INTERNET_PER_MODEL_TIMEOUT_S": (
            "_BULK_INTERNET_PER_MODEL_TIMEOUT_S",
            25.0,
        ),
        "PLUGBOARD_REFRESH_COOLDOWN_S": ("_MODEL_REFRESH_COOLDOWN_S", 60.0),
    },
    "plugboard.ratelimit": {"RATE_LIMIT": ("_LIMIT", 60)},
    "plugboard.cli_subprocess": {
        "PLUGBOARD_SUBPROCESS_LIMIT": ("_CONCURRENCY_LIMIT", 4),
    },
}

# Good values, old names and the bounds each setting applies:
# module -> {variable: (value, attribute, expected)}.
WELL_SET = {
    "plugboard.http_pool": {
        "LLM_ENABLER_HTTP_POOL_SIZE": ("50", "_CONN_LIMIT", 50),
        "PLUGBOARD_HTTP_POOL_PER_HOST": (" 8 ", "_CONN_PER_HOST", 8),
    },
    "plugboard.cache": {"CACHE_TTL": ("60", "_DEFAULT_TTL", 60)},
    "plugboard.semantic_cache": {
        "SEMANTIC_CACHE_THRESHOLD": ("0.9", "_DEFAULT_THRESHOLD", 0.9),
        "EMBED_TIMEOUT": ("7.5", "_EMBED_TIMEOUT", 7.5),  # pre-rename name
    },
    "plugboard.provider_watchdog": {"WATCHDOG_INTERVAL": ("45", "_CHECK_INTERVAL", 45)},
    "plugboard.alerts": {"ALERT_FAILURE_THRESHOLD": ("5", "_ALERT_THRESHOLD", 5)},
    "plugboard.ollama": {"OLLAMA_TIMEOUT": ("90", "_OLLAMA_TIMEOUT", 90)},
    "plugboard.app": {
        "PLUGBOARD_CACHE_RESPONSE_FLOOR_MS": ("-5", "_CACHE_RESPONSE_FLOOR_MS", 0),
        "MAX_BODY_BYTES": ("2048", "_MAX_BODY_BYTES", 2048),
        "PLUGBOARD_BULK_INTERNET_CONCURRENCY": ("0", "_BULK_INTERNET_CONCURRENCY", 1),
        "PLUGBOARD_BULK_INTERNET_PER_MODEL_TIMEOUT_S": (
            "2",
            "_BULK_INTERNET_PER_MODEL_TIMEOUT_S",
            5.0,
        ),
        "LLM_ENABLER_REFRESH_COOLDOWN_S": ("30", "_MODEL_REFRESH_COOLDOWN_S", 30.0),
    },
    "plugboard.ratelimit": {"RATE_LIMIT": ("120", "_LIMIT", 120)},
    "plugboard.cli_subprocess": {
        "LLM_ENABLER_SUBPROCESS_LIMIT": ("0", "_CONCURRENCY_LIMIT", 1),
    },
}


@pytest.fixture(scope="module")
def bad_start(tmp_path_factory):
    # A whole number gets text, a decimal one infinity: both raised or went
    # through before.
    settings = {
        name: "inf" if isinstance(default, float) else "abc"
        for spec in AT_IMPORT.values()
        for name, (_, default) in spec.items()
    }
    wanted = [
        f"{module}:{attr}"
        for module, spec in AT_IMPORT.items()
        for attr, _ in spec.values()
    ]
    return _start(tmp_path_factory.mktemp("bad-start"), settings, wanted)


@pytest.fixture(scope="module")
def good_start(tmp_path_factory):
    settings = {
        name: raw for spec in WELL_SET.values() for name, (raw, _, _) in spec.items()
    }
    wanted = [
        f"{module}:{attr}"
        for module, spec in WELL_SET.items()
        for _, attr, _ in spec.values()
    ]
    return _start(tmp_path_factory.mktemp("good-start"), settings, wanted)


@pytest.mark.parametrize("module", sorted(AT_IMPORT))
def test_bad_value_read_at_import_gives_the_default(bad_start, module):
    assert module not in bad_start["errors"], bad_start["errors"][module]
    spec = AT_IMPORT[module]
    found = {
        name: bad_start["values"][f"{module}:{a}"] for name, (a, _) in spec.items()
    }

    assert found == {name: default for name, (_, default) in spec.items()}
    assert set(spec) <= set(bad_start["reported"])  # one warning each


@pytest.mark.parametrize("module", sorted(WELL_SET))
def test_good_value_read_at_import_is_kept(good_start, module):
    assert module not in good_start["errors"], good_start["errors"][module]
    spec = WELL_SET[module]
    found = {
        name: good_start["values"][f"{module}:{a}"] for name, (_, a, _) in spec.items()
    }

    assert found == {name: expected for name, (_, _, expected) in spec.items()}
    assert good_start["reported"] == []


# -- settings read at each use ----------------------------------------------------


@pytest.fixture
def warnings_seen(monkeypatch, caplog):
    """Each test sees its own warnings, whatever ran before it."""
    monkeypatch.setattr(envcompat, "_bad_numbers", set())
    caplog.set_level("WARNING", logger="plugboard.envcompat")
    return caplog


@pytest.mark.parametrize(
    "raw, expected",
    [("abc", 5.0), ("inf", 5.0), ("nan", 5.0), ("-1", 5.0), ("12.5", 12.5)],
)
def test_ollama_free_space_floor(monkeypatch, warnings_seen, raw, expected):
    # inf made min_free_bytes() raise OverflowError on every model download.
    from plugboard import ollama_admin

    monkeypatch.setenv("PLUGBOARD_OLLAMA_MIN_FREE_GB", raw)

    assert ollama_admin.min_free_gb() == expected
    assert ollama_admin.min_free_bytes() == int(expected * ollama_admin.GB)
    if expected == 5.0:
        assert "PLUGBOARD_OLLAMA_MIN_FREE_GB" in warnings_seen.text


@pytest.fixture
def app():
    """The app, imported before the test runs: its first import sets up the
    gateway's logging, which drops every root handler, caplog's included."""
    from plugboard import app

    return app


@pytest.mark.parametrize(
    "raw, expected", [("abc", 60), ("inf", 60), ("-5", 0), ("30", 30)]
)
def test_default_rpm_limit_of_a_new_key(app, monkeypatch, warnings_seen, raw, expected):
    monkeypatch.setenv("PLUGBOARD_DEFAULT_RPM_LIMIT", raw)

    assert app._default_rpm_limit() == expected
    if raw in ("abc", "inf"):
        assert "PLUGBOARD_DEFAULT_RPM_LIMIT" in warnings_seen.text


@pytest.mark.parametrize(
    "raw, expected",
    [("abc", 10.0), ("inf", 10.0), ("nan", 10.0), ("-1", 0.0), ("2.5", 2.5)],
)
def test_default_monthly_limit_of_a_new_key(
    app, monkeypatch, warnings_seen, raw, expected
):
    # inf gave new keys no monthly limit, and nan gave 0, which disables it.
    monkeypatch.setenv("PLUGBOARD_DEFAULT_MONTHLY_LIMIT_USD", raw)

    assert app._default_monthly_limit_usd() == expected
    if raw in ("abc", "inf", "nan"):
        assert "PLUGBOARD_DEFAULT_MONTHLY_LIMIT_USD" in warnings_seen.text


@pytest.mark.parametrize(
    "reader, variable, raw, expected",
    [
        ("_concurrency", "PLUGBOARD_EMBEDDING_CONCURRENCY", "abc", 2),
        ("_concurrency", "PLUGBOARD_EMBEDDING_CONCURRENCY", "inf", 2),
        ("_concurrency", "PLUGBOARD_EMBEDDING_CONCURRENCY", "8", 8),
        ("_timeout", "PLUGBOARD_EMBEDDING_TIMEOUT_S", "abc", 60.0),
        ("_timeout", "PLUGBOARD_EMBEDDING_TIMEOUT_S", "nan", 60.0),
        ("_timeout", "PLUGBOARD_EMBEDDING_TIMEOUT_S", "30", 30.0),
    ],
)
def test_embedding_engine_limits(
    monkeypatch, warnings_seen, reader, variable, raw, expected
):
    # Values out of range still give the default, as before (other tests).
    from plugboard import embeddings_api

    monkeypatch.setenv(variable, raw)

    assert getattr(embeddings_api, reader)() == expected
    if raw in ("abc", "inf", "nan"):
        assert variable in warnings_seen.text  # it fell back in silence
