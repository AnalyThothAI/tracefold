"""Post-cascade exhaustion shadow hypothesis, intentionally opposite to continuation."""

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
class LiquidationExhaustionConfig:
    min_count: int = 3
    min_notional_usd: Decimal = Decimal("1000000")
    min_dominant_share_bps: int = 8_500
    min_extreme_displacement_bps: int = 100
    max_source_latency_ms: int = 10_000

    @property
    def snapshot(self) -> dict[str, bool | int | str]:
        return {
            "min_count": self.min_count,
            "min_notional_usd": str(self.min_notional_usd),
            "min_dominant_share_bps": self.min_dominant_share_bps,
            "min_extreme_displacement_bps": self.min_extreme_displacement_bps,
            "max_source_latency_ms": self.max_source_latency_ms,
        }

    @property
    def digest(self) -> str:
        return canonical_sha256(self.snapshot)


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
            return _no_trade("exhaustion_burst_count_below_floor")
        if aggregate.dominant_notional_usd < self.config.min_notional_usd:
            return _no_trade("exhaustion_burst_notional_below_floor")
        if aggregate.dominant_liquidated_side is None:
            return _no_trade("exhaustion_burst_side_tied")
        if aggregate.dominant_share_bps < self.config.min_dominant_share_bps:
            return _no_trade("exhaustion_burst_not_one_sided")
        displacement = context.market.displacement_bps
        if displacement is None:
            return _no_trade("extreme_displacement_missing")
        forced_buy = aggregate.dominant_liquidated_side == "short"
        signed_displacement = displacement if forced_buy else -displacement
        if signed_displacement < self.config.min_extreme_displacement_bps:
            return _no_trade("extreme_displacement_below_floor")
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
            decision="short" if forced_buy else "long",
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
