"""#143: the offline baseline, the gold branch, and the one metric both planes share."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from tests.support.news_judgment import recorded_decision, scored_judgment
from tracefold.news.learning import metric as program_metric
from tracefold.news.learning.baseline import (
    BaselineCase,
    build_baseline_cases,
    run_baseline,
)
from tracefold.news.learning.metric import CandidatePrediction, MetricOutcome, accepted_review_metric
from tracefold.news.learning.objective import DevelopmentEpisode
from tracefold.news.models import TRIAGE_POLICY_VERSION
from tracefold.news.program.artifact import load_stable_program_artifact
from tracefold.news.review.desk import EventRubricSubmission


def _frozen_policy_projection() -> dict[str, object]:
    """The exact-policy fields `_production_action` now requires of any policy-scored example.

    The metric no longer falls back to `DEFAULT_POLICY`: an example that cannot prove which policy scored it
    is a different question wearing the same name. Tests carry the defaults explicitly, so a fixture that
    forgets them fails loudly instead of quietly scoring the wrong arm.
    """

    from tracefold.news.artifact_identity import canonical_sha
    from tracefold.news.triage_rules import DEFAULT_POLICY

    values = DEFAULT_POLICY.as_dict()
    return {
        "policy_version": TRIAGE_POLICY_VERSION,
        "policy_values": values,
        "policy_sha256": canonical_sha(values),
    }


_VERDICT: dict[str, Any] = {
    "event_type": "product",
    "assets": [{"symbol": "TSLA", "role": "primary"}],
    "magnitude": 1,
    "direction": "bullish",
    "actionable": True,
    "audience": "us_equity",
    "scope": "single_name",
    "novelty": "new_fact",
    "restates": -1,
    "decision": "push",
    "confidence": 0.9,
    "headline_zh": "特斯拉发布 Cybercab 无人驾驶出租车",
    "why_zh": "新车型进入量产排程，直接改变该名字的交付预期",
    "title_zh": "",
}

_CONTEXT: dict[str, Any] = {
    "schema_version": "news_event_evidence_v1",
    "focus_fact": {"fact_id": "f" * 64, "text": "Tesla launches the Cybercab", "context": ""},
    "card": {
        "event_id": "e" * 64,
        "evidence_version": 1,
        "evidence_sha256": "a" * 64,
        "focus_fact_id": "f" * 64,
        "leader_title": "Tesla launches the Cybercab",
        "leader_description": "",
        "leader_url": "https://example.invalid/1",
        "reporting_origin": "wire",
        "family": "general",
        "admission": "candidate",
        "queue_priority": "normal",
        "asset_class": "equity_or_commodity",
        "engine_type": "news",
        "ingest_mode": "live",
        "storyline_key": "asset:TSLA",
        "comparison_title": "tesla launches the cybercab",
        "raw_first_line": "Tesla launches the Cybercab",
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
    },
}


def _episode(*, dimensions: dict[str, str], expected: dict[str, Any] | None = None) -> DevelopmentEpisode:
    from tracefold.news.program.contracts import TriageContext

    context = TriageContext.from_card(
        _CONTEXT["card"] | {"leader_title": "Tesla launches the Cybercab"},
        watchlist=(),
        told_rows=[],
        now_ms=1787000000000,
        queue_lag_ms=0,
    )
    return DevelopmentEpisode(
        case_id="c" * 64,
        cluster_id="k" * 64,
        stratum="delivered",
        context=context,
        accepted_review={
            "should_push": "should_push",
            "dimensions": dimensions,
            "novelty": {"judgment": "new_fact", "duplicate_of": ""},
            "expected": expected or {},
            "expected_correction": "",
        },
        production_judgment=scored_judgment(_VERDICT),
        policy_metric={
            "gate": {"grounded_assets": ["TSLA"]},
            "storyline": {"title": "Tesla"},
            "seen": [],
            **_frozen_policy_projection(),
        },
    )


def _score(episode: DevelopmentEpisode, verdict: dict[str, Any]) -> MetricOutcome:
    example = program_metric.build_compile_example(episode)
    projection = dict(example.policy_metric)
    projection["recorded_decision_result"] = recorded_decision("push")
    judgment = scored_judgment(verdict)
    return accepted_review_metric(
        dataclasses.replace(example, policy_metric=projection),
        CandidatePrediction(
            verdict=judgment.verdict.model_dump(mode="json"),
            editorial=judgment.editorial.model_dump(mode="json"),
        ),
    )


def test_optimizer_and_baseline_share_one_metric_object() -> None:
    """Two implementations would let the number an operator reads drift from the number GEPA maximizes."""

    assert program_metric.accepted_review_metric is accepted_review_metric
    assert accepted_review_metric.__module__ == "tracefold.news.learning.metric"


def test_failed_dimension_without_gold_is_visible_but_not_scored() -> None:
    """v4 never rewards a blind change when the reviewer supplied no correct value."""

    episode = _episode(dimensions={"magnitude": "fail"})
    changed = _score(episode, {**_VERDICT, "magnitude": 3})
    unchanged = _score(episode, dict(_VERDICT))
    assert changed.score == unchanged.score
    assert ("magnitude", "not_scored_no_gold") in changed.dimension_outcomes
    assert changed.gold_scored_n == 1  # the separately accepted novelty judgment


def test_failed_dimension_with_gold_scores_only_the_stated_value() -> None:
    """The whole point of `news_review_v3`: a coin flip must stop scoring like a repair."""

    dimensions = {"magnitude": "fail", "factual_fidelity": "pass"}
    golded = _episode(dimensions=dimensions, expected={"magnitude": 2})
    ungolded = _episode(dimensions=dimensions)

    right = _score(golded, {**_VERDICT, "magnitude": 2})
    wrong = _score(golded, {**_VERDICT, "magnitude": 3})
    assert right.score > wrong.score

    # Without gold neither candidate gets a point for merely changing the rejected value.
    assert wrong.score < _score(ungolded, {**_VERDICT, "magnitude": 3}).score
    assert right.score == _score(ungolded, {**_VERDICT, "magnitude": 2}).score

    assert right.gold_scored_n == 2 and right.labelled_n == 3
    assert "Accepted correct values: magnitude=2." in right.feedback


def test_gold_asset_grounding_compares_symbol_sets() -> None:
    episode = _episode(
        dimensions={"asset_grounding": "fail"},
        expected={"assets": [{"symbol": "TSLA", "role": "primary"}, {"symbol": "XYZ-NVDA", "role": "mentioned"}]},
    )
    right = _score(
        episode,
        {**_VERDICT, "assets": [{"symbol": "TSLA", "role": "primary"}, {"symbol": "NVDA", "role": "mentioned"}]},
    )
    wrong = _score(episode, {**_VERDICT, "assets": [{"symbol": "TSLA", "role": "primary"}]})
    assert right.score > wrong.score


def test_recorded_mode_scores_the_shipped_action_not_todays_policy() -> None:
    """A retired arm's verdict must stay reproducible after the policy it ran under was replaced."""

    episode = _episode(dimensions={"factual_fidelity": "pass", "magnitude": "pass"})
    held = run_baseline(
        [BaselineCase(episode=episode, recorded_decision_result=recorded_decision("drop"))],
        mode="recorded",
        artifact=load_stable_program_artifact(),
    )
    pushed = run_baseline(
        [BaselineCase(episode=episode, recorded_decision_result=recorded_decision("push"))],
        mode="recorded",
        artifact=load_stable_program_artifact(),
    )
    assert held.cases[0].action == "drop" and pushed.cases[0].action == "push"
    # The reviewer wanted this pushed, so only the pushed arm satisfies the action component.
    assert pushed.scores["case_macro_answered"] > held.scores["case_macro_answered"]
    assert pushed.mode == "recorded" and pushed.population["failure_n"] == 0
    # With nothing unanswered the two means are the same number; they may only diverge on a failure.
    assert pushed.scores["case_macro_answered"] == pushed.scores["case_macro_failure_as_zero"]


def test_recorded_decision_preserves_zero_seen_against_index() -> None:
    """Ledger index zero is the first real match, not the missing-value sentinel."""

    persisted = {**recorded_decision("throttled"), "seen_against": 0, "seen_similarity": 0.91}
    replayed = program_metric.production_decision(
        scored_judgment(_VERDICT),
        {"recorded_decision_result": persisted},
    )

    assert replayed.seen_against == 0


def test_baseline_report_is_content_addressable_and_names_its_subject() -> None:
    episode = _episode(dimensions={"factual_fidelity": "pass"})
    cases = [BaselineCase(episode=episode, recorded_decision_result=recorded_decision("push"))]
    artifact = load_stable_program_artifact()
    first = run_baseline(cases, mode="recorded", artifact=artifact)
    second = run_baseline(cases, mode="recorded", artifact=artifact)
    assert first.report_sha256 == second.report_sha256
    assert first.identity["program_sha256"] == artifact.program_sha256
    assert first.identity["metric_id"] == program_metric.METRIC_ID
    assert first.identity["metric"]["implementation"]["qualname"] == "accepted_review_metric"


def test_hard_gate_keeps_component_denominators_and_effective_weight_mass() -> None:
    dimensions = {
        "trade_impact_breadth": "pass",
        "trade_tradability": "pass",
        "trade_surprise": "pass",
        "trade_development_delta": "pass",
        "trade_channels": "pass",
        "trade_affected_markets": "pass",
        "reader_value": "pass",
        "asset_grounding": "pass",
        "direction": "pass",
        "magnitude": "pass",
        "factual_fidelity": "pass",
        "headline_fidelity": "pass",
        "why_support": "pass",
        "why_value": "pass",
    }
    episode = _episode(dimensions=dimensions)
    episode = episode.model_copy(update={"accepted_review": {**episode.accepted_review, "should_push": "must_hold"}})

    report = run_baseline(
        [BaselineCase(episode=episode, recorded_decision_result=recorded_decision("push"))],
        mode="recorded",
        artifact=load_stable_program_artifact(),
    )

    case = report.cases[0]
    assert case.hard_gate == "must_hold_send"
    assert case.component_scores == {
        "final_action": 0.0,
        "trade_relevance": 0.0,
        "semantics_novelty": 0.0,
        "reader_card": 0.0,
        "reader_card_lint": 0.0,
    }
    assert case.component_denominators == {
        "final_action": 1,
        "trade_relevance": 7,
        "semantics_novelty": 4,
        "reader_card": 4,
        # Six of the seven deterministic card checks; number retention does not apply because this
        # episode's source headline carries no standalone number to preserve.
        "reader_card_lint": 6,
    }
    assert case.effective_weight_mass == 1.1
    assert case.gold_scored_n == 1 and case.labelled_n == 15
    assert report.scores["component_denominators"] == case.component_denominators
    assert report.scores["effective_weight_mass_mean"] == 1.1


def test_build_baseline_cases_drops_loader_only_keys() -> None:
    episode = _episode(dimensions={"factual_fidelity": "pass"})
    raw = {
        **episode.model_dump(mode="json"),
        "event_id": "e" * 64,
        "recorded_decision_result": recorded_decision("drop"),
    }
    cases = build_baseline_cases([raw], action_source="recorded")
    assert cases[0].recorded_decision_result == recorded_decision("drop")
    assert build_baseline_cases([raw], action_source="policy")[0].recorded_decision_result is None


def test_rubric_v4_gold_requires_a_failed_dimension() -> None:
    base = {
        "kind": "event_rubric",
        "should_push": "should_push",
        "novelty": {"judgment": "new_fact"},
        "evidence_refs": ["source:leader:title"],
    }
    ok = EventRubricSubmission(
        **base,
        dimensions={"factual_fidelity": "pass", "magnitude": "fail", "timeliness": "pass"},
        expected={"magnitude": 2},
    )
    assert ok.expected is not None and ok.expected.magnitude == 2
    with pytest.raises(ValueError, match="news_review_expected_requires_failed_dimension:magnitude"):
        EventRubricSubmission(
            **base,
            dimensions={"factual_fidelity": "pass", "magnitude": "pass", "timeliness": "pass"},
            expected={"magnitude": 2},
        )


def test_rubric_v4_submission_without_optional_gold_validates() -> None:

    submission = EventRubricSubmission(
        kind="event_rubric",
        should_push="should_hold",
        dimensions={"factual_fidelity": "pass"},
        novelty={"judgment": "new_fact"},
    )
    assert submission.expected is None
