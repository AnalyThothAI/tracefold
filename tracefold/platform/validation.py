from __future__ import annotations

import math


def require_positive_int(value: object, *, error_code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(error_code)
    return value


def require_nonnegative_int(value: object, *, error_code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(error_code)
    return value


def require_positive_float(value: object, *, error_code: str) -> float:
    parsed = _require_finite_float(value, error_code=error_code)
    if parsed <= 0:
        raise ValueError(error_code)
    return parsed


def require_nonnegative_float(value: object, *, error_code: str) -> float:
    parsed = _require_finite_float(value, error_code=error_code)
    if parsed < 0:
        raise ValueError(error_code)
    return parsed


def _require_finite_float(value: object, *, error_code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(error_code)
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(error_code)
    return parsed
