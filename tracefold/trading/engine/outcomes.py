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
    if market_status not in ("ok", "partial"):
        return {
            "status": "missing",
            "reason": "price_window_incomplete",
            "anchor_ms": anchor_ms,
            "target_ms": anchor_ms + horizon_seconds * 1_000,
        }
    target_ms = anchor_ms + horizon_seconds * 1_000
    start_at = ((anchor_ms + interval_ms - 1) // interval_ms) * interval_ms
    end_at = ((target_ms + interval_ms - 1) // interval_ms) * interval_ms
    endpoints = {int(row["event_at_ms"]): row for row in rows if row.get("closed") is not False}
    start = endpoints.get(start_at)
    end = endpoints.get(end_at)
    if start is None or end is None or start_at >= end_at:
        return {"status": "missing", "reason": "price_endpoint_missing", "anchor_ms": anchor_ms, "target_ms": target_ms}
    first = Decimal(str(start["close"]))
    last = Decimal(str(end["close"]))
    if not first.is_finite() or not last.is_finite() or first <= 0 or last <= 0:
        return {"status": "missing", "reason": "price_nonpositive", "anchor_ms": anchor_ms, "target_ms": target_ms}
    return {
        "status": "ok",
        "version": "price_path_v2",
        "axis_anchor_ms": anchor_ms,
        "target_ms": target_ms,
        "start_close_at_ms": start_at,
        "end_close_at_ms": end_at,
        "start_price": str(first),
        "end_price": str(last),
        "return_bps": str((last / first - 1) * 10_000),
        "measure": "underlying_close_to_close_gross",
        "execution_claim": False,
        "costs_included": False,
    }
