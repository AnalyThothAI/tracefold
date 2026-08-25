"""Post-cascade exhaustion shadow hypothesis, intentionally opposite to continuation."""

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


@dataclass(frozen=True, slots=True)
class LiquidationExhaustionConfig:
    min_count: int = 3

    @property
    def digest(self) -> str:
        return canonical_sha256(asdict(self))


@dataclass(frozen=True, slots=True)
class LiquidationExhaustionStrategy:
    config: LiquidationExhaustionConfig = LiquidationExhaustionConfig()
    strategy_id: StrategyId = "liquidation_exhaustion_shadow_v1"
    strategy_version: str = "liquidation_exhaustion_shadow_v1"
    trigger_kinds: frozenset[TriggerKind] = frozenset({"liquidation"})
    permission: StrategyPermission = "shadow"

    @property
    def config_digest(self) -> str:
        return self.config.digest

    def evaluate(self, context: FrozenStrategyContext) -> StrategyOutcome:
        fact = context.liquidation
        aggregate = context.liquidation_aggregate
        if fact is None or aggregate is None:
            return _no_trade("liquidation_context_missing")
        if not context.source_contract_complete:
            return _no_trade("source_contract_incomplete")
        if aggregate.count < self.config.min_count:
            return _no_trade("exhaustion_burst_count_below_floor")
        required = {
            "intensity_decelerating": context.intensity_decelerating,
            "oi_collapsing": context.oi_collapsing,
            "price_stopped_extreme": context.price_stopped_extreme,
            "liquidity_recovered": context.liquidity_recovered,
        }
        missing = next((name for name, value in required.items() if value is not True), None)
        if missing is not None:
            return _no_trade(f"exhaustion_requires:{missing}")
        return StrategyOutcome(
            decision="short" if fact.forced_order_side == "buy" else "long",
            rule="liquidation_post_cascade_exhaustion",
            setup="forced flow decelerates while price stops extending and liquidity recovers",
            invalidation="price resumes in the forced-flow direction",
            expected_horizon="minutes",
            permission="shadow",
        )


def _no_trade(rule: str) -> StrategyOutcome:
    return StrategyOutcome(
        decision="no_trade",
        rule=rule,
        setup="",
        invalidation="",
        expected_horizon="none",
        permission="shadow",
    )


__all__ = ["LiquidationExhaustionConfig", "LiquidationExhaustionStrategy"]
