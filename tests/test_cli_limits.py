"""What a CLI subscription route cannot serve.

A CLI subscription route (``auth_source`` ``subprocess:...``) flattens the
conversation to text: it cannot pass tools, a tool choice, legacy functions, a
structured response format or a non-text content part. ``unsupported_on_cli``
names each such field of a chat body, in a fixed order, so the router and the
chat route can refuse the request instead of dropping the field silently.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from plugboard import cli_limits
from plugboard.cli_limits import UNSUPPORTED_ON_CLI, unsupported_on_cli

TEXT = [{"role": "user", "content": "hi"}]
TOOL = {"type": "function", "function": {"name": "f"}}


def _parts(*parts):
    return {"messages": [{"role": "user", "content": list(parts)}]}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"messages": TEXT},
        {"messages": TEXT, "tools": None},
        {"messages": TEXT, "tools": []},
        {"messages": TEXT, "tool_choice": None},
        {"messages": TEXT, "tool_choice": "none"},
        {"messages": TEXT, "tool_choice": "auto"},
        {"messages": TEXT, "functions": None},
        {"messages": TEXT, "functions": []},
        {"messages": TEXT, "function_call": "none"},
        {"messages": TEXT, "function_call": "auto"},
        {"messages": TEXT, "response_format": None},
        {"messages": TEXT, "response_format": {}},
        {"messages": TEXT, "response_format": {"type": "text"}},
        _parts({"type": "text", "text": "hi"}),
        {"messages": "not a list"},
        {"messages": ["not an object", None, 3]},
        {"messages": [{"role": "user", "content": None}]},
    ],
)
def test_a_text_only_body_has_nothing_unsupported(body):
    assert unsupported_on_cli(body) == []


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"tools": [TOOL]}, ["tools"]),
        ({"tools": "malformed"}, ["tools"]),
        ({"tool_choice": "required"}, ["tool_choice"]),
        ({"tool_choice": TOOL}, ["tool_choice"]),
        ({"functions": [{"name": "f"}]}, ["functions"]),
        ({"function_call": {"name": "f"}}, ["function_call"]),
        ({"response_format": {"type": "json_object"}}, ["response_format"]),
        (
            {"response_format": {"type": "json_schema", "json_schema": {}}},
            ["response_format"],
        ),
        ({"response_format": "json_object"}, ["response_format"]),
        (
            _parts({"type": "image_url", "image_url": {"url": "data:,"}}),
            ["content:image_url"],
        ),
        (
            _parts(
                {"type": "input_audio", "input_audio": {"data": "", "format": "wav"}}
            ),
            ["content:input_audio"],
        ),
        (_parts({"text": "no type"}), ["content:unknown"]),
        (_parts("not an object"), ["content:unknown"]),
        (
            _parts(
                {"type": "image_url", "image_url": {"url": "a"}},
                {"type": "image_url", "image_url": {"url": "b"}},
            ),
            ["content:image_url"],
        ),
    ],
)
def test_each_unsupported_field_is_named(body, expected):
    assert unsupported_on_cli({"messages": TEXT, **body}) == expected


def test_content_types_are_sorted_and_listed_once_across_messages():
    body = {
        "messages": [
            {
                "role": "user",
                "content": [{"type": "input_audio"}, {"type": "text", "text": "a"}],
            },
            {
                "role": "user",
                "content": [{"type": "image_url"}, {"type": "input_audio"}],
            },
        ]
    }
    assert unsupported_on_cli(body) == ["content:image_url", "content:input_audio"]


def test_everything_together_comes_in_the_fixed_order():
    body = {
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": "a"}}],
            }
        ],
        "function_call": {"name": "f"},
        "functions": [{"name": "f"}],
        "tool_choice": "required",
        "tools": [TOOL],
    }
    assert unsupported_on_cli(body) == [
        "tools",
        "tool_choice",
        "functions",
        "function_call",
        "response_format",
        "content:image_url",
    ]


def test_a_body_that_is_not_an_object_never_raises():
    assert unsupported_on_cli(None) == []  # type: ignore[arg-type]
    assert unsupported_on_cli(["tools"]) == []  # type: ignore[arg-type]


def test_the_reason_code_is_stable():
    assert UNSUPPORTED_ON_CLI == "unsupported_on_cli"


def test_the_module_imports_neither_the_router_nor_the_app():
    tree = ast.parse(Path(cli_limits.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported.add(module)
            imported.update(
                f"{module}.{alias.name}".lstrip(".") for alias in node.names
            )
    assert not {name for name in imported if name.split(".")[-1] in {"router", "app"}}
