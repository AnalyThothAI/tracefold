"""Shared operator-owned strategy thresholds and the decision-to-order-side mapping.

The pure decision functions live under ``trading.strategy``. This module is intentionally smaller:
it defines the one configuration object used to construct those strategies and the only mapping from
their ``long | short`` vocabulary to the execution kernel's ``buy | sell`` vocabulary.

`allow_short` defaults to false. The issue title fixes V1 as a **long-only** core while the body's
policy vocabulary admits `short`; the safe reading of that conflict is to build the mechanism and keep
it off, so turning shorts on is an explicit operator act rather than an accident of a quadrant.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts import OrderSide


@dataclass(frozen=True, slots=True)
class TradePolicy:
    """Operator-owned gates supplied to the pure strategy constructors."""

    allow_short: bool = False
    live_min_surprise: int = 2
    live_max_price_in: int = 1
    # Whale-long-profit floor. `oi-agent-design-2026-08-22.md` §1.5: the 95-100% bucket is the only one
    # with a positive mean (+1.42% at 4 h, N=219) while 85-95% is -0.60% at N=298. The shipped News OI
    # rule does not use this field at all, so the trading lane applies it itself.
    min_whale_long_profit_bps: int = 9_500
    # §1.5 again: the 10-50M OI bucket is the worst (-0.77%), >200M the best. A one-million floor admits
    # the worst bucket wholesale, so the trading lane keeps its own, higher floor.
    min_oi_value_usd: int = 20_000_000

    def as_dict(self) -> dict[str, object]:
        return {
            "allow_short": self.allow_short,
            "live_min_surprise": self.live_min_surprise,
            "live_max_price_in": self.live_max_price_in,
            "min_whale_long_profit_bps": self.min_whale_long_profit_bps,
            "min_oi_value_usd": self.min_oi_value_usd,
        }


DEFAULT_TRADE_POLICY = TradePolicy()


def side_to_order_side(decision: str) -> OrderSide | None:
    if decision == "long":
        return "buy"
    if decision == "short":
        return "sell"
    return None


__all__ = [
    "DEFAULT_TRADE_POLICY",
    "TradePolicy",
    "side_to_order_side",
]
