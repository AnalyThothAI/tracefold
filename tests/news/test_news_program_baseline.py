"""#143: the offline baseline, the gold branch, and the one metric both planes share."""

from __future__ import annotations

from typing import Any

import dspy  # type: ignore[import-untyped]
import pytest

from tracefold.news.agents import program_compiler, program_metric
from tracefold.news.agents.program_baseline import (
    BaselineCase,
    build_baseline_cases,
    run_baseline,
)
from tracefold.news.agents.program_metric import DevelopmentEpisode, accepted_review_metric
from tracefold.news.agents.semantic_program import load_stable_program_artifact
from tracefold.news.review import EventRubricSubmission

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
    "headline_zh": "特斯拉发布 Cybercab",
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
        "priority": "normal",
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
    from tracefold.news.semantic_contract import TriageContext

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
        production_verdict=dict(_VERDICT),
        policy_metric={"gate": {"grounded_assets": ["TSLA"]}, "storyline": {"title": "Tesla"}, "seen": []},
    )


def _score(episode: DevelopmentEpisode, verdict: dict[str, Any]) -> dspy.Prediction:
    case = BaselineCase(episode=episode, recorded_action="push")
    example = program_metric.build_compile_example(episode)
    projection = dict(example.get("policy_metric") or {})
    projection["recorded_action"] = case.recorded_action
    return accepted_review_metric(
        example.copy(policy_metric=projection), dspy.Prediction(verdict=verdict), None, None, None
    )


def test_optimizer_and_baseline_share_one_metric_object() -> None:
    """Two implementations would let the number an operator reads drift from the number GEPA maximizes."""

    assert program_compiler.accepted_review_metric is accepted_review_metric
    assert accepted_review_metric.__module__ == "tracefold.news.agents.program_metric"


def test_failed_dimension_without_gold_still_scores_for_any_change() -> None:
    """The weak fallback, kept only where a rubric cannot hold a correct value."""

    episode = _episode(dimensions={"magnitude": "fail", "factual_fidelity": "pass"})
    changed = _score(episode, {**_VERDICT, "magnitude": 3})
    unchanged = _score(episode, dict(_VERDICT))
    assert changed.score > unchanged.score
    assert changed.gold_scored_n == 1  # novelty only
    assert changed.labelled_n == 3


def test_failed_dimension_with_gold_scores_only_the_stated_value() -> None:
    """The whole point of `news_review_v3`: a coin flip must stop scoring like a repair."""

    dimensions = {"magnitude": "fail", "factual_fidelity": "pass"}
    golded = _episode(dimensions=dimensions, expected={"magnitude": 2})
    ungolded = _episode(dimensions=dimensions)

    right = _score(golded, {**_VERDICT, "magnitude": 2})
    wrong = _score(golded, {**_VERDICT, "magnitude": 3})
    assert right.score > wrong.score

    # The same wrong answer, scored with and without gold. Without it, "not the old value" was the whole test
    # and a coin flip banked the point; with it, only the stated value does.
    assert wrong.score < _score(ungolded, {**_VERDICT, "magnitude": 3}).score
    assert right.score == _score(ungolded, {**_VERDICT, "magnitude": 2}).score

    assert right.gold_scored_n == 2 and right.labelled_n == 3
    assert "Accepted correct values: magnitude=2." in right.feedback


def test_gold_asset_grounding_compares_symbol_sets() -> None:
    episode = _episode(
        dimensions={"asset_grounding": "fail", "factual_fidelity": "pass"},
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
        [BaselineCase(episode=episode, recorded_action="drop")],
        mode="recorded",
        artifact=load_stable_program_artifact(),
    )
    pushed = run_baseline(
        [BaselineCase(episode=episode, recorded_action="push")],
        mode="recorded",
        artifact=load_stable_program_artifact(),
    )
    assert held.cases[0].action == "drop" and pushed.cases[0].action == "push"
    # The reviewer wanted this pushed, so only the pushed arm satisfies the action component.
    assert pushed.score > held.score
    assert pushed.mode == "recorded" and pushed.provider_failure_n == 0


def test_baseline_report_is_content_addressable_and_names_its_subject() -> None:
    episode = _episode(dimensions={"factual_fidelity": "pass"})
    cases = [BaselineCase(episode=episode, recorded_action="push")]
    artifact = load_stable_program_artifact()
    first = run_baseline(cases, mode="recorded", artifact=artifact)
    second = run_baseline(cases, mode="recorded", artifact=artifact)
    assert first.report_sha256 == second.report_sha256
    assert first.identity["program_sha256"] == artifact.program_sha256
    assert first.identity["metric_id"] == program_metric.METRIC_ID
    assert first.identity["metric"]["implementation"]["qualname"] == "accepted_review_metric"


def test_build_baseline_cases_drops_loader_only_keys() -> None:
    episode = _episode(dimensions={"factual_fidelity": "pass"})
    raw = {**episode.model_dump(mode="json"), "event_id": "e" * 64, "recorded_action": "drop"}
    cases = build_baseline_cases([raw], action_source="recorded")
    assert cases[0].recorded_action == "drop"
    assert build_baseline_cases([raw], action_source="policy")[0].recorded_action == ""


def test_rubric_v3_gold_requires_a_failed_dimension() -> None:
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


def test_rubric_v2_submission_still_validates() -> None:
    """v3 only adds gold. A corpus that had to restart on every rubric revision would never reach the floor."""

    submission = EventRubricSubmission(
        kind="event_rubric",
        should_push="should_hold",
        dimensions={"factual_fidelity": "pass"},
        novelty={"judgment": "new_fact"},
    )
    assert submission.expected is None
