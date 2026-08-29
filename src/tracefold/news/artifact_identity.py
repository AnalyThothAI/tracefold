"""Canonical, content-addressed identity primitives for News learning artifacts.

It also owns the one bound every retained learning artifact is held to: a payload must be finite JSON. That
lived beside the trusted compiler while the compiler was the only thing producing retained artifacts; #202
made the offline optimizer produce them directly, so the rule moved to the module that already decides what
an artifact *is*. #319 removed the credential scanner that sat beside it — that one defended against a
hostile payload in a system with no hostile author, while this one defends the hash, since a non-finite
float has no canonical form to be addressed by.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
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


def reject_nonfinite_json(value: Any, *, path: str = "payload") -> None:
    """Refuse a payload that cannot round-trip as canonical JSON.

    Kept through #319's threat-model cut, and not because a payload could be hostile: a non-finite float
    has no canonical JSON form, so it would break the hash every content-addressed artifact is identified
    by. The credential scanners that used to sit beside it are gone.
    """

    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"news_program_compile_nonfinite_value:{path}")
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError(f"news_program_compile_non_string_key:{path}")
            reject_nonfinite_json(child, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            reject_nonfinite_json(child, path=f"{path}[{index}]")
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise TypeError(f"news_program_compile_non_json_value:{path}:{type(value).__name__}")


__all__ = [
    "canonical_json",
    "canonical_sha",
    "reject_nonfinite_json",
]
