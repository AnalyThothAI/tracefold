"""#143/#306: exercise the real optimizer and the real transport, not a stand-in.

Every other optimizer test drives a fake `optimize`, so the governance assertions were proven while the
claim that matters — that this Program, this metric and this proposer actually compile under the optimizer
we ship — was only assumed. These tests run the genuine `gepa.optimize` over the genuine
`ChatCompletionsPredictorAdapter`, `ReflectionLM` and `MetricJudgeEndpoint`, against scripted HTTP.

Since #306 Phase 3 that is a stronger claim than it was: the thing GEPA evaluates is the production
`NewsSemanticProgram`, and the bytes it sends are the bytes production sends.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tests.support.news_judgment import scored_judgment, trade_relevance
from tracefold.news.learning.contracts import (
    REFLECTION_MAX_TOKENS,
    DevelopmentDatasetRef,
    OptimizationBudget,
)
from tracefold.news.learning.objective import DevelopmentEpisode
from tracefold.news.learning.optimizer import (
    InstructionProposer,
    build_reflection_lm,
    build_task_adapter,
)
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


_TASK_BASE = "https://scripted-task.invalid/v1"
_REFLECTION_BASE = "https://scripted-reflection.invalid/v1"
_JUDGE_BASE = "https://scripted-judge.invalid/v1"


class _ScriptedEndpoints:
    """One HTTP handler standing in for all three roles, routed by the URL each builder was given.

    Deliberately not three fake objects: what #306 Phase 3 has to prove is that the real adapter, the real
    reflection callable and the real judge endpoint compose requests a provider can answer, so the seam
    under test is the socket and nothing above it.
    """

    def __init__(self) -> None:
        self.task_calls: list[dict[str, Any]] = []
        self.reflection_calls: list[dict[str, Any]] = []
        self.judge_calls = 0

    def _reply(self, content: str) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "scripted-model",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            },
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        host = request.url.host
        if host == httpx.URL(_REFLECTION_BASE).host:
            self.reflection_calls.append(body)
            return self._reply(f"```\n{_ADVISORY}\n```")
        if host == httpx.URL(_JUDGE_BASE).host:
            self.judge_calls += 1
            verdict = {"headline_equivalent": False, "why_equivalent": False, "facts_preserved": False}
            if "supported_by_evidence" in json.dumps(body["response_format"]):
                verdict = {"supported_by_evidence": False}
            return self._reply(json.dumps({"verdict": verdict}))
        self.task_calls.append(body)
        instruction = body["messages"][0]["content"]
        user = body["messages"][1]["content"]
        if "## semantics_json" in user:
            return self._reply(json.dumps({"card": _CARD}))
        # The proposed instruction has to actually change the answer, or GEPA correctly keeps the seed and
        # the run refuses as `no_program_change`. The accepted gold for the failing cases is magnitude 2.
        magnitude = 2 if _ADVISORY in instruction else 0
        return self._reply(json.dumps({"semantics": {**_SEMANTICS, "magnitude": magnitude}}))


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
    endpoints: _ScriptedEndpoints,
    *,
    budget: OptimizationBudget | None = None,
    optimize_fn: Any = None,
) -> Any:
    """One real GEPA run through the one entry point, over the scripted corpus.

    Until #202 this went through `ProgramCompiler` inside a sealed image against a metered proxy. The
    algorithm, the budget arithmetic and the corpus are unchanged; what is gone is the container that used
    to prove where the two instructions came from.
    """

    from tracefold.news.artifact_identity import canonical_sha
    from tracefold.news.learning.baseline import build_judge
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
            review_rubric_version="news_review_v4",
        ),
        episodes=episodes,
        target_runtime_manifest_sha256="a" * 64,
    )
    async_transport = httpx.MockTransport(endpoints)
    return optimize(
        dataset,
        OptimizationConfig(
            task_adapter=build_task_adapter(
                model_name="openai/scripted-task",
                api_key="k",
                api_base=_TASK_BASE,
                timeout=20.0,
                max_tokens=1_200,
                transport=async_transport,
            ),
            reflection_lm=build_reflection_lm(
                model_name="openai/scripted-reflection",
                api_key="k",
                api_base=_REFLECTION_BASE,
                transport=httpx.MockTransport(endpoints),
            ),
            judge=build_judge(
                model_name="openai/scripted-judge",
                api_key="k",
                api_base=_JUDGE_BASE,
                max_model_calls=400,
                transport=httpx.MockTransport(endpoints),
            ),
            budget=budget or _budget(),
            optimize_fn=optimize_fn,
        ),
    )


def test_real_gepa_compiles_this_program_and_produces_a_learned_instruction() -> None:
    endpoints = _ScriptedEndpoints()
    result = _optimize_real(
        endpoints,
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

    stable = load_stable_program_artifact()
    learned = {name: result.candidate.patch.instruction_for(name) for name in ("event_semantics", "reader_card")}
    assert any(text != stable.instruction_for(name) for name, text in learned.items()), (
        "GEPA changed neither instruction"
    )
    # The real transport answered the real graph: every task request carries a Predictor instruction and a
    # JSON-schema constraint built from the model the answer is validated against.
    assert endpoints.task_calls and endpoints.reflection_calls
    assert all("response_format" in body for body in endpoints.task_calls)
    # GEPA checks its own budget between steps, so a completed run legitimately overshoots by up to one full
    # valset evaluation; the compiler allows exactly that and no more.
    # A completed run overshoots by whatever the step in flight consumed: one reflection minibatch plus, on
    # acceptance, one full valset evaluation. Derived, not guessed — this bound was wrong twice and each time
    # it destroyed a finished run after the work was done.
    val_n = result.report.split["development_selection"]["case_n"]
    minibatch = result.report.optimizer["constructor_scalar_arguments"]["reflection_minibatch_size"]
    assert 0 < result.report.usage["metric_calls"] <= 40 + val_n + minibatch
    assert result.report.usage["reflection_model_calls"] > 0, "the reflection endpoint was never used"

    assert result.report.checkpoint["schema"] == "tracefold.news.compile_checkpoint_receipt.v3"
    assert set(result.report.checkpoint["predictors"]) == {"event_semantics", "reader_card"}

    receipt = result.report.optimizer
    assert receipt["optimizer"]["implementation"] == "gepa.optimize"
    assert receipt["instruction_proposer"]["implementation"].endswith("InstructionProposer")
    assert "wandb_api_key" not in receipt["constructor_scalar_arguments"]
    assert receipt["compile_call"]["valset_identity"] == "disjoint_cluster_split"
    split = result.report.split
    assert split["disjointness"]["shared_case_ids"] == 0
    assert split["train"]["case_n"] > 0 and split["development_selection"]["case_n"] > 0


def test_the_reflection_brief_names_the_whole_instruction_as_the_write_set() -> None:
    """#306 Phase 2: the proposer's job changed from "do not touch this" to "you own all of it"."""

    proposer = InstructionProposer(reflection_lm=lambda prompt: "")
    brief = proposer.context_for("event_semantics")

    assert "COMPLETE instruction" in brief
    assert "replaces the whole instruction" in brief
    # The RulePack stack it used to paste in is the component text now, so the brief must not duplicate it.
    assert "RULEPACK" not in brief
    assert "LEARNEDSTRATEGY" not in brief


def test_instruction_rejection_becomes_scorable_feedback_not_a_silent_zero() -> None:
    """A writer told only "you scored zero" proposes the same rejected text again.

    Until #306 Phase 3 this needed a `_FeedbackCompileProgram` subclass to convert the refusal into a
    prediction, because DSPy's evaluator caught the raise and recorded a failure score without ever calling
    the metric. The adapter this repository owns does it directly, and no provider call is made at all.
    """

    from tracefold.news.learning.metric import _compile_example, bind_metric
    from tracefold.news.learning.optimizer import NewsGepaAdapter

    artifact = load_stable_program_artifact()
    adapter = NewsGepaAdapter(
        adapter=_RefusingAdapter(),
        metric=bind_metric(None),
        proposer=InstructionProposer(reflection_lm=lambda prompt: ""),
    )
    example = _compile_example(compiler_development_corpus()[0])

    # A URL is one of the code-owned instruction bounds; the text is otherwise unremarkable.
    batch = adapter.evaluate(
        [example],
        {
            "event_semantics": "Consult https://example.invalid/rules before judging.",
            "reader_card": artifact.reader_card_instruction,
        },
        capture_traces=True,
    )

    assert batch.scores == [0.0]
    assert batch.outputs[0].instruction_rejected == "news_program_instruction_unsafe"
    records = adapter.make_reflective_dataset({}, batch, ["event_semantics"])
    feedback = records["event_semantics"][0]["Feedback"]
    assert "news_program_instruction_unsafe" in feedback
    assert "Rewrite it without URLs" in feedback


class _RefusingAdapter:
    """Proves the rejection costs nothing: a provider call here is a test failure."""

    def runtime_identity(self, model_binding: str) -> Any:
        from tracefold.news.program.transport import RuntimeModelIdentity

        del model_binding
        return RuntimeModelIdentity.issue(provider="never", model="never/called")

    async def invoke(self, request: Any, spec: Any) -> Any:
        raise AssertionError("a rejected instruction must never reach a provider")


def test_reflection_lm_gets_its_own_budget_and_temperature() -> None:
    from tracefold.news.learning.optimizer import require_model_identity

    task = build_task_adapter(model_name="openai/x", api_key="k", api_base="http://h/v1", timeout=20.0, max_tokens=1200)
    reflection = build_reflection_lm(model_name="openai/y", api_key="k", api_base="http://h/v1")

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
    from tracefold.news.program.transport import ProviderCallMetrics

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
        ProviderCallMetrics(
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
    trip as a *learned* strategy and this guard did not fire. A seed is a complete instruction now, and the
    write-set is a plain mapping, so the failure mode cannot occur.
    """

    def _seed_only(**kwargs: Any) -> Any:
        """Stands in for a run whose Pareto front keeps the seed candidate."""

        from types import SimpleNamespace

        return SimpleNamespace(
            best_candidate=dict(kwargs["seed_candidate"]),
            parents=[[None]],
            val_aggregate_scores=[0.5],
            discovery_eval_counts=[1],
            total_metric_calls=3,
            num_full_val_evals=1,
            seed=kwargs["seed"],
            best_idx=0,
        )

    result = _optimize_real(_ScriptedEndpoints(), optimize_fn=_seed_only)

    # A terminal answer, not a traceback: the run happened, it kept the seed, and the report says so.
    assert result.outcome == "NO_OP"
    assert result.candidate is None
    assert result.report.reasons == ("news_program_compile_no_program_change",)
