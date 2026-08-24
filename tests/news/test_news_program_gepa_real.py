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
from tracefold.news.agents.semantic_program import EligibleDemoBank, load_stable_program_artifact
from tracefold.news.learning.compiler.root import (
    REFLECTION_MAX_TOKENS,
    CompileBudget,
    CompileRequest,
    ProgramCompiler,
    _FeedbackCompileProgram,
    build_compile_lm,
)
from tracefold.news.learning.compiler.security import CompilerProxyTariff
from tracefold.news.learning.judge import CardEquivalenceJudge
from tracefold.news.learning.metric import DevelopmentEpisode
from tracefold.news.learning.proposer import RulePackAwareProposer
from tracefold.news.models import TRIAGE_POLICY_VERSION
from tracefold.news.semantic_contract import TriageContext


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


_TARIFF = CompilerProxyTariff(
    tariff_id="test",
    input_token_overhead=1_000,
    task_input_microusd_per_million=1,
    task_output_microusd_per_million=1,
    reflection_input_microusd_per_million=1,
    reflection_output_microusd_per_million=1,
    metric_judge_input_microusd_per_million=1,
    metric_judge_output_microusd_per_million=1,
)

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
    """Answers both Predictors and the reflection call without a provider."""

    def __init__(self, model: str, *, reflection: bool = False) -> None:
        super().__init__(model=model)
        self.cache = False
        self.num_retries = 0
        self.kwargs = {
            "temperature": 1.0 if reflection else 0,
            "max_tokens": REFLECTION_MAX_TOKENS,
            "api_base": "http://scripted.invalid/v1",
        }
        self.calls: list[str] = []
        self._reflection = reflection

    def observe_exact_call(self):  # mirrors ExactMetadataDspyLM's seam
        from contextlib import contextmanager

        from tracefold.news.agents.semantic_program import ExactProviderCallCapture, ExactProviderMetadata

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


def _episode(index: int, *, should_push: str, novelty: str, magnitude_fail: bool) -> DevelopmentEpisode:
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
        },
        production_judgment=scored_judgment(
            {
                **{key: value for key, value in _SEMANTICS.items() if key != "relevance"},
                **_CARD,
                "magnitude": 0,
                "actionable": True,
                "decision": "push",
                "title_zh": "",
            },
            relevance=trade_relevance(),
        ),
        policy_metric={
            "gate": {"grounded_assets": ["TSLA"], "admission": "candidate"},
            "storyline": {"title": f"Tesla ships product {index}", "family": "general"},
            "seen": [],
            **_frozen_policy_projection(),
        },
    )


def _corpus() -> tuple[DevelopmentEpisode, ...]:
    """Enough coverage that the honest split can find every required stratum on both sides."""

    return tuple(
        _episode(
            index + 1,
            should_push="must_push" if index % 4 == 0 else ("should_push" if index % 2 == 0 else "should_hold"),
            novelty="restatement" if index % 3 == 0 else "new_fact",
            magnitude_fail=index % 2 == 0,
        )
        for index in range(12)
    )


def test_real_gepa_compiles_this_program_and_produces_a_learned_strategy() -> None:
    task = _ScriptedLM("scripted/task")
    reflection = _ScriptedLM("scripted/reflection", reflection=True)
    compiler = ProgramCompiler(
        base_artifact=load_stable_program_artifact(),
        eligible_demo_bank=EligibleDemoBank.issue(()),
        task_lm=task,  # type: ignore[arg-type]
        reflection_lm=reflection,  # type: ignore[arg-type]
        judge=CardEquivalenceJudge(_ScriptedJudgeLM("scripted/judge"), max_model_calls=400),
        tariff=_TARIFF,
    )
    result = compiler.compile(
        CompileRequest(
            development_dataset_sha="0" * 64,
            review_rubric_version="news_review_v4",
            episodes=_corpus(),
            # `_BudgetMeter.before` reserves `max_call_cost` for every call, so the reachable call count is
            # `max_cost / max_call_cost`. Sizing those two independently is how a run silently starves.
            budget=CompileBudget(
                max_metric_calls=40,
                max_task_model_calls=400,
                max_reflection_model_calls=40,
                max_metric_judge_model_calls=400,
                max_cost_microusd=400_000,
                max_call_cost_microusd=1_000,
                seed=143,
            ),
        )
    )

    learned = {strategy.predictor: strategy.text for strategy in result.patch.learned_strategies}
    assert any(text.strip() for text in learned.values()), "GEPA produced no advisory at all"
    # GEPA checks its own budget between steps, so a completed run legitimately overshoots by up to one full
    # valset evaluation; the compiler allows exactly that and no more.
    # A completed run overshoots by whatever the step in flight consumed: one reflection minibatch plus, on
    # acceptance, one full valset evaluation. Derived, not guessed — this bound was wrong twice and each time
    # it destroyed a finished run after the work was done.
    val_n = result.receipt_payloads.split["development_selection"]["case_n"]
    minibatch = result.receipt_payloads.optimizer_config["constructor_scalar_arguments"]["reflection_minibatch_size"]
    assert 0 < result.metric_calls <= 40 + val_n + minibatch
    assert result.reflection_model_calls > 0, "the reflection endpoint was never used"

    # `dspy.GEPA` only rewrites instructions — `build_program` never touches `predictor.demos`. The DemoBank
    # staying empty is therefore a recorded fact about the optimizer, not a defect to chase.
    assert result.patch.demo_refs.event_semantics == ()
    assert result.patch.demo_refs.reader_card == ()

    receipt = result.receipt_payloads.optimizer_config
    assert receipt["instruction_proposer"]["implementation"].endswith("RulePackAwareProposer")
    assert receipt["compile_call"]["valset_identity"] == "disjoint_cluster_split"
    split = result.receipt_payloads.split
    assert split["disjointness"]["shared_case_ids"] == 0
    assert split["train"]["case_n"] > 0 and split["development_selection"]["case_n"] > 0


def test_reflection_prompt_carries_every_rulepack() -> None:
    """The whole point of the custom proposer: the model can see what it is amending."""

    artifact = load_stable_program_artifact()
    proposer = RulePackAwareProposer(artifact)
    brief = proposer.context_for("event_semantics")
    packs = [pack for pack in artifact.rule_packs if pack.target in {"event_semantics", "both"}]
    assert packs, "the stable artifact carries no event_semantics RulePacks"
    for pack in packs:
        assert f"{pack.rule_id}@{pack.revision}" in brief
    assert "LEARNEDSTRATEGY" in brief
    assert "You may NOT change any of it except" in brief


def test_advisory_rejection_becomes_scorable_feedback_not_a_silent_zero() -> None:
    """An LM told only "you scored zero" proposes the same rejected text again."""

    from tracefold.news.learning.metric import accepted_review_metric

    artifact = load_stable_program_artifact()
    program = _FeedbackCompileProgram(artifact)
    # A URL is one of the code-owned advisory bounds; the text is otherwise unremarkable.
    program.event_semantics.signature = program.event_semantics.signature.with_instructions(
        "Consult https://example.invalid/rules before judging."
    )
    prediction = program(evidence_json="{}", card_evidence_json="{}", told_count=0)
    assert prediction.advisory_rejected == "news_program_learned_strategy_unsafe"

    scored = accepted_review_metric(dspy.Example(accepted_review={}), prediction, None, None, None)
    assert scored.score == 0.0
    assert "news_program_learned_strategy_unsafe" in scored.feedback
    assert "Rewrite it without URLs" in scored.feedback


def test_reflection_lm_gets_its_own_budget_and_temperature() -> None:
    task = build_compile_lm(
        model_name="openai/x", api_key="k", api_base="http://h/v1", timeout=20.0, max_tokens=1200, role="task"
    )
    reflection = build_compile_lm(
        model_name="openai/y", api_key="k", api_base="http://h/v1", timeout=20.0, max_tokens=1200, role="reflection"
    )
    assert task.kwargs["temperature"] == 0 and task.kwargs["max_tokens"] == 1200
    # A reflection call has to emit an entire replacement instruction; the task route's ceiling is below what
    # `LearnedStrategy` itself accepts, so deriving one from the other truncated every proposal.
    assert reflection.kwargs["temperature"] == 1.0
    assert reflection.kwargs["max_tokens"] == REFLECTION_MAX_TOKENS
    assert reflection.kwargs["timeout"] >= 300.0
    assert task.num_retries == 0 and reflection.num_retries == 0


@pytest.mark.parametrize("cost", [None, 7])
def test_budget_is_metered_with_or_without_a_provider_price(cost: int | None) -> None:
    from tracefold.news.agents.semantic_program import ExactProviderMetadata
    from tracefold.news.learning.compiler.root import _BudgetMeter

    budget = CompileBudget(
        max_metric_calls=10,
        max_task_model_calls=10,
        max_reflection_model_calls=10,
        max_metric_judge_model_calls=10,
        max_cost_microusd=10_000_000,
        max_call_cost_microusd=1_000_000,
        seed=1,
    )
    meter = _BudgetMeter(budget, tariff=_TARIFF)
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


def test_empty_advisory_survives_a_gepa_round_trip() -> None:
    """A run that learned nothing must not emit DSPy boilerplate as a learned strategy.

    `Signature.with_instructions("")` does not store an empty instruction — DSPy substitutes a generated one.
    The Artifact's baseline advisory is empty, so GEPA's seed candidate is `""` and its first `build_program`
    put "Given the fields ... produce the fields ..." into the advisory slot. When the Pareto front then kept
    the seed, that boilerplate came back out as a *learned* strategy and `no_program_change` did not fire.
    """

    from tracefold.news.learning.compiler.root import _generated_default_instruction, _restore_empty_advisories

    artifact = load_stable_program_artifact()
    program = _FeedbackCompileProgram(artifact)
    seed = {name: pred.signature.instructions for name, pred in program.named_predictors()}
    assert seed == {"event_semantics": "", "reader_card": ""}, "the baseline advisory is not empty"

    # Exactly what GEPA's `build_program` does with its own seed candidate.
    for name, pred in program.named_predictors():
        pred.signature = pred.signature.with_instructions(seed[name])
    polluted = {name: pred.signature.instructions for name, pred in program.named_predictors()}
    assert polluted["event_semantics"] == _generated_default_instruction(program.event_semantics)
    assert "produce the fields" in polluted["event_semantics"]

    _restore_empty_advisories(program)
    assert {name: pred.signature.instructions for name, pred in program.named_predictors()} == seed


def test_a_run_that_learns_nothing_is_refused_as_no_program_change() -> None:
    """The guard only works once the empty advisory round-trips; before the fix it never fired."""

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

    compiler = ProgramCompiler(
        base_artifact=load_stable_program_artifact(),
        eligible_demo_bank=EligibleDemoBank.issue(()),
        task_lm=_ScriptedLM("scripted/task"),  # type: ignore[arg-type]
        reflection_lm=_ScriptedLM("scripted/reflection", reflection=True),  # type: ignore[arg-type]
        optimizer_factory=_SeedOnlyGEPA,
        judge=CardEquivalenceJudge(_ScriptedJudgeLM("scripted/judge"), max_model_calls=400),
        tariff=_TARIFF,
    )
    with pytest.raises(ValueError, match="news_program_compile_no_program_change"):
        compiler.compile(
            CompileRequest(
                development_dataset_sha="0" * 64,
                review_rubric_version="news_review_v4",
                episodes=_corpus(),
                budget=CompileBudget(
                    max_metric_calls=40,
                    max_task_model_calls=400,
                    max_reflection_model_calls=40,
                    max_metric_judge_model_calls=400,
                    max_cost_microusd=400_000,
                    max_call_cost_microusd=1_000,
                    seed=143,
                ),
            )
        )
