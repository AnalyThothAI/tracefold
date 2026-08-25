"""Canonical, content-addressed identity primitives for News learning artifacts.

It also owns the two bounds every retained learning artifact is held to: a payload must be finite JSON,
and it must carry no credential. Those lived beside the trusted compiler while the compiler was the only
thing producing retained artifacts. #202 makes the offline optimizer produce them directly, so the rule
moved to the module that already decides what an artifact *is* rather than being copied into a second
place — the error codes are unchanged, because they are asserted by name.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
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


_FORBIDDEN_KEY_PARTS = frozenset(
    {
        ("api", "key"),
        ("authorization",),
        ("base", "url"),
        ("credential",),
        ("credentials",),
        ("endpoint", "url"),
        ("header",),
        ("headers",),
        ("password",),
        ("private", "key"),
        ("secret",),
    }
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)\b(?:sk|ghp|github_pat|xox[abprs])[-_][a-z0-9_-]{12,}"),
    re.compile(r"(?i)-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
# Three sandbox attestations whose whole point is to record the *absence* of a credential. They are named
# rather than pattern-matched so that `db_credentials_present: false` stays sayable and
# `db_credentials_present: "postgres://..."` does not.
_SAFE_NEGATIVE_ATTESTATIONS = frozenset(
    {
        "ambient_credentials_present",
        "db_credentials_present",
        "holdout_mounted",
    }
)


def reject_nonfinite_json(value: Any, *, path: str = "payload") -> None:
    """Refuse a payload that cannot round-trip as canonical JSON."""

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


def reject_secret_material(value: Any, *, path: str) -> None:
    """Refuse a retained payload that names or carries a credential."""

    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            parts = _key_parts(raw_key)
            safe_negative = str(raw_key) in _SAFE_NEGATIVE_ATTESTATIONS and child is False
            if not safe_negative and any(_contains_parts(parts, forbidden) for forbidden in _FORBIDDEN_KEY_PARTS):
                raise ValueError(f"news_program_compile_secret_key:{path}.{raw_key}")
            reject_secret_material(child, path=f"{path}.{raw_key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            reject_secret_material(child, path=f"{path}[{index}]")
    elif isinstance(value, str) and any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS):
        raise ValueError(f"news_program_compile_secret_value:{path}")


def _key_parts(value: object) -> tuple[str, ...]:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(value))
    return tuple(part for part in re.split(r"[^a-z0-9]+", separated.casefold()) if part)


def _contains_parts(parts: tuple[str, ...], forbidden: tuple[str, ...]) -> bool:
    width = len(forbidden)
    return any(parts[index : index + width] == forbidden for index in range(len(parts) - width + 1))


__all__ = [
    "canonical_json",
    "canonical_sha",
    "reject_nonfinite_json",
    "reject_secret_material",
]
