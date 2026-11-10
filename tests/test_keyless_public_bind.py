"""Keyless mode (API_KEY and ADMIN_KEY empty) never listens on a network.

With both keys empty every route is open, the admin API and the dashboard
included, while ``run.py`` listens on ``0.0.0.0`` when ``HOST`` is unset.
``run.py`` (also behind ``make dev``, the Docker image and Railway) must then
refuse to start unless the interface is loopback.
"""

from __future__ import annotations

import runpy
from pathlib import Path

import dotenv
import pytest
import uvicorn

_RUN_PY = str(Path(__file__).resolve().parents[1] / "run.py")


@pytest.fixture
def calls():
    """Keyword arguments of each uvicorn.run call made by run.py."""
    return []


@pytest.fixture
def start(monkeypatch, calls):
    """Run run.py as ``python run.py``; return the uvicorn.run kwargs."""
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append(kw))
    # setenv first so monkeypatch removes what run.py writes into os.environ.
    monkeypatch.setenv("PLUGBOARD_BIND_HOST", "unset")
    monkeypatch.delenv("PLUGBOARD_BIND_HOST")

    def _start(host, api_key="", admin_key=""):
        if host is None:
            monkeypatch.delenv("HOST", raising=False)
        else:
            monkeypatch.setenv("HOST", host)
        for name, value in (("API_KEY", api_key), ("ADMIN_KEY", admin_key)):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        runpy.run_path(_RUN_PY, run_name="__main__")
        return calls[-1]

    return _start


def _assert_refused(exc_info, calls):
    assert calls == []  # uvicorn never started
    message = str(exc_info.value.code)
    assert exc_info.value.code not in (0, None)
    # The message says what is wrong and both ways out.
    assert "API_KEY" in message and "ADMIN_KEY" in message
    assert "HOST=127.0.0.1" in message
    assert "make init" in message


@pytest.mark.parametrize("host", [None, "0.0.0.0", "::", "192.168.1.10", ""])
@pytest.mark.parametrize("keys", [("", ""), (None, None)])
def test_keyless_start_on_a_network_interface_is_refused(start, calls, host, keys):
    with pytest.raises(SystemExit) as exc:
        start(host, *keys)
    _assert_refused(exc, calls)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_keyless_start_on_loopback_is_allowed(start, host):
    assert start(host)["host"] == host


@pytest.mark.parametrize(
    ("api_key", "admin_key"),
    [
        ("synthetic-api-key", ""),
        ("", "synthetic-admin-key-0123456789"),
        ("synthetic-api-key", "synthetic-admin-key-0123456789"),
    ],
)
def test_one_key_is_enough_to_listen_on_a_network(start, api_key, admin_key):
    # The Docker image listens on 0.0.0.0 with its keys.
    assert start("0.0.0.0", api_key, admin_key)["host"] == "0.0.0.0"


def test_make_dev_is_refused_too(start, calls, monkeypatch):
    # `make dev` = RELOAD=true PLUGBOARD_OPEN_BROWSER=1 python run.py
    monkeypatch.setenv("RELOAD", "true")
    monkeypatch.setenv("PLUGBOARD_OPEN_BROWSER", "1")
    with pytest.raises(SystemExit) as exc:
        start("0.0.0.0")
    _assert_refused(exc, calls)


def test_names_the_server_does_not_read_do_not_count(start, calls, monkeypatch):
    # The gateway reads API_KEY and ADMIN_KEY only: PLUGBOARD_* and the old
    # LLM_ENABLER_* names of the keys (MCP client settings) leave it open,
    # and no bind variable can make 0.0.0.0 look local.
    for name in (
        "PLUGBOARD_API_KEY",
        "PLUGBOARD_ADMIN_KEY",
        "LLM_ENABLER_API_KEY",
        "LLM_ENABLER_ADMIN_KEY",
    ):
        monkeypatch.setenv(name, "synthetic-key-0123456789abcdef")
    monkeypatch.setenv("LLM_ENABLER_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("PLUGBOARD_DEV", "1")
    with pytest.raises(SystemExit) as exc:
        start("0.0.0.0")
    _assert_refused(exc, calls)
