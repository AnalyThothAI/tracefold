"""Point-in-time opportunity labels. These are market paths, never fills or PnL."""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def price_path_label(
    rows: tuple[dict[str, Any], ...],
    *,
    anchor_ms: int,
    horizon_seconds: int,
    market_status: str = "ok",
    interval_ms: int = 60_000,
) -> dict[str, Any]:
    """Use the first completed close on/after each endpoint.

    The independent market observation arrives after the decision and cannot
    change its frozen input. Missing endpoints are explicitly missing, not zero.
    """
    if horizon_seconds <= 0:
        raise ValueError("outcome_horizon_invalid")
    if interval_ms <= 0:
        raise ValueError("outcome_interval_invalid")
    if market_status != "ok":
        return {
            "status": "missing",
            "reason": "price_window_incomplete",
            "anchor_ms": anchor_ms,
            "target_ms": anchor_ms + horizon_seconds * 1_000,
        }
    ordered = sorted(rows, key=lambda row: int(row["event_at_ms"]))
    start = next((row for row in ordered if int(row["event_at_ms"]) >= anchor_ms), None)
    target_ms = anchor_ms + horizon_seconds * 1_000
    end = next((row for row in ordered if int(row["event_at_ms"]) >= target_ms), None)
    if (
        start is None
        or end is None
        or int(start["event_at_ms"]) >= int(end["event_at_ms"])
        or int(start["event_at_ms"]) - anchor_ms >= interval_ms
        or int(end["event_at_ms"]) - target_ms >= interval_ms
    ):
        return {"status": "missing", "reason": "price_endpoint_missing", "anchor_ms": anchor_ms, "target_ms": target_ms}
    first = Decimal(str(start["close"]))
    last = Decimal(str(end["close"]))
    if first <= 0 or last <= 0:
        return {"status": "missing", "reason": "price_nonpositive", "anchor_ms": anchor_ms, "target_ms": target_ms}
    return {
        "status": "ok",
        "version": "price_path_v1",
        "axis_anchor_ms": anchor_ms,
        "target_ms": target_ms,
        "start_close_at_ms": int(start["event_at_ms"]),
        "end_close_at_ms": int(end["event_at_ms"]),
        "start_price": str(first),
        "end_price": str(last),
        "return_bps": str((last / first - 1) * 10_000),
        "measure": "underlying_close_to_close_gross",
        "execution_claim": False,
        "costs_included": False,
    }
