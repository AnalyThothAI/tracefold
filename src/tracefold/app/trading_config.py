"""The capital lane's code-owned rules, assembled from operator settings for the surfaces that report them.

App owns composition, and three surfaces now have to describe the *same* rules: the Workers wiring that
executes them, the CLI replay that reports what they did, and the HTTP status the console reads. Each one
assembling its own copy is how a report ends up naming a floor the scanner is not applying — so the
assembly lives here once and every reader gets the same digest.

Nothing here decides anything. It reads settings and returns the code-owned objects; the thresholds
themselves belong to `tracefold.trading`.
"""

from __future__ import annotations

from typing import cast

from tracefold.news import OI_METRIC_VERSION as NEWS_OI_METRIC_VERSION
from tracefold.platform.config.models import Settings
from tracefold.trading.candidate.eligibility import EligibilityPolicy
from tracefold.trading.candidate.gate import CANDIDATE_GATE_VERSION, GateConfig
from tracefold.trading.contracts import LiveExchangeId
from tracefold.trading.decision.policy import TradePolicy
from tracefold.trading.decision.regime import RegimePolicy
from tracefold.trading.pipeline.runtime import TradingConfig
from tracefold.trading.strategy.root import TradingStrategy, strategies


def trading_config_from_settings(settings: Settings) -> TradingConfig:
    """Operator YAML -> the one frozen object every Trading execution surface uses."""

    trading = settings.trading
    candidates = trading.candidates
    return TradingConfig(
        oi_metric_version=NEWS_OI_METRIC_VERSION,
        venue_priority=cast(tuple[LiveExchangeId, ...], ("binance", "hyperliquid")),
        eligibility=EligibilityPolicy(
            max_age_ms=candidates.max_age_seconds * 1000,
            max_rank_in_window=candidates.max_rank_in_window,
            min_oi_value_usd=candidates.min_oi_value_usd,
            news_lookback_ms=candidates.news_lookback_seconds * 1000,
            oi_lookback_ms=candidates.oi_lookback_seconds * 1000,
            symbol_cooldown_ms=candidates.symbol_cooldown_seconds * 1000,
        ),
        regime=RegimePolicy(
            lookback_ms=trading.regime.lookback_seconds * 1000,
            min_price_move_bps=trading.regime.min_price_move_bps,
            max_price_move_bps=trading.regime.max_price_move_bps,
        ),
        trade=TradePolicy(
            min_whale_long_profit_bps=trading.policy.min_whale_long_profit_bps,
        ),
        fixed_notional_usd=trading.order.fixed_notional_usd,
        max_dspy_cases_per_day=candidates.max_dspy_cases_per_day,
    )


def trading_settings_gate(settings: Settings) -> GateConfig:
    """The Candidate Gate's configuration as the running lane would build it.

    Assembled from the same settings the Workers wiring reads, so a replay cannot describe a floor the
    scanner is not applying — the digest in the report is the digest the ledger's rows are filed under.
    """

    config = trading_config_from_settings(settings)
    return GateConfig.from_policy(
        config.eligibility,
        venue_priority=config.venue_priority,
    )


def trading_settings_strategies(settings: Settings) -> list[TradingStrategy]:
    """Every code-owned strategy, configured as the lane configures it, ordered by identity.

    The whole set rather than the OI one: a Case names the strategy that decided it, and any surface
    comparing a frame against a threshold has to use *that* strategy's numbers.
    """

    config = trading_config_from_settings(settings)
    configured = strategies(
        min_whale_long_profit_bps=config.trade.min_whale_long_profit_bps,
    )
    return sorted(configured.values(), key=lambda strategy: strategy.strategy_id)


__all__ = [
    "CANDIDATE_GATE_VERSION",
    "trading_config_from_settings",
    "trading_settings_gate",
    "trading_settings_strategies",
]
