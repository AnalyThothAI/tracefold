"""The three per-target rulers, their outcomes, and the denominators a report states them under (#651 §8).

Every test here is about one of two things: a score that used to be wrong, or a denominator that used to
be missing. The four marked F2P were failing answers before this unit — a restatement pointing at the
wrong card scored a full 1.0, a symbol named in the wrong market compared equal, a faithful paraphrase
scored zero on literal containment, and an unreachable judge scored zero rather than being counted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import dspy  # type: ignore[import-untyped]
import pytest

from tracefold.news.learning.judge import CardClaimAssessment, FactualEvidenceAssessment, FactualEvidenceSupport
from tracefold.news.learning.target_metrics import (
    JUDGE_UNAVAILABLE_SHARE_MAX,
    TASK_OUTPUT_INVALID,
    TASK_OUTPUT_TRUNCATED,
    classification_metric,
    explanation_metric,
    product_scoreboard,
    summarize_target_outcomes,
    understanding_metric,
)

_TAXONOMY: dict[str, Any] = {
    "subject_codes": ["medtop:20000205"],
    "event_family": "product_service_change",
    "change_state": "announced",
    "assertion_status": "confirmed",
}


def _taxonomy(**overrides: Any) -> dict[str, Any]:
    return {**_TAXONOMY, **overrides}


def _semantics(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "novelty": "new_fact",
        "restates": -1,
        "assets": [{"symbol": "SEI", "market_type": "equity", "role": "primary"}],
    }
    values.update(overrides)
    return values


class _ScriptedJudge:
    """A judge that answers from a table, so a ruler test never depends on a model being reachable.

    `unavailable=True` makes every question unanswerable, which is the arm that has to produce a counted
    exclusion rather than a zero.
    """

    def __init__(
        self,
        *,
        supported: bool = True,
        covered: tuple[bool, ...] = (),
        asserted: tuple[bool, ...] = (),
        unavailable: bool = False,
    ) -> None:
        self._supported = supported
        self._covered = covered
        self._asserted = asserted
        self._unavailable = unavailable
        self.questions: list[str] = []

    @property
    def identity(self) -> dict[str, Any]:
        return {"judge_id": "scripted"}

    @property
    def stats(self) -> dict[str, int]:
        return {"attempts": len(self.questions)}

    def facts_supported(self, evidence_json: str, candidate: Any) -> FactualEvidenceAssessment:
        del evidence_json, candidate
        self.questions.append("facts_supported")
        if self._unavailable:
            return FactualEvidenceAssessment(
                status="unavailable", verdict=None, error_code="metric_judge_unavailable"
            )
        return FactualEvidenceAssessment(
            status="answered", verdict=FactualEvidenceSupport(supported_by_evidence=self._supported)
        )

    def key_facts_covered(self, evidence_json: str, candidate: Any, key_facts: Any) -> CardClaimAssessment:
        del evidence_json, candidate
        self.questions.append("key_facts_covered")
        if self._unavailable:
            return CardClaimAssessment(status="unavailable", answers=None, error_code="metric_judge_unavailable")
        answers = self._covered or tuple(True for _ in key_facts)
        return CardClaimAssessment(status="answered", answers=answers)

    def forbidden_claims_asserted(self, evidence_json: str, candidate: Any, claims: Any) -> CardClaimAssessment:
        del evidence_json, candidate
        self.questions.append("forbidden_claims_asserted")
        if self._unavailable:
            return CardClaimAssessment(status="unavailable", answers=None, error_code="metric_judge_unavailable")
        answers = self._asserted or tuple(False for _ in claims)
        return CardClaimAssessment(status="answered", answers=answers)


# ------------------------------------------------------------------------------- classification


def test_classification_scores_the_axes_the_gold_states_and_names_the_wrong_one() -> None:
    result = classification_metric(
        dspy.Example(gold_taxonomy=_taxonomy()),
        dspy.Prediction(taxonomy=_taxonomy(event_family="other", assertion_status="rumor")),
    )

    assert result.outcome == "scored"
    assert result.score == 0.5
    assert result.components["stated_axes"] == ["subject_codes", "event_family", "change_state", "assertion_status"]
    assert result.components["wrong_axes"] == ["event_family", "assertion_status"]
    assert result.components["subject_precision"] == result.components["subject_recall"] == 1.0
    assert "expected=product_service_change" in result.feedback


def test_a_partial_taxonomy_gold_is_scored_only_on_the_axes_it_states() -> None:
    """The mask is the rule that stops a candidate being charged for an axis nobody labelled."""

    partial = {"event_family": "product_service_change", "change_state": "announced", "assertion_status": "confirmed"}
    result = classification_metric(
        dspy.Example(gold_taxonomy=partial),
        dspy.Prediction(taxonomy=_taxonomy(subject_codes=["medtop:20001279"])),
    )

    assert result.components["stated_axes"] == ["event_family", "change_state", "assertion_status"]
    # All three stated axes are right; the unstated subject codes differ and do not enter the mean.
    assert result.score == 1.0


def test_the_persisted_taxonomy_shape_scores_instead_of_reporting_a_schema_failure() -> None:
    """What a release observation carries is `NewsTaxonomyV1`: four axes plus the codebook identity.

    `ModelTaxonomyV1` forbids extras, so validating the stored mapping directly called a perfectly good
    label a schema failure — which is a candidate-local zero, and would have charged a candidate for the
    shape the report happened to hold.
    """

    persisted = {
        **_taxonomy(),
        "taxonomy_version": "news_taxonomy_v1",
        "codebook_sha256": "6f978685c1ffeb6615bfb5dc05eecb9004ebb6f7de8732602e2823d09a12daac",
    }
    result = classification_metric(dspy.Example(gold_taxonomy=_taxonomy()), dspy.Prediction(taxonomy=persisted))

    assert (result.outcome, result.score) == ("scored", 1.0)


def test_a_taxonomy_the_predictor_declined_is_a_counted_failure_not_an_abstention() -> None:
    result = classification_metric(
        dspy.Example(gold_taxonomy=_taxonomy()),
        dspy.Prediction(taxonomy=None, editorial={"taxonomy_status": "unavailable"}),
    )

    assert (result.outcome, result.score) == ("taxonomy_unavailable", 0.0)


@pytest.mark.parametrize(
    ("prediction", "outcome"),
    [
        (dspy.Prediction(task_output_failure=TASK_OUTPUT_TRUNCATED), "technical_failure"),
        (dspy.Prediction(task_output_failure=TASK_OUTPUT_INVALID), "schema_failure"),
        (dspy.Prediction(taxonomy={"event_family": "not-a-family"}), "schema_failure"),
    ],
)
def test_every_candidate_local_classification_failure_scores_zero_and_stays_in_the_denominator(
    prediction: dspy.Prediction, outcome: str
) -> None:
    result = classification_metric(dspy.Example(gold_taxonomy=_taxonomy()), prediction)

    assert (result.outcome, result.score) == (outcome, 0.0)


def test_a_case_with_no_taxonomy_gold_or_the_wrong_applicable_target_is_excluded() -> None:
    no_gold = classification_metric(dspy.Example(), dspy.Prediction(taxonomy=_taxonomy()))
    not_applicable = classification_metric(
        dspy.Example(gold_taxonomy=_taxonomy(), applicable_targets=("explanation",)),
        dspy.Prediction(taxonomy=_taxonomy()),
    )

    assert (no_gold.outcome, no_gold.score) == ("no_gold", None)
    assert (not_applicable.outcome, not_applicable.score) == ("not_applicable", None)


# ------------------------------------------------------------------------------- understanding


def test_a_restatement_pointing_at_the_wrong_told_card_scores_zero_on_novelty() -> None:
    """F2P (#651 §8). Label equality alone scored this 1.0, and `decide()` acts on the index."""

    gold = dspy.Example(
        gold_novelty="restatement",
        gold_duplicate_of="event-b",
        gold_told_event_ids=("event-a", "event-b"),
    )

    right = understanding_metric(gold, dspy.Prediction(semantics=_semantics(novelty="restatement", restates=1)))
    wrong = understanding_metric(gold, dspy.Prediction(semantics=_semantics(novelty="restatement", restates=0)))

    assert right.outcome == wrong.outcome == "scored"
    assert right.score == 1.0 and right.components["restatement_target_correct"] is True
    assert wrong.score == 0.0 and wrong.components["restatement_target_correct"] is False
    assert "event-b" in wrong.feedback


def test_a_restatement_pointing_at_another_member_of_the_same_fact_cluster_is_right() -> None:
    result = understanding_metric(
        dspy.Example(
            gold_novelty="restatement",
            gold_duplicate_of="event-b",
            gold_told_event_ids=("event-c", "event-b"),
            gold_cluster_event_ids=frozenset({"event-b", "event-c"}),
        ),
        dspy.Prediction(semantics=_semantics(novelty="restatement", restates=0)),
    )

    assert result.score == 1.0


def test_a_gold_restatement_target_the_model_never_saw_is_a_counted_retrieval_miss() -> None:
    result = understanding_metric(
        dspy.Example(
            gold_novelty="restatement",
            gold_duplicate_of="event-z",
            gold_told_event_ids=("event-a", "event-b"),
        ),
        dspy.Prediction(semantics=_semantics(novelty="restatement", restates=0)),
    )

    assert (result.outcome, result.score) == ("retrieval_miss", None)
    assert result.components["gold_duplicate_of"] == "event-z"


def test_the_same_symbol_in_the_wrong_market_is_a_known_wrong_market_and_scores_zero() -> None:
    """F2P (#651 §6.2, §8). `SEI` the listed insurer and `SEI` the coin are different instruments."""

    result = understanding_metric(
        dspy.Example(gold_assets=frozenset({("primary", "SEI", "equity")})),
        dspy.Prediction(semantics=_semantics(assets=[{"symbol": "SEI", "market_type": "crypto", "role": "primary"}])),
    )

    assert result.components["known_wrong_market"] == ["SEI/crypto"]
    assert result.components["primary_f1"] == 0.0
    assert result.components["primary_precision"] == result.components["primary_recall"] == 0.0
    assert result.score == 0.5  # the typed primary F1 is zero; the role of the one shared symbol is right
    assert "wrong market" in result.feedback


def test_naming_the_subject_as_a_mention_is_a_role_error_rather_than_a_grounding_one() -> None:
    result = understanding_metric(
        dspy.Example(gold_assets=frozenset({("primary", "SEI", "equity")})),
        dspy.Prediction(
            semantics=_semantics(assets=[{"symbol": "SEI", "market_type": "equity", "role": "mentioned"}])
        ),
    )

    assert result.components["role_accuracy"] == 0.0
    assert result.components["known_wrong_market"] == []
    assert "role" in result.feedback


def test_understanding_excludes_a_case_with_no_accepted_asset_or_novelty_answer() -> None:
    result = understanding_metric(dspy.Example(), dspy.Prediction(semantics=_semantics()))

    assert (result.outcome, result.score) == ("no_gold", None)


@pytest.mark.parametrize(
    ("prediction", "outcome"),
    [
        (dspy.Prediction(task_output_failure=TASK_OUTPUT_TRUNCATED), "technical_failure"),
        (dspy.Prediction(semantics={"novelty": "not-a-novelty"}), "schema_failure"),
    ],
)
def test_every_candidate_local_understanding_failure_scores_zero(
    prediction: dspy.Prediction, outcome: str
) -> None:
    result = understanding_metric(dspy.Example(gold_novelty="new_fact"), prediction)

    assert (result.outcome, result.score) == (outcome, 0.0)


# ------------------------------------------------------------------------------- explanation

_EVIDENCE = '{"event": {"title": "Exchange raises maintenance margin on SOL perpetuals to 6%"}}'
_KEY_FACTS = ("维持保证金由4%上调至6%",)
_FORBIDDEN = ("交易所下调了保证金要求",)


def _explanation_gold(**overrides: Any) -> dspy.Example:
    values: dict[str, Any] = {
        "evidence_json": _EVIDENCE,
        "source_title": "Exchange raises maintenance margin on SOL perpetuals to 6%",
        "gold_key_facts": _KEY_FACTS,
        "gold_forbidden_claims": (),
        "gold_error_types": ("number_unit",),
    }
    values.update(overrides)
    return dspy.Example(**values)


def _card(why: str) -> dspy.Prediction:
    return dspy.Prediction(card={"headline_zh": "交易所上调SOL永续维持保证金", "why_zh": why})


def test_a_faithful_paraphrase_gets_full_credit_and_a_forbidden_claim_gets_zero() -> None:
    """F2P (#651 §7.3). Literal containment scored the paraphrase zero; nothing scored the forbidden claim."""

    paraphrase = "维持保证金从百分之四提高到百分之六，同等仓位要占用更多资金。"
    faithful = explanation_metric(
        _explanation_gold(),
        _card(paraphrase),
        judge=_ScriptedJudge(supported=True, covered=(True,)),
    )
    forbidden = explanation_metric(
        _explanation_gold(gold_forbidden_claims=_FORBIDDEN),
        _card(paraphrase),
        judge=_ScriptedJudge(supported=True, covered=(True,), asserted=(True,)),
    )

    assert (faithful.outcome, faithful.score) == ("scored", 1.0)
    assert faithful.components["score_basis"] == "f1_support_coverage"
    assert (forbidden.outcome, forbidden.score) == ("scored", 0.0)
    assert forbidden.components["forbidden_claims_asserted"] == list(_FORBIDDEN)
    assert _FORBIDDEN[0] in forbidden.feedback


def test_an_unsupported_card_is_zero_however_well_it_covers_the_reviewer_facts() -> None:
    result = explanation_metric(
        _explanation_gold(),
        _card("维持保证金从百分之四提高到百分之六，因美联储推迟降息而被迫收紧。"),
        judge=_ScriptedJudge(supported=False, covered=(True,)),
    )

    assert result.score == 0.0
    assert result.components["evidence_support"] == 0.0
    assert result.components["severe_error_types"] == ["number_unit"]


def test_a_missing_key_fact_lowers_the_score_and_the_feedback_names_it() -> None:
    result = explanation_metric(
        _explanation_gold(gold_key_facts=(*_KEY_FACTS, "调整立即生效")),
        _card("维持保证金从百分之四提高到百分之六，同等仓位要占用更多资金。"),
        judge=_ScriptedJudge(supported=True, covered=(True, False)),
    )

    assert result.components["key_facts_covered"] == 0.5
    assert result.components["key_facts_missing"] == ["调整立即生效"]
    assert result.score == pytest.approx(2 * 1.0 * 0.5 / 1.5, rel=1e-6)
    assert "调整立即生效" in result.feedback


def test_an_unreachable_judge_leaves_the_case_unscored_and_counted_never_zero() -> None:
    """F2P (#651 §8). A provider outage is not a candidate quality, and scoring it zero said it was."""

    judge = _ScriptedJudge(unavailable=True)
    result = explanation_metric(_explanation_gold(), _card("维持保证金上调，占用资金增加。"), judge=judge)

    assert result.outcome == "judge_unavailable"
    assert result.score is None
    assert result.components["question"] == "facts_supported"
    assert judge.questions == ["facts_supported"]


def test_why_value_never_enters_the_explanation_score() -> None:
    result = explanation_metric(
        _explanation_gold(),
        _card("维持保证金从百分之四提高到百分之六，同等仓位要占用更多资金。"),
        judge=_ScriptedJudge(supported=True, covered=(True,)),
    )

    assert result.components["why_value"] == "never_scored"
    assert result.score == 1.0


def test_an_empty_or_rejected_card_is_a_scored_zero_before_any_judge_call() -> None:
    judge = _ScriptedJudge()
    empty = explanation_metric(_explanation_gold(), _card(""), judge=judge)
    gated = explanation_metric(
        _explanation_gold(),
        dspy.Prediction(card={"headline_zh": "交易所上调保证金", "why_zh": "详见 https://example.invalid/1"}),
        judge=judge,
    )

    assert (empty.outcome, empty.score) == ("scored", 0.0)
    assert empty.components["empty_why_zh"] is True
    assert (gated.outcome, gated.score) == ("scored", 0.0)
    assert gated.components["card_lint_gate"]
    assert judge.questions == []


def test_without_a_judge_route_the_explanation_ruler_falls_back_to_literal_containment() -> None:
    """The offline optimizer has a task endpoint and a reflection endpoint and no third one."""

    literal = explanation_metric(_explanation_gold(), _card("维持保证金由4%上调至6%，占用资金增加。"))
    paraphrase = explanation_metric(_explanation_gold(), _card("维持保证金从百分之四提高到百分之六。"))

    assert literal.score == 1.0 and literal.components["score_basis"] == "coverage_only"
    # The documented bound of the no-judge arm: a false miss on a paraphrase, never a false pass.
    assert paraphrase.score == 0.0


# ------------------------------------------------------------------------------- denominators


def _rows(**counts: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for outcome, n in counts.items():
        rows.extend({"outcome": outcome, "score": 1.0 if outcome == "scored" else None} for _ in range(n))
    return rows


def test_the_denominators_partition_the_population_and_exclude_what_nobody_asked() -> None:
    summary = summarize_target_outcomes(
        _rows(
            scored=6,
            schema_failure=1,
            technical_failure=1,
            no_gold=2,
            not_applicable=3,
            judge_unavailable=1,
            retrieval_miss=1,
        ),
        target="explanation",
    )

    assert summary["case_n"] == 15
    assert summary["not_applicable_n"] == 3
    assert summary["applicable_n"] == 12
    assert (
        summary["applicable_n"]
        == summary["scored_n"] + summary["failure_n"] + summary["no_gold_n"]
        + summary["judge_unavailable_n"] + summary["retrieval_miss_n"]
    )
    # Six ones and two candidate-local zeros; the four excluded cases are in no denominator of the mean.
    assert summary["score"] == 0.75
    assert summary["evaluation_unavailable"] is False


def test_a_run_that_could_not_ask_its_judge_often_enough_is_unavailable_rather_than_a_number() -> None:
    fine = summarize_target_outcomes(_rows(scored=8, judge_unavailable=2), target="explanation")
    broken = summarize_target_outcomes(_rows(scored=7, judge_unavailable=3), target="explanation")

    assert JUDGE_UNAVAILABLE_SHARE_MAX == 0.2
    assert fine["judge_unavailable_share"] == 0.2 and fine["evaluation_unavailable"] is False
    assert broken["judge_unavailable_share"] == 0.3 and broken["evaluation_unavailable"] is True


def test_an_unknown_outcome_is_refused_rather_than_silently_dropped() -> None:
    with pytest.raises(ValueError, match="news_learning_target_outcome_unknown:invented"):
        summarize_target_outcomes([{"outcome": "invented"}], target="classification")


def test_random_and_corrective_strata_are_reported_when_the_dataset_carries_them() -> None:
    summary = summarize_target_outcomes(
        [
            {"outcome": "scored", "score": 1.0, "stratum": "random"},
            {"outcome": "scored", "score": 0.5, "stratum": "corrective"},
            {"outcome": "not_applicable", "stratum": "random"},
        ],
        target="understanding",
    )

    assert summary["strata"] == {"corrective": 1, "random": 1}


def test_the_product_scoreboard_publishes_the_five_blocks_and_keeps_value_pending() -> None:
    board = product_scoreboard(
        {
            "classification": [
                {
                    "outcome": "scored",
                    "components": {"subject_precision": 1.0, "subject_recall": 0.5, "subject_f1": 0.666667},
                },
                {"outcome": "taxonomy_unavailable", "components": {}},
            ],
            "understanding": [
                {
                    "outcome": "scored",
                    "components": {
                        "primary_precision": 0.5,
                        "primary_recall": 1.0,
                        "primary_f1": 0.666667,
                        "role_accuracy": 1.0,
                        "known_wrong_market": ["SEI/crypto"],
                        "gold_primaries": ["SEI/equity"],
                        "predicted_primaries": ["SEI/crypto"],
                        "gold_novelty": "restatement",
                        "predicted_novelty": "restatement",
                        "restatement_target_correct": False,
                    },
                },
                {"outcome": "retrieval_miss", "components": {"gold_novelty": "restatement"}},
            ],
            "explanation": [
                {"outcome": "scored", "components": {"evidence_support": 1.0, "key_facts_covered": 0.5}},
            ],
        },
        taxonomy_summary={
            "event_family_accuracy": 0.5,
            "change_state_accuracy": 1.0,
            "assertion_status_accuracy": 1.0,
            "support": {"event_family": {"other": 2}},
            "zero_support": {"event_family": []},
            "confusion": {
                "event_family": [
                    {"gold": "other", "predicted": "other", "n": 1},
                    {"gold": "other", "predicted": "listing", "n": 1},
                ]
            },
        },
        runtime={"physical_call_count": 4, "cost_unobservable": True},
    )

    assert board["schema"] == "tracefold.news.product_scoreboard.v1"
    assert board["classification"]["subject_precision"] == 1.0
    assert board["classification"]["event_family_macro_f1"] is not None
    assert board["classification"]["abstention_coverage"] == 0.5
    assert board["entities"]["known_wrong_market"] == ["SEI/crypto"]
    assert board["entities"]["unrecognized_primaries"] == ["SEI/crypto"]
    assert board["explanation"]["support_rate"] == 1.0
    assert board["explanation"]["key_fact_coverage"] == 0.5
    assert board["explanation"]["value_pending"] is True
    assert board["novelty"]["told_recall"] == 0.5
    assert board["novelty"]["restatement_target_accuracy"] == 0.0
    assert board["novelty"]["retrieval_miss_n"] == 1
    assert board["runtime"]["cost_unobservable"] is True


# ------------------------------------------------------------------------------- production fixtures

_FIDELITY = json.loads(
    (Path(__file__).parents[1] / "fixtures/news/reader_card_fidelity_cases.json").read_text(encoding="utf-8")
)
_RAW_651 = json.loads(
    (Path(__file__).parents[1] / "fixtures/news/issue_651_raw_cases.json").read_text(encoding="utf-8")
)


def test_the_reviewed_production_snapshots_carry_the_explanation_gold_the_ruler_reads() -> None:
    """The two reviewed fidelity snapshots are real accepted reviews, not synthesized ones."""

    reviewed = [case for case in _FIDELITY["cases"] if case["accepted_review"]]

    assert [case["name"] for case in reviewed] == ["platinum", "ingram"]
    for case in reviewed:
        payload = dict(case["accepted_review"]["payload"])
        assert "expected" in payload
        assert set(case["production_judgment"]) == {"verdict", "editorial"}


def test_every_651_snapshot_carries_a_verdict_and_no_accepted_review() -> None:
    """These eight are the typed-market corpus: production answers with nobody's Gold behind them.

    A ruler run over them must therefore produce `no_gold`, never a score — which is the denominator this
    unit exists to make visible, and the reason a mean over "whatever scored" is not a measurement.
    """

    cases = _RAW_651["cases"]

    assert len(cases) == 8
    assert all(len(case["verdicts"]) == 1 for case in cases.values())
    assert all(case["reviews"] == [] for case in cases.values())
    for case in cases.values():
        verdict = dict(case["verdicts"][0]["verdict"])
        result = understanding_metric(dspy.Example(), dspy.Prediction(semantics=verdict))
        assert (result.outcome, result.score) == ("no_gold", None)
