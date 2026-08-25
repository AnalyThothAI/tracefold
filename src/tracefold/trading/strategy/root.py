"""One explicit code-owned strategy map and deterministic capital selection."""

from __future__ import annotations

from typing import Protocol, cast

from ..contracts import FrozenStrategyContext, StrategyId, StrategyOutcome, StrategyPermission, TriggerKind
from .liquidation_burst import LiquidationContinuationStrategy
from .liquidation_exhaustion import LiquidationExhaustionStrategy
from .news_oi_alignment import NewsOiAlignmentConfig, NewsOiAlignmentStrategy
from .oi_momentum import OiMomentumConfig, OiMomentumStrategy


class TradingStrategy(Protocol):
    strategy_id: StrategyId
    strategy_version: str
    trigger_kinds: frozenset[TriggerKind]
    permission: StrategyPermission

    @property
    def config_digest(self) -> str: ...

    def evaluate(self, context: FrozenStrategyContext) -> StrategyOutcome: ...


def strategies(
    *,
    allow_short: bool = False,
    min_whale_long_profit_bps: int = 9_500,
    min_oi_value_usd: int = 20_000_000,
    live_min_surprise: int = 2,
    live_max_price_in: int = 1,
) -> dict[StrategyId, TradingStrategy]:
    """No registry lifecycle: this literal map is the complete production strategy set."""

    configured: tuple[TradingStrategy, ...] = (
        cast(
            TradingStrategy,
            OiMomentumStrategy(
                OiMomentumConfig(
                    allow_short=allow_short,
                    min_whale_long_profit_bps=min_whale_long_profit_bps,
                    min_oi_value_usd=min_oi_value_usd,
                )
            ),
        ),
        cast(
            TradingStrategy,
            NewsOiAlignmentStrategy(
                NewsOiAlignmentConfig(
                    allow_short=allow_short,
                    min_whale_long_profit_bps=min_whale_long_profit_bps,
                    min_oi_value_usd=min_oi_value_usd,
                    live_min_surprise=live_min_surprise,
                    live_max_price_in=live_max_price_in,
                )
            ),
        ),
        cast(TradingStrategy, LiquidationContinuationStrategy()),
        cast(TradingStrategy, LiquidationExhaustionStrategy()),
    )
    return {strategy.strategy_id: strategy for strategy in configured}


def capital_strategy_id(*, trigger_kind: TriggerKind, has_oi: bool, has_news: bool) -> StrategyId | None:
    if trigger_kind == "liquidation":
        return None
    if has_oi and has_news:
        return "news_oi_alignment_v1"
    if trigger_kind == "oi" and has_oi:
        return "oi_momentum_v1"
    # News-only is deliberately shadow/no-trade until it has OI context.
    return "news_oi_alignment_v1" if trigger_kind == "news" else None


__all__ = ["TradingStrategy", "capital_strategy_id", "strategies"]
