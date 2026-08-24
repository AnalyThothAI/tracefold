from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, ClassVar

import dspy
import pytest

from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.learning.compiler.root import (
    CompileBudget,
    CompileRequest,
    DevelopmentEpisode,
    ProgramCompiler,
    _BudgetedLM,
    _BudgetMeter,
    _honest_split,
    _metric_receipt,
    _optimizer_config_receipt,
    _retrieval_receipt,
    accepted_review_metric,
)
from tracefold.news.learning.compiler.trusted import build_eligible_demo_bank
from tracefold.news.models import TRIAGE_POLICY_VERSION, TriageVerdict
from tracefold.news.program.artifact import (
    EligibleDemoBank,
    load_stable_program_artifact,
)
from tracefold.news.program.contracts import (
    EditorialEnvelope,
    ScoredJudgment,
    TradeRelevanceV1,
    TriageContext,
)
from tracefold.news.program.dspy_adapter import (
    DspyStrictJSONAdapter,
    ExactProviderCallCapture,
    ExactProviderMetadata,
)
from tracefold.news.told_context import ToldLedgerEntry


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


def _relevance(**overrides: Any) -> TradeRelevanceV1:
    values: dict[str, Any] = {
        "impact_breadth": "single_instrument",
        "tradability": "direct",
        "surprise": "material_vs_expectation",
        "development_delta": "state_change",
        "channels": ["earnings_cashflow"],
        "affected_markets": ["single_asset"],
        "reader_value": "realtime",
    }
    values.update(overrides)
    return TradeRelevanceV1.model_validate(values)


def _metric_verdict(**overrides: Any) -> dict[str, Any]:
    verdict: dict[str, Any] = {
        "decision": "push",
        "novelty": "new_fact",
        "restates": -1,
        "event_type": "filing",
        "assets": [{"symbol": "ABC", "role": "primary"}],
        "direction": "bullish",
        "scope": "single_name",
        "magnitude": 2,
        "actionable": True,
        "confidence": 0.8,
        "audience": "us_equity",
        "headline_zh": "发行人提交重大更新",
        "title_zh": "",
        "why_zh": "时间表发生变化。",
    }
    verdict.update(overrides)
    return verdict


def _judgment(*, relevance: TradeRelevanceV1 | None = None, **verdict: Any) -> ScoredJudgment:
    return ScoredJudgment.issue(
        verdict=TriageVerdict.model_validate(_metric_verdict(**verdict)),
        editorial=EditorialEnvelope.issue(
            editorial_origin="model",
            relevance=relevance or _relevance(),
        ),
    )


class _NoopJudge:
    def __init__(self) -> None:
        self.identity = {"judge_id": "test/noop", "failure_cache": False}
        self.stats = {
            "attempts": 0,
            "model_calls": 0,
            "cache_entries": 0,
            "failures": 0,
            "actual_cost_microusd": 0,
        }

    def retains(self, *_args: Any, **_kwargs: Any) -> bool:
        return False


class _EvidenceJudge(_NoopJudge):
    def __init__(self, *, supported: bool) -> None:
        super().__init__()
        self.supported = supported
        self.evidence: list[str] = []

    def facts_supported(self, evidence_json: str, _candidate: dict[str, Any]) -> bool:
        self.evidence.append(evidence_json)
        return self.supported


class _MeteredFakeLM:
    cache = False
    num_retries = 0

    def __init__(
        self,
        model: str,
        *,
        cost: float = 0.000001,
        api_base: str = "https://compiler.test/v1",
    ) -> None:
        self.model = model
        self.cost = cost
        self.kwargs = {"api_base": api_base}
        self.history: list[dict[str, Any]] = []
        self._capture: ExactProviderCallCapture | None = None

    @contextmanager
    def observe_exact_call(self) -> Iterator[ExactProviderCallCapture]:
        capture = ExactProviderCallCapture()
        self._capture = capture
        try:
            yield capture
        finally:
            self._capture = None

    def __call__(self, *args: Any, **kwargs: Any) -> list[str]:
        del args, kwargs
        # Shared history is deliberately wrong; the compiler must use the call-local observation.
        self.history.append({"uuid": f"{self.model}:{len(self.history)}", "cost": 0.5})
        assert self._capture is not None
        self._capture.record_metadata(
            ExactProviderMetadata(provider_cost_microusd=round(self.cost * 1_000_000), finish_reason="stop")
        )
        return ["unused"]

    async def acall(self, *args: Any, **kwargs: Any) -> list[str]:
        return self(*args, **kwargs)


class _FakeGEPA:
    calls: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, metric: Any, **kwargs: Any) -> None:
        self.metric = metric
        self.kwargs = kwargs
        self.calls.append(kwargs)

    def compile(self, student: Any, *, trainset: list[Any], teacher: None, valset: list[Any]) -> Any:
        assert teacher is None
        # The whole point of the split: GEPA optimizes on one set and picks the winner on another.
        assert trainset and valset
        assert {example.case_id for example in trainset}.isdisjoint({example.case_id for example in valset})
        assert {example.cluster_id for example in trainset}.isdisjoint({example.cluster_id for example in valset})
        assert trainset[0].evidence_json.startswith("<tracefold-untrusted-event-json-v1>\n")
        assert trainset[0].evidence_json.endswith("\n</tracefold-untrusted-event-json-v1>")
        assert isinstance(dspy.settings.adapter, DspyStrictJSONAdapter)
        assert dspy.settings.disable_history is True
        dspy.settings.lm(prompt="task budget probe")
        self.kwargs["reflection_lm"](prompt="reflection budget probe")
        student.event_semantics.signature = student.event_semantics.signature.with_instructions(
            student.event_semantics.signature.instructions + "\nCompiler candidate instruction."
        )
        student.detailed_results = SimpleNamespace(
            parents=[[None], [0]],
            val_aggregate_scores=[0.4, 0.7],
            discovery_eval_counts=[1, 2],
            total_metric_calls=2,
            num_full_val_evals=1,
            seed=17,
            best_idx=1,
        )
        return student


def _request(*, max_calls: int = 4) -> CompileRequest:
    context = TriageContext.from_card(
        {
            "event_id": "event-1",
            "evidence_version": 1,
            "evidence_sha256": "e" * 64,
            "focus_fact_id": "fact-1",
            "leader_title": "Issuer files a material update",
            "leader_description": "The filing changes the expected timetable.",
            "opened_at_ms": 1_800_000_000_000,
            "storyline_key": "asset:ABC",
            "grounded_assets": ["ABC"],
            "asset_class": "equity",
            "admission": "candidate",
        },
        watchlist=(),
        told_rows=(),
        now_ms=1_800_000_000_000,
        queue_lag_ms=0,
    )
    return CompileRequest(
        development_dataset_sha="d" * 64,
        review_rubric_version="news_review_v4",
        episodes=tuple(
            {
                "case_id": f"case-{cluster}-{name}",
                "cluster_id": f"cluster-{cluster}",
                "stratum": "review_failure",
                "context": context,
                "policy_metric": {
                    "gate": {
                        "grounded_assets": ["ABC"],
                        "watchlist_symbols": [],
                        "admission": "candidate",
                    },
                    "storyline": {"title": "Issuer files a material update", "family": "filing"},
                    "seen": [],
                    "told": [],
                    "recorded_decision_result": {
                        "final": "push",
                        "rule_baseline": "push",
                        "override_rule": None,
                        "throttled_by": None,
                        "watchlist_hits": [],
                        "seen_similarity": None,
                        "seen_against": -1,
                        "seen_scope": "",
                    },
                    **_frozen_policy_projection(),
                },
                "accepted_review": {
                    "should_push": should_push,
                    "dimensions": {"direction": "fail", "factual_fidelity": "pass"},
                    "novelty": {"judgment": "new_fact", "duplicate_of": ""},
                    "expected_correction": "The direction must follow the filing's actual mechanism.",
                },
                "production_judgment": _judgment(direction="neutral").model_dump(mode="json"),
            }
            # Two clusters so the split is possible, and both halves carry every required stratum:
            # a safety/positive case and a safety/negative one. Anything less fails closed.
            for cluster in (1, 2)
            for name, should_push in (("push", "must_push"), ("hold", "must_hold"))
        ),
        budget=CompileBudget(
            max_metric_calls=3,
            max_task_model_calls=max_calls,
            max_reflection_model_calls=4,
            max_metric_judge_model_calls=16,
            max_cost_microusd=20,
            max_call_cost_microusd=5,
            seed=17,
        ),
    )


def _compiler(base: Any | None = None) -> ProgramCompiler:
    return ProgramCompiler(
        base_artifact=base or load_stable_program_artifact(),
        eligible_demo_bank=EligibleDemoBank.issue(()),
        task_lm=_MeteredFakeLM("task/model", cost=0.000002),  # type: ignore[arg-type]
        reflection_lm=_MeteredFakeLM("reflection/model", cost=0.000003),  # type: ignore[arg-type]
        optimizer_factory=_FakeGEPA,
        judge=_NoopJudge(),
    )


def test_compiler_budget_uses_exact_call_metadata_not_shared_history() -> None:
    lm = _MeteredFakeLM("task/model", cost=0.000002)
    meter = _BudgetMeter(
        CompileBudget(
            max_metric_calls=1,
            max_task_model_calls=1,
            max_reflection_model_calls=1,
            max_metric_judge_model_calls=1,
            max_cost_microusd=10,
            max_call_cost_microusd=10,
            seed=17,
        )
    )

    assert _BudgetedLM(lm, role="task", meter=meter)(prompt="probe") == ["unused"]  # type: ignore[arg-type]
    assert lm.history[-1]["cost"] == 0.5
    assert meter.actual_cost_microusd == 2


def test_compiler_charges_a_provider_response_even_when_the_lm_raises_afterward() -> None:
    class ResponseThenErrorLM(_MeteredFakeLM):
        def __call__(self, *args: Any, **kwargs: Any) -> list[str]:
            super().__call__(*args, **kwargs)
            raise RuntimeError("parse failed after provider response")

    lm = ResponseThenErrorLM("task/model", cost=0.000004)
    meter = _BudgetMeter(
        CompileBudget(
            max_metric_calls=1,
            max_task_model_calls=1,
            max_reflection_model_calls=1,
            max_metric_judge_model_calls=1,
            max_cost_microusd=10,
            max_call_cost_microusd=10,
            seed=17,
        )
    )

    with pytest.raises(RuntimeError, match="parse failed"):
        _BudgetedLM(lm, role="task", meter=meter)(prompt="probe")  # type: ignore[arg-type]
    assert meter.task_model_calls == 1
    assert meter.actual_cost_microusd == 4


def test_compile_is_bounded_development_only_and_returns_only_typed_patch() -> None:
    _FakeGEPA.calls.clear()

    result = _compiler().compile(_request())

    kwargs = _FakeGEPA.calls[-1]
    assert kwargs["auto"] is None
    assert kwargs["max_full_evals"] is None
    assert kwargs["max_metric_calls"] == 3
    assert kwargs["track_stats"] is True
    assert kwargs["track_best_outputs"] is False
    assert result.patch.learning_epoch == "program_v7"
    assert result.patch.parent_program_sha256 == load_stable_program_artifact().program_sha256
    assert result.patch.patch_sha256 == result.patch.computed_sha256()
    assert [strategy.predictor for strategy in result.patch.learned_strategies] == [
        "event_semantics",
        "reader_card",
    ]
    assert result.metric_calls == 2
    assert result.task_model_calls == 1
    assert result.reflection_model_calls == 1
    assert result.metric_judge_model_calls == 0
    assert result.actual_cost_microusd == 5
    assert result.failure_cluster_ids == ("cluster-1", "cluster-2")
    assert result.target_dimensions == ("direction", "should_push")
    receipts = result.receipt_payloads.model_dump(mode="json")
    assert receipts["optimizer_config"]["dspy_context"]["disable_history"] is True
    assert "source" in receipts["metric"]["implementation"]
    assert "artifact" not in type(result).model_fields
    assert "proposal_input" not in type(result).model_fields


def test_eligible_demo_bank_uses_the_same_delimited_model_evidence_as_compile_examples() -> None:
    episode = _request().episodes[0].model_dump(mode="json")
    episode["accepted_review"] = {
        "review_id": "review-1",
        "should_push": "should_push",
        "dimensions": {
            "factual_fidelity": "pass",
            "headline_fidelity": "pass",
            "why_support": "pass",
            "why_value": "pass",
        },
        "novelty": {"judgment": "new_fact", "duplicate_of": ""},
        "expected_correction": "",
    }
    episode["production_judgment"] = _judgment(
        assets=[],
        direction="neutral",
        magnitude=1,
        headline_zh="发行人提交重大更新",
        why_zh="时间表发生变化。",
    ).model_dump(mode="json")
    case = {
        "case_id": episode["case_id"],
        "cluster_id": episode["cluster_id"],
        "evidence_sha256": "e" * 64,
    }
    payload = {
        "role": "development",
        "learning_epoch": "program_v7",
        "cases": [case],
    }
    dataset_sha = canonical_sha({"kind": "dataset", "payload": payload})

    bank = build_eligible_demo_bank(
        dataset_sha=dataset_sha,
        dataset_payload=payload,
        episodes=(episode,),
    )

    assert len(bank.records) == 2
    for record in bank.records:
        evidence_json = record.signature_inputs["evidence_json"]
        assert evidence_json.startswith("<tracefold-untrusted-event-json-v1>\n")
        assert evidence_json.endswith("\n</tracefold-untrusted-event-json-v1>")


def test_non_root_program_cannot_be_a_compiler_parent() -> None:
    non_root = load_stable_program_artifact().model_copy(update={"parent_program_sha256": "f" * 64})
    with pytest.raises(ValueError, match="parent_must_be_exact_stable_root"):
        _compiler(non_root)


def test_task_and_reflection_calls_have_independent_explicit_budgets() -> None:
    result = _compiler().compile(_request(max_calls=1))

    assert result.task_model_calls == 1
    assert result.reflection_model_calls == 1


def test_non_json_trajectory_value_fails_closed() -> None:
    class _UnsafeGEPA(_FakeGEPA):
        def compile(self, student: Any, *, trainset: list[Any], teacher: None, valset: list[Any]) -> Any:
            student = super().compile(student, trainset=trainset, teacher=teacher, valset=valset)
            student.detailed_results.parents = [[object()]]
            return student

    compiler = ProgramCompiler(
        base_artifact=load_stable_program_artifact(),
        eligible_demo_bank=EligibleDemoBank.issue(()),
        task_lm=_MeteredFakeLM("task/model"),  # type: ignore[arg-type]
        reflection_lm=_MeteredFakeLM("reflection/model"),  # type: ignore[arg-type]
        optimizer_factory=_UnsafeGEPA,
        judge=_NoopJudge(),
    )

    with pytest.raises(TypeError, match="non_json_receipt_value"):
        compiler.compile(_request())


def test_nonfinite_trajectory_value_fails_closed() -> None:
    class _NonfiniteGEPA(_FakeGEPA):
        def compile(self, student: Any, *, trainset: list[Any], teacher: None, valset: list[Any]) -> Any:
            student = super().compile(student, trainset=trainset, teacher=teacher, valset=valset)
            student.detailed_results.val_aggregate_scores = [float("nan")]
            return student

    compiler = ProgramCompiler(
        base_artifact=load_stable_program_artifact(),
        eligible_demo_bank=EligibleDemoBank.issue(()),
        task_lm=_MeteredFakeLM("task/model"),  # type: ignore[arg-type]
        reflection_lm=_MeteredFakeLM("reflection/model"),  # type: ignore[arg-type]
        optimizer_factory=_NonfiniteGEPA,
        judge=_NoopJudge(),
    )

    with pytest.raises(TypeError, match="nonfinite_receipt_value"):
        compiler.compile(_request())


def test_metric_receipt_hash_binds_the_executed_implementation_source() -> None:
    def changed_metric(*args: Any, **kwargs: Any) -> dspy.Prediction:
        del args, kwargs
        return dspy.Prediction(score=0.5, feedback="changed")

    original = _metric_receipt(accepted_review_metric, review_rubric_version="news_review_v4")
    changed = _metric_receipt(changed_metric, review_rubric_version="news_review_v4")

    assert original["metric_id"] == changed["metric_id"]
    assert canonical_sha(original) != canonical_sha(changed)


def test_optimizer_config_receipt_hash_binds_every_scalar_and_both_model_identities() -> None:
    constructor = {
        "max_metric_calls": 3,
        "reflection_minibatch_size": 1,
        "seed": 17,
        "track_stats": True,
    }
    metric_sha = "a" * 64
    base = _optimizer_config_receipt(
        constructor=constructor,
        task_lm=_MeteredFakeLM("task/model"),  # type: ignore[arg-type]
        reflection_lm=_MeteredFakeLM("reflection/model"),  # type: ignore[arg-type]
        optimizer_factory=_FakeGEPA,
        metric_sha256=metric_sha,
        example_count=1,
        train_count=1,
        val_count=1,
    )
    changed_scalar = _optimizer_config_receipt(
        constructor={**constructor, "seed": 18},
        task_lm=_MeteredFakeLM("task/model"),  # type: ignore[arg-type]
        reflection_lm=_MeteredFakeLM("reflection/model"),  # type: ignore[arg-type]
        optimizer_factory=_FakeGEPA,
        metric_sha256=metric_sha,
        example_count=1,
        train_count=1,
        val_count=1,
    )
    changed_model = _optimizer_config_receipt(
        constructor=constructor,
        task_lm=_MeteredFakeLM("task/other-model"),  # type: ignore[arg-type]
        reflection_lm=_MeteredFakeLM("reflection/model"),  # type: ignore[arg-type]
        optimizer_factory=_FakeGEPA,
        metric_sha256=metric_sha,
        example_count=1,
        train_count=1,
        val_count=1,
    )
    changed_endpoint = _optimizer_config_receipt(
        constructor=constructor,
        task_lm=_MeteredFakeLM("task/model", api_base="https://other-compiler.test/v1"),  # type: ignore[arg-type]
        reflection_lm=_MeteredFakeLM("reflection/model"),  # type: ignore[arg-type]
        optimizer_factory=_FakeGEPA,
        metric_sha256=metric_sha,
        example_count=1,
        train_count=1,
        val_count=1,
    )

    assert canonical_sha(base) != canonical_sha(changed_scalar)
    assert canonical_sha(base) != canonical_sha(changed_model)
    assert canonical_sha(base) != canonical_sha(changed_endpoint)
    assert "compiler.test" not in repr(base)


# ---------------------------------------------------------------- production-action metric
def _metric_gold(**overrides: Any) -> Any:
    gold: dict[str, Any] = {
        "accepted_review": {
            "should_push": "should_push",
            "dimensions": {},
            "novelty": {"judgment": "uncertain", "duplicate_of": ""},
        },
        "production_judgment": None,
        "policy_metric": {
            "gate": {
                "grounded_assets": ["ABC"],
                "watchlist_symbols": [],
                "admission": "candidate",
            },
            "storyline": {"title": "Issuer files a material update", "family": "filing"},
            "seen": [],
            "told": [],
            **_frozen_policy_projection(),
        },
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(gold.get(key), dict):
            gold[key] = {**gold[key], **value}
        else:
            gold[key] = value
    return dspy.Example(**gold)


def _score(gold: Any, verdict: dict[str, Any], pred_name: str | None = None) -> Any:
    return accepted_review_metric(
        gold,
        dspy.Prediction(
            verdict=verdict,
            editorial=EditorialEnvelope.issue(editorial_origin="model", relevance=_relevance()),
        ),
        None,
        pred_name,
        None,
        None,
    )


def test_metric_scores_the_production_action_not_the_models_intent() -> None:
    """`decision` is an intent `decide()` routinely overrides. A grounded restatement is dropped whatever the
    model asked for, so a metric reading `decision` would reward a card the reader never receives."""

    told = [
        {"i": 0, "event_id": "prior", "dir": "bullish", "headline_zh": "发行人提交重大更新", "grounded_assets": ["ABC"]}
    ]
    gold = _metric_gold(
        accepted_review={"should_push": "should_hold"},
        policy_metric={"told": told, "seen": [dict(told[0], direction="bullish")], **_frozen_policy_projection()},
    )
    # The model asks to push; the policy drops it as a grounded restatement, which is what the reviewer wanted.
    restatement = _metric_verdict(decision="push", novelty="restatement", restates=0)
    assert _score(gold, restatement).score == 1.0

    # The same verdict scored against the old contract — the model's own `decision` — would have failed it.
    assert restatement["decision"] == "push"


def test_metric_sees_the_stale_source_withhold_production_applies() -> None:
    """#154: `decide()` can turn a push into `throttled` on artifact age.

    CLAUDE.md pins that `news learning baseline` runs literally the same metric object as production. If
    `source_age_s` is dropped from the projection the metric scores `push` where the reader got nothing, and the
    optimizer is rewarded for an action production would not have taken.
    """

    pushed = _metric_verdict(decision="push")
    fresh = _metric_gold(policy_metric={"gate": {"source_age_s": 2}})
    stale = _metric_gold(policy_metric={"gate": {"source_age_s": 16 * 3600}})

    assert _score(fresh, pushed).production_action == "push"
    assert _score(stale, pushed).production_action == "throttled"

    # And the reviewer's verdict is scored against that action, not the model's intent: a `should_hold` review
    # is satisfied by the stale withhold and failed by the fresh push.
    held = {"accepted_review": {"should_push": "should_hold"}}
    assert _score(_metric_gold(**held, policy_metric={"gate": {"source_age_s": 16 * 3600}}), pushed).score == 1.0
    assert _score(_metric_gold(**held, policy_metric={"gate": {"source_age_s": 2}}), pushed).score < 1.0


def test_metric_hard_gates_cannot_be_averaged_away_by_retention_anchors() -> None:
    gold = _metric_gold(
        accepted_review={
            "should_push": "must_push",
            "dimensions": {"headline_fidelity": "pass", "why_support": "pass", "direction": "pass"},
        },
        production_judgment=_judgment(),
    )
    # Four accepted dimensions agree, but the reader never gets a fact the reviewer marked `must_push`.
    missed = _metric_verdict(event_type="noise", magnitude=0, actionable=False, decision="drop")
    result = _score(gold, missed)
    assert result.score == 0.0
    assert "must receive" in result.feedback


def test_metric_rejects_an_ungrounded_primary_asset_outright() -> None:
    gold = _metric_gold()
    hallucinated = _metric_verdict(assets=[{"symbol": "XYZ", "role": "primary"}])
    result = _score(gold, hallucinated)
    assert result.score == 0.0 and "XYZ" in result.feedback


def test_factual_failure_must_be_repaired_against_evidence_not_merely_reworded() -> None:
    gold = _metric_gold(
        accepted_review={
            "should_push": "should_push",
            "dimensions": {"factual_fidelity": "fail"},
            "novelty": {"judgment": "uncertain", "duplicate_of": ""},
        },
        production_judgment=_judgment(),
    ).copy(card_evidence_json="<trusted-test-evidence>issuer filed no update</trusted-test-evidence>")
    changed = _metric_verdict(headline_zh="发行人已提交重大更新")
    prediction = dspy.Prediction(
        verdict=changed,
        editorial=EditorialEnvelope.issue(editorial_origin="model", relevance=_relevance()),
    )
    rejecting = _EvidenceJudge(supported=False)
    accepting = _EvidenceJudge(supported=True)

    rejected = accepted_review_metric(gold, prediction, judge=rejecting)
    verified = accepted_review_metric(gold, prediction, judge=accepting)

    assert rejected.hard_gate == "factual_contradiction" and rejected.score == 0.0
    assert rejecting.evidence == [gold.card_evidence_json]
    assert verified.hard_gate == "" and verified.score == 1.0
    assert accepting.evidence == [gold.card_evidence_json]


def test_factual_failure_fails_closed_without_an_evidence_judge() -> None:
    gold = _metric_gold(
        accepted_review={
            "should_push": "should_push",
            "dimensions": {"factual_fidelity": "fail"},
            "novelty": {"judgment": "uncertain", "duplicate_of": ""},
        },
        production_judgment=_judgment(),
    ).copy(card_evidence_json="<trusted-test-evidence>issuer filed no update</trusted-test-evidence>")
    changed = _metric_verdict(headline_zh="任意改写不能证明事实已经修复")

    outcome = _score(gold, changed)

    assert outcome.hard_gate == "factual_contradiction"
    assert "could not be verified" in outcome.feedback


def test_metric_does_not_guess_a_failed_dimension_without_exact_gold() -> None:
    """A rejected value and no replacement value do not reveal the correct answer."""

    labelled = _metric_gold(
        accepted_review={"should_push": "uncertain", "dimensions": {"direction": "fail", "magnitude": "uncertain"}},
        production_judgment=_judgment(direction="neutral"),
    )
    changed = _score(labelled, _metric_verdict(direction="bullish"))
    unchanged = _score(labelled, _metric_verdict(direction="neutral"))
    assert changed.score == unchanged.score == 0.0
    assert changed.dimension_outcomes == (("direction", "not_scored_no_gold"),)


def test_metric_feedback_never_asks_a_predictor_to_repair_what_it_cannot_cause() -> None:
    gold = _metric_gold(
        accepted_review={
            "should_push": "should_push",
            "dimensions": {"asset_grounding": "fail", "why_support": "fail"},
            "novelty": {"judgment": "new_fact", "duplicate_of": ""},
        },
        production_judgment=_judgment(),
    )
    semantics = _score(gold, _metric_verdict(), "event_semantics").feedback
    card = _score(gold, _metric_verdict(), "reader_card").feedback
    assert "asset_grounding" in semantics and "why_support" not in semantics
    assert "why_support" in card and "asset_grounding" not in card


def test_reader_card_ignores_claimed_action_ownership_for_an_ordinary_action_mismatch() -> None:
    gold = _metric_gold(
        accepted_review={"should_push": "must_push"},
        policy_metric={"action_feedback_owner": "headline_duplicate"},
    )

    outcome = _score(gold, _metric_verdict(magnitude=1), "reader_card")

    assert outcome.hard_gate == "must_push_miss"
    assert outcome.production_rule == "trade_relevance_inconsistent"
    assert outcome.production_throttled_by == ""
    assert "must receive" not in outcome.feedback
    assert "No ReaderCard-owned correction" in outcome.feedback


def test_reader_card_gets_action_feedback_for_an_exact_seen_headline_duplicate() -> None:
    gold = _metric_gold(
        accepted_review={"should_push": "must_push"},
        policy_metric={
            "seen": [
                {
                    "event_id": "prior",
                    "headline_zh": "发行人提交重大更新",
                    "direction": "bullish",
                    "grounded_assets": ["ABC"],
                    "assets": [{"symbol": "ABC", "role": "primary"}],
                }
            ]
        },
    )

    outcome = _score(gold, _metric_verdict(), "reader_card")

    assert outcome.hard_gate == "must_push_miss"
    assert outcome.production_action == "throttled"
    assert outcome.production_rule == "trade_relevance_realtime"
    assert outcome.production_throttled_by.endswith(":seen")
    assert "must receive" in outcome.feedback


def test_watchlist_objective_guard_action_never_becomes_event_semantics_feedback() -> None:
    gold = _metric_gold(
        accepted_review={
            "should_push": "must_hold",
            "dimensions": {},
            "novelty": {"judgment": "uncertain", "duplicate_of": ""},
        }
    )
    projection = dict(gold.policy_metric)
    projection["gate"] = {**dict(projection["gate"]), "watchlist_symbols": ["ABC"]}
    gold = gold.copy(policy_metric=projection)
    editorial = EditorialEnvelope.issue(
        editorial_origin="model",
        relevance=_relevance(
            reader_value="background",
            tradability="contextual",
            channels=[],
            affected_markets=[],
        ),
    )
    prediction = dspy.Prediction(verdict=_metric_verdict(), editorial=editorial)

    outcome = accepted_review_metric(gold, prediction, None, "event_semantics", None, None)

    assert outcome.hard_gate == "must_hold_send"
    assert outcome.objective_guard == "watchlist"
    assert "must not receive" not in outcome.feedback
    assert "policy resolved" not in outcome.feedback
    assert "code-owned objective guard" in outcome.feedback


def test_metric_receipt_binds_the_weights_the_policy_and_the_rubric() -> None:
    receipt = _metric_receipt(accepted_review_metric, review_rubric_version="news_review_v4")
    assert receipt["weights"] == {
        "final_action": 0.45,
        "trade_relevance": 0.35,
        "semantics_novelty": 0.10,
        "reader_card": 0.10,
    }
    assert receipt["action_source"]["policy"] == "tracefold.news.triage_rules.decide"
    assert receipt["action_source"]["operational_controls"].startswith("none_")
    # The receipt no longer carries a policy *value*: the policy that scores an example travels with that
    # example and is verified against its own SHA, so a receipt value could only ever disagree with it.
    assert "per_example" in receipt["action_source"]["policy_values"]
    assert receipt["review_rubric_version"]
    source_units = receipt["implementation"]["source_unit_sha256"]
    assert set(source_units) == {
        "tracefold.news.learning.metric",
        "tracefold.news.models.base_symbol",
        "tracefold.news.events.storyline",
        "tracefold.news.triage_rules",
    }
    assert receipt["implementation"]["helper_source_root_sha256"] == canonical_sha(source_units)
    assert all(len(value) == 64 for value in source_units.values())
    assert "factual_contradiction" in receipt["hard_gates"]
    assert "factual_contradiction_unchanged" not in receipt["hard_gates"]
    # Reweighting, repointing at another policy, or moving rubric all move the receipt hash.
    assert canonical_sha({**receipt, "weights": {"final_action": 1.0}}) != canonical_sha(receipt)


def test_the_metric_scores_editorial_judgment_with_no_operational_input() -> None:
    """A card silenced for operational reasons would not be evidence that its editorial judgment was wrong.

    #137 removed the pause/mute plane, so there is nothing left for `decide()` to exclude — but the sealed
    projection must still carry no control state, or a future control plane could leak into the reward.
    """

    import inspect as _inspect

    from tracefold.news.triage_rules import decide as _decide

    assert "muted" not in _inspect.signature(_decide).parameters
    assert "control" not in _metric_gold()["policy_metric"]


# ---------------------------------------------------------------- honest split
def _split_episode(cluster: str, case: str, should_push: str, order: int) -> Any:
    return DevelopmentEpisode.model_validate(
        {
            "case_id": case,
            "cluster_id": cluster,
            "stratum": "review_failure",
            "context": _request().episodes[0].context,
            "accepted_review": {
                "should_push": should_push,
                "dimensions": {"factual_fidelity": "pass"},
                "novelty": {"judgment": "new_fact", "duplicate_of": ""},
            },
            "production_judgment": _judgment().model_dump(mode="json"),
            "policy_metric": {"seen": [], "told": [], **_frozen_policy_projection()},
        }
    )


def _balanced(cluster: str, index: int) -> list[Any]:
    return [
        _split_episode(cluster, f"{cluster}-push", "must_push", index),
        _split_episode(cluster, f"{cluster}-hold", "must_hold", index),
    ]


def test_split_is_disjoint_time_ordered_and_never_divides_a_fact_cluster() -> None:
    episodes = [*_balanced("c1", 0), *_balanced("c2", 1), *_balanced("c3", 2), *_balanced("c4", 3)]
    train, val, receipt = _honest_split(episodes)

    train_clusters = {episode.cluster_id for episode in train}
    val_clusters = {episode.cluster_id for episode in val}
    assert train_clusters.isdisjoint(val_clusters)
    assert {episode.case_id for episode in train}.isdisjoint({episode.case_id for episode in val})
    # Earliest clusters train, latest select. No shuffle, no seed.
    assert train_clusters == {"c1", "c2", "c3"} and val_clusters == {"c4"}
    assert receipt["train"]["cluster_n"] == 3 and receipt["development_selection"]["cluster_n"] == 1
    assert receipt["disjointness"]["shared_case_ids"] == 0
    assert len(receipt["train"]["cluster_root_sha256"]) == 64
    # Deterministic: the same episodes in any input order produce the same split.
    assert _honest_split(list(reversed(episodes)))[2] == receipt


def test_split_fails_closed_when_a_half_cannot_detect_the_regressions_it_exists_for() -> None:
    # Every cluster is a push case, so neither half can catch a must-hold regression.
    episodes = [_split_episode(f"c{i}", f"c{i}-push", "must_push", i) for i in range(4)]
    with pytest.raises(ValueError, match="split_coverage_incomplete"):
        _honest_split(episodes)

    with pytest.raises(ValueError, match="split_requires_two_clusters"):
        _honest_split(_balanced("only", 0))


def test_retrieval_is_reported_separately_so_a_scalar_score_cannot_hide_a_recall_failure() -> None:
    """ "The model called it new" and "the model was never shown the card" are different defects."""

    context = _request().episodes[0].context
    shown = context.model_copy(
        update={
            "told": context.told.model_copy(
                update={
                    "entries": (
                        ToldLedgerEntry(
                            i=0, event_id="prior", at_ms=1, ago_min=1, magnitude=1, direction="bullish", headline_zh="x"
                        ),
                    )
                }
            )
        }
    )

    def episode(ctx: Any, target: str) -> Any:
        return DevelopmentEpisode.model_validate(
            {
                "case_id": f"case-{target}-{id(ctx)}",
                "cluster_id": "c1",
                "stratum": "s",
                "context": ctx,
                "accepted_review": {"novelty": {"judgment": "restatement", "duplicate_of": target}},
                "policy_metric": {"seen": [{"event_id": "prior"}], "told": []},
            }
        )

    receipt = _retrieval_receipt([episode(shown, "prior"), episode(context, "prior")])
    assert receipt["accepted_restatements_in_window"] == 2
    assert receipt["target_recall_n"] == 1 and receipt["target_recall"] == 0.5
    assert receipt["selected_ranks"] == [0]

    # A target that was never in the bounded window is not a retrieval failure and is not counted.
    outside = DevelopmentEpisode.model_validate(
        {
            "case_id": "case-outside",
            "cluster_id": "c1",
            "stratum": "s",
            "context": context,
            "accepted_review": {"novelty": {"judgment": "restatement", "duplicate_of": "long-gone"}},
            "policy_metric": {"seen": [{"event_id": "prior"}], "told": []},
        }
    )
    assert _retrieval_receipt([outside])["accepted_restatements_in_window"] == 0
