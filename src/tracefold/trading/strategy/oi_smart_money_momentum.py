"""NewsLiquid's three-dimensional smart-money event, as one pure, versioned, paper-only strategy (#265).

The template it implements is three conditions on one frame:

    5-minute OI rise  >= 5%
    smart-money / OI  >  50%
    profit metric     >  0

plus a confirmed price direction and a chasing ceiling above it.

**The OI floor is 5% and the ceiling is 10% by operator decision, not by measurement (#273).** The
template's own number was 10%, and at 10% this lane is starved: over the seven days to 2026-08-27,
462 parsed frames yielded four that cleared all three conditions — 0.6 a day, which makes proving the
execution kernel a matter of months. At 5% the same corpus yields 32, about 4.6 a day. What the
operator bought with that is throughput for a *paper* lane whose purpose is to produce receipts; what
it did not buy is evidence, and the change must not be read as one. The 30-day report in #273
stratifies the outcome by OI bucket precisely because this issue is open.

**Why this is a new strategy identity and not a retuned `oi_momentum_v1`.** That strategy means "OI
build-up continuation with 95% whale profit inside the measured 1-6% pre-move band". This one changes
the window, the OI-change condition, the ratio condition, the profit condition and the price minimum.
Four of the five numbers move, and reusing the id would make every historical Case replay under rules
it was never decided by. The old decoder stays so those Cases remain readable.

**Inclusivity is in the field names and it is not negotiable.** `min_` reads `>=` and the two `above`
conditions read `>`, exactly as the tests spell them: `500` qualifies and `499` does not; `5001`
qualifies and `5000` does not; `1` qualifies and `0` does not.

**The price minimum is 0 here and that is a deliberate change, not an oversight.** The lane's shared
regime band starts at 100 bps, and until #264 that band was applied at the freeze, so a frame with a
0.4% pre-move never reached any strategy at all. This strategy's thesis is that the OI and smart-money
conditions are the signal and price only has to *confirm the direction*; a hidden 1% minimum would be a
second, unmeasured entry condition.

**The 1000 bps ceiling contradicts a measurement, and the contradiction is the point of recording it.**
`docs/research/oi-agent-design-2026-08-22.md` §1.6 bucketed the full OI corpus by the same 1 h pre-move
this rule reads: 1-3% returned +1.27% at 4 h, 3-6% returned +0.80%, and **6-12% returned -0.77% on
N=151, with a median 1 h MAE of -3.35%**. Raising the ceiling from 600 to 1000 admits the bottom half
of that measured-negative bucket, and against this lane's 200 bps stop an MAE of that size is most of
those entries stopping out. Two things make it a decision an operator may take rather than a mistake
to be silently inherited: the measurement is over the *whole* corpus, and whether the three
smart-money conditions change its sign inside their own cohort is unmeasured; and this lane risks no
capital, so the cost of finding out is a paper receipt. It is not a prediction that the bucket is
profitable. #273's report stratifies realised outcomes by pre-move band for exactly this reason.

The shared regime band is deliberately left at 600. It is a different owner's number — it still gates
the News-only lane at the freeze, and an OI-bearing Case only records it — so a Case may now carry
`regime=unclear/move_above_band_chasing` and still be a long. That reads as a contradiction and is
not one: #265 §4 put Alpha thresholds in the strategy, and this is what that looks like when the two
numbers disagree.

Two things this strategy deliberately does **not** do:

* **It never returns a short, and it has no `short_disabled` rule.** A `fall` frame is refused by
  `not_oi_rise` before any side exists, and no other path can produce one, so a `short_disabled` reason
  would be a branch nothing can reach. `allow_short` stays in the config because it is the lane-wide
  operator switch a Case must freeze; a test pins that flipping it changes no decision.
* **It never calls a model.** There is no News counterpart in its inputs, its decision is arithmetic
  over frozen numbers, and it therefore spends none of the daily DSPy budget.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts import (
    FrozenStrategyContext,
    StrategyId,
    StrategyOutcome,
    StrategyPermission,
    TriggerKind,
    canonical_sha256,
)


@dataclass(frozen=True, slots=True)
class OiSmartMoneyMomentumConfig:
    """Every number the strategy executes, and nothing the Candidate Gate already owns.

    The absolute OI liquidity floor is absent on purpose: it is a universe/routability rule with one
    owner in the Candidate Gate (#264). A strategy re-checking it made the same number both an
    admission rule and an Alpha rule, which is why moving the canary floor from 20M to 5M moved
    neither.
    """

    # Not a preference: the frame must *prove* it was measured over this window, or the Case has no
    # basis for reading "10%" as a five-minute move.
    measurement_window_ms: int = 300_000
    # 5%, not the template's 10%: an operator throughput decision recorded in the module docstring
    # and in #273. Editing it moves `digest` and therefore only ever decides new Cases.
    min_oi_change_bps: int = 500
    # Strictly above. 5000 bps is exactly half and does not qualify.
    min_whale_oi_ratio_bps: int = 5_000
    # Strictly above. The provider's own `Whale Long Profit N%` percentage — not an account count and
    # not a dollar PnL (see `OiSourceContract`).
    min_whale_long_profit_bps: int = 0
    # Direction confirmation only. Zero, and stated as zero rather than inherited from the shared band.
    min_price_move_bps: int = 0
    # Above the measured 6-12% loss bucket's floor. See the module docstring: this one is a decision
    # taken against a measurement, not one taken from it.
    max_price_move_bps: int = 1_000
    allow_short: bool = False

    @property
    def snapshot(self) -> dict[str, bool | int | str]:
        return {
            "allow_short": self.allow_short,
            "max_price_move_bps": self.max_price_move_bps,
            "measurement_window_ms": self.measurement_window_ms,
            "min_oi_change_bps": self.min_oi_change_bps,
            "min_price_move_bps": self.min_price_move_bps,
            "min_whale_long_profit_bps": self.min_whale_long_profit_bps,
            "min_whale_oi_ratio_bps": self.min_whale_oi_ratio_bps,
        }

    @property
    def digest(self) -> str:
        return canonical_sha256(self.snapshot)


@dataclass(frozen=True, slots=True)
class OiSmartMoneyMomentumStrategy:
    config: OiSmartMoneyMomentumConfig = OiSmartMoneyMomentumConfig()
    strategy_id: StrategyId = "oi_smart_money_momentum_v1"
    strategy_version: str = "oi_smart_money_momentum_v1"
    trigger_kinds: frozenset[TriggerKind] = frozenset({"oi"})
    permission: StrategyPermission = "paper"

    @property
    def config_digest(self) -> str:
        return self.config.digest

    @property
    def config_snapshot(self) -> dict[str, bool | int | str]:
        return self.config.snapshot

    def evaluate(self, context: FrozenStrategyContext) -> StrategyOutcome:
        """Frozen numbers in, one named answer out. No clock, no read, no model.

        The order is the order an operator reads the template in — source, then the three conditions,
        then execution safety — so the reason a Case carries is the first thing about it that failed
        rather than whichever rule happened to be written first.
        """

        oi = context.oi
        if oi is None:
            return _no_trade("oi_context_missing")
        # The window first. Without it "10%" is a number with no interval attached, and every replay of
        # the Case would be a claim about a measurement nobody checked.
        if oi.measurement_window_ms != self.config.measurement_window_ms:
            return _no_trade("source_window_mismatch")
        if oi.oi_direction != "rise":
            return _no_trade("not_oi_rise")
        if oi.oi_change_bps < self.config.min_oi_change_bps:
            return _no_trade("smart_money_oi_change_below_floor")
        if oi.whale_oi_ratio_bps <= self.config.min_whale_oi_ratio_bps:
            return _no_trade("smart_money_ratio_below_or_equal_floor")
        if oi.whale_long_profit_bps <= self.config.min_whale_long_profit_bps:
            return _no_trade("smart_money_profit_not_positive")

        pre_move = context.market.pre_move_bps
        if pre_move is None or pre_move < self.config.min_price_move_bps:
            # A missing pre-move is not "no confirmation", it is "no evidence", and both refuse. The
            # Candidate Gate already deferred every frame with no candle at all, so reaching here with
            # `None` means the mark existed and the lookback end did not.
            return _no_trade("price_direction_not_confirmed")
        if pre_move > self.config.max_price_move_bps:
            return _no_trade("move_above_band_chasing")

        return StrategyOutcome(
            decision="long",
            rule="smart_money_momentum_long",
            setup=(
                f"{self.config.measurement_window_ms // 60_000}m 持仓上升 "
                f"{oi.oi_change_bps / 100:.2f}%，鲸鱼占比 {oi.whale_oi_ratio_bps / 100:.2f}%，"
                f"盈利指标 {oi.whale_long_profit_bps / 100:.2f}%，价格已确认方向 {pre_move / 100:.2f}%"
            ),
            invalidation="价格跌破冻结止损，或持仓时限到期",
            expected_horizon="minutes",
            permission="paper",
        )


def _no_trade(rule: str) -> StrategyOutcome:
    return StrategyOutcome(
        decision="no_trade",
        rule=rule,
        setup="",
        invalidation="",
        expected_horizon="none",
        permission="paper",
    )


__all__ = ["OiSmartMoneyMomentumConfig", "OiSmartMoneyMomentumStrategy"]
