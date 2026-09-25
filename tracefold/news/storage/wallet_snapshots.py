"""Read historical wallet evidence through the current strict value contract.

Only the three retired ranking/statistic keys are projected away. Stored evidence,
including initial/send JSON and frozen external cards, is never rewritten here.
"""

from __future__ import annotations

from typing import Any

from ..wallet_contracts import NetBuySnapshot

_RETIRED_MEMBER_KEYS = frozenset({"rank_quality", "source_closed_trades", "source_profit_factor"})


def wallet_snapshot(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    current = dict(value)
    window = dict(current["window"])
    window["members"] = [
        {key: item for key, item in member.items() if key not in _RETIRED_MEMBER_KEYS} for member in window["members"]
    ]
    current["window"] = window
    return NetBuySnapshot.model_validate(current).model_dump(mode="json")


def wallet_event_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for key in ("initial_snapshot", "latest_snapshot", "send_snapshot"):
        if key in result:
            result[key] = wallet_snapshot(result[key])
    return result
