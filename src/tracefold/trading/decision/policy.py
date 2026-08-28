"""The remaining configurable decision threshold; execution policy is code-owned."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TradePolicy:
    """Operator-owned research floor supplied to the pure strategy constructors."""

    # Whale-long-profit floor. `oi-agent-design-2026-08-22.md` §1.5: the 95-100% bucket is the only one
    # with a positive mean (+1.42% at 4 h, N=219) while 85-95% is -0.60% at N=298. The shipped News OI
    # rule does not use this field at all, so the trading lane applies it itself.
    min_whale_long_profit_bps: int = 9_500
    # §1.5 again: the 10-50M OI bucket is the worst (-0.77%), >200M the best. A one-million floor admits
    # the worst bucket wholesale, so the trading lane keeps its own, higher floor.

    def as_dict(self) -> dict[str, object]:
        return {
            "min_whale_long_profit_bps": self.min_whale_long_profit_bps,
        }


DEFAULT_TRADE_POLICY = TradePolicy()

__all__ = [
    "DEFAULT_TRADE_POLICY",
    "TradePolicy",
]
