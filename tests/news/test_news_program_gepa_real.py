"""#143/#344: exercise the public DSPy optimizer and native Program, not a stand-in.

Every other optimizer test drives a fake `compile`, so the governance assertions were proven while the
claim that matters — that this Program, this metric and this proposer actually compile under the optimizer
we ship — was only assumed. This test runs public `dspy.GEPA.compile` over `NativeNewsProgram`, with typed
scripted LMs at the one public DSPy LM seam.
"""

from __future__ import annotations

from typing import Any

import dspy  # type: ignore[import-untyped]
import pytest

from tests.support.news_judgment import scored_judgment, trade_relevance
from tracefold.news.learning.contracts import (
    METRIC_JUDGE_MAX_TOKENS,
    METRIC_JUDGE_TIMEOUT_SECONDS,
    REFLECTION_MAX_TOKENS,
    DevelopmentDatasetRef,
    ModelExecutionIdentity,
    OptimizationBudget,
)
from tracefold.news.learning.objective import DevelopmentEpisode
from tracefold.news.learning.optimizer import (
    build_reflection_lm,
    build_task_lm,
)
from tracefold.news.models import TRIAGE_POLICY_VERSION
from tracefold.news.program.artifact import (
    load_stable_program_artifact,
)
from tracefold.news.program.contracts import TriageContext
from tracefold.news.program.lm import LMCallLedger
from tracefold.news.review.desk import REVIEW_RUBRIC_VERSION


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


_SEMANTICS = {
    "novelty": "new_fact",
    "restates": -1,
    "assets": [{"symbol": "TSLA", "role": "primary"}],
    "magnitude": 2,
    "direction": "bullish",
    "audience": "us_equity",
    "scope": "single_name",
    "confidence": 0.9,
    "relevance": trade_relevance().model_dump(mode="json"),
    "taxonomy": {
        "subject_codes": ["medtop:20000205"],
        "event_family": "product_service_change",
        "change_state": "announced",
        "assertion_status": "confirmed",
    },
}
_ADVISORY = "Prefer the stated accepted magnitude when the evidence names a concrete product."
_CARD = {"headline_zh": "特斯拉发布 Cybercab", "why_zh": "新车型进入量产排程，改变该名字的交付预期"}


_TASK_BASE = "https://scripted-task.invalid/v1"
_REFLECTION_BASE = "https://scripted-reflection.invalid/v1"
_JUDGE_BASE = "https://scripted-judge.invalid/v1"


class _ScriptedTaskLM(dspy.BaseLM):  # type: ignore[misc]
    """Dynamic typed task LM: the learned instruction changes its semantics answer."""

    forward_contract = "typed_lm"

    def __init__(self) -> None:
        super().__init__("openai/scripted-task", cache=False, num_retries=0)
        self.requests: list[dspy.LMRequest] = []

    @property
    def supports_response_schema(self) -> bool:
        return True

    @property
    def supported_params(self) -> set[str]:
        return {"response_format"}

    def forward(self, request: dspy.LMRequest) -> dspy.LMResponse:
        from tracefold.news.artifact_identity import canonical_json

        self.requests.append(request)
        rendered = str(request.messages)
        if "semantics_json" in rendered:
            answer: dict[str, Any] = {"card": _CARD}
        else:
            magnitude = 2 if _ADVISORY in rendered else 0
            answer = {"semantics": {**_SEMANTICS, "magnitude": magnitude}}
        return dspy.LMResponse.from_text(
            canonical_json(answer),
            model=self.model,
            usage={"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
            cost=0,
        )


class _ScriptedReflectionLM(dspy.BaseLM):  # type: ignore[misc]
    forward_contract = "typed_lm"

    def __init__(self) -> None:
        super().__init__("openai/scripted-reflection", cache=False, num_retries=0)
        self.requests: list[dspy.LMRequest] = []

    @property
    def supports_response_schema(self) -> bool:
        return True

    @property
    def supported_params(self) -> set[str]:
        return {"response_format"}

    def forward(self, request: dspy.LMRequest) -> dspy.LMResponse:
        from tracefold.news.artifact_identity import canonical_json

        self.requests.append(request)
        text = canonical_json({"new_instruction": _ADVISORY}) if request.config.response_format else _ADVISORY
        return dspy.LMResponse.from_text(
            text,
            model=self.model,
            usage={"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
            cost=0,
        )


class _ScriptedModels:
    def __init__(self) -> None:
        self.task = _ScriptedTaskLM()
        self.reflection = _ScriptedReflectionLM()


class _Judge:
    def __init__(self) -> None:
        binding = ModelExecutionIdentity.issue(
            role="metric_judge",
            model="judge/model",
            api_base=_JUDGE_BASE,
            max_output_tokens=METRIC_JUDGE_MAX_TOKENS,
            timeout_seconds=METRIC_JUDGE_TIMEOUT_SECONDS,
            temperature=0.0,
            model_kwargs={},
        )
        self.identity = {
            "judge_id": "test/noop",
            "execution": {
                "role_binding": binding.model_dump(mode="json"),
                "max_model_calls": 400,
                "timeout_seconds": METRIC_JUDGE_TIMEOUT_SECONDS,
            },
        }
        self.stats = {"attempts": 0, "model_calls": 0, "failures": 0, "actual_cost_microusd": 0}

    def retains(self, *_args: Any, **_kwargs: Any) -> bool:
        return False


def _episode(
    index: int,
    *,
    should_push: str,
    novelty: str,
    magnitude_fail: bool,
    production_magnitude: int,
    reader_value: str,
) -> DevelopmentEpisode:
    opened_at_ms = 1_787_000_000_000 + index * 60_000
    card = {
        "event_id": f"{index:064d}",
        "evidence_version": 1,
        "evidence_sha256": "a" * 64,
        "focus_fact_id": f"{index:064d}",
        "leader_title": f"Tesla ships product {index}",
        "leader_description": "",
        "leader_url": f"https://example.invalid/{index}",
        "reporting_origin": "wire",
        "dedupe_family": "general",
        "admission": "candidate",
        "queue_priority": "normal",
        "asset_class": "equity_or_commodity",
        "engine_type": "news",
        "ingest_mode": "live",
        "storyline_key": "asset:TSLA",
        "comparison_title": f"tesla ships product {index}",
        "raw_first_line": f"Tesla ships product {index}",
        "grounded_assets": ["TSLA"],
        "watchlist_hits": [],
        "member_count": 1,
        "opened_at_ms": opened_at_ms,
        "expires_at_ms": 1787043200000 + index * 60_000,
        "last_member_at_ms": opened_at_ms,
        "macro_lexicon": False,
        "provenance": ["1018"],
        "trace_id": f"{index:032d}",
        "leader_item_id": f"{index:064d}",
        "provider_metadata": {},
    }
    return DevelopmentEpisode(
        case_id=f"{index:064x}",
        cluster_id=f"{index:064x}",
        stratum="delivered",
        context=TriageContext.from_card(
            card,
            watchlist=(),
            told_rows=[],
            now_ms=opened_at_ms,
            queue_lag_ms=0,
        ),
        accepted_review={
            "should_push": should_push,
            "dimensions": {
                "factual_fidelity": "pass",
                "timeliness": "pass",
                "magnitude": "fail" if magnitude_fail else "pass",
            },
            "novelty": {"judgment": novelty, "duplicate_of": ""},
            "expected": {"magnitude": 2} if magnitude_fail else {},
            "expected_correction": "",
            # #199: the owner an operator wrote into the submission is what grants GEPA permission, and a
            # corpus without one is a corpus with no targets at all. The controls carry none on purpose —
            # nobody blames anything for an answer that was right.
            "first_bad_owner_explicit": "triage_prompt" if magnitude_fail else None,
            "first_bad_owner": "triage_prompt" if magnitude_fail else None,
            "evidence_refs": ["filing#magnitude"] if magnitude_fail else [],
            "taxonomy": {
                "subject_codes": [],
                "event_family": "other",
                "change_state": "unknown",
                "assertion_status": "unknown",
            },
        },
        production_judgment=scored_judgment(
            {
                **{key: value for key, value in _SEMANTICS.items() if key not in {"relevance", "taxonomy"}},
                **_CARD,
                "magnitude": production_magnitude,
            },
            relevance=trade_relevance(reader_value=reader_value),
        ),
        policy_metric={
            "gate": {"grounded_assets": ["TSLA"], "admission": "candidate"},
            "storyline": {"title": f"Tesla ships product {index}", "dedupe_family": "general"},
            "seen": [],
            **_frozen_policy_projection(),
        },
    )


# One repeating quartet, so both halves of the honest split carry every required stratum *and* the two
# things #199 added to that requirement: at least one verified Prompt target, and at least one case the
# stable Program already answers correctly.
#
# `realtime_eligible` needs `magnitude >= 2`, so a production magnitude of 0 under `reader_value=realtime`
# resolves to `trade_relevance_inconsistent` — which is why the target is both a magnitude failure and a
# `must_push` the reader never got, and why every control has to state a magnitude the policy accepts. The
# predecessor gave all twelve cases magnitude 0 and called four of them controls; the Objective Plan
# correctly refused every one as `stable_hard_gate:relevance_inconsistent`.
_CORPUS_ROLES: tuple[tuple[str, bool, int, str], ...] = (
    # should_push, magnitude failed, production magnitude, production reader_value
    ("must_push", True, 0, "realtime"),  # target: safety + positive action
    ("must_push", False, 2, "realtime"),  # control: safety + positive action, pushed and correct
    ("must_hold", False, 2, "background"),  # control: safety + negative action, held and correct
    ("should_push", False, 2, "realtime"),  # control: soft positive action
)


def compiler_development_corpus() -> tuple[DevelopmentEpisode, ...]:
    """Enough coverage that the honest split can find every required stratum on both sides."""

    return tuple(
        _episode(
            index + 1,
            should_push=_CORPUS_ROLES[index % 4][0],
            novelty="new_fact",
            magnitude_fail=_CORPUS_ROLES[index % 4][1],
            production_magnitude=_CORPUS_ROLES[index % 4][2],
            reader_value=_CORPUS_ROLES[index % 4][3],
        )
        for index in range(12)
    )


def _budget(**overrides: Any) -> OptimizationBudget:
    values: dict[str, Any] = {
        "max_metric_calls": 40,
        "max_task_model_calls": 400,
        "max_reflection_model_calls": 40,
        "max_metric_judge_model_calls": 400,
        "max_cost_microusd": 400_000,
        "max_call_cost_microusd": 1_000,
        "max_wall_clock_seconds": 3_600.0,
        "seed": 143,
    }
    values.update(overrides)
    return OptimizationBudget(**values)


def _optimize_real(
    models: _ScriptedModels,
    *,
    budget: OptimizationBudget | None = None,
    compile_fn: Any = None,
    gepa_log_dir: str | None = None,
) -> Any:
    """One real GEPA run through the one entry point, over the scripted corpus.

    The task and reflection roles share one audited ledger, matching the production learning runtime.
    """

    from tracefold.news.artifact_identity import canonical_sha
    from tracefold.news.learning.optimizer import FrozenDevelopmentDataset, OptimizationConfig, optimize

    episodes = compiler_development_corpus()
    dataset_payload = {"role": "development", "learning_epoch": "bundle_00000000", "cases": []}
    dataset = FrozenDevelopmentDataset.bind(
        dataset_payload=dataset_payload,
        ref=DevelopmentDatasetRef(
            development_dataset_sha256=canonical_sha({"kind": "dataset", "payload": dataset_payload}),
            episode_projection_root_sha256=canonical_sha([e.model_dump(mode="json") for e in episodes]),
            episode_count=len(episodes),
            learning_epoch="bundle_00000000",
            learning_epoch_started_at_ms=1,
            review_rubric_version=REVIEW_RUBRIC_VERSION,
        ),
        episodes=episodes,
        target_runtime_manifest_sha256="a" * 64,
    )
    ledger = LMCallLedger(max_calls_per_predictor=None, max_calls_per_route=None, max_calls_per_scope=None)
    return optimize(
        dataset,
        OptimizationConfig(
            task_lm=build_task_lm(
                model_name="openai/scripted-task",
                api_key="k",
                api_base=_TASK_BASE,
                timeout=20.0,
                max_tokens=1_200,
                ledger=ledger,
                delegate=models.task,
            ),
            reflection_lm=build_reflection_lm(
                model_name="openai/scripted-reflection",
                api_key="k",
                api_base=_REFLECTION_BASE,
                ledger=ledger,
                delegate=models.reflection,
            ),
            judge=_Judge(),
            budget=budget or _budget(),
            compile_fn=compile_fn,
            gepa_log_dir=gepa_log_dir,
        ),
    )


def test_real_gepa_compiles_once_with_stock_proposer_and_objective_selector(monkeypatch: Any, tmp_path: Any) -> None:
    models = _ScriptedModels()
    compile_calls = 0
    real_compile = dspy.GEPA.compile

    def counted_compile(self: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal compile_calls
        compile_calls += 1
        return real_compile(self, *args, **kwargs)

    monkeypatch.setattr(dspy.GEPA, "compile", counted_compile)
    result = _optimize_real(
        models,
        # `_BudgetMeter.before` reserves `max_call_cost` for every call, so the reachable call count is
        # `max_cost / max_call_cost`. Sizing those two independently is how a run silently starves.
        budget=_budget(
            max_metric_calls=40,
            max_task_model_calls=400,
            max_reflection_model_calls=40,
            max_metric_judge_model_calls=400,
            max_cost_microusd=400_000,
            max_call_cost_microusd=1_000,
            seed=143,
        ),
        gepa_log_dir=str(tmp_path / "gepa"),
    )

    assert compile_calls == 1
    assert result.outcome == "ADVANCE"
    assert result.candidate is not None
    stable = load_stable_program_artifact()
    learned = {
        "event_semantics": result.candidate.patch.instruction_for("event_semantics"),
        "reader_card": result.candidate.patch.instruction_for("reader_card"),
    }
    assert learned["event_semantics"] != stable.instruction_for("event_semantics") or learned[
        "reader_card"
    ] != stable.instruction_for("reader_card"), "GEPA changed neither instruction"
    # Public DSPy rendered both native Signatures with their typed response contracts.
    assert models.task.requests and models.reflection.requests
    assert all(request.config.response_format is not None for request in models.task.requests)
    # GEPA checks its own budget between steps, so a completed run legitimately overshoots by up to one full
    # valset evaluation; the compiler allows exactly that and no more.
    # A completed run overshoots by whatever the step in flight consumed: one reflection minibatch plus, on
    # acceptance, one full valset evaluation. Derived, not guessed — this bound was wrong twice and each time
    # it destroyed a finished run after the work was done.
    val_n = result.report.split["development_selection"]["case_n"]
    minibatch = result.report.optimizer["constructor_scalar_arguments"]["reflection_minibatch_size"]
    assert 0 < result.report.usage["metric_calls"] <= 40 + val_n + minibatch
    assert result.report.usage["reflection_model_calls"] > 0, "the reflection endpoint was never used"

    receipt = result.report.optimizer
    assert receipt["optimizer"]["implementation"] == "dspy.GEPA"
    assert receipt["instruction_proposer"] is None
    assert receipt["component_selector"]["allowed"] == ["event_semantics"]
    assert (tmp_path / "gepa" / "gepa_state.bin").is_file()
    report_json = result.report.model_dump_json()
    assert '"trajectory"' not in report_json
    assert '"checkpoint"' not in report_json
    assert "wandb_api_key" not in receipt["constructor_scalar_arguments"]
    assert receipt["compile_call"]["valset_identity"] == "disjoint_cluster_split"
    split = result.report.split
    assert split["disjointness"]["shared_case_ids"] == 0
    assert split["train"]["case_n"] > 0 and split["development_selection"]["case_n"] > 0


def test_instruction_rejection_becomes_scorable_feedback_not_a_silent_zero() -> None:
    """A writer told only "you scored zero" proposes the same rejected text again.

    The native Program returns a Prediction carrying the stable refusal code, so DSPy's metric sees the
    reason and no provider call is made at all.
    """

    from tracefold.news.learning.metric import _compile_example, bind_metric
    from tracefold.news.learning.optimizer import _DspyAcceptedReviewMetric
    from tracefold.news.program.module import NativeNewsProgram

    artifact = load_stable_program_artifact()
    program = NativeNewsProgram(artifact)
    program.event_semantics.signature = program.event_semantics.signature.with_instructions(
        "Judge the filing on its own mechanism. " * 1400
    )
    example = _compile_example(compiler_development_corpus()[0])
    task = _ScriptedTaskLM()

    prediction = program(context=example.context, event_lm=task, card_lm=task)
    outcome = _DspyAcceptedReviewMetric(bind_metric(None))(
        dspy.Example(gold=example),
        prediction,
        None,
        "event_semantics",
        None,
    )

    assert prediction.instruction_rejected == "news_program_instruction_too_large"
    assert task.requests == []
    assert outcome.score == 0.0
    assert "news_program_instruction_too_large" in outcome.feedback


def test_reflection_lm_gets_its_own_budget_and_temperature() -> None:
    from tracefold.news.learning.optimizer import require_model_identity

    ledger = LMCallLedger(max_calls_per_predictor=None, max_calls_per_route=None, max_calls_per_scope=None)
    task = build_task_lm(
        model_name="openai/scripted-task",
        api_key="k",
        api_base="http://h/v1",
        timeout=20.0,
        max_tokens=1200,
        ledger=ledger,
        delegate=_ScriptedTaskLM(),
    )
    reflection = build_reflection_lm(
        model_name="openai/scripted-reflection",
        api_key="k",
        api_base="http://h/v1",
        ledger=ledger,
        delegate=_ScriptedReflectionLM(),
    )

    task_identity = require_model_identity(task, role="task")
    assert (task_identity.temperature, task_identity.max_output_tokens) == (0, 1200)
    # A reflection call has to emit an entire replacement instruction; the task route's ceiling is below what
    # the instruction bound itself accepts, so deriving one from the other truncated every proposal.
    reflection_identity = require_model_identity(reflection, role="reflection")
    assert reflection_identity.temperature == 1.0
    assert reflection_identity.max_output_tokens == REFLECTION_MAX_TOKENS
    assert reflection_identity.timeout_seconds >= 300.0


@pytest.mark.parametrize("cost", [None, 7])
def test_budget_is_metered_with_or_without_a_provider_price(cost: int | None) -> None:
    from tracefold.news.learning.optimizer import _BudgetMeter

    budget = _budget(
        max_metric_calls=10,
        max_task_model_calls=10,
        max_reflection_model_calls=10,
        max_metric_judge_model_calls=10,
        max_cost_microusd=10_000_000,
        max_call_cost_microusd=1_000_000,
        seed=1,
    )
    meter = _BudgetMeter(budget, imputed_call_cost_microusd=budget.max_call_cost_microusd)
    meter.before("task")
    meter.after(
        "task",
        dspy.LMResponse.from_text(
            "{}",
            model="m",
            usage={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
            cost=None if cost is None else cost / 1_000_000,
        ),
    )
    assert meter.actual_cost_microusd > 0
    assert meter.imputed_cost_calls == (1 if cost is None else 0)


def test_a_run_that_learns_nothing_is_refused_as_no_program_change() -> None:
    """A complete run whose Pareto front keeps the seed is `NO_OP`, not a candidate.

    #306 Phase 2 deleted the two tests that used to sit above this one. Both were about DSPy substituting a
    generated instruction for the empty advisory the baseline carried — the seed came back out of the round
    trip as a *learned* strategy and this guard did not fire. A seed is a complete instruction now, and the
    write-set is a plain mapping, so the failure mode cannot occur.
    """

    def _seed_only(student: Any, *, trainset: list[dspy.Example], valset: list[dspy.Example]) -> Any:
        """Stands in for a run whose Pareto front keeps the seed candidate."""

        from types import SimpleNamespace

        del trainset, valset
        optimized = student.deepcopy()
        optimized.detailed_results = SimpleNamespace(
            parents=[[None]],
            val_aggregate_scores=[0.5],
            discovery_eval_counts=[1],
            total_metric_calls=3,
            num_full_val_evals=1,
            seed=143,
            best_idx=0,
        )
        return optimized

    result = _optimize_real(_ScriptedModels(), compile_fn=_seed_only)

    # A terminal answer, not a traceback: the run happened, it kept the seed, and the report says so.
    assert result.outcome == "NO_OP"
    assert result.candidate is None
    assert result.report.reasons == ("news_program_compile_no_program_change",)
