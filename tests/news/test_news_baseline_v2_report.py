"""#150: the baseline must answer one question per mode and publish failures instead of hiding them."""

from __future__ import annotations

from typing import Any

import pytest

from tracefold.news.agents.program_baseline import (
    BASELINE_SCHEMA,
    BaselineCase,
    _failed_case,
    run_baseline,
)
from tracefold.news.agents.program_metric import _SEMANTICS_DIMENSIONS, DevelopmentEpisode
from tracefold.news.agents.semantic_program import load_stable_program_artifact
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.semantic_contract import TriageContext
from tracefold.news.triage_rules import DEFAULT_POLICY

_CARD: dict[str, Any] = {
    "event_id": "e" * 64,
    "evidence_version": 1,
    "evidence_sha256": "a" * 64,
    "focus_fact_id": "f" * 64,
    "leader_title": "Tesla commits a new production line",
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
    "comparison_title": "tesla commits a new production line",
    "raw_first_line": "Tesla commits a new production line",
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

_VERDICT: dict[str, Any] = {
    "novelty": "new_fact",
    "restates": -1,
    "event_type": "product",
    "assets": [{"symbol": "TSLA", "role": "primary"}],
    "magnitude": 2,
    "direction": "bullish",
    "actionable": True,
    "audience": "us_equity",
    "scope": "single_name",
    "decision": "push",
    "confidence": 0.9,
    "headline_zh": "特斯拉承诺新增产线",
    "why_zh": "新增产能直接改变该名字的交付预期",
    "title_zh": "",
}


def _policy() -> dict[str, Any]:
    values = DEFAULT_POLICY.as_dict()
    return {
        "policy_version": "news_triage_policy_v8",
        "policy_values": values,
        "policy_sha256": canonical_sha(values),
    }


def _case(index: int, *, cluster: str | None = None, should_push: str = "should_push") -> BaselineCase:
    episode = DevelopmentEpisode(
        case_id=f"{index:064x}",
        cluster_id=cluster or f"{index:064x}",
        stratum="delivered",
        context=TriageContext.from_card(
            {**_CARD, "opened_at_ms": 1787000000000 + index * 1000},
            watchlist=(),
            told_rows=[],
            now_ms=1787000000000 + index * 1000,
            queue_lag_ms=0,
        ),
        accepted_review={
            "should_push": should_push,
            "dimensions": {"factual_fidelity": "pass", "magnitude": "pass"},
            "novelty": {"judgment": "new_fact", "duplicate_of": ""},
        },
        production_verdict=dict(_VERDICT),
        policy_metric={
            "gate": {"grounded_assets": ["TSLA"]},
            "storyline": {"title": "Tesla"},
            "seen": [],
            **_policy(),
        },
    )
    return BaselineCase(episode=episode, recorded_action="push")


def _report(cases: list[BaselineCase]) -> Any:
    return run_baseline(cases, mode="recorded", artifact=load_stable_program_artifact())


def test_failures_are_published_as_a_second_score_not_dropped_from_the_first() -> None:
    """One answered case scoring 1 and one provider failure is not a 1.0 baseline.

    The v1 report computed its only mean over `error_code is None`, so unanswered cases lifted the number by
    disappearing: 29 failures turned a 0.482 lower bound into a published 0.587.
    """

    report = _report([_case(1)])
    answered = report.cases[0]
    assert answered.score == pytest.approx(1.0)

    # Splice in one unanswered case exactly as a live run would record it.
    from tracefold.news.agents.program_baseline import _build_report

    spliced = _build_report(
        [answered, _failed_case(_case(2), "provider_timeout")],
        cases=[_case(1), _case(2)],
        mode="recorded",
        artifact=load_stable_program_artifact(),
        judge=None,
        strict_scores={},
        latency={},
        route={},
        runtime_identity={},
    )
    assert spliced.population == {
        "requested_n": 2,
        "answered_n": 1,
        "failure_n": 1,
        "failure_rate": pytest.approx(0.5),
    }
    assert spliced.scores["case_macro_answered"] == pytest.approx(1.0)
    assert spliced.scores["case_macro_failure_as_zero"] == pytest.approx(0.5)
    assert spliced.failures["by_code"] == {"provider_timeout": 1}


def test_a_report_with_no_answered_case_is_refused_rather_than_scored_zero() -> None:
    from tracefold.news.agents.program_baseline import _build_report

    with pytest.raises(ValueError, match="news_program_baseline_all_cases_failed"):
        _build_report(
            [_failed_case(_case(1), "provider_timeout")],
            cases=[_case(1)],
            mode="recorded",
            artifact=load_stable_program_artifact(),
            judge=None,
            strict_scores={},
            latency={},
            route={},
            runtime_identity={},
        )


def test_one_fact_cluster_gets_one_vote() -> None:
    """Three cases about the same fact must not outweigh a lone case about another."""

    same = [_case(index, cluster="c" * 64) for index in (1, 2, 3)]
    report = _report([*same, _case(9)])
    assert report.scores["cluster_n"] == 2
    assert report.population["requested_n"] == 4


def test_cluster_interval_is_stable_across_input_order() -> None:
    cases = [_case(index, cluster=f"{index % 3:064x}") for index in range(1, 10)]
    forward = _report(cases).scores["cluster_interval_95"]
    backward = _report(list(reversed(cases))).scores["cluster_interval_95"]
    assert forward == backward


def test_prediction_dimensions_move_with_predictions_while_labels_do_not() -> None:
    """The v1 `dimensions` table read the accepted review and never the candidate, so a before/after run
    could change every prediction and stay byte-identical."""

    kept = _report([_case(1)])
    # Same corpus, a candidate that no longer reproduces the accepted card.
    changed_case = _case(1)
    changed = run_baseline(
        [
            BaselineCase(
                episode=changed_case.episode.model_copy(
                    update={"production_verdict": {**_VERDICT, "magnitude": 0, "headline_zh": "别的说法"}}
                ),
                recorded_action="push",
            )
        ],
        mode="recorded",
        artifact=load_stable_program_artifact(),
    )
    assert kept.review_label_distribution == changed.review_label_distribution
    assert kept.prediction_dimensions == changed.prediction_dimensions, (
        "recorded mode scores the stored verdict against itself, so both stay retention hits"
    )
    assert "magnitude" in kept.prediction_dimensions
    assert kept.prediction_dimensions["magnitude"]["retention_hit"] == 1


def test_timeliness_is_delivery_owned_and_still_visible_as_a_label() -> None:
    """It is delivery-owned, and `TriageVerdict` has no timeliness field — scoring it against
    EventSemantics handed a Predictor feedback about latency it cannot repair.

    Dropping it from the report would have been the other half of the same mistake: operators keep labelling
    it, so it stays in the corpus distribution under `delivery` where it cannot be read as something a
    Predictor was graded on.
    """

    assert "timeliness" not in _SEMANTICS_DIMENSIONS
    labelled = _case(1)
    dimensions = {**labelled.episode.accepted_review["dimensions"], "timeliness": "fail"}
    review = {**labelled.episode.accepted_review, "dimensions": dimensions}
    report = _report(
        [
            BaselineCase(
                episode=labelled.episode.model_copy(update={"accepted_review": review}),
                recorded_action="push",
            )
        ]
    )
    assert report.review_label_distribution["delivery"]["timeliness"] == {
        "pass": 0,
        "fail": 1,
        "n": 1,
        "pass_rate": 0.0,
    }
    assert "timeliness" not in report.review_label_distribution["event_semantics"]
    assert "timeliness" not in report.review_label_distribution.get("reader_card", {})
    # No prediction exists to score against it, so it never appears in the candidate's own table.
    assert "timeliness" not in report.prediction_dimensions


def test_report_identity_pins_program_policy_and_corpus() -> None:
    report = _report([_case(1)])
    identity = report.identity
    assert identity["program_sha256"] == load_stable_program_artifact().program_sha256
    assert identity["policy_sha256"] == canonical_sha(DEFAULT_POLICY.as_dict())
    assert identity["policy_values"]["similarity_max"] is not None
    assert report.schema_id == BASELINE_SCHEMA
    assert report.execution_scope, "every mode names what it does and does not execute"


def test_a_report_cannot_cover_two_policies() -> None:
    """A run spanning two arms cannot honestly name one policy, so it refuses instead of picking."""

    other = _case(2)
    drifted = dict(other.episode.policy_metric)
    values = {**DEFAULT_POLICY.as_dict(), "similarity_max": 0.9}
    drifted.update({"policy_values": values, "policy_sha256": canonical_sha(values)})
    mixed = BaselineCase(
        episode=other.episode.model_copy(update={"policy_metric": drifted}),
        recorded_action="push",
    )
    with pytest.raises(ValueError, match="news_program_baseline_policy_not_uniform"):
        _report([_case(1), mixed])
