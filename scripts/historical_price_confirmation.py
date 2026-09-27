"""Frozen pre-v4 price-confirmation rule for offline historical cohort reports.

This module is never imported by the Trading service or Runtime.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal
from itertools import pairwise
from typing import Any, Literal

from pydantic import Field, model_validator

from tracefold.trading.engine.contracts import ExitPlan, Frozen
from tracefold.trading.engine.features import CATALYST_SOURCE_KIND, catalyst_text_values


class Candidate(Frozen):
    candidate_id: str = Field(min_length=1, max_length=80)
    asset_id: str = Field(min_length=1, max_length=128)
    instrument_semantics_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    side: Literal["long", "short"]
    exit_plan: ExitPlan
    required_evidence_refs: tuple[str, ...] = ()
    strategy_family: Literal["event_price_confirmation"] = "event_price_confirmation"
    strategy_version: Literal["event_price_confirmation_v1"] = "event_price_confirmation_v1"
    entry_feature_id: Literal["perp_close_1m"] = "perp_close_1m"
    entry_operator: Literal["gt", "lt"] = "gt"
    entry_level: Decimal = Decimal(0)
    entry_observed: Decimal = Decimal(0)
    previous_close: Decimal = Decimal(0)
    entry_observed_at_ms: int = 0
    source_first_visible_at_ms: int = 0
    entry_ready: bool = True
    source_gate_ready: bool = True
    watch_eligible: bool = False
    strategy_gate_reason: str | None = None

    @model_validator(mode="after")
    def check_entry(self) -> Candidate:
        crossed = (
            self.previous_close <= self.entry_level and self.entry_observed > self.entry_level
            if self.entry_operator == "gt"
            else self.previous_close >= self.entry_level and self.entry_observed < self.entry_level
        )
        crossed = crossed and self.entry_observed_at_ms > self.source_first_visible_at_ms
        if self.entry_ready != (self.source_gate_ready and crossed):
            raise ValueError("candidate_entry_state_inconsistent")
        if self.watch_eligible and (not self.source_gate_ready or self.entry_ready):
            raise ValueError("candidate_watch_state_inconsistent")
        return self


STRATEGY_VERSION = "event_price_confirmation_v1"
LOOKBACK_CLOSED_BARS = 15
BAR_MS = 60_000
ENTRY_WINDOW_MS = 120_000
MAX_HOLDING_SECONDS = 14_400


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
    if source_fact.get("kind") not in ("oi", "catalyst", CATALYST_SOURCE_KIND):
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
    exit_plan = ExitPlan(
        stop_distance_bps=stop_bps, take_profit_bps=2 * stop_bps, max_holding_seconds=MAX_HOLDING_SECONDS
    )
    if source_fact["kind"] == "oi":
        source_ready = all(source_fact.get(key) is not None for key in ("oi_change_bps", "measurement_definition"))
    elif source_fact["kind"] == "catalyst":
        # Archived pre-#706 snapshots froze the retired headline/why catalyst; this offline
        # report reads them as recorded. Live Trading has no such reading path.
        source_ready = any(
            isinstance(source_fact.get(key), str) and source_fact[key].strip() for key in ("headline", "why")
        )
    else:
        source_ready = bool(catalyst_text_values(source_fact))
    if source_first_visible_at_ms <= 0:
        raise ValueError("source_visibility_missing")
    crossing = range_cross_side(previous_close=previous_close, close=close, upper=upper, lower=lower)
    source_precedes_crossing = stamps[-1] > source_first_visible_at_ms
    preexisting = crossing is not None and not source_precedes_crossing
    reason = "source_fact_unavailable" if not source_ready else "breakout_precedes_source" if preexisting else None
    candidates = []
    directions: tuple[tuple[Literal["long", "short"], Literal["gt", "lt"], Decimal], ...] = (
        ("long", "gt", upper),
        ("short", "lt", lower),
    )
    for side, operator, level in directions:
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
