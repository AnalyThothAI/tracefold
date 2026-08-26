"""One explicit code-owned strategy map and deterministic capital selection."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, cast

from ..contracts import (
    FrozenStrategyContext,
    StrategyId,
    StrategyOutcome,
    StrategyPermission,
    TradingCaseManifest,
    TriggerKind,
)
from .liquidation_burst import LiquidationContinuationConfig, LiquidationContinuationStrategy
from .liquidation_exhaustion import LiquidationExhaustionConfig, LiquidationExhaustionStrategy
from .news_oi_alignment import NewsOiAlignmentConfig, NewsOiAlignmentStrategy
from .oi_momentum import OiMomentumConfig, OiMomentumStrategy


class TradingStrategy(Protocol):
    strategy_id: StrategyId
    strategy_version: str
    trigger_kinds: frozenset[TriggerKind]
    permission: StrategyPermission

    @property
    def config_digest(self) -> str: ...

    @property
    def config_snapshot(self) -> dict[str, bool | int | str]: ...

    def evaluate(self, context: FrozenStrategyContext) -> StrategyOutcome: ...


def strategies(
    *,
    allow_short: bool = False,
    min_whale_long_profit_bps: int = 9_500,
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
                )
            ),
        ),
        cast(
            TradingStrategy,
            NewsOiAlignmentStrategy(
                NewsOiAlignmentConfig(
                    allow_short=allow_short,
                    min_whale_long_profit_bps=min_whale_long_profit_bps,
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


def strategy_from_manifest(manifest: TradingCaseManifest) -> TradingStrategy | None:
    """Rebuild the exact versioned strategy from the Case, never from today's settings."""

    if manifest.strategy_version != manifest.strategy_id:
        return None
    try:
        strategy: TradingStrategy
        config = manifest.strategy_config
        if manifest.strategy_id == "oi_momentum_v1":
            _exact_keys(config, "allow_short", "min_whale_long_profit_bps")
            strategy = cast(
                TradingStrategy,
                OiMomentumStrategy(
                    OiMomentumConfig(
                        allow_short=_bool(config, "allow_short"),
                        min_whale_long_profit_bps=_int(config, "min_whale_long_profit_bps"),
                    )
                ),
            )
        elif manifest.strategy_id == "news_oi_alignment_v1":
            _exact_keys(
                config,
                "allow_short",
                "min_whale_long_profit_bps",
                "live_min_surprise",
                "live_max_price_in",
            )
            strategy = cast(
                TradingStrategy,
                NewsOiAlignmentStrategy(
                    NewsOiAlignmentConfig(
                        allow_short=_bool(config, "allow_short"),
                        min_whale_long_profit_bps=_int(config, "min_whale_long_profit_bps"),
                        live_min_surprise=_int(config, "live_min_surprise"),
                        live_max_price_in=_int(config, "live_max_price_in"),
                    )
                ),
            )
        elif manifest.strategy_id == "liquidation_continuation_shadow_v1":
            _exact_keys(
                config,
                "window_ms",
                "min_count",
                "min_notional_usd",
                "min_dominant_share_bps",
                "min_acceleration_bps",
                "min_price_momentum_bps",
                "max_pre_move_bps",
                "max_spread_bps",
                "min_depth_notional_usd",
                "max_source_latency_ms",
            )
            strategy = cast(
                TradingStrategy,
                LiquidationContinuationStrategy(
                    LiquidationContinuationConfig(
                        window_ms=_int(config, "window_ms"),
                        min_count=_int(config, "min_count"),
                        min_notional_usd=_decimal(config, "min_notional_usd"),
                        min_dominant_share_bps=_int(config, "min_dominant_share_bps"),
                        min_acceleration_bps=_int(config, "min_acceleration_bps"),
                        min_price_momentum_bps=_int(config, "min_price_momentum_bps"),
                        max_pre_move_bps=_int(config, "max_pre_move_bps"),
                        max_spread_bps=_int(config, "max_spread_bps"),
                        min_depth_notional_usd=_decimal(config, "min_depth_notional_usd"),
                        max_source_latency_ms=_int(config, "max_source_latency_ms"),
                    )
                ),
            )
        else:
            _exact_keys(
                config,
                "min_count",
                "min_notional_usd",
                "min_dominant_share_bps",
                "min_extreme_displacement_bps",
                "max_source_latency_ms",
            )
            strategy = cast(
                TradingStrategy,
                LiquidationExhaustionStrategy(
                    LiquidationExhaustionConfig(
                        min_count=_int(config, "min_count"),
                        min_notional_usd=_decimal(config, "min_notional_usd"),
                        min_dominant_share_bps=_int(config, "min_dominant_share_bps"),
                        min_extreme_displacement_bps=_int(config, "min_extreme_displacement_bps"),
                        max_source_latency_ms=_int(config, "max_source_latency_ms"),
                    )
                ),
            )
    except (InvalidOperation, TypeError, ValueError):
        return None
    return strategy if strategy.config_digest == manifest.strategy_config_digest else None


def _exact_keys(config: Mapping[str, Any], *expected: str) -> None:
    if set(config) != set(expected):
        raise ValueError("strategy_config_shape_invalid")


def _bool(config: Mapping[str, Any], key: str) -> bool:
    value = config[key]
    if type(value) is not bool:
        raise TypeError(key)
    return value


def _int(config: Mapping[str, Any], key: str) -> int:
    value = config[key]
    if type(value) is not int:
        raise TypeError(key)
    return value


def _decimal(config: Mapping[str, Any], key: str) -> Decimal:
    value = config[key]
    if not isinstance(value, str):
        raise TypeError(key)
    return Decimal(value)


__all__ = ["TradingStrategy", "capital_strategy_id", "strategies", "strategy_from_manifest"]
