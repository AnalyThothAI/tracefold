"""Bound overlapping OI observations and revisions of one public catalyst."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

EPISODE_VERSION = "episode_v1"
MAX_WINDOW_MS = 900_000


def same_episode(kind: str, current: dict[str, Any], previous: dict[str, Any]) -> bool:
    if kind == "catalyst":
        return current.get("event_id") is not None and current.get("event_id") == previous.get("event_id")
    keys = ("direction", "measurement_definition", "source_venue")
    return all(current.get(key) is not None and current.get(key) == previous.get(key) for key in keys)


def material_update(kind: str, current: dict[str, Any], previous: dict[str, Any]) -> bool:
    if kind == "catalyst":
        prior = set(previous.get("claim_refs", ()))
        return bool(set(current.get("claim_refs", ())) - prior)
    try:
        old = abs(Decimal(str(previous["oi_change_bps"])))
        new = abs(Decimal(str(current["oi_change_bps"])))
    except (KeyError, ArithmeticError):
        return False
    # Compare with the last published observation at the transaction fence.
    return old.is_finite() and new.is_finite() and old > 0 and new >= 2 * old


def window_ms(payload: dict[str, Any]) -> int:
    return min(MAX_WINDOW_MS, max(1, int(payload.get("measurement_window_ms") or MAX_WINDOW_MS)))
