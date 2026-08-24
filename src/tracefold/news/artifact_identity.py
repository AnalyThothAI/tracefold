"""Canonical, content-addressed identity primitives for News learning artifacts."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    """Serialize one News artifact exactly the same way in every adapter."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    )


def canonical_sha(value: Any) -> str:
    """Return the SHA-256 identity of ``canonical_json(value)``."""

    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


__all__ = ["canonical_json", "canonical_sha"]
