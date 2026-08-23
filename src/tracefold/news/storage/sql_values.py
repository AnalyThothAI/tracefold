"""Shared code-owned SQL values and canonical JSON encoding for News storage."""

from __future__ import annotations

import json
from typing import Any, Final

from ..models import ADMITTED_ADMISSIONS

_JSON_SEPARATORS = (",", ":")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=_JSON_SEPARATORS, default=str)


_ADMITTED_SQL: Final = ", ".join(f"'{value}'" for value in sorted(ADMITTED_ADMISSIONS))
