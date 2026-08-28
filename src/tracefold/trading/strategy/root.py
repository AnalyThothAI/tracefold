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
from .oi_smart_money_momentum import OiSmartMoneyMomentumConfig, OiSmartMoneyMomentumStrategy


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
    min_whale_long_profit_bps: int = 9_500,
) -> dict[StrategyId, TradingStrategy]:
    """No registry lifecycle: this literal map is the complete production strategy set."""

    configured: tuple[TradingStrategy, ...] = (
        cast(TradingStrategy, OiSmartMoneyMomentumStrategy(OiSmartMoneyMomentumConfig(allow_short=False))),
        cast(
            TradingStrategy,
            OiMomentumStrategy(
                OiMomentumConfig(
                    allow_short=False,
                    min_whale_long_profit_bps=min_whale_long_profit_bps,
                )
            ),
        ),
        cast(
            TradingStrategy,
            NewsOiAlignmentStrategy(
                NewsOiAlignmentConfig(
                    allow_short=False,
                    min_whale_long_profit_bps=min_whale_long_profit_bps,
                )
            ),
        ),
        cast(TradingStrategy, LiquidationContinuationStrategy()),
        cast(TradingStrategy, LiquidationExhaustionStrategy()),
    )
    return {strategy.strategy_id: strategy for strategy in configured}


def capital_strategy_id(*, trigger_kind: TriggerKind, has_oi: bool, has_news: bool) -> StrategyId | None:
    """Which strategy decides one frozen Case. One literal mapping; no registry, no lookup order.

    An OI trigger is answered by `oi_smart_money_momentum_v1` whether or not a News verdict attached
    (#265 §5.2). OI is the primary trigger and its thesis is arithmetic over the frame's own numbers, so
    a News counterpart is supplemental evidence at most: requiring one would put the reader's own
    push/drop back in the capital path, and routing on its presence would make the same frame reach two
    different strategies depending on whether an unrelated Event happened to land nearby. It is also
    what keeps the OI lane free of model calls — `news_oi_alignment_v1` is the only strategy that spends
    the daily DSPy budget, and it is now reached only by a News trigger.

    `has_news` is therefore unused for an OI trigger and kept in the signature because the News branch
    below still describes its two cases through it.
    """

    if trigger_kind == "liquidation":
        return None
    if trigger_kind == "oi":
        return "oi_smart_money_momentum_v1" if has_oi else None
    # A News trigger reaches here only with OI context: since #273 the Candidate Gate refuses a
    # News-only trigger as `eligibility:oi_context_missing` before a Case is frozen, rather than
    # freezing one so this strategy can say the same thing from inside a manifest. The strategy keeps
    # its own `oi_context_missing` branch — a strategy that trusts its caller to have checked is a
    # strategy that cannot be replayed on its own — but nothing in production should reach it.
    return "news_oi_alignment_v1" if trigger_kind == "news" else None


def strategy_from_manifest(manifest: TradingCaseManifest) -> TradingStrategy | None:
    """Rebuild the exact versioned strategy from the Case, never from today's settings."""

    if manifest.strategy_version != manifest.strategy_id:
        return None
    try:
        strategy: TradingStrategy
        config = manifest.strategy_config
        if manifest.strategy_id == "oi_smart_money_momentum_v1":
            _exact_keys(
                config,
                "allow_short",
                "max_price_move_bps",
                "measurement_window_ms",
                "min_oi_change_bps",
                "min_price_move_bps",
                "min_whale_long_profit_bps",
                "min_whale_oi_ratio_bps",
            )
            strategy = cast(
                TradingStrategy,
                OiSmartMoneyMomentumStrategy(
                    OiSmartMoneyMomentumConfig(
                        allow_short=_bool(config, "allow_short"),
                        max_price_move_bps=_int(config, "max_price_move_bps"),
                        measurement_window_ms=_int(config, "measurement_window_ms"),
                        min_oi_change_bps=_int(config, "min_oi_change_bps"),
                        min_price_move_bps=_int(config, "min_price_move_bps"),
                        min_whale_long_profit_bps=_int(config, "min_whale_long_profit_bps"),
                        min_whale_oi_ratio_bps=_int(config, "min_whale_oi_ratio_bps"),
                    )
                ),
            )
        elif manifest.strategy_id == "oi_momentum_v1":
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
