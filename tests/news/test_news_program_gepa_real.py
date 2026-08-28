"""#143: exercise the real `dspy.GEPA`, not a stand-in.

Every existing compiler test drives `_FakeGEPA`, so the governance assertions were proven while the claim that
matters — that this Program, this metric and this proposer actually compile under the optimizer we ship — was
only assumed. These tests run the genuine optimizer against a scripted LM.
"""

from __future__ import annotations

import json
from typing import Any

import dspy  # type: ignore[import-untyped]
import pytest

from tests.support.news_judgment import scored_judgment, trade_relevance
from tracefold.news.learning.contracts import (
    METRIC_JUDGE_MAX_TOKENS,
    METRIC_JUDGE_TIMEOUT_SECONDS,
    REFLECTION_MAX_TOKENS,
    REFLECTION_TIMEOUT_SECONDS,
    DevelopmentDatasetRef,
    ModelExecutionIdentity,
    OptimizationBudget,
)
from tracefold.news.learning.judge import CardEquivalenceJudge
from tracefold.news.learning.objective import DevelopmentEpisode
from tracefold.news.learning.optimizer import InstructionProposer, _FeedbackCompileProgram, build_optimizer_lm
from tracefold.news.models import TRIAGE_POLICY_VERSION
from tracefold.news.program.artifact import (
    load_stable_program_artifact,
)
from tracefold.news.program.contracts import TriageContext


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
    "event_type": "product",
    "assets": [{"symbol": "TSLA", "role": "primary"}],
    "magnitude": 2,
    "direction": "bullish",
    "audience": "us_equity",
    "scope": "single_name",
    "confidence": 0.9,
    "relevance": trade_relevance().model_dump(mode="json"),
}
_ADVISORY = "Prefer the stated accepted magnitude when the evidence names a concrete product."
_CARD = {"headline_zh": "特斯拉发布 Cybercab", "why_zh": "新车型进入量产排程，改变该名字的交付预期"}


class _ScriptedLM(dspy.BaseLM):  # type: ignore[misc]
    """Answers both Predictors and the reflection call without a provider.

    It carries the same `tracefold_compiler_endpoint_identity` stamp `build_compile_lm` puts on a real
    route: `ProgramCompiler` refuses an unstamped LM at construction, because the role contract this
    fixture claims to run under is precisely what an identity attests.
    """

    def __init__(self, model: str, *, reflection: bool = False) -> None:
        super().__init__(model=model)
        self.cache = False
        self.num_retries = 0
        api_base = "http://scripted.invalid/v1"
        self.kwargs = {
            "temperature": 1.0 if reflection else 0,
            "max_tokens": REFLECTION_MAX_TOKENS,
            "api_base": api_base,
        }
        self.calls: list[str] = []
        self._reflection = reflection
        self.tracefold_compiler_endpoint_identity = ModelExecutionIdentity.issue(
            role="reflection" if reflection else "task",
            model=model,
            api_base=api_base,
            max_output_tokens=REFLECTION_MAX_TOKENS if reflection else 1_200,
            timeout_seconds=REFLECTION_TIMEOUT_SECONDS if reflection else 20.0,
            temperature=1.0 if reflection else 0.0,
            model_kwargs={},
        )

    def observe_exact_call(self):  # mirrors ExactMetadataDspyLM's seam
        from contextlib import contextmanager

        from tracefold.news.program.dspy_adapter import (
            ExactProviderCallCapture,
            ExactProviderMetadata,
        )

        @contextmanager
        def _cm():
            capture = ExactProviderCallCapture()
            yield capture
            capture.record_metadata(
                ExactProviderMetadata(
                    response_model=self.model,
                    input_tokens=10,
                    output_tokens=10,
                    cached_tokens=0,
                    total_tokens=20,
                    provider_cost_microusd=None,
                    finish_reason="stop",
                )
            )

        return _cm()

    def __call__(self, prompt: Any = None, messages: Any = None, **kwargs: Any) -> list[str]:
        text = json.dumps(prompt if isinstance(prompt, str) else messages)
        self.calls.append(text)
        if self._reflection:
            return [f"```\n{_ADVISORY}\n```"]
        field = "card" if "semantics_json" in text else "semantics"
        if field == "card":
            return [json.dumps({"card": _CARD})]
        # The advisory has to actually change the answer, or GEPA correctly keeps the seed and the compile
        # refuses as `no_program_change`. The accepted gold for the failing cases is magnitude 2.
        magnitude = 2 if _ADVISORY in text else 0
        return [json.dumps({"semantics": {**_SEMANTICS, "magnitude": magnitude}})]


class _ScriptedJudgeLM(_ScriptedLM):
    """The judge route, stamped like the real one.

    `optimize` refuses an unstamped judge: the metric calls it directly, so without a role binding a
    candidate would retain judge-derived scores with no record of the endpoint that produced them.
    """

    def __init__(self, model: str) -> None:
        super().__init__(model)
        self.tracefold_compiler_role_binding = ModelExecutionIdentity.issue(
            role="metric_judge",
            model=model,
            api_base="https://scripted.test/v1",
            max_output_tokens=METRIC_JUDGE_MAX_TOKENS,
            timeout_seconds=METRIC_JUDGE_TIMEOUT_SECONDS,
            temperature=0,
            model_kwargs={},
        )

    def __call__(self, prompt: Any = None, messages: Any = None, **kwargs: Any) -> list[str]:
        del prompt, messages, kwargs
        self.calls.append("metric_judge")
        return [
            json.dumps(
                {
                    "verdict": {
                        "headline_equivalent": False,
                        "why_equivalent": False,
                        "facts_preserved": False,
                    }
                }
            )
        ]


def _episode(
    index: int,
    *,
    should_push: str,
    novelty: str,
    magnitude_fail: bool,
    production_magnitude: int,
    reader_value: str,
) -> DevelopmentEpisode:
    card = {
        "event_id": f"{index:064d}",
        "evidence_version": 1,
        "evidence_sha256": "a" * 64,
        "focus_fact_id": f"{index:064d}",
        "leader_title": f"Tesla ships product {index}",
        "leader_description": "",
        "leader_url": f"https://example.invalid/{index}",
        "reporting_origin": "wire",
        "family": "general",
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
        "opened_at_ms": 1787000000000 + index * 60_000,
        "expires_at_ms": 1787043200000 + index * 60_000,
        "last_member_at_ms": 1787000000000 + index * 60_000,
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
        context=TriageContext.from_card(card, watchlist=(), told_rows=[], now_ms=card["opened_at_ms"], queue_lag_ms=0),
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
        },
        production_judgment=scored_judgment(
            {
                **{key: value for key, value in _SEMANTICS.items() if key != "relevance"},
                **_CARD,
                "magnitude": production_magnitude,
                "actionable": True,
                "decision": "push",
                "title_zh": "",
            },
            relevance=trade_relevance(reader_value=reader_value),
        ),
        policy_metric={
            "gate": {"grounded_assets": ["TSLA"], "admission": "candidate"},
            "storyline": {"title": f"Tesla ships product {index}", "family": "general"},
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
    task: Any,
    reflection: Any,
    *,
    budget: OptimizationBudget | None = None,
    optimizer_factory: Any = None,
) -> Any:
    """One real GEPA run through the one entry point, over the scripted corpus.

    Until #202 this went through `ProgramCompiler` inside a sealed image against a metered proxy. The
    algorithm, the budget arithmetic and the corpus are unchanged; what is gone is the container that used
    to prove where the two instructions came from.
    """

    from tracefold.news.artifact_identity import canonical_sha
    from tracefold.news.learning.optimizer import FrozenDevelopmentDataset, OptimizationConfig, optimize

    episodes = compiler_development_corpus()
    dataset_payload = {"role": "development", "learning_epoch": "program_v7", "cases": []}
    dataset = FrozenDevelopmentDataset.bind(
        dataset_payload=dataset_payload,
        ref=DevelopmentDatasetRef(
            development_dataset_sha256=canonical_sha({"kind": "dataset", "payload": dataset_payload}),
            episode_projection_root_sha256=canonical_sha([e.model_dump(mode="json") for e in episodes]),
            episode_count=len(episodes),
            learning_epoch_started_at_ms=1,
            review_rubric_version="news_review_v4",
        ),
        episodes=episodes,
        target_runtime_manifest_sha256="a" * 64,
    )
    config_kwargs: dict[str, Any] = {}
    if optimizer_factory is not None:
        config_kwargs["optimizer_factory"] = optimizer_factory
    return optimize(
        dataset,
        OptimizationConfig(
            task_lm=task,
            reflection_lm=reflection,
            judge=CardEquivalenceJudge(
                _ScriptedJudgeLM("scripted/judge"),
                max_tokens=METRIC_JUDGE_MAX_TOKENS,
                max_model_calls=400,
            ),
            budget=budget or _budget(),
            **config_kwargs,
        ),
    )


def test_real_gepa_compiles_this_program_and_produces_a_learned_strategy() -> None:
    task = _ScriptedLM("scripted/task")
    reflection = _ScriptedLM("scripted/reflection", reflection=True)
    result = _optimize_real(
        task,
        reflection,
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
    )

    learned = {name: result.candidate.patch.instruction_for(name) for name in ("event_semantics", "reader_card")}
    assert any(text.strip() for text in learned.values()), "GEPA produced no advisory at all"
    # GEPA checks its own budget between steps, so a completed run legitimately overshoots by up to one full
    # valset evaluation; the compiler allows exactly that and no more.
    # A completed run overshoots by whatever the step in flight consumed: one reflection minibatch plus, on
    # acceptance, one full valset evaluation. Derived, not guessed — this bound was wrong twice and each time
    # it destroyed a finished run after the work was done.
    val_n = result.report.split["development_selection"]["case_n"]
    minibatch = result.report.optimizer["constructor_scalar_arguments"]["reflection_minibatch_size"]
    assert 0 < result.report.usage["metric_calls"] <= 40 + val_n + minibatch
    assert result.report.usage["reflection_model_calls"] > 0, "the reflection endpoint was never used"

    assert result.report.checkpoint["schema"] == "tracefold.news.compile_checkpoint_receipt.v2"
    assert set(result.report.checkpoint["predictors"]) == {"event_semantics", "reader_card"}

    receipt = result.report.optimizer
    assert receipt["instruction_proposer"]["implementation"].endswith("InstructionProposer")
    assert receipt["omitted_unset_arguments"] == ["wandb_api_key"]
    assert "wandb_api_key" not in receipt["constructor_scalar_arguments"]
    assert receipt["compile_call"]["valset_identity"] == "disjoint_cluster_split"
    split = result.report.split
    assert split["disjointness"]["shared_case_ids"] == 0
    assert split["train"]["case_n"] > 0 and split["development_selection"]["case_n"] > 0


def test_the_reflection_brief_names_the_whole_instruction_as_the_write_set() -> None:
    """#306 Phase 2: the proposer's job changed from "do not touch this" to "you own all of it"."""

    proposer = InstructionProposer(load_stable_program_artifact())
    brief = proposer.context_for("event_semantics")

    assert "COMPLETE instruction" in brief
    assert "replaces the whole instruction" in brief
    # The RulePack stack it used to paste in is the component text now, so the brief must not duplicate it.
    assert "RULEPACK" not in brief
    assert "LEARNEDSTRATEGY" not in brief


def test_instruction_rejection_becomes_scorable_feedback_not_a_silent_zero() -> None:
    """An LM told only "you scored zero" proposes the same rejected text again."""

    from tracefold.news.learning.metric import accepted_review_metric

    artifact = load_stable_program_artifact()
    program = _FeedbackCompileProgram(artifact)
    # A URL is one of the code-owned instruction bounds; the text is otherwise unremarkable.
    program.event_semantics.signature = program.event_semantics.signature.with_instructions(
        "Consult https://example.invalid/rules before judging."
    )
    prediction = program(evidence_json="{}", card_evidence_json="{}", told_count=0)
    assert prediction.instruction_rejected == "news_program_instruction_unsafe"

    scored = accepted_review_metric(dspy.Example(accepted_review={}), prediction, None, None, None)
    assert scored.score == 0.0
    assert "news_program_instruction_unsafe" in scored.feedback
    assert "Rewrite it without URLs" in scored.feedback


def test_reflection_lm_gets_its_own_budget_and_temperature() -> None:
    task = build_optimizer_lm(
        model_name="openai/x", api_key="k", api_base="http://h/v1", timeout=20.0, max_tokens=1200, role="task"
    )
    reflection = build_optimizer_lm(
        model_name="openai/y", api_key="k", api_base="http://h/v1", timeout=20.0, max_tokens=1200, role="reflection"
    )
    assert task.kwargs["temperature"] == 0 and task.kwargs["max_tokens"] == 1200
    # A reflection call has to emit an entire replacement instruction; the task route's ceiling is below what
    # the instruction bound itself accepts, so deriving one from the other truncated every proposal.
    assert reflection.kwargs["temperature"] == 1.0
    assert reflection.kwargs["max_tokens"] == REFLECTION_MAX_TOKENS
    assert reflection.kwargs["timeout"] >= 300.0
    assert task.num_retries == 0 and reflection.num_retries == 0


@pytest.mark.parametrize("cost", [None, 7])
def test_budget_is_metered_with_or_without_a_provider_price(cost: int | None) -> None:
    from tracefold.news.learning.optimizer import _BudgetMeter
    from tracefold.news.program.dspy_adapter import ExactProviderMetadata

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
        ExactProviderMetadata(
            response_model="m",
            input_tokens=100,
            output_tokens=10,
            cached_tokens=0,
            total_tokens=110,
            provider_cost_microusd=cost,
            finish_reason="stop",
        )
    )
    assert meter.actual_cost_microusd > 0
    assert meter.imputed_cost_calls == (1 if cost is None else 0)


def test_a_run_that_learns_nothing_is_refused_as_no_program_change() -> None:
    """A complete run whose Pareto front keeps the seed is `NO_OP`, not a candidate.

    #306 Phase 2 deleted the two tests that used to sit above this one. Both were about DSPy substituting a
    generated instruction for the empty advisory the baseline carried — the seed came back out of the round
    trip as a *learned* strategy and this guard did not fire. A seed is a complete instruction now, so
    `with_instructions` has nothing to substitute and the failure mode cannot occur.
    """

    class _SeedOnlyGEPA:
        """Stands in for a GEPA run whose Pareto front keeps the seed program."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.detailed_results = type(
                "R", (), {"total_metric_calls": 3, "candidates": [], "val_aggregate_scores": []}
            )()

        def compile(self, student: Any, *, trainset: Any, teacher: Any, valset: Any) -> Any:
            for _name, pred in student.named_predictors():
                pred.signature = pred.signature.with_instructions(pred.signature.instructions)
            student.detailed_results = self.detailed_results
            return student

    result = _optimize_real(
        _ScriptedLM("scripted/task"),
        _ScriptedLM("scripted/reflection", reflection=True),
        optimizer_factory=_SeedOnlyGEPA,
    )

    # A terminal answer, not a traceback: the run happened, it kept the seed, and the report says so.
    assert result.outcome == "NO_OP"
    assert result.candidate is None
    assert result.report.reasons == ("news_program_compile_no_program_change",)
