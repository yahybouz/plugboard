"""Pre-rename environment variable names keep working."""

from __future__ import annotations

import pytest

from plugboard import envcompat


def test_legacy_names_are_copied_to_the_new_prefix():
    env = {"LLM_ENABLER_DATA": "/srv/data", "HOST": "127.0.0.1"}

    copied = envcompat.apply(env)

    assert copied == ["LLM_ENABLER_DATA"]
    assert env["PLUGBOARD_DATA"] == "/srv/data"


def test_new_names_win_over_legacy_ones():
    env = {"LLM_ENABLER_RATE": "1", "PLUGBOARD_RATE": "0"}

    assert envcompat.apply(env) == []
    assert env["PLUGBOARD_RATE"] == "0"


def test_legacy_dev_flag_is_not_carried_over():
    # PLUGBOARD_DEV=1 lets the server start without ADMIN_KEY; an old .env
    # with LLM_ENABLER_DEV=1 (which only quietened a warning) must not do that.
    env = {"LLM_ENABLER_DEV": "1"}

    assert envcompat.apply(env) == []
    assert "PLUGBOARD_DEV" not in env


def test_old_prefix_is_the_pre_rename_name():
    assert envcompat.OLD_PREFIX == "LLM_ENABLER_"


def test_conflicting_values_are_reported(caplog):
    env = {"LLM_ENABLER_TIMEOUT_S": "5", "PLUGBOARD_TIMEOUT_S": "9"}

    with caplog.at_level("WARNING", logger="plugboard.envcompat"):
        envcompat.apply(env)

    assert env["PLUGBOARD_TIMEOUT_S"] == "9"
    assert "LLM_ENABLER_TIMEOUT_S and PLUGBOARD_TIMEOUT_S" in caplog.text


def test_legacy_auto_login_opt_out_wins():
    # An operator who disabled auto-login under the old name keeps it off,
    # even when a regenerated .env sets the new name to "auto".
    env = {"LLM_ENABLER_LOCAL_AUTO_LOGIN": "0", "PLUGBOARD_LOCAL_AUTO_LOGIN": "auto"}

    envcompat.apply(env)

    assert env["PLUGBOARD_LOCAL_AUTO_LOGIN"] == "0"


def test_legacy_auto_login_opt_in_does_not_override_new_opt_out():
    env = {"LLM_ENABLER_LOCAL_AUTO_LOGIN": "1", "PLUGBOARD_LOCAL_AUTO_LOGIN": "0"}

    envcompat.apply(env)

    assert env["PLUGBOARD_LOCAL_AUTO_LOGIN"] == "0"


def test_version_file_matches_package_version():
    from pathlib import Path

    import plugboard

    version_file = Path(__file__).resolve().parent.parent / "VERSION"
    assert version_file.read_text().strip() == plugboard.__version__


# -- numbers: one reader for every numeric setting -------------------------------


@pytest.fixture(autouse=True)
def _fresh_number_warnings(monkeypatch):
    """Each test sees its own warnings, whatever ran before it."""
    monkeypatch.setattr(envcompat, "_bad_numbers", set())


def test_number_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("PB_TEST_INT", " 12 ")
    monkeypatch.setenv("PB_TEST_FLOAT", "1.75")

    assert envcompat.get_int("PB_TEST_INT", default=3) == 12
    assert envcompat.get_float("PB_TEST_FLOAT", default=0.3) == 1.75


def test_unset_or_blank_number_gives_the_default(monkeypatch):
    monkeypatch.delenv("PB_TEST_INT", raising=False)
    monkeypatch.setenv("PB_TEST_FLOAT", "   ")

    assert envcompat.get_int("PB_TEST_INT", default=3) == 3
    assert envcompat.get_float("PB_TEST_FLOAT", default=0.5) == 0.5


@pytest.mark.parametrize(
    "reader, raw, default",
    [
        ("get_int", "oops", 7),
        ("get_int", "1.5", 7),  # not a whole number
        ("get_float", "nope", 1.5),
        ("get_float", "nan", 1.5),  # passes every comparison
    ],
)
def test_invalid_number_gives_the_default_and_a_warning(
    monkeypatch, caplog, reader, raw, default
):
    monkeypatch.setenv("PB_TEST_BAD", raw)

    with caplog.at_level("WARNING", logger="plugboard.envcompat"):
        assert getattr(envcompat, reader)("PB_TEST_BAD", default=default) == default

    assert "PB_TEST_BAD" in caplog.text


@pytest.mark.parametrize(
    "reader, raw, default",
    [
        ("get_float", "inf", 1.5),
        ("get_float", "-inf", 1.5),
        ("get_float", "+Infinity", 1.5),
        ("get_float", "1e999", 1.5),  # too large: read as infinity
        ("get_float", "-nan", 1.5),
        ("get_int", "inf", 7),
        ("get_int", "-inf", 7),
        ("get_int", "nan", 7),
    ],
)
def test_infinite_number_gives_the_default_and_a_warning(
    monkeypatch, caplog, reader, raw, default
):
    # Infinity passes every lower bound: a cap set to inf is no cap, and
    # int(inf) raises OverflowError where the setting is used.
    monkeypatch.setenv("PB_TEST_INF", raw)

    with caplog.at_level("WARNING", logger="plugboard.envcompat"):
        assert getattr(envcompat, reader)("PB_TEST_INF", default=default) == default

    assert "PB_TEST_INF" in caplog.text


def test_infinity_outside_the_bounds_gives_the_default(monkeypatch):
    monkeypatch.setenv("PB_TEST_FLOAT", "-inf")
    assert envcompat.get_float("PB_TEST_FLOAT", default=0.3, minimum=0.0) == 0.3
    monkeypatch.setenv("PB_TEST_FLOAT", "inf")
    assert envcompat.get_float("PB_TEST_FLOAT", default=20.0, above=0) == 20.0


def test_very_long_whole_number_is_read_without_error(monkeypatch):
    # Too large for a float: checking it as one raised OverflowError.
    monkeypatch.setenv("PB_TEST_INT", "9" * 400)

    assert envcompat.get_int("PB_TEST_INT", default=3) == int("9" * 400)


def test_infinite_model_cap_does_not_stop_the_llm_pricing_pass(monkeypatch):
    # int(inf) raised OverflowError: the pass stopped with an error.
    from plugboard import llm_pricing

    monkeypatch.setenv("PLUGBOARD_LLM_PRICING_MAX_MODELS", "inf")

    assert llm_pricing.max_models_per_pass() == 20


def test_infinite_budget_does_not_lift_the_llm_pricing_cap(monkeypatch):
    from plugboard import llm_pricing

    monkeypatch.setenv("PLUGBOARD_LLM_PRICING_MAX_USD", "inf")

    assert llm_pricing.max_usd_per_day() == 0.50


def test_infinite_drift_threshold_still_holds_price_changes(monkeypatch):
    # inf held no price change for review, however large.
    from plugboard import pricing_merge

    monkeypatch.setenv("PLUGBOARD_PRICE_DRIFT_PCT", "inf")

    assert pricing_merge.drift_threshold_pct() == 20.0


def test_invalid_number_is_reported_once(monkeypatch, caplog):
    monkeypatch.setenv("PB_TEST_ONCE", "twice?")

    with caplog.at_level("WARNING", logger="plugboard.envcompat"):
        envcompat.get_int("PB_TEST_ONCE", default=1)
        envcompat.get_int("PB_TEST_ONCE", default=1)

    assert caplog.text.count("PB_TEST_ONCE") == 1


def test_bounds_give_the_default_outside_them(monkeypatch):
    monkeypatch.setenv("PB_TEST_FLOAT", "-2")
    assert envcompat.get_float("PB_TEST_FLOAT", default=0.3, minimum=0.0) == 0.3
    monkeypatch.setenv("PB_TEST_FLOAT", "0")
    assert envcompat.get_float("PB_TEST_FLOAT", default=0.3, minimum=0.0) == 0.0
    # ``above`` excludes the bound itself: a zero timeout is no timeout.
    assert envcompat.get_float("PB_TEST_FLOAT", default=20.0, above=0) == 20.0

    monkeypatch.setenv("PB_TEST_INT", "-1")
    assert envcompat.get_int("PB_TEST_INT", default=5) == -1  # the caller clamps
    assert envcompat.get_int("PB_TEST_INT", default=5, minimum=0) == 5


def test_number_reader_accepts_legacy_names(monkeypatch):
    monkeypatch.delenv("PLUGBOARD_TEST_HOURS", raising=False)
    monkeypatch.setenv("TEST_HOURS", "4")

    assert envcompat.get_int("PLUGBOARD_TEST_HOURS", "TEST_HOURS", default=6) == 4


def test_nan_budget_does_not_lift_the_llm_pricing_cap(monkeypatch):
    # NaN passed the "cannot be negative" check and compares false with any
    # spend: the daily budget of the LLM pricing pass was then never reached.
    from plugboard import llm_pricing

    monkeypatch.setenv("PLUGBOARD_LLM_PRICING_MAX_USD", "nan")

    assert llm_pricing.max_usd_per_day() == 0.50


def test_numeric_settings_share_the_one_reader():
    import plugboard.app as app_mod
    from plugboard import llm_pricing, model_availability, models_dev
    from plugboard import pricing_scheduler, router

    copies = {
        app_mod: ("_env_int", "_env_float"),
        llm_pricing: ("_float_env",),
        model_availability: ("_env_int", "_env_float"),
        models_dev: ("_env_float",),
        pricing_scheduler: ("_env_int",),
        router: ("_read_env_float",),
    }
    left = [
        f"{module.__name__}.{name}"
        for module, names in copies.items()
        for name in names
        if hasattr(module, name)
    ]
    assert left == []
