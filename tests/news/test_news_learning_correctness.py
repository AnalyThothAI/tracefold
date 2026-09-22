"""#663: accepted labels, frozen inputs and optimizer failures are distinct facts."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import dspy
import pytest
from pydantic import ValidationError

from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.learning.dataset import DevelopmentDatasetStore
from tracefold.news.learning.objective import build_gepa_objective_plan
from tracefold.news.learning.optimizer import _understanding_example
from tracefold.news.learning.supervision import project_supervision
from tracefold.news.learning.target_metrics import classification_metric, understanding_metric
from tracefold.news.review.desk import EventRubricSubmission, ExpectedAsset

from .test_news_program_gepa_real import _episode
from .test_news_target_metrics import _semantics, _taxonomy


def test_pass_binds_original_output_and_absent_never_becomes_gold() -> None:
    episode = _episode(1, target=True)
    recorded = episode.production_judgment.model_dump(mode="json")
    review = {"dimensions": {"asset_grounding": "pass"}}
    sealed = project_supervision(review, recorded)
    assert sealed["labels"]["asset_grounding"] == recorded["verdict"]["assets"]
    review["supervision"] = deepcopy(sealed)
    recorded["verdict"]["assets"] = []
    assert project_supervision(review, recorded)["labels"] == sealed["labels"]
    assert project_supervision({"dimensions": {}}, recorded)["targets"] == ()


def test_single_axis_review_to_plan_example_and_metric() -> None:
    review = EventRubricSubmission(dimensions={}, taxonomy={"change_state": "announced"})
    payload = review.model_dump(mode="json", exclude_none=True)
    episode = _episode(1, target=True).model_copy(update={"accepted_review": payload})
    plan = build_gepa_objective_plan((episode, _episode(2, target=False)), "classification")
    assert episode.case_id in plan.optimizer_case_ids
    result = classification_metric(
        dspy.Example(gold_taxonomy=payload["taxonomy"]),
        dspy.Prediction(taxonomy=_taxonomy(change_state="announced", event_family="other")),
    )
    assert result.score == 1
    assert result.components["stated_axes"] == ["change_state"]
    assert result.objective_scores == {"change_state_accuracy": 1}


def test_action_only_does_not_enter_component_optimizer_but_fact_kind_does() -> None:
    action = {"should_push": "must_hold", "dimensions": {}}
    assert "understanding" not in project_supervision(action)["targets"]
    correction = {"dimensions": {"fact_kind": "fail"}, "expected": {"fact_kind": "official_measure"}}
    ep = _episode(1, target=True).model_copy(
        update={"accepted_review": correction, "applicable_targets": ("understanding",)}
    )
    assert "understanding" in project_supervision(correction)["targets"]
    example = _understanding_example(ep)
    result = understanding_metric(example, dspy.Prediction(semantics={**_semantics(), "fact_kind": "statement"}))
    assert result.score == 0
    assert result.components["fact_kind_accuracy"] == 0


def test_retrieval_miss_does_not_hide_an_entity_error() -> None:
    gold = dspy.Example(
        gold_assets=frozenset({("primary", "SEI", "equity")}),
        gold_novelty="restatement",
        gold_duplicate_of="B",
        gold_told_event_ids=("D",),
    )
    result = understanding_metric(
        gold,
        dspy.Prediction(semantics=_semantics(assets=[{"symbol": "SEI", "market_type": "crypto", "role": "primary"}])),
    )
    assert result.outcome == "scored" and result.score == 0
    assert result.components["novelty_excluded"] == "retrieval_miss"
    assert "novelty_accuracy" not in result.objective_scores


def test_only_accepted_equivalence_can_replace_an_absent_exact_duplicate() -> None:
    values = dict(gold_novelty="restatement", gold_duplicate_of="B", gold_told_event_ids=("C",))
    pred = dspy.Prediction(semantics=_semantics(novelty="restatement", restates=0))
    assert understanding_metric(dspy.Example(**values, gold_duplicate_targets=("B", "C")), pred).score == 1
    assert (
        understanding_metric(dspy.Example(**values, gold_cluster_event_ids=("B", "C")), pred).outcome
        == "retrieval_miss"
    )


def test_typed_roles_do_not_depend_on_claim_order() -> None:
    assets = [
        {"symbol": "SEI", "market_type": "crypto", "role": "mentioned"},
        {"symbol": "SEI", "market_type": "equity", "role": "primary"},
    ]
    gold = dspy.Example(gold_assets=frozenset({("primary", "SEI", "equity"), ("mentioned", "SEI", "crypto")}))
    for rows in (assets, list(reversed(assets))):
        assert understanding_metric(gold, dspy.Prediction(semantics=_semantics(assets=rows))).score == 1


@pytest.mark.parametrize("market", [None, "equitty"])
def test_new_market_labels_are_strict(market: str | None) -> None:
    payload = {"symbol": "SEI", **({} if market is None else {"market_type": market})}
    with pytest.raises(ValidationError):
        ExpectedAsset.model_validate(payload)
    assert ExpectedAsset(symbol="SEI", market_type="unknown").market_type == "unknown"


def test_selected_execution_is_reask_not_first_or_latest_context() -> None:
    ep = _episode(1, target=True)
    context = ep.context.model_dump(mode="json")
    judgment = ep.production_judgment
    executions = []
    for index in range(3):
        selected = {**context, "now_ms": context["now_ms"] + index * 1000}
        trace = {
            "context_sha256": canonical_sha(selected),
            "calls": [],
            "verdict_sha256": canonical_sha(judgment.verdict.model_dump(mode="json")),
        }
        executions.append(
            {
                "execution_index": index,
                "context": selected,
                "context_sha256": canonical_sha(selected),
                "trace": trace,
                "recording_call_indices": [],
            }
        )
    row = {
        "verdict": judgment.verdict.model_dump(mode="json"),
        "model_editorial": judgment.editorial.model_dump(mode="json"),
        "judgment_sha256": judgment.scored_judgment_sha256,
        "evidence_sha256": context["evidence"]["evidence_sha256"],
        "trace": {
            "program_executions": executions,
            "program_execution_index": 1,
            "program_trace": executions[1]["trace"],
        },
    }
    assert DevelopmentDatasetStore._selected_context(row) == executions[1]["context"]
    row["trace"]["program_execution_index"] = 0
    with pytest.raises(ValueError, match="selected_execution_mismatch"):
        DevelopmentDatasetStore._selected_context(row)


def test_repeated_export_reads_only_sealed_content() -> None:
    episode = _episode(1, target=True).model_dump(mode="json")
    store = object.__new__(DevelopmentDatasetStore)
    store._load_dataset_payload = lambda sha: {"episodes": [deepcopy(episode)]}
    store._validate_dataset_payload = lambda sha, payload: SimpleNamespace(
        role="development", episodes=tuple(payload["episodes"])
    )
    store._project_episodes = lambda *args, **kwargs: pytest.fail("export reread mutable database")
    first = store.development_compile_export("a" * 64)
    second = store.development_compile_export("a" * 64)
    assert first == second


def test_native_gepa_semantic_judge_budget_and_cache_without_network() -> None:
    from tracefold.news.learning.judge import CardEquivalenceJudge
    from tracefold.news.learning.optimizer import GepaNoProgramChange, _BudgetMeter, run_gepa
    from tracefold.news.program.artifact import load_stable_program_state
    from tracefold.news.review.desk import REVIEW_RUBRIC_VERSION

    from .test_news_learning_optimize import _budget
    from .test_news_program_gepa_real import _CARD_ANSWER, _FixedAnswerTaskLM, _graded_corpus, _models
    from .test_news_program_judge import _ScriptedJudgeLM

    task, reflection, *_ = _models(_FixedAnswerTaskLM({"card": _CARD_ANSWER}))
    corpus = tuple(
        ep.model_copy(
            update={"accepted_review": {**ep.accepted_review, "explanation": {"error_types": ["unsupported_cause"]}}}
        )
        for ep in _graded_corpus()
    )
    endpoint = _ScriptedJudgeLM(facts_supported=False)
    judge = CardEquivalenceJudge(endpoint)
    meter = _BudgetMeter(_budget(max_metric_judge_model_calls=100), imputed_call_cost_microusd=1000)
    judge.bind_run_budget(meter)
    try:
        result = run_gepa(
            base_program=load_stable_program_state(),
            episodes=corpus,
            task_lm=task,
            reflection_lm=reflection,
            target="explanation",
            judge=judge,
            explanation_protocol="semantic",
            max_metric_calls=12,
            seed=456,
            review_rubric_version=REVIEW_RUBRIC_VERSION,
        )
    except GepaNoProgramChange as caught:
        result = caught.result
    assert result.metric["judge_route"] == "configured"
    assert endpoint.calls > 0
    assert meter.metric_judge_model_calls == endpoint.calls
    assert meter.unknown_cost_calls == endpoint.calls
    assert meter.observed_cost_microusd == 0
    assert meter.budget_cost_microusd == endpoint.calls * 1000
    assert meter._reserved_cost == 0
    assert judge.stats["actual_cost_microusd"] is None


@pytest.mark.parametrize("failure", ["unavailable", "type_error", "budget"])
def test_native_gepa_never_turns_judge_failure_into_a_candidate(failure: str) -> None:
    from tracefold.news.learning.judge import CardEquivalenceJudge
    from tracefold.news.learning.optimizer import OptimizationRunTerminated, _BudgetMeter, run_gepa
    from tracefold.news.program.artifact import load_stable_program_state
    from tracefold.news.review.desk import REVIEW_RUBRIC_VERSION

    from .test_news_learning_optimize import _budget
    from .test_news_program_gepa_real import _CARD_ANSWER, _FixedAnswerTaskLM, _graded_corpus, _models
    from .test_news_program_judge import _ScriptedJudgeLM

    task, reflection, *_ = _models(_FixedAnswerTaskLM({"card": _CARD_ANSWER}))
    endpoint = _ScriptedJudgeLM(
        fail=failure == "unavailable",
        steps=[TypeError("judge implementation defect")] if failure == "type_error" else None,
    )
    judge = CardEquivalenceJudge(endpoint)
    meter = _BudgetMeter(
        _budget(max_metric_judge_model_calls=0 if failure == "budget" else 100), imputed_call_cost_microusd=1000
    )
    judge.bind_run_budget(meter)
    exception = TypeError if failure == "type_error" else OptimizationRunTerminated
    with pytest.raises(exception):
        run_gepa(
            base_program=load_stable_program_state(),
            episodes=_graded_corpus(),
            task_lm=task,
            reflection_lm=reflection,
            target="explanation",
            judge=judge,
            explanation_protocol="semantic",
            max_metric_calls=12,
            seed=456,
            review_rubric_version=REVIEW_RUBRIC_VERSION,
        )
    assert endpoint.calls == meter.metric_judge_model_calls
    assert meter._reserved_cost == 0
