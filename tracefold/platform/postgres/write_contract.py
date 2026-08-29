from __future__ import annotations

from typing import Any


def mutation_count(cursor: Any, *, error_code: str) -> int:
    """Return trustworthy mutation evidence from a database cursor."""
    try:
        rowcount: object = cursor.rowcount
    except AttributeError as exc:
        raise TypeError(error_code) from exc
    if isinstance(rowcount, bool) or not isinstance(rowcount, int) or rowcount < 0:
        raise TypeError(error_code)
    return rowcount


def expect_mutation_count(cursor: Any, *, expected: int, error_code: str) -> int:
    """Require an exact mutation count for CAS and RETURNING consistency."""
    rowcount = mutation_count(cursor, error_code=error_code)
    if rowcount != expected:
        raise TypeError(error_code)
    return rowcount


def returning_mutation_count(cursor: Any, row: Any | None, *, error_code: str) -> int:
    """Keep a single-row RETURNING payload consistent with its cursor count."""
    return expect_mutation_count(
        cursor,
        expected=0 if row is None else 1,
        error_code=error_code,
    )
