"""The capital lane's code-owned rules, assembled from operator settings for the surfaces that report them.

App owns composition, and three surfaces now have to describe the *same* rules: the Workers wiring that
executes them, the CLI replay that reports what they did, and the HTTP status the console reads. Each one
assembling its own copy is how a report ends up naming a floor the scanner is not applying — so the
assembly lives here once and every reader gets the same digest.

Nothing here decides anything. It reads settings and returns the code-owned objects; the thresholds
themselves belong to `tracefold.trading`.
"""

from __future__ import annotations

from typing import Any

from tracefold.trading.candidate.eligibility import EligibilityPolicy
from tracefold.trading.candidate.gate import CANDIDATE_GATE_VERSION, GateConfig
from tracefold.trading.strategy.root import TradingStrategy, strategies


def trading_settings_gate(settings: Any) -> GateConfig:
    """The Candidate Gate's configuration as the running lane would build it.

    Assembled from the same settings the Workers wiring reads, so a replay cannot describe a floor the
    scanner is not applying — the digest in the report is the digest the ledger's rows are filed under.
    """

    candidates = settings.trading.candidates
    return GateConfig.from_policy(
        EligibilityPolicy(
            max_age_ms=candidates.max_age_seconds * 1000,
            max_rank_in_window=candidates.max_rank_in_window,
            min_oi_value_usd=candidates.min_oi_value_usd,
            symbol_cooldown_ms=candidates.symbol_cooldown_seconds * 1000,
        ),
        venue_priority=settings.trading.venues.enabled,
    )


def trading_settings_strategies(settings: Any) -> list[TradingStrategy]:
    """Every code-owned strategy, configured as the lane configures it, ordered by identity.

    The whole set rather than the OI one: a Case names the strategy that decided it, and any surface
    comparing a frame against a threshold has to use *that* strategy's numbers.
    """

    policy = settings.trading.policy
    configured = strategies(
        allow_short=policy.allow_short,
        min_whale_long_profit_bps=policy.min_whale_long_profit_bps,
        live_min_surprise=policy.live_min_surprise,
        live_max_price_in=policy.live_max_price_in,
    )
    return sorted(configured.values(), key=lambda strategy: strategy.strategy_id)


__all__ = ["CANDIDATE_GATE_VERSION", "trading_settings_gate", "trading_settings_strategies"]
