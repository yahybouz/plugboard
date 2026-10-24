"""Bound the string-copy work of nested transport normalization, without load tests."""

from __future__ import annotations

from typing import SupportsIndex

import pytest

from plugboard.decisions import is_decision_model


class _CopyMeter(str):
    copied: int = 0

    def strip(self, chars: str | None = None) -> _CopyMeter:
        value = super().strip(chars)
        type(self).copied += len(value)
        return type(self)(value)

    def lower(self) -> _CopyMeter:
        value = super().lower()
        type(self).copied += len(value)
        return type(self)(value)

    def partition(self, sep: str) -> tuple[_CopyMeter, _CopyMeter, _CopyMeter]:
        first, separator, rest = super().partition(sep)
        type(self).copied += len(first) + len(separator) + len(rest)
        return type(self)(first), type(self)(separator), type(self)(rest)

    def __getitem__(self, key: SupportsIndex | slice) -> _CopyMeter:
        value = super().__getitem__(key)
        if isinstance(key, slice):
            type(self).copied += len(value)
        return type(self)(value)


@pytest.mark.parametrize("repeats", [64, 128, 256])
@pytest.mark.parametrize(
    "suffix,expected", [("gpt-5:kev", False), ("kev-0.8b:latest", True)]
)
def test_parent_nested_alias_string_copy_work_is_linear(repeats, suffix, expected):
    model = _CopyMeter("ollama:" * repeats + suffix)
    _CopyMeter.copied = 0
    assert is_decision_model(model) is expected
    assert _CopyMeter.copied <= 24 * len(model)
