"""The price window a Case is frozen against. Arithmetic, never a model.

Two facts this module exists to keep true:

1. **OI direction is not price direction.** A frame says open interest rose; it says nothing about
   which way. The policy pairs it with the realised move over a fixed lookback, and only the pair is
   allowed to suggest a side.

2. **The pre-move filter is a band, not a floor.** `docs/research/oi-agent-design-2026-08-22.md` §1.6
   measured 630 aligned frames: pre-1h move <1% -> +4h -0.50%; 1-3% -> **+1.27%**; 3-6% -> +0.80%;
   6-12% -> **-0.77%**; >12% -> -0.61% with a -8.20% median 1h MAE. The shape is an inverted U and the
   losses are all above the band, so a rule with only a minimum keeps exactly the chasing trades the
   measurement rejects. The band itself lives in `policy.py`, with the Case that executed it.

The OI/price quadrant is gone (#331). It was `oi_momentum_v1`'s entry rule, and after that strategy
was cut the only thing `assess()` still decided was `no_price_fail_closed` — which the lane's own
"there is no candle at the cutoff" check already answers, by name, in the admission ledger. A
`regime` column that no rule reads and every console renders is a business claim nothing makes.

The lookback is code-owned rather than derived, because the measured bands above are 1 h bands:
changing the window silently invalidates the thresholds the policy executes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from .contracts import Bar

# One interval plus provider timestamp jitter, matching the News reaction plane's tolerance. Wide enough
# that a boundary rounding difference is not a hole; narrow enough that an illiquid gap never forward-fills.
DEFAULT_BAR_GAP_TOLERANCE_MS = 330_000
DEFAULT_PRE_MOVE_LOOKBACK_MS = 3_600_000


@dataclass(frozen=True, slots=True)
class PriceWindow:
    """How far back the pre-move is measured, and how large a candle gap still counts as data."""

    lookback_ms: int = DEFAULT_PRE_MOVE_LOOKBACK_MS
    bar_gap_tolerance_ms: int = DEFAULT_BAR_GAP_TOLERANCE_MS

    def as_dict(self) -> dict[str, int]:
        return {
            "lookback_ms": self.lookback_ms,
            "bar_gap_tolerance_ms": self.bar_gap_tolerance_ms,
        }


DEFAULT_PRICE_WINDOW = PriceWindow()


def select_bar(bars: Sequence[Bar], *, target_ms: int, gap_tolerance_ms: int) -> Bar | None:
    """The last bar closed at or before `target_ms`, or None when the nearest one is too far back.

    No forward fill. A halted contract, a delisted market or an illiquid gap has to read as missing
    data; treating it as an unchanged price is how a mark gets frozen at a price nobody could have
    traded at.
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
    window: PriceWindow = DEFAULT_PRICE_WINDOW,
) -> int | None:
    """The realised move over the lookback ending at the trigger. None when either end is missing."""

    start = select_bar(bars, target_ms=anchor_at_ms - window.lookback_ms, gap_tolerance_ms=window.bar_gap_tolerance_ms)
    end = select_bar(bars, target_ms=anchor_at_ms, gap_tolerance_ms=window.bar_gap_tolerance_ms)
    if start is None or end is None:
        return None
    return move_bps(start.close, end.close)


__all__ = [
    "DEFAULT_BAR_GAP_TOLERANCE_MS",
    "DEFAULT_PRE_MOVE_LOOKBACK_MS",
    "DEFAULT_PRICE_WINDOW",
    "PriceWindow",
    "move_bps",
    "pre_move_bps",
    "select_bar",
]
