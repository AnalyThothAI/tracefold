"""NewsLiquid's three-dimensional smart-money event, as one pure, versioned, paper-only strategy (#265).

The template it implements is three conditions on one frame:

    5-minute OI rise  >= 10%
    smart-money / OI  >  50%
    profit metric     >  0

plus the execution safety this lane already measured — a confirmed price direction and the chasing
ceiling above it.

**Why this is a new strategy identity and not a retuned `oi_momentum_v1`.** That strategy means "OI
build-up continuation with 95% whale profit inside the measured 1-6% pre-move band". This one changes
the window, the OI-change condition, the ratio condition, the profit condition and the price minimum.
Four of the five numbers move, and reusing the id would make every historical Case replay under rules
it was never decided by. The old decoder stays so those Cases remain readable.

**Inclusivity is in the field names and it is not negotiable.** `min_` reads `>=` and the two `above`
conditions read `>`, exactly as the tests spell them: `1000` qualifies and `999` does not; `5001`
qualifies and `5000` does not; `1` qualifies and `0` does not.

**The price minimum is 0 here and that is a deliberate change, not an oversight.** The lane's shared
regime band starts at 100 bps, and until #264 that band was applied at the freeze, so a frame with a
0.4% pre-move never reached any strategy at all. This strategy's thesis is that the OI and smart-money
conditions are the signal and price only has to *confirm the direction*; a hidden 1% minimum would be a
second, unmeasured entry condition. The 600 bps ceiling is kept, because it is measured:
`docs/research/oi-agent-design-2026-08-22.md` §1.6 found every bucket above it has a negative mean.

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
    min_oi_change_bps: int = 1_000
    # Strictly above. 5000 bps is exactly half and does not qualify.
    min_whale_oi_ratio_bps: int = 5_000
    # Strictly above. The provider's own `Whale Long Profit N%` percentage — not an account count and
    # not a dollar PnL (see `OiSourceContract`).
    min_whale_long_profit_bps: int = 0
    # Direction confirmation only. Zero, and stated as zero rather than inherited from the shared band.
    min_price_move_bps: int = 0
    max_price_move_bps: int = 600
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
        if context.mode != "paper":
            # This strategy is paper-only by identity, not by configuration. A `live_reviewed` lane
            # must reach a named refusal here rather than have the permission check catch it later.
            return _no_trade("strategy_permission_paper_only")

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
