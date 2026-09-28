"""Validate stored wallet evidence with the current strict value contract."""

from __future__ import annotations

from typing import Any

from ..wallet_contracts import NetBuySnapshot


def wallet_snapshot(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return NetBuySnapshot.model_validate(value).model_dump(mode="json")


def wallet_event_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for key in ("initial_snapshot", "latest_snapshot", "send_snapshot"):
        if key in result:
            result[key] = wallet_snapshot(result[key])
    return result
