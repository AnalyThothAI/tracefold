from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, ClassVar

import dspy
import pytest

from tracefold.news.agents.program_compiler import (
    CompileBudget,
    CompileBudgetExceeded,
    CompileRequest,
    ProgramCompiler,
    _BudgetedLM,
    _BudgetMeter,
    _metric_receipt,
    _optimizer_config_receipt,
    accepted_review_metric,
)
from tracefold.news.agents.program_compiler_trusted import build_eligible_demo_bank
from tracefold.news.agents.semantic_program import (
    DspyStrictJSONAdapter,
    EligibleDemoBank,
    ExactProviderCallCapture,
    ExactProviderMetadata,
    TriageContext,
    load_stable_program_artifact,
)
from tracefold.news.artifact_identity import canonical_sha


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
        assert trainset == valset
        assert len(trainset) == 1
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
        episodes=(
            {
                "case_id": "case-1",
                "cluster_id": "cluster-1",
                "stratum": "review_failure",
                "context": context,
                "accepted_review": {
                    "should_push": "should_push",
                    "dimensions": {"direction": "fail", "factual_fidelity": "pass"},
                    "novelty": {"judgment": "new_fact", "duplicate_of": ""},
                    "expected_correction": "The direction must follow the filing's actual mechanism.",
                },
                "production_verdict": {
                    "decision": "push",
                    "novelty": "new_fact",
                    "direction": "neutral",
                },
            },
        ),
        budget=CompileBudget(
            max_metric_calls=3,
            max_task_model_calls=max_calls,
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
    )


def test_compiler_budget_uses_exact_call_metadata_not_shared_history() -> None:
    lm = _MeteredFakeLM("task/model", cost=0.000002)
    meter = _BudgetMeter(
        CompileBudget(
            max_metric_calls=1,
            max_task_model_calls=1,
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
    assert result.patch.learning_epoch == "program_v4"
    assert result.patch.parent_program_sha256 == load_stable_program_artifact().program_sha256
    assert result.patch.patch_sha256 == result.patch.computed_sha256()
    assert [strategy.predictor for strategy in result.patch.learned_strategies] == [
        "event_semantics",
        "reader_card",
    ]
    assert result.metric_calls == 2
    assert result.task_model_calls == 1
    assert result.reflection_model_calls == 1
    assert result.actual_cost_microusd == 5
    assert result.failure_cluster_ids == ("cluster-1",)
    assert result.target_dimensions == ("direction",)
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
    episode["production_verdict"] = {
        "novelty": "new_fact",
        "restates": -1,
        "event_type": "filing",
        "assets": [],
        "direction": "neutral",
        "scope": "single_name",
        "magnitude": 1,
        "actionable": True,
        "confidence": 0.8,
        "decision": "push",
        "audience": "us_equity",
        "headline_zh": "发行人提交重大更新",
        "why_zh": "时间表发生变化。",
    }
    case = {
        "case_id": "case-1",
        "cluster_id": "cluster-1",
        "evidence_sha256": "e" * 64,
    }
    payload = {
        "role": "development",
        "learning_epoch": "program_v4",
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


def test_task_and_reflection_calls_share_one_explicit_call_budget() -> None:
    with pytest.raises(CompileBudgetExceeded, match="task_model_call_budget_exhausted"):
        _compiler().compile(_request(max_calls=1))


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
    )

    with pytest.raises(TypeError, match="nonfinite_receipt_value"):
        compiler.compile(_request())


def test_metric_receipt_hash_binds_the_executed_implementation_source() -> None:
    def changed_metric(*args: Any, **kwargs: Any) -> dspy.Prediction:
        del args, kwargs
        return dspy.Prediction(score=0.5, feedback="changed")

    original = _metric_receipt(accepted_review_metric)
    changed = _metric_receipt(changed_metric)

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
    )
    changed_scalar = _optimizer_config_receipt(
        constructor={**constructor, "seed": 18},
        task_lm=_MeteredFakeLM("task/model"),  # type: ignore[arg-type]
        reflection_lm=_MeteredFakeLM("reflection/model"),  # type: ignore[arg-type]
        optimizer_factory=_FakeGEPA,
        metric_sha256=metric_sha,
        example_count=1,
    )
    changed_model = _optimizer_config_receipt(
        constructor=constructor,
        task_lm=_MeteredFakeLM("task/other-model"),  # type: ignore[arg-type]
        reflection_lm=_MeteredFakeLM("reflection/model"),  # type: ignore[arg-type]
        optimizer_factory=_FakeGEPA,
        metric_sha256=metric_sha,
        example_count=1,
    )
    changed_endpoint = _optimizer_config_receipt(
        constructor=constructor,
        task_lm=_MeteredFakeLM("task/model", api_base="https://other-compiler.test/v1"),  # type: ignore[arg-type]
        reflection_lm=_MeteredFakeLM("reflection/model"),  # type: ignore[arg-type]
        optimizer_factory=_FakeGEPA,
        metric_sha256=metric_sha,
        example_count=1,
    )

    assert canonical_sha(base) != canonical_sha(changed_scalar)
    assert canonical_sha(base) != canonical_sha(changed_model)
    assert canonical_sha(base) != canonical_sha(changed_endpoint)
    assert "compiler.test" not in repr(base)
