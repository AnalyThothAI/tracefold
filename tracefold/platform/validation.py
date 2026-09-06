from __future__ import annotations

import math


def require_nonnegative_float(value: object, *, error_code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(error_code)
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(error_code)
    return parsed
