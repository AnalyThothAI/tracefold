"""Public quote/reaction/review projections for Market Review storage."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..row_values import optional_int
from .instruments import normalize_symbol
from .pricing import (
    REACTION_METRIC_VERSION,
    PriceInstrument,
    coverage_pct,
    horizon_zh,
    median_bps,
    price_kind_for,
    price_kind_zh,
    quote_state_zh,
    reaction_reason_zh,
    reaction_state_zh,
)


def _coverage_rows(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Coverage before accuracy: the denominators and the named reasons a horizon could not be priced."""

    rows: list[dict[str, Any]] = []
    for horizon, key in (("1h", "priced_1h"), ("4h", "priced_4h")):
        eligible = int(data.get(f"eligible_{horizon}") or 0)
        unavailable = [
            {"reason": reason, "reason_zh": reaction_reason_zh(reason), "n": int(data.get(reason_key) or 0)}
            for reason, reason_key in (
                ("instrument_unresolved", f"unresolved_{horizon}"),
                ("no_candle_within_gap", f"gap_{horizon}"),
                ("history_expired", f"expired_{horizon}"),
                ("reference_only", f"reference_{horizon}"),
            )
            if int(data.get(reason_key) or 0) > 0
        ]
        rows.append(
            {
                "horizon": horizon,
                "horizon_zh": horizon_zh(horizon),
                "eligible_n": eligible,
                "priced_n": int(data.get(key) or 0),
                "coverage_pct": coverage_pct(int(data.get(key) or 0), eligible),
                "no_primary_n": int(data.get(f"no_primary_{horizon}") or 0),
                "degraded_n": int(data.get(f"degraded_{horizon}") or 0),
                "unavailable": unavailable,
            }
        )
    return rows


def _unlisted_quote(symbol: str) -> dict[str, Any]:
    return {
        "requested_symbol": symbol,
        "symbol": normalize_symbol(symbol),
        "base_symbol": normalize_symbol(symbol),
        "venue": None,
        "venue_symbol": None,
        "instrument_class": None,
        "quote_asset": None,
        "price": None,
        "price_kind": None,
        "price_kind_zh": "",
        "change_pct": None,
        "change_basis": None,
        "change_basis_zh": "",
        "source_at_ms": None,
        "received_at_ms": None,
        "received_age_ms": None,
        "source_age_ms": None,
        "effective_age_ms": None,
        "freshness_basis": None,
        "reference_at_ms": None,
        "reference_age_ms": None,
        "state": "unlisted",
        "state_zh": quote_state_zh("unlisted"),
    }


def _unavailable_quote(symbol: str, instrument: PriceInstrument) -> dict[str, Any]:
    return {
        "requested_symbol": symbol,
        "symbol": instrument.base_symbol,
        "base_symbol": instrument.base_symbol,
        "venue": instrument.venue,
        "venue_symbol": instrument.venue_symbol,
        "instrument_class": instrument.instrument_class,
        "quote_asset": instrument.quote_asset,
        "price": None,
        "price_kind": price_kind_for(instrument.venue),
        "price_kind_zh": price_kind_zh(price_kind_for(instrument.venue)),
        "change_pct": None,
        "change_basis": None,
        "change_basis_zh": "",
        "source_at_ms": None,
        "received_at_ms": None,
        "received_age_ms": None,
        "source_age_ms": None,
        "effective_age_ms": None,
        "freshness_basis": None,
        "reference_at_ms": None,
        "reference_age_ms": None,
        "state": "unavailable",
        "state_zh": quote_state_zh("unavailable"),
    }


def _reaction_public(row: Mapping[str, Any]) -> dict[str, Any]:
    state = str(row.get("state") or "pending")
    reason = row.get("unavailable_reason")
    return {
        "symbol": str(row["symbol"]),
        "metric_version": str(row.get("metric_version") or ""),
        "venue": str(row.get("venue") or "") or None,
        "venue_symbol": str(row.get("venue_symbol") or "") or None,
        "instrument_class": str(row.get("instrument_class") or "unknown"),
        "anchor_at_ms": int(row["anchor_at_ms"]),
        "p0": None if row.get("p0") is None else str(row["p0"]),
        "p0_at_ms": optional_int(row.get("p0_at_ms")),
        "p1": None if row.get("p1") is None else str(row["p1"]),
        "p1_at_ms": optional_int(row.get("p1_at_ms")),
        "p4": None if row.get("p4") is None else str(row["p4"]),
        "p4_at_ms": optional_int(row.get("p4_at_ms")),
        "return_1h_bps": optional_int(row.get("return_1h_bps")),
        "return_4h_bps": optional_int(row.get("return_4h_bps")),
        "is_primary": bool(row.get("is_primary")),
        "state": state,
        "state_zh": reaction_state_zh(state),
        "unavailable_reason": reason,
        "unavailable_reason_zh": reaction_reason_zh(reason),
        "updated_at_ms": optional_int(row.get("updated_at_ms")),
    }


def _aggregate_public(row: Mapping[str, Any], *, now_ms: int) -> dict[str, Any]:
    """Event-level state: pending until a horizon matures, unavailable when nothing about it can be priced."""

    anchor = int(row["anchor_at_ms"])
    primary_n = int(row.get("primary_n") or 0)
    row_n = int(row.get("row_n") or 0)
    unavailable_n = int(row.get("unavailable_n") or 0)
    matured = int(now_ms) >= anchor + 3_600_000
    bps_1h = [int(value) for value in (row.get("bps_1h") or [])]
    bps_4h = [int(value) for value in (row.get("bps_4h") or [])]
    p0s = [str(value) for value in (row.get("p0s") or [])]
    reason = row.get("unavailable_reason")
    if bps_1h and bps_4h and len(bps_4h) >= len(bps_1h):
        state = "complete"
    elif bps_1h:
        state = "partial"
    elif primary_n > 0 and row_n > 0 and unavailable_n >= row_n:
        state = "unavailable"
    elif matured and row_n == 0:
        # The model named primaries the Gate never grounded, so nothing was ever measured for them. That is
        # a permanent answer once the horizon has passed, and calling it "pending" would leave the card
        # saying 未到期 for the rest of retention.
        state, reason = "unavailable", reason or "instrument_unresolved"
    else:
        # Either the horizon has not matured or the cold loop has not reached it yet; both are "not yet",
        # and neither may render as a zero return.
        state = "pending"
    if state != "unavailable":
        reason = None
    return {
        "state": state,
        "state_zh": reaction_state_zh(state),
        "p0": p0s[0] if primary_n == 1 and len(p0s) == 1 else None,
        "return_1h_bps": median_bps(bps_1h),
        "return_4h_bps": median_bps(bps_4h),
        "asset_n": primary_n,
        "priced_n": len(bps_1h),
        "unavailable_reason": reason,
        "unavailable_reason_zh": reaction_reason_zh(reason),
        "metric_version": REACTION_METRIC_VERSION,
    }
