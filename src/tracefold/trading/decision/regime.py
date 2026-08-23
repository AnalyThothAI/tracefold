"""The deterministic OI/price quadrant. Arithmetic, never a model.

Two decisions here are the ones that actually decide what this lane trades, so they carry their
evidence:

1. **OI direction is not price direction.** A frame says open interest rose; it says nothing about
   which way. The quadrant pairs it with the realised price move over a fixed lookback, and only the
   pair is allowed to suggest a side.

2. **The pre-move filter is a band, not a floor.** `docs/research/oi-agent-design-2026-08-22.md` §1.6
   measured 630 aligned frames: pre-1h move <1% -> +4h -0.50%; 1-3% -> **+1.27%**; 3-6% -> +0.80%;
   6-12% -> **-0.77%**; >12% -> -0.61% with a -8.20% median 1h MAE. The shape is an inverted U and the
   losses are all above the band, so a rule with only a minimum keeps exactly the chasing trades the
   measurement rejects. `max_pre_move_bps` is therefore mandatory, and the default band is 100-600 bps
   over a 1 h lookback — the two buckets with a positive mean and the thickest samples.

The lookback is fixed in config rather than derived, because the measured bands above are 1 h bands:
changing the window silently invalidates the thresholds.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from ..contracts import Bar, OiRegime, RegimeAssessment

# One interval plus provider timestamp jitter, matching the News reaction plane's tolerance. Wide enough
# that a boundary rounding difference is not a hole; narrow enough that an illiquid gap never forward-fills.
DEFAULT_BAR_GAP_TOLERANCE_MS = 330_000


@dataclass(frozen=True, slots=True)
class RegimePolicy:
    """Operator-owned thresholds. Defaults are the measured band, not a preference."""

    lookback_ms: int = 3_600_000
    min_price_move_bps: int = 100
    max_price_move_bps: int = 600
    bar_gap_tolerance_ms: int = DEFAULT_BAR_GAP_TOLERANCE_MS

    def as_dict(self) -> dict[str, int]:
        return {
            "lookback_ms": self.lookback_ms,
            "min_price_move_bps": self.min_price_move_bps,
            "max_price_move_bps": self.max_price_move_bps,
            "bar_gap_tolerance_ms": self.bar_gap_tolerance_ms,
        }


DEFAULT_REGIME_POLICY = RegimePolicy()


def select_bar(bars: Sequence[Bar], *, target_ms: int, gap_tolerance_ms: int) -> Bar | None:
    """The last bar closed at or before `target_ms`, or None when the nearest one is too far back.

    No forward fill. A halted contract, a delisted market or an illiquid gap has to read as missing
    data; treating it as an unchanged price is how a regime gets computed against a price nobody could
    have traded at.
    """

    best: Bar | None = None
    for bar in bars:
        if bar.close_at_ms <= int(target_ms) and (best is None or bar.close_at_ms > best.close_at_ms):
            best = bar
    if best is None or int(target_ms) - best.close_at_ms > int(gap_tolerance_ms):
        return None
    return best


def move_bps(p0: Decimal | None, p1: Decimal | None) -> int | None:
    """`(p1 / p0) - 1` in integer basis points, Decimal throughout so the number is reproducible."""

    if p0 is None or p1 is None or p0 <= 0:
        return None
    return int(((Decimal(p1) / Decimal(p0) - 1) * 10_000).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))


def pre_move_bps(
    bars: Sequence[Bar],
    *,
    anchor_at_ms: int,
    policy: RegimePolicy = DEFAULT_REGIME_POLICY,
) -> int | None:
    """The realised move over the lookback ending at the trigger. None when either end is missing."""

    start = select_bar(bars, target_ms=anchor_at_ms - policy.lookback_ms, gap_tolerance_ms=policy.bar_gap_tolerance_ms)
    end = select_bar(bars, target_ms=anchor_at_ms, gap_tolerance_ms=policy.bar_gap_tolerance_ms)
    if start is None or end is None:
        return None
    return move_bps(start.close, end.close)


def assess(
    *,
    oi_direction: str | None,
    move: int | None,
    policy: RegimePolicy = DEFAULT_REGIME_POLICY,
) -> RegimeAssessment:
    """Classify the quadrant and say why. Missing price is `unclear`, never a guess."""

    direction = str(oi_direction or "").strip().lower() or None
    if move is None:
        return RegimeAssessment(
            regime=OiRegime.UNCLEAR,
            reason="no_price_fail_closed",
            pre_move_bps=None,
            oi_direction=direction,
        )
    magnitude = abs(move)
    if magnitude < policy.min_price_move_bps:
        return RegimeAssessment(
            regime=OiRegime.UNCLEAR,
            reason="move_below_band",
            pre_move_bps=move,
            oi_direction=direction,
        )
    if magnitude > policy.max_price_move_bps:
        # The measured loss bucket. Everything above the band has a negative mean and the worst MAE, so
        # this is a rejection with a name, not a silent clamp into the band.
        return RegimeAssessment(
            regime=OiRegime.UNCLEAR,
            reason="move_above_band_chasing",
            pre_move_bps=move,
            oi_direction=direction,
        )
    if direction == "rise":
        regime = OiRegime.BUILDUP_UP if move > 0 else OiRegime.BUILDUP_DOWN
    elif direction == "fall":
        regime = OiRegime.DELEVERAGING_UP if move > 0 else OiRegime.DELEVERAGING_DOWN
    else:
        return RegimeAssessment(
            regime=OiRegime.UNCLEAR,
            reason="oi_direction_unknown",
            pre_move_bps=move,
            oi_direction=direction,
        )
    return RegimeAssessment(regime=regime, reason="quadrant", pre_move_bps=move, oi_direction=direction)


def permits_entry(regime: OiRegime) -> bool:
    """Only a build-up may open. Deleveraging is positions closing; there is nothing to follow."""

    return regime in (OiRegime.BUILDUP_UP, OiRegime.BUILDUP_DOWN)


def regime_side(regime: OiRegime) -> str | None:
    """The side a build-up would confirm. `None` for every regime that may not open."""

    if regime is OiRegime.BUILDUP_UP:
        return "long"
    if regime is OiRegime.BUILDUP_DOWN:
        return "short"
    return None


__all__ = [
    "DEFAULT_REGIME_POLICY",
    "RegimePolicy",
    "assess",
    "move_bps",
    "permits_entry",
    "pre_move_bps",
    "regime_side",
    "select_bar",
]
