"""Cascade-continuation shadow hypothesis. OpenNews stays fail-closed on source completeness."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal

from ..contracts import (
    FrozenStrategyContext,
    StrategyId,
    StrategyOutcome,
    StrategyPermission,
    TriggerKind,
    canonical_sha256,
)


@dataclass(frozen=True, slots=True)
class LiquidationContinuationConfig:
    window_ms: int = 60_000
    min_count: int = 2
    min_notional_usd: Decimal = Decimal("500000")
    max_source_latency_ms: int = 10_000

    @property
    def digest(self) -> str:
        return canonical_sha256(asdict(self))


@dataclass(frozen=True, slots=True)
class LiquidationContinuationStrategy:
    config: LiquidationContinuationConfig = LiquidationContinuationConfig()
    strategy_id: StrategyId = "liquidation_continuation_shadow_v1"
    strategy_version: str = "liquidation_continuation_shadow_v1"
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
        if fact.source_latency_ms > self.config.max_source_latency_ms:
            return _no_trade("source_latency_above_bound")
        if aggregate.count < self.config.min_count:
            return _no_trade("burst_count_below_floor")
        if aggregate.notional_usd < self.config.min_notional_usd:
            return _no_trade("burst_notional_below_floor")
        return StrategyOutcome(
            decision="long" if fact.forced_order_side == "buy" else "short",
            rule="liquidation_cascade_continuation",
            setup="one-sided forced flow continues with confirmed price momentum",
            invalidation="forced-flow intensity decelerates or price stops extending",
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


__all__ = ["LiquidationContinuationConfig", "LiquidationContinuationStrategy"]
