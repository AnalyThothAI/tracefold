"""Reading an optional number off a stored row, once.

A `jsonb` field and a nullable column both arrive as `Any`, and both read models -- the feed's verdict
projection and Market Review's Reaction projection -- answered the same question about them: give me
this value as an `int`/`float`, or `None` when it is absent or is not a number. Two copies of that
answer disagreed on `bool`, which is an `int` in Python and never a magnitude, a confidence or a
basis-point return in any row this repository stores. One definition, and it refuses `bool`.

Nothing here coerces a stored fact into a different one: an unparseable value becomes `None`, which is
what both read surfaces already report for a field their row does not carry.
"""

from __future__ import annotations

from typing import Any


def optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = ["optional_float", "optional_int"]
