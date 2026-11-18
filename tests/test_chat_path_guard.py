"""Moving the chat path out of app.py changes nothing (PLU-95): same client
routes in the same places, same client contract, same exception handlers and
middleware stack as c3e4cea."""

import json

from tests import chat_path_snapshot


def _expected(key):
    return json.loads(chat_path_snapshot.FIXTURE.read_text(encoding="utf-8"))[key]


def test_client_routes_are_unchanged():
    from plugboard.app import app

    assert chat_path_snapshot.client_routes(app) == _expected("client_routes")


def test_client_contract_is_unchanged():
    assert chat_path_snapshot.client_contract() == _expected("client_contract")


def test_exception_handlers_are_unchanged():
    from plugboard.app import app

    assert chat_path_snapshot.exception_handlers(app) == _expected("exception_handlers")


def test_middleware_stack_is_unchanged():
    from plugboard.app import app

    assert chat_path_snapshot.middleware(app) == _expected("middleware")
