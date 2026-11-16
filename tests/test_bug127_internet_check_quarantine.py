"""BUG-127: the internet check quarantines only a refused model.

The check put a model in quarantine for good as soon as its error held a
word such as ``key``, ``exited``, ``401`` or ``403``: an expired key, a CLI
that exited in error or an account the provider turned away removed the
model, although nothing was learnt about it. Now it is quarantined only when
the reason says the model is refused or does not exist, as the router
decides (BUG-90): any other failure answers ``"status": "error"`` and records
nothing, neither in the quarantine nor in ``providers.json``.
"""

from __future__ import annotations

import pytest

import plugboard.app as app_module
from plugboard import model_quarantine as mq
from plugboard.cli_subprocess import CliSubprocessError
from plugboard.router import ProviderHTTPError

PROVIDER = "check-provider"
MODEL = "check-model"


@pytest.fixture
def qfile():
    path = mq._QUARANTINE_FILE
    path.unlink(missing_ok=True)
    yield path
    path.unlink(missing_ok=True)


def _check(monkeypatch, error: Exception):
    async def fail(body, timeout=20.0):
        raise error

    def no_write(*args, **kwargs):  # pragma: no cover - the assertion itself
        raise AssertionError("a failed check must not write providers.json")

    monkeypatch.setattr(app_module, "forward", fail)
    monkeypatch.setattr(app_module, "update_provider", no_write)
    monkeypatch.setattr(
        app_module,
        "resolve_provider_and_model",
        lambda mid: ({"id": PROVIDER, "models": [{"id": MODEL}]}, MODEL),
    )
    return app_module.check_model_internet_access(
        {"model_id": f"{PROVIDER}:{MODEL}", "persist": True}, _=None
    )


def _entry():
    state = mq.get_state(mq._QUARANTINE_FILE)
    return (state.get("providers") or {}).get(PROVIDER, {}).get(MODEL)


def _http(status: int, body: str) -> ProviderHTTPError:
    return ProviderHTTPError(
        f"[{PROVIDER}] HTTP {status}: {body}", provider_id=PROVIDER, status=status
    )


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            _http(400, '{"error": "missing API key header"}'),
            id="key",
        ),
        pytest.param(
            CliSubprocessError(
                "CLI 'claude' exited with code 1: unexpected end of input",
                exit_code=1,
                provider_name="claude",
                model_id=MODEL,
            ),
            id="exited",
        ),
        pytest.param(_http(401, '{"error": "Unauthorized"}'), id="401"),
        pytest.param(
            _http(403, '{"error": {"message": "Forbidden: account on hold"}}'),
            id="403",
        ),
        pytest.param(_http(404, "404 page not found"), id="404-not-the-model"),
    ],
)
async def test_a_failure_that_names_no_refused_model_quarantines_nothing(
    qfile, monkeypatch, error
):
    out = await _check(monkeypatch, error)

    assert out["status"] == "error", out
    assert out["internet_access"] is False
    assert out["error"]
    assert _entry() is None
    assert MODEL not in mq.get_quarantined_suffixes(qfile, provider_id=PROVIDER)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(_http(404, '{"code": "model_not_found"}'), id="model_not_found"),
        pytest.param(
            _http(
                404, f'{{"error": {{"message": "The model `{MODEL}` does not exist"}}}}'
            ),
            id="does-not-exist",
        ),
        pytest.param(
            _http(
                404,
                f'{{"error": "model \\"{MODEL}\\" not found, try pulling it first"}}',
            ),
            id="404-model-not-found",
        ),
        pytest.param(
            CliSubprocessError(
                "CLI 'codex' exited with code 1: The model is not supported "
                "when using Codex with a ChatGPT account.",
                exit_code=1,
                provider_name="codex",
                model_id=MODEL,
            ),
            id="account-unsupported",
        ),
        pytest.param(
            _http(403, '{"error": {"status": "PERMISSION_DENIED"}}'),
            id="permission-denied",
        ),
    ],
)
async def test_a_refused_model_is_still_quarantined(qfile, monkeypatch, error):
    out = await _check(monkeypatch, error)

    assert out["status"] == "quarantined", out
    assert MODEL in mq.get_quarantined_suffixes(qfile, provider_id=PROVIDER)
