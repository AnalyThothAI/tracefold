"""The characterized OI build-up continuation strategy from #104."""

from __future__ import annotations

from dataclasses import asdict, dataclass

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
    allow_short: bool = False
    min_whale_long_profit_bps: int = 9_500
    min_oi_value_usd: int = 20_000_000

    @property
    def digest(self) -> str:
        return canonical_sha256(asdict(self))


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
    if context.mode != "paper" and permission != "live_reviewed":
        return _no_trade("strategy_permission_shadow_or_paper", permission)
    if not permits_entry(context.regime.regime):
        return _no_trade(f"regime_no_entry:{context.regime.regime.value}", permission)
    side = regime_side(context.regime.regime)
    if side == "short" and not config.allow_short:
        return _no_trade("short_disabled_long_only", permission)
    if oi.whale_long_profit_bps < config.min_whale_long_profit_bps:
        return _no_trade("whale_long_profit_below_floor", permission)
    if oi.oi_value_usd < config.min_oi_value_usd:
        return _no_trade("oi_value_below_floor", permission)
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
