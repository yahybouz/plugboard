"""What the operator and integrators read for 4.14.3 (4.14.3 review, B3, B6).

B3: the PROXY protocol option of the production runbook must be complete
enough to apply as written: the whole ``tailscale serve`` command (a TCP
forwarder: Tailscale 1.104.7 refuses ``--proxy-protocol`` on HTTP/HTTPS), the
nginx ``listen`` that keeps ``ssl`` when nginx terminates TLS, the real-IP
directives, ``X-Forwarded-For`` overwritten in every proxying ``location``,
and the check of the production host's Tailscale.

B6: the release publishes one API contract, 3.0.0; the intermediate numbers
of the branch (2.6.0, 2.7.0) were never published.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _section(text: str, title: str) -> str:
    match = re.search(rf"(?ms)^## {re.escape(title)}\n(.*?)(?=^## |\Z)", text)
    assert match, title
    return match.group(1)


def _runbook_section() -> str:
    text = (ROOT / "docs" / "runbooks" / "production-instance.md").read_text(
        encoding="utf-8"
    )
    return _section(text, "Client address and rate limit (BUG-119)")


def _release() -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    return _section(text, "[4.14.3] — 2026-11-15")


def test_option_b_gives_the_whole_tailscale_serve_command():
    section = _runbook_section()
    # Tailscale keeps TLS (as `--https` today), or nginx does.
    assert (
        "tailscale serve --bg --tls-terminated-tcp=<port> --proxy-protocol=2 "
        "tcp://127.0.0.1:<nginx-port>" in section
    )
    assert (
        "tailscale serve --bg --tcp=<port> --proxy-protocol=2 "
        "tcp://127.0.0.1:<nginx-port>" in section
    )
    # The web handler on that port goes first: Tailscale refuses both.
    assert "tailscale serve --https=<port> off" in section
    assert "tailscale serve status --json" in section  # kept to roll back


def test_option_b_nginx_keeps_ssl_and_reads_the_proxy_header():
    section = _runbook_section()
    assert "listen 127.0.0.1:<nginx-port> proxy_protocol;" in section
    assert "listen 127.0.0.1:<nginx-port> ssl proxy_protocol;" in section
    assert "set_real_ip_from 127.0.0.1;" in section
    assert "real_ip_header proxy_protocol;" in section
    # A listener in PROXY protocol refuses a client that sends no header.
    assert "local probe" in " ".join(section.split())


def test_both_options_overwrite_x_forwarded_for_in_every_location():
    section = _runbook_section()
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in section
    assert "every `location`" in " ".join(section.split())
    assert "inherit" in section  # one proxy_set_header in a location drops the rest
    assert "$proxy_add_x_forwarded_for" in section  # named, as what not to use


def test_option_a_gives_its_listen_and_frees_the_port():
    section = _runbook_section()
    assert "listen <tailnet-ip>:<port> ssl;" in section
    assert "tailscale cert" in section


def test_the_production_tailscale_is_checked_before_option_b():
    section = _runbook_section()
    assert "tailscale version" in section
    assert "tailscale serve --help | grep -- --proxy-protocol" in section


def test_the_release_names_one_published_contract():
    release = _release()
    assert "2.6.0" not in release
    assert "2.7.0" not in release
    (note,) = [
        line for line in release.splitlines() if line.startswith("**API contract**")
    ]
    assert "3.0.0" in note and "2.5.0" in note
    for word in ("model_not_found", "invalid_request", "checks"):
        assert word in note, word
