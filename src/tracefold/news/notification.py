from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from typing import Literal

PUSH_PROVIDER_SCORE_THRESHOLD = 70.0
PUSH_SOURCE_FRESHNESS_MS = 15 * 60 * 1_000

_CL_FAMILY_ASSET_SYMBOLS = frozenset({"cl", "xyz-cl"})

NewsPushIneligibleReason = Literal[
    "disabled",
    "score_threshold",
    "no_asset",
    "cl_family_only",
    "baseline",
    "stale",
]


@dataclass(frozen=True, slots=True)
class NewsPushEligibility:
    """Current Story Push qualification, independent of delivery history."""

    eligible: bool
    ineligible_reason: NewsPushIneligibleReason | None


def evaluate_news_push_eligibility(
    provider_evidence: Mapping[str, object] | None,
    *,
    enabled: bool,
    baseline_at_ms: int | None,
    now_ms: int,
) -> NewsPushEligibility:
    """Apply the single News-owned live-alert admission policy."""

    if not isinstance(provider_evidence, Mapping):
        return _ineligible("score_threshold")
    score = provider_evidence.get("provider_score")
    if (
        not isinstance(score, int | float)
        or isinstance(score, bool)
        or not isfinite(float(score))
        or float(score) <= PUSH_PROVIDER_SCORE_THRESHOLD
    ):
        return _ineligible("score_threshold")

    symbols = _qualifying_asset_symbols(provider_evidence.get("provider_metadata"))
    if not symbols:
        return _ineligible("no_asset")
    if symbols.issubset(_CL_FAMILY_ASSET_SYMBOLS):
        return _ineligible("cl_family_only")
    if not enabled:
        return _ineligible("disabled")

    published_at_ms = provider_evidence.get("published_at_ms")
    if not isinstance(published_at_ms, int) or isinstance(published_at_ms, bool):
        return _ineligible("stale")
    eligibility_observed_at_ms = provider_evidence.get("eligibility_observed_at_ms")
    if (
        baseline_at_ms is None
        or published_at_ms <= baseline_at_ms
        or not isinstance(eligibility_observed_at_ms, int)
        or isinstance(eligibility_observed_at_ms, bool)
        or eligibility_observed_at_ms <= baseline_at_ms
    ):
        return _ineligible("baseline")
    if published_at_ms < int(now_ms) - PUSH_SOURCE_FRESHNESS_MS:
        return _ineligible("stale")
    return NewsPushEligibility(eligible=True, ineligible_reason=None)


def _qualifying_asset_symbols(value: object) -> frozenset[str]:
    if not isinstance(value, Mapping):
        return frozenset()
    raw_assets = value.get("coins")
    if not isinstance(raw_assets, list):
        return frozenset()
    return frozenset(
        str(raw_asset["symbol"]).strip().casefold()
        for raw_asset in raw_assets
        if isinstance(raw_asset, Mapping)
        and isinstance(raw_asset.get("symbol"), str)
        and str(raw_asset["symbol"]).strip()
        and isinstance(raw_asset.get("market_type"), str)
        and str(raw_asset["market_type"]).strip()
    )


def _ineligible(reason: NewsPushIneligibleReason) -> NewsPushEligibility:
    return NewsPushEligibility(eligible=False, ineligible_reason=reason)


__all__ = [
    "PUSH_PROVIDER_SCORE_THRESHOLD",
    "PUSH_SOURCE_FRESHNESS_MS",
    "NewsPushEligibility",
    "NewsPushIneligibleReason",
    "evaluate_news_push_eligibility",
]
