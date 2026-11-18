"""admin_mfa.matching_step: the TOTP step a code belongs to (PB-AUTH-01)."""

import pytest

from plugboard import admin_mfa

SECRET = admin_mfa.generate_secret()
T = 1_800_000_000.0


def test_matching_step_finds_the_current_step():
    code = admin_mfa._generate_code(SECRET, at=T)
    assert admin_mfa.matching_step(SECRET, code, at=T) == int(T // 30)


def test_matching_step_tolerates_one_step_of_skew():
    previous = admin_mfa._generate_code(SECRET, at=T - 30)
    assert admin_mfa.matching_step(SECRET, previous, at=T) == int(T // 30) - 1


@pytest.mark.parametrize("code", ["", "12345", "1234567", "abcdef", " 12345"])
def test_matching_step_refuses_malformed_codes(code):
    assert admin_mfa.matching_step(SECRET, code, at=T) is None


def test_matching_step_refuses_an_old_code():
    old = admin_mfa._generate_code(SECRET, at=T - 90)
    assert admin_mfa.matching_step(SECRET, old, at=T) is None


def test_matching_step_refuses_a_secret_that_is_not_base32():
    assert admin_mfa.matching_step("not base32 !", "123456", at=T) is None
