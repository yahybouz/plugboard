"""Logs never show a personal dashboard key nor its TOTP secret (PB-AUTH-01)."""

import json
import logging
import os
import secrets
import subprocess
import sys

import pytest

from plugboard import admin_mfa
from plugboard.log_redact import RedactingFilter, redact
from tests.test_bug143_redaction_bounded_time import (
    CHILD,
    CHILD_TIMEOUT_S,
    LIMIT_S,
    N,
    ROOT,
)


def test_a_personal_key_is_masked():
    key = "pba_" + secrets.token_urlsafe(32)
    out = redact(f"refused bearer {key} from 203.0.113.9")
    assert key not in out and "pba_***REDACTED***" in out


def test_the_otpauth_secret_is_masked():
    secret = admin_mfa.generate_secret()
    uri = admin_mfa.provisioning_uri(secret, account="person-one")
    assert secret not in redact(f"uri {uri}")


@pytest.mark.parametrize(
    "template",
    [
        "{key}",
        "key={key}",
        '{{"key": "{key}"}}',
        "Bearer {key}, retry",
        "({key})",
        # Glued to a word character, so no word boundary before "pba_".
        "id_{key}",
        "x{key}",
        # URL-encoded separators end with a word character too.
        "Bearer%20{key}",
        "GET /x?auth=Bearer%20{key}",
        "%22{key}%22",
        "key%3D{key}",
    ],
)
def test_a_personal_key_is_masked_wherever_it_sits(template):
    # Keys made of dashes and underscores only at the edges included.
    for key in (
        "pba_" + secrets.token_urlsafe(32),
        "pba_-" + secrets.token_urlsafe(32),
        "pba_" + secrets.token_urlsafe(32) + "_",
    ):
        out = redact(template.format(key=key))
        assert key not in out and key[4:] not in out


@pytest.mark.parametrize("length", [8, 12, 26, 31])
def test_a_cut_personal_key_is_masked(length):
    cut = "pba_" + secrets.token_urlsafe(32)[:length]
    out = redact(f"upstream said: {cut}")
    assert cut not in out and "pba_***REDACTED***" in out


@pytest.mark.parametrize("word", ["pbar", "pba_x", "pba_abc", "pba_abcdefg", "pbar_x"])
def test_ordinary_words_are_left_alone(word):
    assert redact(f"see {word} here") == f"see {word} here"


def test_the_display_prefix_stays_readable():
    # key_prefix (10 characters) is shown to admins and in the audit: kept.
    prefix = "pba_" + secrets.token_urlsafe(32)[:6]
    assert redact(f"key {prefix}") == f"key {prefix}"


def test_the_log_filter_masks_a_personal_key(caplog):
    key = "pba_" + secrets.token_urlsafe(32)
    logger = logging.getLogger("tests.admin_people_redaction")
    logger.addFilter(RedactingFilter())
    try:
        with caplog.at_level(logging.INFO, logger=logger.name):
            logger.info("auth refused key=%s", key)
    finally:
        logger.filters.clear()
    assert key not in caplog.text and "pba_***REDACTED***" in caplog.text


HOSTILE = {
    "pba_ repeated": "'pba_' * (n // 4)",
    "pba_ then word": "'pba_' + 'a' * n",
    "pba_ then dashes": "'pba_' + '-' * n",
    "pba_ short runs": "('pba_' + 'a' * 31 + ' ') * (n // 36)",
    "pba_ glued": "'xpba_' * (n // 5)",
    "id_pba_ repeated": "'id_pba_' * (n // 7)",
    "encoded quote pba_ repeated": "'%22pba_' * (n // 7)",
    "pba_ runs of 7": "('pba_' + 'a' * 7 + ' ') * (n // 12)",
    "pba_ then mixed": "'pba_' + 'a-_' * (n // 3)",
    "pba_ in a long word": "'a' * n + 'pba_'",
}


@pytest.mark.parametrize("expr", list(HOSTILE.values()), ids=list(HOSTILE))
def test_the_personal_key_pattern_takes_bounded_time(expr):
    env = dict(os.environ, PLUGBOARD_SKIP_DOTENV="1")
    try:
        done = subprocess.run(
            [sys.executable, "-c", CHILD, str(N), expr],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=CHILD_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"redact() still running after {CHILD_TIMEOUT_S} s")
    assert done.returncode == 0, done.stderr[-800:]
    result = json.loads(done.stdout)
    assert result["len"] >= N - 40
    assert result["s"] < LIMIT_S, f"{result['s']:.2f} s for {result['len']} chars"
