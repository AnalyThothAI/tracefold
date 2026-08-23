"""#150: the metric follows the policy the arm ran, never the policy this process happened to import."""

from __future__ import annotations

from typing import Any

import dspy  # type: ignore[import-untyped]
import pytest

from tracefold.news.agents.program_baseline import BaselineCase, run_baseline
from tracefold.news.agents.program_metric import DevelopmentEpisode, accepted_review_metric, build_compile_example
from tracefold.news.agents.semantic_program import load_stable_program_artifact
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.semantic_contract import TriageContext
from tracefold.news.triage_rules import DEFAULT_POLICY

_CARD: dict[str, Any] = {
    "event_id": "e" * 64,
    "evidence_version": 1,
    "evidence_sha256": "a" * 64,
    "focus_fact_id": "f" * 64,
    "leader_title": "Issuer publishes a routine update",
    "leader_description": "",
    "leader_url": "https://example.invalid/1",
    "reporting_origin": "wire",
    "family": "general",
    "admission": "candidate",
    "priority": "normal",
    "asset_class": "equity_or_commodity",
    "engine_type": "news",
    "ingest_mode": "live",
    "storyline_key": "asset:TSLA",
    "comparison_title": "issuer publishes a routine update",
    "raw_first_line": "Issuer publishes a routine update",
    "grounded_assets": ["TSLA"],
    "watchlist_hits": [],
    "member_count": 1,
    "opened_at_ms": 1787000000000,
    "expires_at_ms": 1787043200000,
    "last_member_at_ms": 1787000000000,
    "macro_lexicon": False,
    "provenance": ["1018"],
    "trace_id": "t" * 32,
    "leader_item_id": "e" * 64,
    "provider_metadata": {},
}

# magnitude 1 sits exactly on the default `min_push_magnitude`, so raising that knob flips the action.
_VERDICT: dict[str, Any] = {
    "novelty": "new_fact",
    "restates": -1,
    "event_type": "product",
    "assets": [{"symbol": "TSLA", "role": "primary"}],
    "magnitude": 1,
    "direction": "bullish",
    "actionable": True,
    "audience": "us_equity",
    "scope": "single_name",
    "decision": "push",
    "confidence": 0.9,
    "headline_zh": "发行人发布例行更新",
    "why_zh": "例行更新不改变该名字的交付或盈利",
    "title_zh": "",
}


def _episode(policy_values: dict[str, Any] | None) -> DevelopmentEpisode:
    projection: dict[str, Any] = {
        "gate": {"grounded_assets": ["TSLA"], "priority": "normal", "admission": "candidate"},
        "storyline": {"title": "Issuer", "family": "general"},
        "seen": [],
    }
    if policy_values is not None:
        projection.update(
            {
                "policy_version": "news_triage_policy_v8",
                "policy_values": policy_values,
                "policy_sha256": canonical_sha(policy_values),
            }
        )
    return DevelopmentEpisode(
        case_id="c" * 64,
        cluster_id="k" * 64,
        stratum="delivered",
        context=TriageContext.from_card(_CARD, watchlist=(), told_rows=[], now_ms=1787000000000, queue_lag_ms=0),
        accepted_review={
            "should_push": "should_push",
            "dimensions": {"factual_fidelity": "pass"},
            "novelty": {"judgment": "new_fact", "duplicate_of": ""},
        },
        production_verdict=dict(_VERDICT),
        policy_metric=projection,
    )


def _action(policy_values: dict[str, Any] | None) -> str:
    example = build_compile_example(_episode(policy_values))
    outcome = accepted_review_metric(example, dspy.Prediction(verdict=dict(_VERDICT)), None, None, None)
    return str(outcome.production_action)


def test_the_metric_follows_the_frozen_policy_not_the_imported_default() -> None:
    """`news.policy` is operator-owned. Before #150 the metric imported `DEFAULT_POLICY` and still called
    itself a production-action metric, so raising `min_push_magnitude` would have left every offline score
    describing an arm production never ran."""

    defaults = DEFAULT_POLICY.as_dict()
    assert defaults["min_push_magnitude"] == 1
    assert _action(defaults) == "push"

    stricter = {**defaults, "min_push_magnitude": 2}
    # Same verdict, same evidence — only the frozen policy differs, and the action follows it.
    assert _action(stricter) == "drop"


def test_a_policy_scored_example_without_a_policy_fails_closed() -> None:
    with pytest.raises(ValueError, match="news_program_metric_policy_values_missing"):
        _action(None)


def test_a_tampered_policy_hash_fails_closed() -> None:
    """A corpus that cannot verify its own policy is a construction bug, not a bad candidate.

    Scoring it 0 would be the wrong kind of fail-closed: the run would finish, publish a number, and blame
    the Program for a corpus defect. It raises instead, so the run stops with the two hashes named.
    """

    values = DEFAULT_POLICY.as_dict()
    episode = _episode(values)
    tampered = dict(episode.policy_metric)
    tampered["policy_values"] = {**values, "similarity_max": 0.9}
    example = build_compile_example(episode.model_copy(update={"policy_metric": tampered}))
    with pytest.raises(ValueError, match=r"news_program_metric_policy_sha256_mismatch:[0-9a-f]{16}!=[0-9a-f]{16}"):
        accepted_review_metric(example, dspy.Prediction(verdict=dict(_VERDICT)), None, None, None)


def test_recorded_scoring_still_bypasses_todays_policy() -> None:
    """A retired arm's shipped action must stay reproducible after the policy it ran under was replaced."""

    case = BaselineCase(episode=_episode(None), recorded_action="push")
    report = run_baseline([case], mode="recorded", artifact=load_stable_program_artifact())
    assert report.cases[0].action == "push"
    # No policy was needed, so the report says so instead of naming one it did not use.
    assert report.identity["policy_sha256"] is None
