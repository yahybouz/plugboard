"""Pre-rename environment variable names keep working."""

from __future__ import annotations

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
