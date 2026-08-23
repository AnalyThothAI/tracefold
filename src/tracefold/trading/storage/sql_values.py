"""Shared SQL value encoding for Trading storage modules."""

from __future__ import annotations

import json
from typing import Any

_JSON_SEPARATORS = (",", ":")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=_JSON_SEPARATORS, default=str)


__all__: list[str] = []
