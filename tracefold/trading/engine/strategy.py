"""Frozen event-following price confirmation for catalyst and OI facts.

These are research starting parameters. The same setup and crossing function
are used by initial analysis and the conditional watcher.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal
from itertools import pairwise
from typing import Any, Literal

from .contracts import Candidate, ExitPlan

STRATEGY_VERSION = "event_price_confirmation_v1"
LOOKBACK_CLOSED_BARS = 15
BAR_MS = 60_000
ENTRY_WINDOW_MS = 120_000


def range_cross_side(
    *, previous_close: Decimal, close: Decimal, upper: Decimal, lower: Decimal
) -> Literal["long", "short"] | None:
    if previous_close <= upper and close > upper:
        return "long"
    if previous_close >= lower and close < lower:
        return "short"
    return None


def build_event_price_candidates(
    *,
    asset_id: str,
    instrument_semantics_digest: str,
    source_fact: dict[str, Any],
    source_first_visible_at_ms: int,
    perp_rows: tuple[dict[str, Any], ...],
) -> tuple[Candidate, ...]:
    if source_fact.get("kind") not in ("oi", "catalyst"):
        raise ValueError("strategy_source_kind_invalid")
    if len(perp_rows) < LOOKBACK_CLOSED_BARS + 1:
        raise ValueError("strategy_closed_bar_history_incomplete")
    rows = perp_rows[-LOOKBACK_CLOSED_BARS - 1 :]
    stamps = [int(row["event_at_ms"]) for row in rows]
    if any(right - left != BAR_MS for left, right in pairwise(stamps)):
        raise ValueError("strategy_closed_bar_gap")
    prices = [(Decimal(str(row["high"])), Decimal(str(row["low"])), Decimal(str(row["close"]))) for row in rows]
    if any(not all(value.is_finite() and value > 0 for value in row) or row[0] < row[1] for row in prices):
        raise ValueError("strategy_price_invalid")
    baseline = prices[:-1]
    upper = max(high for high, _, _ in baseline)
    lower = min(low for _, low, _ in baseline)
    if lower >= upper:
        raise ValueError("strategy_range_invalid")
    true_ranges = [
        max(high - low, abs(high - baseline[index - 1][2]), abs(low - baseline[index - 1][2]))
        for index, (high, low, _) in enumerate(baseline)
        if index > 0
    ]
    atr14 = sum(true_ranges, Decimal(0)) / Decimal(14)
    close = prices[-1][2]
    previous_close = baseline[-1][2]
    stop_bps = min(
        1_000, max(100, int((Decimal(2) * atr14 / close * 10_000).to_integral_value(rounding=ROUND_CEILING)))
    )
    exit_plan = ExitPlan(stop_distance_bps=stop_bps, take_profit_bps=2 * stop_bps, max_holding_seconds=14_400)
    if source_fact["kind"] == "oi":
        source_ready = all(source_fact.get(key) is not None for key in ("oi_change_bps", "measurement_definition"))
    else:
        source_ready = any(source_fact.get(key) for key in ("headline_zh", "title", "why_zh"))
    if source_first_visible_at_ms <= 0:
        raise ValueError("source_visibility_missing")
    crossing = range_cross_side(previous_close=previous_close, close=close, upper=upper, lower=lower)
    source_precedes_crossing = stamps[-1] > source_first_visible_at_ms
    preexisting = crossing is not None and not source_precedes_crossing
    reason = "source_fact_unavailable" if not source_ready else "breakout_precedes_source" if preexisting else None
    candidates = []
    for side, operator, level in (("long", "gt", upper), ("short", "lt", lower)):
        candidates.append(
            Candidate(
                candidate_id=f"{asset_id}:{side}:{STRATEGY_VERSION}",
                asset_id=asset_id,
                instrument_semantics_digest=instrument_semantics_digest,
                side=side,
                exit_plan=exit_plan,
                required_evidence_refs=("source", "market:perp_bars"),
                entry_operator=operator,
                entry_level=level,
                entry_observed=close,
                previous_close=previous_close,
                entry_observed_at_ms=stamps[-1],
                source_first_visible_at_ms=source_first_visible_at_ms,
                entry_ready=bool(source_ready and not preexisting and crossing == side),
                source_gate_ready=bool(source_ready and not preexisting),
                watch_eligible=bool(source_ready and crossing is None),
                strategy_gate_reason=reason,
            )
        )
    return tuple(candidates)


def triggered_candidate(
    *,
    asset_id: str,
    instrument_semantics_digest: str,
    condition: dict[str, Any],
    trigger_side: Literal["long", "short"],
    trigger_at_ms: int,
    trigger_close: Decimal,
    previous_close: Decimal,
    latest_closed_rows: tuple[dict[str, Any], ...],
) -> Candidate:
    upper = Decimal(str(condition["upper_level"]))
    lower = Decimal(str(condition["lower_level"]))
    valid_trigger = range_cross_side(
        previous_close=previous_close, close=trigger_close, upper=upper, lower=lower
    ) == trigger_side and trigger_at_ms > int(condition["source_first_visible_at_ms"])
    later = [row for row in latest_closed_rows if int(row["event_at_ms"]) > trigger_at_ms]
    reentered = any(
        Decimal(str(row["close"])) <= upper if trigger_side == "long" else Decimal(str(row["close"])) >= lower
        for row in later
    )
    level = upper if trigger_side == "long" else lower
    valid = valid_trigger and not reentered
    return Candidate(
        candidate_id=f"{asset_id}:{trigger_side}:{STRATEGY_VERSION}",
        asset_id=asset_id,
        instrument_semantics_digest=instrument_semantics_digest,
        side=trigger_side,
        exit_plan=ExitPlan.model_validate(condition["exit_plan"]),
        required_evidence_refs=("source", "market:perp_bars"),
        entry_operator="gt" if trigger_side == "long" else "lt",
        entry_level=level,
        entry_observed=trigger_close,
        previous_close=previous_close,
        entry_observed_at_ms=trigger_at_ms,
        source_first_visible_at_ms=int(condition["source_first_visible_at_ms"]),
        entry_ready=valid,
        source_gate_ready=valid,
        watch_eligible=False,
        strategy_gate_reason="trigger_invalid" if not valid_trigger else "price_reentered_range" if reentered else None,
    )
