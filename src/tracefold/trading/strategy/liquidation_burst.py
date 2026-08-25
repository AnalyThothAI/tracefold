"""Cascade-continuation shadow hypothesis. OpenNews stays fail-closed on source completeness."""

from __future__ import annotations

from dataclasses import dataclass
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
    min_dominant_share_bps: int = 8_000
    min_acceleration_bps: int = 0
    min_price_momentum_bps: int = 10
    max_pre_move_bps: int = 250
    max_spread_bps: int = 20
    min_depth_notional_usd: Decimal = Decimal("1000000")
    max_source_latency_ms: int = 10_000

    @property
    def snapshot(self) -> dict[str, bool | int | str]:
        return {
            "window_ms": self.window_ms,
            "min_count": self.min_count,
            "min_notional_usd": str(self.min_notional_usd),
            "min_dominant_share_bps": self.min_dominant_share_bps,
            "min_acceleration_bps": self.min_acceleration_bps,
            "min_price_momentum_bps": self.min_price_momentum_bps,
            "max_pre_move_bps": self.max_pre_move_bps,
            "max_spread_bps": self.max_spread_bps,
            "min_depth_notional_usd": str(self.min_depth_notional_usd),
            "max_source_latency_ms": self.max_source_latency_ms,
        }

    @property
    def digest(self) -> str:
        return canonical_sha256(self.snapshot)


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

    @property
    def config_snapshot(self) -> dict[str, bool | int | str]:
        return self.config.snapshot

    def evaluate(self, context: FrozenStrategyContext) -> StrategyOutcome:
        fact = context.liquidation
        aggregate = context.liquidation_aggregate
        if fact is None or aggregate is None:
            return _no_trade("liquidation_context_missing")
        if not fact.source_contract.complete:
            return _no_trade("source_contract_incomplete")
        if fact.source_latency_ms > self.config.max_source_latency_ms:
            return _no_trade("source_latency_above_bound")
        if aggregate.dominant_count < self.config.min_count:
            return _no_trade("burst_count_below_floor")
        if aggregate.dominant_notional_usd < self.config.min_notional_usd:
            return _no_trade("burst_notional_below_floor")
        if aggregate.dominant_liquidated_side is None:
            return _no_trade("burst_side_tied")
        if aggregate.dominant_share_bps < self.config.min_dominant_share_bps:
            return _no_trade("burst_not_one_sided")
        if aggregate.dominant_acceleration_bps is None:
            return _no_trade("burst_acceleration_missing")
        if aggregate.dominant_acceleration_bps < self.config.min_acceleration_bps:
            return _no_trade("burst_acceleration_below_floor")
        momentum = context.market.price_momentum_bps
        if momentum is None:
            return _no_trade("price_momentum_missing")
        forced_buy = aggregate.dominant_liquidated_side == "short"
        signed_momentum = momentum if forced_buy else -momentum
        if signed_momentum < self.config.min_price_momentum_bps:
            return _no_trade("price_momentum_not_confirmed")
        pre_move = context.market.pre_move_bps
        if pre_move is None:
            return _no_trade("pre_move_missing")
        if abs(pre_move) > self.config.max_pre_move_bps:
            return _no_trade("pre_move_above_ceiling")
        if context.oi is None:
            return _no_trade("oi_context_missing")
        if context.market.funding_bps is None:
            return _no_trade("funding_context_missing")
        if context.market.spread_bps is None:
            return _no_trade("spread_context_missing")
        if context.market.spread_bps > self.config.max_spread_bps:
            return _no_trade("spread_above_bound")
        if context.market.depth_notional_usd is None:
            return _no_trade("depth_context_missing")
        if context.market.depth_notional_usd < self.config.min_depth_notional_usd:
            return _no_trade("depth_below_floor")
        return StrategyOutcome(
            decision="long" if forced_buy else "short",
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
