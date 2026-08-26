"""News directness/surprise plus the deterministic OI regime; one model value, pure gates."""

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
from ..decision.regime import regime_side
from .oi_momentum import OiMomentumConfig, oi_gate


@dataclass(frozen=True, slots=True)
class NewsOiAlignmentConfig:
    allow_short: bool = False
    min_whale_long_profit_bps: int = 9_500
    live_min_surprise: int = 2
    live_max_price_in: int = 1

    @property
    def snapshot(self) -> dict[str, bool | int | str]:
        return {
            "allow_short": self.allow_short,
            "min_whale_long_profit_bps": self.min_whale_long_profit_bps,
            "live_min_surprise": self.live_min_surprise,
            "live_max_price_in": self.live_max_price_in,
        }

    @property
    def digest(self) -> str:
        return canonical_sha256(self.snapshot)


@dataclass(frozen=True, slots=True)
class NewsOiAlignmentStrategy:
    config: NewsOiAlignmentConfig = NewsOiAlignmentConfig()
    strategy_id: StrategyId = "news_oi_alignment_v1"
    strategy_version: str = "news_oi_alignment_v1"
    trigger_kinds: frozenset[TriggerKind] = frozenset({"oi", "news"})
    permission: StrategyPermission = "live_reviewed"

    @property
    def config_digest(self) -> str:
        return self.config.digest

    @property
    def config_snapshot(self) -> dict[str, bool | int | str]:
        return self.config.snapshot

    def evaluate(self, context: FrozenStrategyContext) -> StrategyOutcome:
        if context.news is None:
            return _no_trade("news_context_missing")
        gate = oi_gate(
            context,
            config=OiMomentumConfig(
                allow_short=self.config.allow_short,
                min_whale_long_profit_bps=self.config.min_whale_long_profit_bps,
            ),
            permission=self.permission,
        )
        if gate is not None:
            return gate
        decision = context.news_decision
        if decision is None:
            return _no_trade("model_absent")
        if decision.decision == "no_trade":
            return _no_trade(f"model_no_trade:{decision.reason_code}")
        side = regime_side(context.regime.regime)
        if decision.decision != side:
            return _no_trade("model_contradicts_regime")
        if context.mode == "live_reviewed":
            if decision.directness != "direct":
                return _no_trade(f"live_requires_direct:{decision.directness}")
            if decision.surprise < self.config.live_min_surprise:
                return _no_trade("live_requires_surprise")
            if decision.price_in > self.config.live_max_price_in:
                return _no_trade("live_already_priced_in")
            if decision.alignment != "aligned":
                return _no_trade(f"live_requires_alignment:{decision.alignment}")
        return StrategyOutcome(
            decision=decision.decision,
            rule="news_oi_aligned",
            setup=decision.thesis_zh,
            invalidation=decision.invalidation_zh,
            expected_horizon=decision.horizon,
            permission="live_reviewed" if context.mode == "live_reviewed" else "paper",
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


__all__ = ["NewsOiAlignmentConfig", "NewsOiAlignmentStrategy"]
