from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID


def postgres_safe_json(value: Any) -> Any:
    if isinstance(value, str):
        return postgres_safe_text(value)
    if isinstance(value, list):
        return [postgres_safe_json(item) for item in value]
    if isinstance(value, tuple):
        return [postgres_safe_json(item) for item in value]
    if isinstance(value, dict):
        return {str(key).replace("\x00", ""): postgres_safe_json(item) for key, item in value.items()}
    if isinstance(value, datetime | date | UUID):
        return str(value)
    return value


def postgres_safe_text(value: Any) -> str:
    return str(value or "").replace("\x00", "")
