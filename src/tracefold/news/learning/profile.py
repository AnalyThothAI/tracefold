"""The release profile every learning artifact is sealed under, and the root that names it.

Its own module because three readers need it and none should import the other two to get it: the dataset
store stamps it into a frozen corpus, the evaluator publishes it as the trusted root, and the release
plane checks a candidate against it (#202 §8).

`TRUSTED_ROOT_SHA` is computed, never pinned to a literal. It is the reader contract, the rubric, the
accepted-review profile and the evaluator version together — change any one and every report says so.

Every threshold here answers "does this corpus carry enough independent evidence to decide something".
#259 removed the one that did not: `natural_days_min`, a count of how many distinct UTC calendar dates
the accepted cases opened on. It measured neither sample size nor time span — two cases two minutes
apart across midnight are two dates, a hundred cases spread over 23 h inside one date are one — and
combined with the active-bundle filter it made a freshly deployed Stable unusable until the calendar
caught up. Out-of-time generalization is proven once, by the Future Holdout in `validation` below, whose
window must begin after a candidate was registered. `natural_day_n` and `window_duration_hours` survive
as dataset diagnostics that tell an operator how concentrated the accepted cases are in time; neither is
a pass/fail input, and no stable-age, window-age or calendar-day gate may replace them.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..review.desk import READER_CONTRACT_SHA256, READER_CONTRACT_VERSION, REVIEW_RUBRIC_VERSION
from .contracts import LEARNING_PROFILE_ID

EVALUATOR_VERSION = "news_candidate_evaluator_v2"

_PROFILE: dict[str, Any] = {
    "profile_id": LEARNING_PROFILE_ID,
    # No `learning_epoch` (#314). The profile names the gates a corpus must clear; which epoch a corpus was
    # frozen in is a per-deployment fact carried by the dataset's own `learning_epoch` and `agent_cohort`,
    # and naming it here made a static document claim to know the running bundle.
    # Coverage, not calendar: independent connected fact clusters by role, the strata both split halves
    # have to carry, and at least one safety case. #259 deleted `natural_days_min` from this set.
    "development": {
        "boundary_clusters_min": 30,
        "retention_clusters_min": 100,
        "negative_clusters_min": 50,
        "strata_min": 3,
        "safety_required": True,
    },
    # The only temporal contract in the profile, and it is a *future* one: the holdout window opens after
    # the candidate was registered, runs at least a day, and has to carry real reviewed clusters.
    "validation": {
        "duration_hours_min": 24,
        "eligible_events_min": 200,
        "planned_primary_clusters": 50,
        "primary_clusters_min": 30,
        "max_review_budget": 100,
    },
    "guardrails": {
        "mean_total_tokens_growth_pct": 0.10,
        "mean_call_growth_pct": 0.10,
        "mean_provider_cost_growth_pct": 0.10,
        "candidate_latency_p95_ms_max": 30_000,
        "candidate_degraded_or_error_rate_max": 0.05,
        "canary_candidate_min_n": 8,
        "critical_regressions": 0,
    },
    "bootstrap": {"seed": 112, "replicates": 2_000, "confidence": 0.95},
    # One kind, since #202: a bounded Prompt patch. A policy change is a configuration release.
    "supported_candidates": ["prompt"],
}
TRUSTED_ROOT_SHA = hashlib.sha256(
    json.dumps(
        {
            "profile": _PROFILE,
            "rubric": REVIEW_RUBRIC_VERSION,
            "reader_contract_version": READER_CONTRACT_VERSION,
            "reader_contract_sha256": READER_CONTRACT_SHA256,
            "evaluator": EVALUATOR_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()


__all__ = ["EVALUATOR_VERSION", "TRUSTED_ROOT_SHA", "_PROFILE"]
