"""Frozen OI expansion and closed-price confirmation research strategy.

The version identifies a finite shadow candidate, not a claim of positive net
edge. Only the closed perp bars and source OI fact determine entry levels. A
model can explain or select a candidate but cannot change these parameters.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from .contracts import Candidate, ExitPlan

STRATEGY_VERSION = "oi_price_confirmation_v1"
LOOKBACK_CLOSED_BARS = 15
EXIT_PLAN = ExitPlan(stop_distance_bps=200, take_profit_bps=400, max_holding_seconds=14_400)


def build_oi_price_candidates(
    *,
    asset_id: str,
    instrument_semantics_digest: str,
    source_fact: dict[str, Any],
    perp_rows: tuple[dict[str, Any], ...],
    market_oi_available: bool,
) -> tuple[Candidate, ...]:
    if source_fact.get("kind") != "oi":
        # Catalyst cases still receive a frozen assessment, but this OI-only
        # strategy offers no executable candidate for them.
        return ()
    if len(perp_rows) < LOOKBACK_CLOSED_BARS + 1:
        raise ValueError("strategy_closed_bar_history_incomplete")
    prior = perp_rows[-LOOKBACK_CLOSED_BARS - 1 : -1]
    latest = perp_rows[-1]
    high = max(Decimal(str(row["high"])) for row in prior)
    low = min(Decimal(str(row["low"])) for row in prior)
    close = Decimal(str(latest["close"]))
    if high <= 0 or low <= 0 or close <= 0:
        raise ValueError("strategy_price_nonpositive")
    oi_change = source_fact.get("oi_change_bps")
    oi_value = source_fact.get("oi_value_usd")
    source_gate = (
        isinstance(oi_change, (int, float))
        and not isinstance(oi_change, bool)
        and Decimal(str(oi_change)) > 0
        and isinstance(oi_value, (int, float))
        and not isinstance(oi_value, bool)
        and Decimal(str(oi_value)) > 0
        and market_oi_available
    )
    reason = None if source_gate else "oi_expansion_or_market_quantity_unavailable"
    candidates = []
    directions: tuple[tuple[Literal["long", "short"], Literal["gte", "lte"], Decimal], ...] = (
        ("long", "gte", high),
        ("short", "lte", low),
    )
    for side, operator, level in directions:
        crossed = close >= level if operator == "gte" else close <= level
        candidates.append(
            Candidate(
                candidate_id=f"{asset_id}:{side}:{STRATEGY_VERSION}",
                asset_id=asset_id,
                instrument_semantics_digest=instrument_semantics_digest,
                side=side,
                exit_plan=EXIT_PLAN,
                required_evidence_refs=("source", "market:perp_bars", "market:open_interest"),
                entry_operator=operator,
                entry_level=level,
                entry_observed=close,
                entry_observed_at_ms=int(latest["event_at_ms"]),
                entry_ready=bool(source_gate and crossed),
                source_gate_ready=bool(source_gate),
                watch_eligible=bool(source_gate and not crossed),
                strategy_gate_reason=reason,
            )
        )
    return tuple(candidates)
