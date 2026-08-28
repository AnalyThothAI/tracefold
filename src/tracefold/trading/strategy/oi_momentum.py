"""The characterized OI build-up continuation strategy from #104."""

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
from ..decision.regime import permits_entry, regime_side


@dataclass(frozen=True, slots=True)
class OiMomentumConfig:
    """No liquidity floor (#264). The absolute OI floor is a universe/routability rule and belongs to
    the Candidate Gate, which is the only place it is now executed; a strategy re-checking it made the
    same number an admission rule and an Alpha rule at once, and moving one moved neither."""

    allow_short: bool = False
    min_whale_long_profit_bps: int = 9_500

    @property
    def snapshot(self) -> dict[str, bool | int | str]:
        return {
            "allow_short": self.allow_short,
            "min_whale_long_profit_bps": self.min_whale_long_profit_bps,
        }

    @property
    def digest(self) -> str:
        return canonical_sha256(self.snapshot)


@dataclass(frozen=True, slots=True)
class OiMomentumStrategy:
    config: OiMomentumConfig = OiMomentumConfig()
    strategy_id: StrategyId = "oi_momentum_v1"
    strategy_version: str = "oi_momentum_v1"
    trigger_kinds: frozenset[TriggerKind] = frozenset({"oi"})
    permission: StrategyPermission = "paper"

    @property
    def config_digest(self) -> str:
        return self.config.digest

    @property
    def config_snapshot(self) -> dict[str, bool | int | str]:
        return self.config.snapshot

    def evaluate(self, context: FrozenStrategyContext) -> StrategyOutcome:
        rejection = oi_gate(context, config=self.config, permission="paper")
        if rejection is not None:
            return rejection
        side = regime_side(context.regime.regime)
        if side not in {"long", "short"}:  # defensive; oi_gate already proved permits_entry
            return _no_trade("regime_no_side", "paper")
        return StrategyOutcome(
            decision=side,
            rule="oi_momentum_regime",
            setup="OI build-up with price moving inside the measured continuation band",
            invalidation="price leaves the frozen continuation regime",
            expected_horizon="hours",
            permission="paper",
        )


def oi_gate(
    context: FrozenStrategyContext,
    *,
    config: OiMomentumConfig,
    permission: StrategyPermission,
) -> StrategyOutcome | None:
    oi = context.oi
    if oi is None:
        return _no_trade("oi_context_missing", permission)
    if not permits_entry(context.regime.regime):
        return _no_trade(f"regime_no_entry:{context.regime.regime.value}", permission)
    side = regime_side(context.regime.regime)
    if side == "short" and not config.allow_short:
        return _no_trade("short_disabled_long_only", permission)
    if oi.whale_long_profit_bps < config.min_whale_long_profit_bps:
        return _no_trade("whale_long_profit_below_floor", permission)
    return None


def _no_trade(rule: str, permission: StrategyPermission) -> StrategyOutcome:
    return StrategyOutcome(
        decision="no_trade",
        rule=rule,
        setup="",
        invalidation="",
        expected_horizon="none",
        permission=permission,
    )


__all__ = ["OiMomentumConfig", "OiMomentumStrategy", "oi_gate"]
