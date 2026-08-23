"""The pure trade policy: frozen context in, `no_trade | long | short` out, and always a rule name.

Same discipline as `news.decide()`. No clock, no database, no provider, no model call — every input is
an argument, so a decision is replayable from the case row alone and every path is named. When two
rules could fire, the more conservative one wins and says so.

Three shapes of case, three different authorities:

* `oi_only` — arithmetic only. This is the execution-kernel trial lane and it never reaches live.
* `news_only` — shadow/paper in V1. A News verdict on its own has no confirmation, and the measured
  cohort behind it did not beat a random long (see the #104 assessment), so it is not permitted to
  open real exposure.
* `news_oi` — the only kind a live mode may execute, and only when the model's read and the
  deterministic regime agree.

`allow_short` defaults to false. The issue title fixes V1 as a **long-only** core while the body's
policy vocabulary admits `short`; the safe reading of that conflict is to build the mechanism and keep
it off, so turning shorts on is an explicit operator act rather than an accident of a quadrant.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import (
    CaseKind,
    OiRegime,
    OrderSide,
    PolicyOutcome,
    TradeDecision,
    TradingMode,
)
from .regime import permits_entry, regime_side


@dataclass(frozen=True, slots=True)
class TradePolicy:
    """Operator-owned gates for the pure mapping."""

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

_LIVE_MODES: frozenset[str] = frozenset({"live_reviewed", "live_bounded"})


def _reject(rule: str) -> PolicyOutcome:
    return PolicyOutcome(decision="no_trade", rule=rule)


def decide(
    *,
    case_kind: CaseKind,
    mode: TradingMode,
    regime: OiRegime,
    decision: TradeDecision | None,
    whale_long_profit_bps: int | None,
    oi_value_usd: int | None,
    policy: TradePolicy = DEFAULT_TRADE_POLICY,
) -> PolicyOutcome:
    """The single decision point. Every return names the rule that produced it."""

    if not permits_entry(regime):
        return _reject(f"regime_no_entry:{regime.value}")
    side = regime_side(regime)
    if side is None:  # pragma: no cover - permits_entry already guarantees a side
        return _reject("regime_no_side")
    if side == "short" and not policy.allow_short:
        return _reject("short_disabled_long_only")

    if whale_long_profit_bps is not None and whale_long_profit_bps < policy.min_whale_long_profit_bps:
        return _reject("whale_long_profit_below_floor")
    if oi_value_usd is not None and oi_value_usd < policy.min_oi_value_usd:
        return _reject("oi_value_below_floor")

    live = mode in _LIVE_MODES

    if case_kind == "oi_only":
        if live:
            # The trial lane exists to exercise the order kernel at a rate News cannot supply. Letting it
            # reach real money would make the kernel's high frequency a capital risk rather than a test.
            return _reject("oi_only_never_live")
        return PolicyOutcome(decision=side, rule="oi_only_paper_regime")

    if decision is None:
        return _reject("model_absent")
    if decision.decision == "no_trade":
        return _reject(f"model_no_trade:{decision.reason_code}")
    if decision.decision != side:
        return _reject("model_contradicts_regime")

    if case_kind == "news_only":
        if live:
            return _reject("news_only_never_live")
        return PolicyOutcome(decision=side, rule="news_only_paper_aligned")

    # `news_oi` from here down.
    if not live:
        return PolicyOutcome(decision=side, rule="news_oi_paper_aligned")
    if decision.directness != "direct":
        return _reject(f"live_requires_direct:{decision.directness}")
    if decision.surprise < policy.live_min_surprise:
        return _reject("live_requires_surprise")
    if decision.price_in > policy.live_max_price_in:
        return _reject("live_already_priced_in")
    if decision.alignment != "aligned":
        return _reject(f"live_requires_alignment:{decision.alignment}")
    return PolicyOutcome(decision=side, rule="news_oi_live_aligned")


def side_to_order_side(decision: str) -> OrderSide | None:
    if decision == "long":
        return "buy"
    if decision == "short":
        return "sell"
    return None


__all__ = ["DEFAULT_TRADE_POLICY", "TradePolicy", "decide", "side_to_order_side"]
