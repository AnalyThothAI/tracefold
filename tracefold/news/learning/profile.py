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
from collections.abc import Mapping
from typing import Any

from ..review.desk import READER_CONTRACT_SHA256, READER_CONTRACT_VERSION, REVIEW_RUBRIC_VERSION
from .contracts import LEARNING_PROFILE_ID

EVALUATOR_VERSION = "news_candidate_evaluator_v4"

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
        "train_taxonomy_target_clusters_min": 60,
        "train_taxonomy_control_clusters_min": 60,
        "selection_taxonomy_target_clusters_min": 30,
        "selection_taxonomy_control_clusters_min": 30,
        "calibration_clusters_min": 50,
        "calibration_kappa_min": 0.75,
        "calibration_subject_set_f1_min": 0.80,
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


_DEVELOPMENT_COVERAGE_GATES: tuple[tuple[str, str], ...] = (
    ("boundary_cluster_n", "boundary_clusters_min"),
    ("retention_cluster_n", "retention_clusters_min"),
    ("negative_cluster_n", "negative_clusters_min"),
    ("stratum_n", "strata_min"),
    ("train_taxonomy_target_cluster_n", "train_taxonomy_target_clusters_min"),
    ("train_taxonomy_control_cluster_n", "train_taxonomy_control_clusters_min"),
    ("development_selection_taxonomy_target_cluster_n", "selection_taxonomy_target_clusters_min"),
    ("development_selection_taxonomy_control_cluster_n", "selection_taxonomy_control_clusters_min"),
)


def development_coverage_blockers(counts: Mapping[str, Any]) -> tuple[str, ...]:
    """All zero-call development-corpus gates, from sealed counts and the Objective split."""

    requirements = _PROFILE["development"]
    blockers = [
        f"development_{field_name}_insufficient"
        for field_name, threshold_name in _DEVELOPMENT_COVERAGE_GATES
        if int(counts.get(field_name) or 0) < int(requirements[threshold_name])
    ]
    if requirements["safety_required"] and int(counts.get("safety_cluster_n") or 0) == 0:
        blockers.append("development_safety_empty")
    calibration = counts.get("calibration")
    if not isinstance(calibration, Mapping):
        blockers.append("development_calibration_missing")
        return tuple(blockers)
    if int(calibration.get("cluster_n") or 0) < int(requirements["calibration_clusters_min"]):
        blockers.append("development_calibration_cluster_n_insufficient")
    if int(calibration.get("disagreement_unadjudicated_n") or 0):
        blockers.append("development_calibration_adjudication_incomplete")
    blockers.extend(
        f"development_calibration_{axis}_kappa_insufficient"
        for axis in ("event_family", "change_state", "assertion_status")
        if float(dict(calibration.get("kappa") or {}).get(axis) or 0.0) < float(requirements["calibration_kappa_min"])
    )
    if float(calibration.get("subject_mean_set_f1") or 0.0) < float(requirements["calibration_subject_set_f1_min"]):
        blockers.append("development_calibration_subject_set_f1_insufficient")
    return tuple(blockers)


__all__ = ["EVALUATOR_VERSION", "TRUSTED_ROOT_SHA", "_PROFILE", "development_coverage_blockers"]
