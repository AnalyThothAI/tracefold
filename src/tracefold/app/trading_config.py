"""The capital lane's code-owned rules, assembled once for the surfaces that execute and report them.

App owns composition, and three surfaces have to describe the *same* rules: the Workers wiring that
executes them, the CLI replay that reports what they did, and the HTTP status the console reads. Each
one assembling its own copy is how a report ends up naming a floor the lane is not applying — so the
assembly lives here once and every reader gets the same digest.

Nothing here decides anything. It reads settings and returns the code-owned objects; the thresholds
themselves belong to `tracefold.trading`, and the policy's own numbers are not operator-settable at
all (#331) — a capital threshold in a YAML file is a rule with no version and no frozen evidence.
"""

from __future__ import annotations

from tracefold.news import OI_METRIC_VERSION as NEWS_OI_METRIC_VERSION
from tracefold.platform.config.models import Settings
from tracefold.trading.admission import ADMISSION_VERSION, AdmissionConfig
from tracefold.trading.capital_lane import CapitalLaneConfig
from tracefold.trading.market_context import PriceWindow
from tracefold.trading.policy import CAPITAL_POLICY, CapitalPolicy


def capital_lane_config(settings: Settings) -> CapitalLaneConfig:
    """Operator YAML -> the one frozen object every capital-lane surface uses."""

    candidates = settings.trading.candidates
    return CapitalLaneConfig(
        oi_metric_version=NEWS_OI_METRIC_VERSION,
        admission=AdmissionConfig(
            max_age_ms=candidates.max_age_seconds * 1000,
            max_rank_in_window=candidates.max_rank_in_window,
            min_oi_value_usd=candidates.min_oi_value_usd,
            symbol_cooldown_ms=candidates.symbol_cooldown_seconds * 1000,
        ),
        price_window=PriceWindow(),
        policy=CAPITAL_POLICY,
        target_notional_usd=settings.trading.order.fixed_notional_usd,
    )


def trading_admission_config(settings: Settings) -> AdmissionConfig:
    """Admission as the running lane would build it, so a replay cannot describe a floor it is not applying."""

    return capital_lane_config(settings).admission


def trading_capital_policy(settings: Settings) -> CapitalPolicy:
    """The one production policy. Takes `settings` so every reader goes through the same assembly."""

    return capital_lane_config(settings).policy


__all__ = [
    "ADMISSION_VERSION",
    "capital_lane_config",
    "trading_admission_config",
    "trading_capital_policy",
]
