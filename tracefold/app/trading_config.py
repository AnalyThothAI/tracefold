"""The Signal lane's code-owned rules, assembled once for execution and read surfaces.

App owns composition, and the Workers wiring plus HTTP/CLI read surfaces have to describe the *same*
rules. Each one assembling its own copy is how a report ends up naming a floor the lane is not applying,
so the assembly lives here once and every reader gets the same digest.

Nothing here decides anything. It reads settings and returns the code-owned objects; the thresholds
themselves belong to `tracefold.trading`, and the policy's own numbers are not operator-settable at
all — an Alpha threshold in a YAML file is a rule with no version and no frozen evidence.
"""

from __future__ import annotations

from tracefold.news import OI_METRIC_VERSION as NEWS_OI_METRIC_VERSION
from tracefold.platform.config.models import Settings
from tracefold.trading.admission import ADMISSION_VERSION, AdmissionConfig
from tracefold.trading.market_context import PriceWindow
from tracefold.trading.policy import ALPHA_POLICY
from tracefold.trading.signal_lane import SIGNAL_TTL_MS, SignalLaneConfig


def signal_lane_config(settings: Settings) -> SignalLaneConfig:
    """Operator YAML -> the one frozen object every Signal-lane surface uses."""

    candidates = settings.trading.candidates
    max_age_ms = candidates.max_age_seconds * 1000
    return SignalLaneConfig(
        oi_metric_version=NEWS_OI_METRIC_VERSION,
        admission=AdmissionConfig(
            max_age_ms=max_age_ms,
            min_oi_value_usd=candidates.min_oi_value_usd,
        ),
        price_window=PriceWindow(),
        policy=ALPHA_POLICY,
        signal_ttl_ms=min(SIGNAL_TTL_MS, max_age_ms),
    )


# `trading_admission_config` and `trading_alpha_policy` were here until #460: one-line readers for
# `signal_lane_config(settings).admission` and `.policy`, written for the replay command that no longer
# exists. Every current caller wants the whole frozen object, and an accessor whose only job is to hand
# back one of its fields is a second name for the same rule.
__all__ = ["ADMISSION_VERSION", "signal_lane_config"]
