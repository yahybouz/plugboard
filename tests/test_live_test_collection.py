"""The live opt-in applies to external tests, not framework unit tests."""

from pathlib import Path
import shutil

import pytest

pytest_plugins = ["pytester"]

_TESTS_ROOT = Path(__file__).parent


@pytest.mark.parametrize("opt_in", [None, "0", "true", "1"])
def test_only_explicit_live_tests_require_opt_in(pytester, monkeypatch, opt_in):
    """Directory names must not skip offline tests or enable external tests."""
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    if opt_in is None:
        monkeypatch.delenv("PLUGBOARD_RUN_LIVE_TESTS", raising=False)
    else:
        monkeypatch.setenv("PLUGBOARD_RUN_LIVE_TESTS", opt_in)

    shutil.copyfile(_TESTS_ROOT / "conftest.py", pytester.path / "conftest.py")
    framework = pytester.path / "live_model_tests"
    framework.mkdir()
    shutil.copyfile(
        _TESTS_ROOT / "live_model_tests" / "conftest.py", framework / "conftest.py"
    )
    (framework / "test_offline.py").write_text(
        "def test_framework_offline():\n    assert 1 + 1 == 2\n"
    )
    # Bodies record execution; no external service is contacted even on opt-in.
    live_test = (
        "from pathlib import Path\n"
        "import pytest\n"
        "@pytest.mark.live\n"
        "def test_external_probe():\n"
        "    Path(__file__).with_suffix('.executed').touch()\n"
    )
    (framework / "test_external.py").write_text(live_test)
    pytester.makepyfile(test_outside_live=live_test)

    result = pytester.runpytest_inprocess(
        "-q",
        "-p",
        "pytest_asyncio.plugin",
        "--asyncio-mode=auto",
        "-o",
        "asyncio_default_fixture_loop_scope=function",
    )

    enabled = opt_in == "1"
    result.assert_outcomes(passed=3 if enabled else 1, skipped=0 if enabled else 2)
    assert (framework / "test_external.executed").exists() is enabled
    assert (pytester.path / "test_outside_live.executed").exists() is enabled
