"""#150: the baseline must answer one question per mode and publish failures instead of hiding them."""

from __future__ import annotations

from typing import Any

import dspy  # type: ignore[import-untyped]
import pytest

from tests.support.news_judgment import recorded_decision, scored_judgment
from tracefold.news.agents.semantic_program import load_stable_program_artifact
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.learning.baseline import (
    BASELINE_SCHEMA,
    BaselineCase,
    _failed_case,
    run_baseline,
)
from tracefold.news.learning.judge import CardEquivalenceJudge
from tracefold.news.learning.metric import _SEMANTICS_DIMENSIONS, DevelopmentEpisode
from tracefold.news.models import TRIAGE_POLICY_VERSION
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
    "queue_priority": "normal",
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
        "policy_version": TRIAGE_POLICY_VERSION,
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
        production_judgment=scored_judgment(_VERDICT),
        policy_metric={
            "gate": {"grounded_assets": ["TSLA"]},
            "storyline": {"title": "Tesla"},
            "seen": [],
            **_policy(),
        },
    )
    return BaselineCase(episode=episode, recorded_decision_result=recorded_decision("push"))


def _report(cases: list[BaselineCase]) -> Any:
    return run_baseline(cases, mode="recorded", artifact=load_stable_program_artifact())


class _SilentJudgeLM(dspy.BaseLM):  # type: ignore[misc]
    """Never answers, because `recorded` must never ask it. Any call is the bug this pins."""

    def __init__(self) -> None:
        super().__init__(model="scripted/judge")
        self.cache = False
        self.num_retries = 0
        self.calls = 0

    def __call__(self, prompt: Any = None, messages: Any = None, **kwargs: Any) -> list[str]:
        self.calls += 1
        raise AssertionError("recorded mode consulted the semantic judge")


def test_recorded_factual_failure_fails_closed_without_a_judge_call() -> None:
    """Historical scoring never asks a model whether the already-shipped card repaired itself."""

    case = _case(1)
    review = {
        **case.episode.accepted_review,
        "dimensions": {"factual_fidelity": "fail"},
        "novelty": {"judgment": "uncertain", "duplicate_of": ""},
    }
    recorded = BaselineCase(
        episode=case.episode.model_copy(update={"accepted_review": review}),
        recorded_decision_result=case.recorded_decision_result,
    )
    lm = _SilentJudgeLM()

    report = run_baseline(
        [recorded],
        mode="recorded",
        artifact=load_stable_program_artifact(),
        judge=CardEquivalenceJudge(lm),
    )

    assert report.cases[0].score == 0.0
    assert report.cases[0].hard_gate == "factual_contradiction"
    assert lm.calls == 0
    assert report.semantic_judge["attempts"] == 0
    assert report.semantic_judge["model_calls"] == 0


def test_failures_are_published_as_a_second_score_not_dropped_from_the_first() -> None:
    """One answered case scoring 1 and one provider failure is not a 1.0 baseline.

    The v1 report computed its only mean over `error_code is None`, so unanswered cases lifted the number by
    disappearing: 29 failures turned a 0.482 lower bound into a published 0.587.
    """

    report = _report([_case(1)])
    answered = report.cases[0]
    assert answered.score == pytest.approx(1.0)

    # Splice in one unanswered case exactly as a live run would record it.
    from tracefold.news.learning.baseline import _build_report

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


def test_a_run_that_answered_nothing_still_publishes_its_receipt() -> None:
    """ "The route answered nothing" is the most important `runtime_live` result, and it used to be the one
    result that produced no report and no `--out` file — after every provider call had been paid for.

    A null score says "not measured" without reading as a measured zero, which is what the raise was for.
    Everything that makes the run diagnosable survives: per-case error codes, the failure breakdown, the
    requested population and the lower bound.
    """

    from tracefold.news.learning.baseline import _build_report

    report = _build_report(
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
    assert report.scores["case_macro_answered"] is None
    assert report.scores["case_macro_failure_as_zero"] == 0.0
    assert report.population == {
        "requested_n": 1,
        "answered_n": 0,
        "failure_n": 1,
        "failure_rate": 1.0,
    }
    assert report.failures["by_code"] == {"provider_timeout": 1}
    assert report.cases[0].error_code == "provider_timeout"


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
                    update={
                        "production_judgment": scored_judgment({**_VERDICT, "magnitude": 0, "headline_zh": "别的说法"})
                    }
                ),
                recorded_decision_result=recorded_decision("push"),
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


def test_report_exposes_complete_diagnostics_for_each_score_component() -> None:
    """A scalar mass and four denominators cannot show which fields actually support each weight."""

    diagnostics = _report([_case(1)]).scores["component_diagnostics"]

    assert diagnostics["final_action"] == {
        "denominator": 1,
        "effective_weight_mass": 0.45,
        "gold_scored_n": 1,
        "labelled_n": 1,
        "gold_coverage": 1.0,
        "field_n": {"should_push": 1},
    }
    assert diagnostics["trade_relevance"] == {
        "denominator": 0,
        "effective_weight_mass": 0.0,
        "gold_scored_n": 0,
        "labelled_n": 0,
        "gold_coverage": None,
        "field_n": {
            "trade_impact_breadth": 0,
            "trade_tradability": 0,
            "trade_surprise": 0,
            "trade_development_delta": 0,
            "trade_channels": 0,
            "trade_affected_markets": 0,
            "reader_value": 0,
        },
    }
    assert diagnostics["semantics_novelty"] == {
        "denominator": 2,
        "effective_weight_mass": 0.1,
        "gold_scored_n": 1,
        "labelled_n": 2,
        "gold_coverage": 0.5,
        "field_n": {
            "asset_grounding": 0,
            "direction": 0,
            "magnitude": 1,
            "novelty": 1,
        },
    }
    assert diagnostics["reader_card"] == {
        "denominator": 1,
        "effective_weight_mass": 0.1,
        "gold_scored_n": 0,
        "labelled_n": 1,
        "gold_coverage": 0.0,
        "field_n": {
            "factual_fidelity": 1,
            "headline_fidelity": 0,
            "why_support": 0,
            "why_value": 0,
        },
    }


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
                recorded_decision_result=recorded_decision("push"),
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


def test_report_identity_pins_program_and_corpus_and_names_no_unused_policy() -> None:
    report = _report([_case(1)])
    identity = report.identity
    assert identity["program_sha256"] == load_stable_program_artifact().program_sha256
    # `recorded` returns before policy replay. Naming today's configured arm here claimed a dependency the
    # number does not have — the same ambient-state confusion #150 removed from the metric itself.
    assert identity["policy_sha256"] is None
    assert identity["policy_values"] is None
    assert identity["policy_source"] is None
    assert report.schema_id == BASELINE_SCHEMA
    assert report.execution_scope, "every mode names what it does and does not execute"


def test_a_report_cannot_cover_two_policies() -> None:
    """A run spanning two arms cannot honestly name one policy, so it refuses instead of picking.

    Checked on the identity function directly: the uniformity guard only applies to a run that replays
    `decide()`, and `recorded` never does.
    """

    from tracefold.news.learning.baseline import _policy_identity

    other = _case(2)
    drifted = dict(other.episode.policy_metric)
    values = {**DEFAULT_POLICY.as_dict(), "similarity_max": 0.9}
    drifted.update({"policy_values": values, "policy_sha256": canonical_sha(values)})
    replayed = [
        BaselineCase(episode=_case(1).episode),
        BaselineCase(episode=other.episode.model_copy(update={"policy_metric": drifted})),
    ]
    with pytest.raises(ValueError, match="news_program_baseline_policy_not_uniform"):
        _policy_identity(replayed)

    # And a recorded run over the same two cases names no policy at all rather than picking one.
    recorded = [
        BaselineCase(episode=case.episode, recorded_decision_result=recorded_decision("push")) for case in replayed
    ]
    assert _policy_identity(recorded)["policy_sha256"] is None


def test_every_identity_component_moves_the_report_sha() -> None:
    """A report is only evidence if two of them can be compared. Each thing the report claims to pin —
    Program, policy, runtime binding, metric/judge and corpus — must be able to change the SHA on its own,
    or a receipt could be reused for a run it does not describe.
    """

    artifact = load_stable_program_artifact()
    base = run_baseline([_case(1)], mode="recorded", artifact=artifact)
    variants: dict[str, str] = {"baseline": base.report_sha256}
    # Policy is covered in `test_news_baseline_modes.py`: `recorded` returns before policy replay, so its
    # identity correctly names no policy and a policy change cannot move this address.

    # Program: a different state root is a different subject even at the same score.
    other_program = artifact.model_copy(update={"program_sha256": "b" * 64})
    variants["program"] = run_baseline([_case(1)], mode="recorded", artifact=other_program).report_sha256

    # Runtime binding: which models answered is part of what the number means.
    variants["runtime_binding"] = run_baseline(
        [_case(1)], mode="recorded", artifact=artifact, runtime_identity={"slots": {"event_semantics.primary": "x"}}
    ).report_sha256

    # Corpus: one more case is a different question, even a case that scores the same.
    variants["corpus"] = run_baseline([_case(1), _case(2)], mode="recorded", artifact=artifact).report_sha256

    # Metric/judge: the ruler is part of the measurement. `recorded` never calls the judge — the candidate is
    # the production verdict, so the texts already match — but its identity is still what the number means.
    variants["judge"] = run_baseline(
        [_case(1)], mode="recorded", artifact=artifact, judge=CardEquivalenceJudge(_SilentJudgeLM())
    ).report_sha256

    assert len(set(variants.values())) == len(variants), variants


def test_the_metric_version_label_moves_with_the_metric_definition() -> None:
    """v3 (#150): `timeliness` left the scored set, the policy stopped being process-global, and the metric
    started returning typed outcomes. A label that stays put while the definition moves is a label that lies.
    """

    from tracefold.news.learning.metric import METRIC_ID

    assert METRIC_ID.endswith("_v4")
    assert _report([_case(1)]).identity["metric_id"] == METRIC_ID


def test_a_dimension_reports_how_often_nobody_labelled_it() -> None:
    """`n` alone cannot separate "scored on 40 of 242" from "scored on 240 of 242"."""

    unlabelled = _case(2)
    review = {**unlabelled.episode.accepted_review, "dimensions": {"factual_fidelity": "pass"}}
    report = _report(
        [
            _case(1),
            BaselineCase(
                episode=unlabelled.episode.model_copy(update={"accepted_review": review}),
                recorded_decision_result=recorded_decision("push"),
            ),
        ]
    )
    assert report.prediction_dimensions["factual_fidelity"]["not_labelled"] == 0
    assert report.prediction_dimensions["magnitude"]["not_labelled"] == 1


def test_the_published_policy_hash_is_recomputed_not_forwarded() -> None:
    """A backstop behind the pre-flight check, and reachable only from here: the pre-flight refuses a tampered
    corpus before `_build_report` runs, so this pins the second line of defence directly."""

    from tracefold.news.learning.baseline import _policy_identity

    drifted = dict(_case(1).episode.policy_metric)
    drifted["policy_values"] = {**drifted["policy_values"], "similarity_max": 0.9}
    tampered = BaselineCase(episode=_case(1).episode.model_copy(update={"policy_metric": drifted}))
    with pytest.raises(ValueError, match="news_program_baseline_policy_identity_mismatch"):
        _policy_identity([tampered])
