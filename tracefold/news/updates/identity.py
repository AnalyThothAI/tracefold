"""Code-owned content identities; no artifact upgrades or legacy decoders."""
from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any

from pydantic import BaseModel


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(child) for child in value]
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def identity(kind: str, *parts: Any) -> str:
    return f"{kind}:{digest(parts)}"
