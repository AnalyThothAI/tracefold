"""#202 PR-A: the one offline entry point, its write-set, and its three terminal states.

The corpus, the fake GEPA and the metered fake endpoints are imported from the compiler suite rather than
rebuilt. That is the point of the first test in this file: `optimize()` is supposed to be the same
optimization the trusted compiler ran, so it has to be provable on the same inputs — a second corpus here
would let the two drift while both suites stayed green.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.learning.contracts import (
    METRIC_JUDGE_MAX_TOKENS,
    METRIC_JUDGE_TIMEOUT_SECONDS,
    DevelopmentDatasetRef,
    ModelExecutionIdentity,
    OptimizationBudget,
    OptimizationRunReport,
    PromptCandidateV1,
    PromptPatchV1,
)
from tracefold.news.learning.objective import DevelopmentEpisode
from tracefold.news.learning.optimizer import (
    FrozenDevelopmentDataset,
    OptimizationBudgetExceeded,
    OptimizationConfig,
    _BudgetedLM,
    _BudgetMeter,
    optimize,
)
from tracefold.news.program.artifact import ProgramStrategyArtifactV1, load_stable_program_artifact

from .test_news_gepa_core import _episodes as _corpus
from .test_news_gepa_core import _FakeGEPA, _MeteredFakeLM, _NoopJudge

_DATASET_PAYLOAD = {"role": "development", "learning_epoch": "program_v7", "cases": []}


class _StampedJudge(_NoopJudge):
    """A judge carrying the identity and the admission ceiling the entry point requires.

    `_NoopJudge` deliberately carries neither: the metric calls the judge directly, so a judge without a
    stamped role binding and without a ceiling bound to the declared budget is exactly the hole the review
    of #205 found — judge-derived scores with no endpoint provenance, and calls no pre-flight bounded.
    """

    def __init__(self, *, max_model_calls: int = 16, role: str = "metric_judge") -> None:
        super().__init__()
        binding = ModelExecutionIdentity.issue(
            role=role,  # type: ignore[arg-type]
            model="judge/model",
            api_base="https://judge.test/v1",
            max_output_tokens=METRIC_JUDGE_MAX_TOKENS,
            timeout_seconds=METRIC_JUDGE_TIMEOUT_SECONDS,
            temperature=0.0,
            model_kwargs={},
        )
        self.identity = {
            "judge_id": "test/stamped",
            "execution": {
                "role_binding": binding.model_dump(mode="json"),
                "max_model_calls": max_model_calls,
                "timeout_seconds": METRIC_JUDGE_TIMEOUT_SECONDS,
            },
        }


_RUNTIME_MANIFEST_SHA = "a" * 64
_NOW_MS = 1_800_000_123_456


def _budget(**overrides: Any) -> OptimizationBudget:
    values: dict[str, Any] = {
        "max_metric_calls": 3,
        "max_task_model_calls": 4,
        "max_reflection_model_calls": 4,
        "max_metric_judge_model_calls": 16,
        "max_cost_microusd": 20,
        "max_call_cost_microusd": 5,
        "max_wall_clock_seconds": 900.0,
        "seed": 17,
    }
    values.update(overrides)
    return OptimizationBudget(**values)


def _episodes(**review_overrides: Any) -> tuple[DevelopmentEpisode, ...]:
    """The compiler suite's corpus, optionally with the operator's explicit owner removed."""

    episodes = _corpus()
    if not review_overrides:
        return episodes
    rebuilt = []
    for episode in episodes:
        review = dict(episode.accepted_review)
        for key, value in review_overrides.items():
            if value is None:
                review.pop(key, None)
            else:
                review[key] = value
        rebuilt.append(episode.model_copy(update={"accepted_review": review}))
    return tuple(rebuilt)


def _dataset(episodes: tuple[DevelopmentEpisode, ...] | None = None) -> FrozenDevelopmentDataset:
    cases = episodes if episodes is not None else _episodes()
    ref = DevelopmentDatasetRef(
        development_dataset_sha256=canonical_sha({"kind": "dataset", "payload": _DATASET_PAYLOAD}),
        episode_projection_root_sha256=canonical_sha([case.model_dump(mode="json") for case in cases]),
        episode_count=len(cases),
        learning_epoch_started_at_ms=1_787_549_907_739,
        review_rubric_version="news_review_v4",
    )
    return FrozenDevelopmentDataset.bind(
        ref=ref,
        episodes=cases,
        dataset_payload=_DATASET_PAYLOAD,
        target_runtime_manifest_sha256=_RUNTIME_MANIFEST_SHA,
    )


def _config(
    *,
    optimizer_factory: Any = _FakeGEPA,
    budget: OptimizationBudget | None = None,
    monotonic: Any = None,
    task_lm: Any = None,
    judge: Any = None,
) -> OptimizationConfig:
    return OptimizationConfig(
        task_lm=task_lm or _MeteredFakeLM("task/model", cost=0.000002),
        reflection_lm=_MeteredFakeLM("reflection/model", cost=0.000003, role="reflection"),
        judge=judge or _StampedJudge(),
        budget=budget or _budget(),
        optimizer_factory=optimizer_factory,
        now_ms=lambda: _NOW_MS,
        monotonic=monotonic or (lambda: 0.0),
    )


def test_the_offline_entry_point_runs_the_same_optimization_the_compiler_ran() -> None:
    """Characterization: same corpus, same split, same metric, same optimizer construction.

    Not "similar": the split receipt, the metric receipt and the scalar constructor arguments are compared
    byte for byte against a direct `run_gepa` over the same episodes. #202 keeps exactly one GEPA
    construction in the repository, and this is what makes that claim checkable rather than asserted.
    """

    from tracefold.news.learning.optimizer import _BudgetedLM, _BudgetMeter, run_gepa

    _FakeGEPA.calls.clear()
    meter = _BudgetMeter(_budget(), imputed_call_cost_microusd=5)
    compiled = run_gepa(
        base_program=load_stable_program_artifact(),
        episodes=_corpus(),
        task_lm=_BudgetedLM(_MeteredFakeLM("task/model", cost=0.000002), role="task", meter=meter),
        reflection_lm=_BudgetedLM(
            _MeteredFakeLM("reflection/model", cost=0.000003, role="reflection"),
            role="reflection",
            meter=meter,
        ),
        judge=_StampedJudge(),
        max_metric_calls=3,
        seed=17,
        review_rubric_version="news_review_v4",
        optimizer_factory=_FakeGEPA,
    )
    compiler_constructor = dict(_FakeGEPA.calls[-1])

    _FakeGEPA.calls.clear()
    result = optimize(_dataset(), _config())
    entry_constructor = dict(_FakeGEPA.calls[-1])

    assert result.outcome == "ADVANCE"
    assert result.report.split == compiled.split
    assert result.report.retrieval == compiled.retrieval
    assert result.report.metric == compiled.metric
    assert result.report.trajectory == compiled.trajectory
    assert result.report.optimizer == compiled.optimizer_config
    assert result.report.objective["target_failure_cluster_ids"] == list(compiled.failure_cluster_ids)
    assert result.report.objective["target_dimensions"] == list(compiled.target_dimensions)
    assert entry_constructor["max_metric_calls"] == compiler_constructor["max_metric_calls"]
    assert entry_constructor["reflection_minibatch_size"] == compiler_constructor["reflection_minibatch_size"]
    assert entry_constructor["component_selector"] == compiler_constructor["component_selector"]
    assert entry_constructor["seed"] == compiler_constructor["seed"]
    assert result.candidate is not None
    assert result.candidate.patch.event_semantics_instruction == compiled.patch.event_semantics_instruction
    assert result.candidate.patch.reader_card_instruction == compiled.patch.reader_card_instruction


def test_advance_produces_a_candidate_the_report_names_and_nothing_it_may_promote() -> None:
    result = optimize(_dataset(), _config())

    assert result.outcome == "ADVANCE" == result.report.outcome
    candidate = result.candidate
    assert candidate is not None
    assert candidate.schema_version == "news_prompt_candidate_v1"
    assert candidate.parent_program_sha256 == load_stable_program_artifact().program_sha256
    assert candidate.development_dataset_sha256 == canonical_sha({"kind": "dataset", "payload": _DATASET_PAYLOAD})
    assert candidate.target_runtime_manifest_sha256 == _RUNTIME_MANIFEST_SHA
    stable = load_stable_program_artifact()
    assert candidate.patch.event_semantics_instruction == (
        stable.event_semantics_instruction + "\nCompiler candidate instruction."
    )
    assert candidate.patch.reader_card_instruction == stable.reader_card_instruction
    assert candidate.objective_summary["schema"] == "tracefold.news.optimization_objective_summary.v2"
    assert candidate.objective_summary["plan_schema"] == "tracefold.news.gepa_objective_plan.v2"
    assert candidate.objective_summary["optimizer_case_n"] == candidate.objective_summary["optimizer_cluster_n"]
    assert result.report.candidate_sha256 == candidate.candidate_sha256
    assert result.report.reasons == ()
    # A proposal that asks to activate or publish itself is outside this contract. Protect that authority
    # boundary without freezing every harmless metadata field the schema may gain later.
    for forbidden in ("stage", "activation", "artifact_root", "promote"):
        with pytest.raises(ValidationError):
            type(candidate).model_validate({**candidate.model_dump(mode="json"), forbidden: "requested"})


def test_a_run_that_learned_nothing_is_a_no_op_with_a_complete_report() -> None:
    class _SeedGEPA(_FakeGEPA):
        def compile(self, student: Any, *, trainset: list[Any], teacher: None, valset: list[Any]) -> Any:
            compiled = super().compile(student, trainset=trainset, teacher=teacher, valset=valset)
            # Put the seed back: a Pareto front that kept the seed is exactly this, and since #306 Phase 2
            # the seed is the complete instruction rather than the empty advisory it used to be.
            compiled.event_semantics.signature = student.event_semantics.signature.with_instructions(
                load_stable_program_artifact().event_semantics_instruction
            )
            return compiled

    result = optimize(_dataset(), _config(optimizer_factory=_SeedGEPA))

    assert result.outcome == "NO_OP"
    assert result.candidate is None
    assert result.report.candidate_sha256 is None
    assert result.report.reasons == ("news_program_compile_no_program_change",)
    # A `NO_OP` still spent a budget, so it still has to say what it spent it on.
    assert result.report.split is not None
    assert result.report.usage["task_model_calls"] == 1
    assert result.report.usage["reflection_model_calls"] == 1


def test_a_corpus_with_no_verified_prompt_target_is_rejected_before_any_endpoint_is_touched() -> None:
    """The Objective Plan's refusal is a terminal artifact, and it costs nothing.

    `readiness` answers the same question with zero model calls. Running `optimize` on a corpus it would
    have blocked must not become the expensive way to learn the same thing.
    """

    task_lm = _MeteredFakeLM("task/model", cost=0.000002)
    dataset = _dataset(_episodes(first_bad_owner_explicit=None))

    result = optimize(dataset, _config(task_lm=task_lm))

    assert result.outcome == "REJECTED"
    assert result.candidate is None
    assert "news_program_compile_no_verified_failure_clusters" in result.report.reasons
    assert task_lm.history == []
    assert result.report.usage["task_model_calls"] == 0
    assert result.report.split is None
    assert result.report.objective["exclusion_reasons"]


def test_an_exhausted_call_budget_is_rejected_and_never_produces_a_candidate() -> None:
    class _GreedyGEPA(_FakeGEPA):
        def compile(self, student: Any, *, trainset: list[Any], teacher: None, valset: list[Any]) -> Any:
            import dspy

            dspy.settings.lm(prompt="one")
            dspy.settings.lm(prompt="two")
            return super().compile(student, trainset=trainset, teacher=teacher, valset=valset)

    result = optimize(_dataset(), _config(optimizer_factory=_GreedyGEPA, budget=_budget(max_task_model_calls=1)))

    assert result.outcome == "REJECTED"
    assert result.candidate is None
    assert result.report.reasons == ("news_program_compile_task_model_call_budget_exhausted",)


def test_an_exhausted_wall_clock_stops_the_next_call_rather_than_reporting_the_last() -> None:
    ticks = iter([0.0, 0.0, 10_000.0, 10_000.0, 10_000.0, 10_000.0])

    result = optimize(
        _dataset(),
        # Above the longest role deadline, so the budget is admissible — and then exceeded mid-run.
        _config(budget=_budget(max_wall_clock_seconds=600.0), monotonic=lambda: next(ticks)),
    )

    assert result.outcome == "REJECTED"
    assert result.candidate is None
    assert result.report.reasons == ("news_learning_optimize_wall_clock_exhausted",)


def test_the_same_frozen_corpus_and_the_same_run_produce_a_byte_stable_report() -> None:
    first = optimize(_dataset(), _config())
    second = optimize(_dataset(), _config())

    assert first.report.report_sha256 == second.report.report_sha256
    assert first.report.model_dump(mode="json") == second.report.model_dump(mode="json")
    assert first.candidate is not None and second.candidate is not None
    assert first.candidate.candidate_sha256 == second.candidate.candidate_sha256


def test_a_dataset_ref_that_describes_a_different_projection_fails_closed() -> None:
    episodes = _episodes()
    ref = DevelopmentDatasetRef(
        development_dataset_sha256=canonical_sha({"kind": "dataset", "payload": _DATASET_PAYLOAD}),
        episode_projection_root_sha256="b" * 64,
        episode_count=len(episodes),
        learning_epoch_started_at_ms=1,
        review_rubric_version="news_review_v4",
    )
    with pytest.raises(ValueError, match="projection_root_mismatch"):
        FrozenDevelopmentDataset.bind(
            ref=ref,
            episodes=episodes,
            dataset_payload=_DATASET_PAYLOAD,
            target_runtime_manifest_sha256=_RUNTIME_MANIFEST_SHA,
        )


def test_a_dataset_ref_naming_an_artifact_it_was_not_built_from_fails_closed() -> None:
    """#202 review: a matching projection root beside an unrelated artifact hash is still a lie.

    A candidate issued from it would name a dataset it was never built from, and the evaluator that later
    loads that SHA would score a different corpus — or find nothing — while the report claimed the run was
    dataset-bound. The durable identity is recomputed here, not accepted.
    """

    episodes = _episodes()
    ref = DevelopmentDatasetRef(
        development_dataset_sha256="c" * 64,
        episode_projection_root_sha256=canonical_sha([case.model_dump(mode="json") for case in episodes]),
        episode_count=len(episodes),
        learning_epoch_started_at_ms=1,
        review_rubric_version="news_review_v4",
    )
    with pytest.raises(ValueError, match="dataset_artifact_hash_mismatch"):
        FrozenDevelopmentDataset.bind(
            ref=ref,
            episodes=episodes,
            dataset_payload=_DATASET_PAYLOAD,
            target_runtime_manifest_sha256=_RUNTIME_MANIFEST_SHA,
        )


def test_a_parent_that_is_not_the_active_stable_cannot_be_optimized_against() -> None:
    descendant = ProgramStrategyArtifactV1.issue(
        event_semantics_instruction="A previously learned instruction.",
        reader_card_instruction="Keep the mechanism concrete.",
    )
    episodes = _episodes()
    ref = DevelopmentDatasetRef(
        development_dataset_sha256=canonical_sha({"kind": "dataset", "payload": _DATASET_PAYLOAD}),
        episode_projection_root_sha256=canonical_sha([case.model_dump(mode="json") for case in episodes]),
        episode_count=len(episodes),
        learning_epoch_started_at_ms=1,
        review_rubric_version="news_review_v4",
    )
    with pytest.raises(ValueError, match="parent_must_be_active_stable"):
        FrozenDevelopmentDataset.bind(
            ref=ref,
            episodes=episodes,
            dataset_payload=_DATASET_PAYLOAD,
            target_runtime_manifest_sha256=_RUNTIME_MANIFEST_SHA,
            parent_program=descendant,
        )


def test_the_write_set_is_two_instructions_and_the_safety_bounds_are_not_restated() -> None:
    patch = PromptPatchV1(
        event_semantics_instruction="Prefer the filing's own mechanism.",
        reader_card_instruction="Name the mechanism.",
    )

    assert patch.model_dump(mode="json") == {
        "event_semantics_instruction": "Prefer the filing's own mechanism.",
        "reader_card_instruction": "Name the mechanism.",
    }
    with pytest.raises(ValidationError):
        PromptPatchV1(
            event_semantics_instruction="Prefer the filing's own mechanism.",
            reader_card_instruction="Name the mechanism.",
            policy={"suppress_low_signal": False},  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError, match="instruction_unsafe"):
        PromptPatchV1(
            event_semantics_instruction="Read https://example.test/policy first.",
            reader_card_instruction="Name the mechanism.",
        )
    with pytest.raises(ValidationError, match="instruction_too_large"):
        PromptPatchV1(event_semantics_instruction="x" * 200_000, reader_card_instruction="Name the mechanism.")
    with pytest.raises(ValidationError, match="instruction_empty"):
        PromptPatchV1(event_semantics_instruction="", reader_card_instruction="Name the mechanism.")


@pytest.mark.property
@given(
    extra=st.text(min_size=1, max_size=24).filter(
        lambda name: name not in {"event_semantics_instruction", "reader_card_instruction"}
    )
)
def test_no_field_beyond_the_two_instructions_can_enter_the_write_set(extra: str) -> None:
    with pytest.raises(ValidationError):
        PromptPatchV1.model_validate(
            {
                "event_semantics_instruction": "Prefer the mechanism.",
                "reader_card_instruction": "Name it.",
                extra: "anything",
            }
        )


def test_a_candidate_carrying_a_credential_is_refused_before_it_is_stored() -> None:
    with pytest.raises(ValidationError, match="secret_key"):
        PromptCandidateV1.issue(
            parent_program_sha256=load_stable_program_artifact().program_sha256,
            development_dataset_sha256="d" * 64,
            target_runtime_manifest_sha256=_RUNTIME_MANIFEST_SHA,
            patch=PromptPatchV1(
                event_semantics_instruction="Prefer the mechanism.",
                reader_card_instruction="Name it.",
            ),
            objective_summary={},
            optimizer={},
            model_identities={"task": {"api_key": "sk-live-not-a-real-key"}},
            budget={},
            usage={},
            created_at_ms=_NOW_MS,
        )


def test_a_tampered_candidate_or_report_hash_is_refused() -> None:
    result = optimize(_dataset(), _config())
    assert result.candidate is not None
    payload = result.candidate.model_dump(mode="json")
    payload["created_at_ms"] = payload["created_at_ms"] + 1
    with pytest.raises(ValidationError, match="prompt_candidate_hash_mismatch"):
        PromptCandidateV1.model_validate(payload)

    report = result.report.model_dump(mode="json")
    report["outcome"] = "NO_OP"
    with pytest.raises(ValidationError, match="outcome_mismatch"):
        OptimizationRunReport.model_validate(report)


def test_a_non_advance_report_must_say_why_it_shipped_nothing() -> None:
    result = optimize(_dataset(_episodes(first_bad_owner_explicit=None)), _config())
    payload = result.report.model_dump(mode="json")
    payload["reasons"] = []
    with pytest.raises(ValidationError, match="reason_required"):
        OptimizationRunReport.model_validate(payload)


def test_an_unpriced_provider_call_is_charged_at_the_declared_ceiling_rather_than_failing_closed() -> None:
    """The tariff is going away with the proxy that reserved against it (#202 §6.2).

    Neither endpoint this project runs on reports a resolvable price, so a meter that only understands
    provider-reported cost stops on the first call. Charging the operator's own declared per-call ceiling
    keeps the cost budget meaningful — and over-charges, which is the direction that stops a run early.
    """

    from tracefold.news.program.dspy_adapter import ExactProviderMetadata

    meter = _BudgetMeter(_budget(max_cost_microusd=12, max_call_cost_microusd=5), imputed_call_cost_microusd=5)
    meter.before("task")
    meter.after(ExactProviderMetadata(provider_cost_microusd=None, finish_reason="stop"))
    assert meter.actual_cost_microusd == 5
    assert meter.imputed_cost_calls == 1

    unpriced = _BudgetMeter(_budget())
    unpriced.before("task")
    with pytest.raises(OptimizationBudgetExceeded, match="provider_cost_unavailable"):
        unpriced.after(ExactProviderMetadata(provider_cost_microusd=None, finish_reason="stop"))


def test_the_metered_lm_still_refuses_a_cached_or_silently_retrying_route() -> None:
    meter = _BudgetMeter(_budget())

    class _Cached(_MeteredFakeLM):
        cache = True

    with pytest.raises(ValueError, match="cache_must_be_disabled"):
        _BudgetedLM(_Cached("task/model"), role="task", meter=meter)  # type: ignore[arg-type]


@pytest.mark.parametrize("capability", ["session", "repository", "activation", "artifact_root"])
def test_the_offline_job_rejects_storage_and_activation_capabilities(capability: str) -> None:
    """The optimizer may gain benign configuration without gaining a path to store or activate a result."""

    with pytest.raises(TypeError):
        replace(_config(), **{capability: object()})


def test_an_unbounded_or_unstamped_judge_is_refused_before_anything_is_spent() -> None:
    """#205 review, both halves.

    The metric calls the judge directly, so `_BudgetedLM` never sees those requests — the judge admits them
    itself, atomically, before each provider call. That is a real pre-call bound only if the ceiling it
    admits against is the one declared here. And a judge with no stamped role binding produces scores a
    candidate would retain without naming the endpoint that produced them.
    """

    task_lm = _MeteredFakeLM("task/model", cost=0.000002)

    with pytest.raises(ValueError, match="metric_judge_identity_unavailable"):
        optimize(_dataset(), _config(task_lm=task_lm, judge=_NoopJudge()))
    with pytest.raises(ValueError, match="metric_judge_identity_unavailable"):
        optimize(_dataset(), _config(task_lm=task_lm, judge=_StampedJudge(role="task")))
    # A ceiling above the declared budget is not a ceiling this run set.
    with pytest.raises(ValueError, match="metric_judge_call_budget_unbound"):
        optimize(_dataset(), _config(task_lm=task_lm, judge=_StampedJudge(max_model_calls=17)))
    assert task_lm.history == []


def test_a_wall_clock_that_cannot_bound_one_call_is_refused() -> None:
    """A 60 s budget that still waits 300 s for a reflection response is a number, not a deadline.

    The clock is checked before each call, never during one: a request in flight runs to its own attested
    deadline, and clamping that would break the role contract `ModelExecutionIdentity` exists to attest.
    So the worst case is the budget plus one call, and the budget has to be able to cover that one call.
    """

    with pytest.raises(ValueError, match="wall_clock_below_call_deadline"):
        optimize(_dataset(), _config(budget=_budget(max_wall_clock_seconds=60.0)))


def test_an_unpriced_judge_call_is_charged_rather_than_counted_as_free() -> None:
    judge = _StampedJudge()
    judge.stats = {**judge.stats, "attempts": 4, "model_calls": 4, "actual_cost_microusd": 0}

    result = optimize(_dataset(), _config(judge=judge))

    # 4 calls x the declared per-call ceiling, not the zero the provider reported.
    assert result.report.usage["metric_judge_cost_microusd"] == 4 * 5
    assert result.report.usage["metric_judge_cost_imputed"] is True
    # And the run is rejected for it, because the total now exceeds `max_cost_microusd`.
    assert result.outcome == "REJECTED"
    assert "news_program_compile_cost_budget_exceeded" in result.report.reasons
