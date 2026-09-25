"""Compile one recorded model proposal against frozen, code-owned conditions."""

from __future__ import annotations

import hashlib
import json
from typing import Any


class InvalidAssessment(ValueError):
    """A malformed reference or plan identity, not a rejected strategy proposal."""


def is_citable_evidence(item: dict[str, Any]) -> bool:
    """Use the same frozen availability rule for brief IDs and compilation."""
    cutoff = item.get("knowledge_cutoff_ms")
    event_at = item.get("event_at_ms")
    received_at = item.get("received_at_ms")
    values = item.get("values")
    return (
        item.get("status") == "ok"
        and isinstance(values, dict)
        and any(value is not None and value != "" for value in values.values())
        and bool(item.get("unit_definition"))
        and isinstance(cutoff, int)
        and isinstance(event_at, int)
        and isinstance(received_at, int)
        and event_at <= cutoff
        and received_at <= cutoff
    )


def decision_identity(case_id: str, decision: dict[str, object]) -> str:
    data = json.dumps(
        (case_id, decision), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode()
    return hashlib.sha256(data).hexdigest()
