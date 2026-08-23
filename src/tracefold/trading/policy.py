"""The pure trade policy: frozen context in, `no_trade | long | short` out, and always a rule name.

Same discipline as `news.decide()`. No clock, no database, no provider, no model call — every input is
an argument, so a decision is replayable from the case row alone and every path is named. When two
rules could fire, the more conservative one wins and says so.

Three shapes of case, three different authorities for the *side*:

* `oi_only` — the deterministic quadrant owns the side. This is the execution-kernel trial lane; it
  never reaches a live mode and it never calls a model.
* `news_only` — there is no OI frame, so there is no quadrant, and the **model** owns the side. That
  is only tolerable because the kind is paper-only: the measured News cohort did not beat a random
  long (see the #104 assessment), so a model-chosen side may never open real exposure. Deriving the
  side from a quadrant that cannot exist is what made this kind structurally unreachable before.
* `news_oi` — both exist, so both must agree. This is the only kind a live mode may execute.

`decide()` is split in two on purpose. `pre_model_reject()` holds every gate that needs no model
answer, and the runner calls it **before** spending a provider call — otherwise three of the four
quadrants burn the daily model budget only to be refused afterwards by a pure function. `decide()`
re-applies those same gates, so the ordering is an optimisation and never the thing that makes the
policy correct.

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
    PolicyDecision,
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


def deterministic_side(case_kind: CaseKind, regime: OiRegime) -> PolicyDecision | None:
    """The side the quadrant fixes, or None when this kind does not take its side from a quadrant."""

    if case_kind == "news_only":
        return None
    side = regime_side(regime)
    if side == "long":
        return "long"
    if side == "short":
        return "short"
    return None


def pre_model_reject(
    *,
    case_kind: CaseKind,
    mode: TradingMode,
    regime: OiRegime,
    whale_long_profit_bps: int | None,
    oi_value_usd: int | None,
    policy: TradePolicy = DEFAULT_TRADE_POLICY,
) -> PolicyOutcome | None:
    """Every rejection that needs no model answer. `None` means a model call may be worth spending.

    The runner calls this before charging the daily budget. Without it a `deleveraging_up` frame, a
    `buildup_down` frame under the default long-only setting, or a frame below the whale floor each
    spend a provider call and are then refused by arithmetic — twelve of those exhaust the day before
    a tradeable case ever arrives.
    """

    live = mode in _LIVE_MODES

    if case_kind == "oi_only" and live:
        # The trial lane exists to exercise the order kernel at a rate News cannot supply. Letting it
        # reach real money would make the kernel's high frequency a capital risk rather than a test.
        return _reject("oi_only_never_live")
    if case_kind == "news_only" and live:
        return _reject("news_only_never_live")

    if case_kind != "news_only":
        if not permits_entry(regime):
            return _reject(f"regime_no_entry:{regime.value}")
        side = deterministic_side(case_kind, regime)
        if side is None:  # pragma: no cover - permits_entry already guarantees a side
            return _reject("regime_no_side")
        if side == "short" and not policy.allow_short:
            return _reject("short_disabled_long_only")

    if whale_long_profit_bps is not None and whale_long_profit_bps < policy.min_whale_long_profit_bps:
        return _reject("whale_long_profit_below_floor")
    if oi_value_usd is not None and oi_value_usd < policy.min_oi_value_usd:
        return _reject("oi_value_below_floor")
    return None


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

    early = pre_model_reject(
        case_kind=case_kind,
        mode=mode,
        regime=regime,
        whale_long_profit_bps=whale_long_profit_bps,
        oi_value_usd=oi_value_usd,
        policy=policy,
    )
    if early is not None:
        return early

    live = mode in _LIVE_MODES

    if case_kind == "oi_only":
        quadrant = deterministic_side(case_kind, regime)
        if quadrant is None:  # pragma: no cover - pre_model_reject already guaranteed one
            return _reject("regime_no_side")
        return PolicyOutcome(decision=quadrant, rule="oi_only_paper_regime")

    if decision is None:
        return _reject("model_absent")
    if decision.decision == "no_trade":
        return _reject(f"model_no_trade:{decision.reason_code}")

    if case_kind == "news_only":
        # No quadrant exists, so the model owns the side. Tolerable only because this kind is
        # paper-only, which `pre_model_reject` has already enforced above.
        model_side = decision.decision
        if model_side == "short" and not policy.allow_short:
            return _reject("short_disabled_long_only")
        return PolicyOutcome(decision=model_side, rule="news_only_paper_model_side")

    quadrant = deterministic_side(case_kind, regime)
    if quadrant is None:  # pragma: no cover - pre_model_reject already guaranteed one
        return _reject("regime_no_side")
    if decision.decision != quadrant:
        return _reject("model_contradicts_regime")

    # `news_oi` from here down.
    if not live:
        return PolicyOutcome(decision=quadrant, rule="news_oi_paper_aligned")
    if decision.directness != "direct":
        return _reject(f"live_requires_direct:{decision.directness}")
    if decision.surprise < policy.live_min_surprise:
        return _reject("live_requires_surprise")
    if decision.price_in > policy.live_max_price_in:
        return _reject("live_already_priced_in")
    if decision.alignment != "aligned":
        return _reject(f"live_requires_alignment:{decision.alignment}")
    return PolicyOutcome(decision=quadrant, rule="news_oi_live_aligned")


def side_to_order_side(decision: str) -> OrderSide | None:
    if decision == "long":
        return "buy"
    if decision == "short":
        return "sell"
    return None


__all__ = [
    "DEFAULT_TRADE_POLICY",
    "TradePolicy",
    "decide",
    "deterministic_side",
    "pre_model_reject",
    "side_to_order_side",
]
