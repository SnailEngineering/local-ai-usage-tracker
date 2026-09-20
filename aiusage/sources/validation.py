"""Validate external JSON before it reaches arithmetic or SQLite bindings."""

from __future__ import annotations


def object_value(value, *, optional: bool = False) -> dict:
    if value is None and optional:
        return {}
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def text_value(value) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError("expected text or null")
    return value


def token_value(value) -> int:
    if value is None:
        return 0
    # bool is an int subclass; floats and numeric strings are malformed too.
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError("expected a nonnegative SQLite integer token count")
    return value
