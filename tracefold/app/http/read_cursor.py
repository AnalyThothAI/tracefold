"""Scope-bound keyset positions for bounded console research reads."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Any

from .exceptions import ApiBadRequest


def encode_read_cursor(scope: list[Any], *, to_ms: int, value: int, at_ms: int, identity: str) -> str:
    payload = [_scope_key(scope), to_ms, value, at_ms, identity]
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")


def decode_read_cursor(cursor: str, scope: list[Any], *, error: str) -> tuple[int, int, int, str] | None:
    if not cursor:
        return None
    try:
        data = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        if (
            not isinstance(data, list)
            or len(data) != 5
            or data[0] != _scope_key(scope)
            or any(type(x) is not int or not 0 <= x < 2**63 for x in data[1:4])
            or not isinstance(data[4], str)
            or not 1 <= len(data[4]) <= 128
        ):
            raise ValueError
        return data[1], data[2], data[3], data[4]
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error):
        raise ApiBadRequest(error, field="cursor") from None


def _scope_key(scope: list[Any]) -> str:
    return hashlib.sha256(json.dumps(scope, separators=(",", ":")).encode()).hexdigest()
