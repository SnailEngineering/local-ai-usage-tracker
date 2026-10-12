"""Validate external JSON before it reaches arithmetic or SQLite bindings."""

from __future__ import annotations

from datetime import date


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


def day_value(value) -> str:
    """Require a real calendar date in the exact persisted YYYY-MM-DD format."""
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError("expected YYYY-MM-DD")
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError("expected YYYY-MM-DD")
    return value


def token_value(value) -> int:
    if value is None:
        return 0
    # bool is an int subclass; floats and numeric strings are malformed too.
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError("expected a nonnegative SQLite integer token count")
    return value
