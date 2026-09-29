"""Pure LIVE-bar geometry and two-sided paper outcomes for a frozen Case."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from itertools import pairwise
from typing import Literal

BAR_MS = 60_000
GEOMETRY_VERSION = "leg_geometry_v1"
MAX_HOLD_MS = 14_400_000


@dataclass(frozen=True, slots=True)
class Bar:
    closed_at_ms: int
    high: Decimal
    low: Decimal
    close: Decimal

    def __post_init__(self) -> None:
        if self.closed_at_ms <= 0 or self.closed_at_ms % BAR_MS:
            raise ValueError("paper_bar_clock_invalid")
        if any(not value.is_finite() or value <= 0 for value in (self.high, self.low, self.close)):
            raise ValueError("paper_bar_price_invalid")
        if not self.high >= self.close >= self.low:
            raise ValueError("paper_bar_range_invalid")


@dataclass(frozen=True, slots=True)
class LegGeometry:
    stop_bps: int
    tp_bps: int
    max_hold_ms: int = MAX_HOLD_MS
    version: str = GEOMETRY_VERSION


@dataclass(frozen=True, slots=True)
class PaperLeg:
    side: Literal["long", "short"]
    status: Literal["complete", "missing"]
    outcome: Literal["tp", "sl", "timeout"] | None
    reason: str | None
    anchor_at_ms: int | None
    exit_at_ms: int | None
    anchor_price: Decimal | None
    exit_price: Decimal | None
    gross_bps: Decimal | None
    cost_bps: Decimal | None
    net_r: Decimal | None


def geometry(history: tuple[Bar, ...]) -> LegGeometry:
    """2x ATR14 of 16 consecutive closed LIVE 1m bars, clipped to 100..1000 bps."""
    rows = history[-16:]
    if len(rows) != 16 or any(b.closed_at_ms - a.closed_at_ms != BAR_MS for a, b in pairwise(rows)):
        raise ValueError("geometry_history_incomplete")
    baseline = rows[1:]
    true_ranges = [
        max(now.high - now.low, abs(now.high - previous.close), abs(now.low - previous.close))
        for previous, now in pairwise(baseline)
    ]
    atr14 = sum(true_ranges, Decimal(0)) / 14
    stop = int((2 * atr14 / rows[-1].close * 10_000).to_integral_value(rounding=ROUND_CEILING))
    stop = max(100, min(1_000, stop))
    return LegGeometry(stop_bps=stop, tp_bps=2 * stop)


def paper_leg(
    *,
    side: Literal["long", "short"],
    decided_at_ms: int,
    bars: tuple[Bar, ...],
    leg_geometry: LegGeometry,
    half_spread_bps: Decimal,
    taker_fee_bps: Decimal = Decimal(5),
) -> PaperLeg:
    """Anchor on the first later close; SL wins a bar that touches both barriers."""
    if not half_spread_bps.is_finite() or half_spread_bps < 0 or taker_fee_bps < 0:
        raise ValueError("paper_cost_invalid")
    if leg_geometry.stop_bps <= 0 or leg_geometry.tp_bps <= 0 or leg_geometry.max_hold_ms <= 0:
        raise ValueError("paper_geometry_invalid")

    def missing(reason: str, anchor: Bar | None = None) -> PaperLeg:
        return PaperLeg(
            side,
            "missing",
            None,
            reason,
            None if anchor is None else anchor.closed_at_ms,
            None,
            None if anchor is None else anchor.close,
            None,
            None,
            None,
            None,
        )

    anchor_index = next((index for index, bar in enumerate(bars) if bar.closed_at_ms > decided_at_ms), None)
    if anchor_index is None:
        return missing("anchor_missing")
    anchor = bars[anchor_index]
    if anchor.closed_at_ms != (decided_at_ms // BAR_MS + 1) * BAR_MS:
        return missing("anchor_gap")
    last_at = anchor.closed_at_ms + leg_geometry.max_hold_ms
    window = tuple(bar for bar in bars[anchor_index:] if bar.closed_at_ms <= last_at)
    stop_fraction = Decimal(leg_geometry.stop_bps) / 10_000
    tp_fraction = Decimal(leg_geometry.tp_bps) / 10_000
    if side == "long":
        sl_price = anchor.close * (1 - stop_fraction)
        tp_price = anchor.close * (1 + tp_fraction)
    else:
        sl_price = anchor.close * (1 + stop_fraction)
        tp_price = anchor.close * (1 - tp_fraction)
    previous = anchor
    for bar in window[1:]:
        if bar.closed_at_ms - previous.closed_at_ms != BAR_MS:
            return missing("bar_gap", anchor)
        previous = bar
        sl_hit = bar.low <= sl_price if side == "long" else bar.high >= sl_price
        tp_hit = bar.high >= tp_price if side == "long" else bar.low <= tp_price
        outcome: Literal["tp", "sl", "timeout"] | None = (
            "sl" if sl_hit else "tp" if tp_hit else "timeout" if bar.closed_at_ms == last_at else None
        )
        if outcome is None:
            continue
        exit_price = sl_price if outcome == "sl" else tp_price if outcome == "tp" else bar.close
        gross_bps = (exit_price / anchor.close - 1) * 10_000 * (1 if side == "long" else -1)
        cost_bps = 2 * taker_fee_bps + half_spread_bps
        return PaperLeg(
            side,
            "complete",
            outcome,
            None,
            anchor.closed_at_ms,
            bar.closed_at_ms,
            anchor.close,
            exit_price,
            gross_bps,
            cost_bps,
            (gross_bps - cost_bps) / leg_geometry.stop_bps,
        )
    return missing("horizon_incomplete", anchor)


def both_legs(
    *, decided_at_ms: int, bars: tuple[Bar, ...], leg_geometry: LegGeometry, half_spread_bps: Decimal
) -> tuple[PaperLeg, PaperLeg]:
    return (
        paper_leg(
            side="long",
            decided_at_ms=decided_at_ms,
            bars=bars,
            leg_geometry=leg_geometry,
            half_spread_bps=half_spread_bps,
        ),
        paper_leg(
            side="short",
            decided_at_ms=decided_at_ms,
            bars=bars,
            leg_geometry=leg_geometry,
            half_spread_bps=half_spread_bps,
        ),
    )
