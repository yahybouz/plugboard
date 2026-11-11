"""PLU-7: protected providers never connect to a non-public address."""

from __future__ import annotations

import errno
import socket

import pytest

from plugboard import netguard

NON_PUBLIC = [
    "127.0.0.1",
    "10.0.0.1",
    "172.17.0.1",
    "169.254.169.254",
    "100.64.0.1",
    "198.18.0.1",
    "::1",
    "::ffff:10.0.0.1",
    "64:ff9b::a00:1",
    "2002:a00:1::1",
    "fe80::1%eth0",
]
PUBLIC = ["1.1.1.1", "::ffff:1.1.1.1", "2606:4700::1111"]


def _family(address: str) -> socket.AddressFamily:
    return socket.AF_INET6 if ":" in address else socket.AF_INET


# -- address check and socket guard ------------------------------------------------


@pytest.mark.parametrize("address", NON_PUBLIC)
def test_non_public_is_refused(address):
    assert netguard.ip_is_public(address) is False
    with pytest.raises(netguard.EgressRefused) as exc:
        netguard.refuse_non_public(
            (_family(address), socket.SOCK_STREAM, 6, "", (address, 443))
        )
    assert exc.value.errno == errno.EACCES


@pytest.mark.parametrize("address", PUBLIC)
def test_public_gets_a_socket(address):
    family = _family(address)
    sock = netguard.refuse_non_public(
        (family, socket.SOCK_STREAM, 6, "", (address, 443))
    )
    try:
        assert sock.family == family
    finally:
        sock.close()


def test_llm_pricing_reexports_ip_is_public():
    from plugboard import llm_pricing

    assert llm_pricing.ip_is_public is netguard.ip_is_public
