"""Offline baseline: what the Program scores, and whether the production route answers at all.

Every DSPy tutorial starts by measuring a baseline, and until #143 this repository had none. #150 is the
second half of that: the first version answered two different questions under one name.

Three modes, each with its own question and its own exclusions:

``recorded``      Score the verdict production persisted, against the action it actually shipped. No model
                  request, no policy replay — so it stays reproducible across policy revisions and is the
                  calibration proof for metric wiring.
``compile_live``  Run `DspyCompileProgram` — literally the graph GEPA optimizes — against one task endpoint.
                  This is the optimizer's baseline. It has no fallback route, no fast retry, no per-route
                  deadline and no circuit breaker, so its failure rate is not production's.
``runtime_live``  Run the configured `DspyNewsSemanticProgram` through `composition.semantic_judge()`: four
                  slots, one shared fast retry per route, fallback restarting the graph, per-route deadline
                  and the primary circuit breaker. This is the production *Program route*, and nothing more —
                  it does not simulate the consumer's transaction, the advisory lock, stale-evidence re-ask,
                  the degraded wire-card fallback, RabbitMQ or delivery. The report names those exclusions
                  rather than claiming end-to-end parity.

The report has no single ambiguous `score`. A provider failure is a real outcome, so it is published as both
an answered-only mean (quality given an answer) and a failure-as-zero mean (the end-to-end lower bound); the
first version reported only the former, which let 29 unanswered cases lift a 0.482 to a 0.587 by disappearing.

Read-only throughout: opens the database as `serve` only, writes no verdict, delivers nothing, and proposes,
accepts and promotes nothing.
"""

from __future__ import annotations

import asyncio
import math
import random
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, NamedTuple

import dspy  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field

from ..artifact_identity import canonical_sha
from ..models import TriageVerdict
from ..program.artifact import (
    ProgramStrategyArtifactV1,
    render_model_evidence_json,
)
from ..program.contracts import TriageContext
from ..program.dspy_adapter import DspyStrictJSONAdapter
from ..program.graph import DspyCompileProgram
from ..program.runtime import PROGRAM_VERSION
from .judge import CardEquivalenceJudge
from .metric import (
    COMPONENT_FIELDS,
    LABEL_GROUP,
    METRIC_ID,
    UNGROUPED_LABEL,
    DevelopmentEpisode,
    bind_metric,
    build_compile_example,
    metric_receipt,
    retrieval_receipt,
    verify_policy_projection,
)

BaselineMode = Literal["recorded", "compile_live", "runtime_live"]
BASELINE_SCHEMA: Literal["tracefold.news.program_baseline_report.v2"] = "tracefold.news.program_baseline_report.v2"
# Bootstrap convention shared with the release evaluator, so a cluster interval here means the same
# thing it means there.
_BOOTSTRAP = {"seed": 112, "replicates": 2_000, "confidence": 0.95}
_EXECUTION_SCOPE = {
    "recorded": ("no model call", "no policy replay", "scores the action that shipped"),
    "compile_live": (
        "the graph GEPA optimizes",
        "single task endpoint",
        "no fallback route",
        "no fast retry",
        "no per-route deadline",
        "no circuit breaker",
    ),
    "runtime_live": (
        "configured four-slot Program route",
        "one shared fast retry per route",
        "fallback restarts the graph",
        "per-route deadline and primary circuit breaker",
        "replays the frozen production ToldContext; no arm-local ledger replay",
        "excludes: consumer transaction, advisory lock, stale-evidence re-ask,"
        " degraded wire-card fallback, broker, delivery",
    ),
}


class BaselineCase(BaseModel):
    """One scored case plus the persisted production ``DecisionResult`` when replay is forbidden."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    episode: DevelopmentEpisode
    recorded_decision_result: dict[str, Any] | None = None


class CaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    cluster_id: str
    stratum: str
    score: float
    action: str
    should_push: str
    feedback: str
    gold_scored_n: int = 0
    labelled_n: int = 0
    latency_ms: int = 0
    error_code: str | None = None
    # Per-dimension outcome of *this* candidate: gold_hit/gold_miss, retention_hit/retention_miss,
    # ungolded_change/ungolded_unchanged, field_absent.
    dimension_outcomes: tuple[tuple[str, str], ...] = ()
    # Which hard gate zeroed this case, or "" when none did.
    hard_gate: str = ""
    production_rule: str = ""
    production_throttled_by: str = ""
    objective_guard: str = "none"
    component_scores: dict[str, float | None] = Field(default_factory=dict)
    component_denominators: dict[str, int] = Field(default_factory=dict)
    component_diagnostics: dict[str, dict[str, Any]] = Field(default_factory=dict)
    effective_weight_mass: float = 0.0
    route: str | None = None
    physical_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    provider_cost_microusd: int | None = None

    @property
    def answered(self) -> bool:
        return self.error_code is None


class BaselineReport(BaseModel):
    """A report with no single ambiguous number.

    `population` and `scores` are separate on purpose: a provider failure is an outcome, not an absence. The
    answered-only mean says how good the output is *given* an answer; the failure-as-zero mean is the
    end-to-end lower bound. Publishing only the first is what let 29 unanswered cases turn 0.482 into 0.587.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_id: Literal["tracefold.news.program_baseline_report.v2"] = BASELINE_SCHEMA
    mode: BaselineMode
    identity: dict[str, Any]
    execution_scope: tuple[str, ...]
    population: dict[str, Any]
    scores: dict[str, Any]
    action_confusion: dict[str, Any]
    hard_gates: dict[str, Any]
    failures: dict[str, Any]
    # Corpus metadata: what reviewers labelled. Byte-identical however the predictions change — which is why
    # it is named for what it is and is no longer the only per-dimension table in the report.
    review_label_distribution: dict[str, Any]
    # What this candidate actually did, dimension by dimension.
    prediction_dimensions: dict[str, Any]
    gold_coverage: dict[str, Any]
    retrieval: dict[str, Any]
    semantic_judge: dict[str, Any] = Field(default_factory=dict)
    latency_ms: dict[str, Any] = Field(default_factory=dict)
    route: dict[str, Any] = Field(default_factory=dict)
    cases: tuple[CaseResult, ...] = ()

    @property
    def report_sha256(self) -> str:
        """The address of the *measurement*, with wall-clock latency excluded.

        Latency belongs in the report — an operator has to see it — but it cannot be part of the identity:
        hashing it made two live runs with byte-identical predictions produce two different addresses purely
        from millisecond jitter, so the one thing a content address is for ("same measurement?") was exactly
        what it could not answer. `latency_ms` is still covered by `latency_sha256` for anyone who wants to
        compare timings as well.
        """

        payload = self.model_dump(mode="json")
        payload["latency_ms"] = {}
        payload["cases"] = [{**case, "latency_ms": 0} for case in payload["cases"]]
        return canonical_sha(payload)

    @property
    def latency_sha256(self) -> str:
        return canonical_sha({"latency_ms": self.latency_ms, "cases": [case.latency_ms for case in self.cases]})


def _gold_example(case: BaselineCase) -> dspy.Example:
    """The metric's gold side. `recorded` mode pins the shipped action; the others let `decide()` run."""

    example = build_compile_example(case.episode)
    if not case.recorded_decision_result:
        raise ValueError(f"news_program_baseline_recorded_decision_missing:{case.episode.case_id[:16]}")
    projection = dict(example.get("policy_metric") or {})
    projection["recorded_decision_result"] = dict(case.recorded_decision_result)
    example = example.copy(policy_metric=projection)
    return example


def _stored_prediction(case: BaselineCase) -> dspy.Prediction:
    judgment = case.episode.production_judgment
    if judgment is None:
        return dspy.Prediction(verdict={}, editorial={})
    return dspy.Prediction(
        verdict=judgment.verdict.model_dump(mode="json"),
        editorial=judgment.editorial.model_dump(mode="json"),
    )


def _dimension_tally(cases: Sequence[BaselineCase]) -> dict[str, Any]:
    """What reviewers labelled, grouped by the stage each label describes.

    Grouped rather than flat because #150's Stage D is an ownership repair: `timeliness` is delivery-owned and
    no longer scored against EventSemantics, but dropping it from the report would hide that operators keep
    labelling it. Under `delivery` it stays visible as corpus metadata and can never be mistaken for
    something a Predictor was graded on; `not_scored` is the catch-all for a dimension nobody has placed yet.
    """

    tally: dict[str, dict[str, dict[str, int]]] = {}
    for case in cases:
        dimensions = dict(case.episode.accepted_review.get("dimensions") or {})
        # Driven by the labels the reviewer actually wrote, not by a list of names kept here. A rubric that
        # grows a dimension shows up under `not_scored` on the next run instead of disappearing.
        for name, value in dimensions.items():
            label = str(value or "")
            if label not in {"pass", "fail"}:
                continue
            group = LABEL_GROUP.get(str(name), UNGROUPED_LABEL)
            row = tally.setdefault(group, {}).setdefault(str(name), {"pass": 0, "fail": 0})
            row[label] += 1
    return {
        owner: {
            name: {
                **row,
                "n": row["pass"] + row["fail"],
                "pass_rate": round(row["pass"] / (row["pass"] + row["fail"]), 6),
            }
            for name, row in sorted(rows.items())
        }
        for owner, rows in sorted(tally.items())
    }


def _hard_gates(results: Sequence[CaseResult]) -> dict[str, Any]:
    """Which gate zeroed each case. A scalar that can only go to zero must say why it did.

    Read off the metric's own typed `hard_gate` field. The predecessor recovered this by matching the
    feedback sentence, which is prose the reflection model reads and a maintainer may reword at any time.
    """

    tally: dict[str, int] = {}
    for result in results:
        if result.hard_gate:
            tally[result.hard_gate] = tally.get(result.hard_gate, 0) + 1
    return {"by_gate": dict(sorted(tally.items())), "n": sum(tally.values())}


def build_runtime_lm(
    *,
    model_name: str,
    api_key: str,
    api_base: str,
    timeout: float,
    max_tokens: int,
    model_kwargs: Mapping[str, Any] | None = None,
) -> dspy.LM:
    """Provider binding for the explicit live evaluation modes; the CLI layer never imports DSPy."""

    extras = dict(model_kwargs or {})
    owned = {"api_key", "api_base", "base_url", "cache", "num_retries", "temperature", "max_tokens", "timeout"}
    if owned & set(extras):
        raise ValueError(f"news_program_baseline_model_kwargs_owned:{','.join(sorted(owned & set(extras)))}")
    return dspy.LM(
        str(model_name),
        api_key=str(api_key),
        api_base=str(api_base),
        timeout=float(timeout),
        max_tokens=int(max_tokens),
        temperature=0,
        cache=False,
        num_retries=0,
        **extras,
    )


def build_metric_lm(
    *,
    model_name: str,
    api_key: str,
    api_base: str,
    model_kwargs: Mapping[str, Any],
    timeout: float = 120.0,
    max_tokens: int = 4_096,
) -> dspy.LM:
    """A metric-side endpoint (judge, drafter). `model_kwargs` comes from the app's provider resolution.

    Passing it matters: for `deepseek-v4-*` it carries `extra_body.thinking = disabled`, and this gateway
    enables thinking by default. Without it the model spends its whole output budget reasoning and returns an
    empty answer — which is what made every early judge verdict truncate and every early draft fail to parse.
    Raising `max_tokens` only hid that.
    """

    return dspy.LM(
        str(model_name),
        api_key=str(api_key),
        api_base=str(api_base),
        timeout=float(timeout),
        max_tokens=int(max_tokens),
        temperature=0,
        cache=False,
        num_retries=0,
        **dict(model_kwargs),
    )


def build_judge(
    *,
    model_name: str,
    api_key: str,
    api_base: str,
    model_kwargs: Mapping[str, Any] | None = None,
    timeout: float = 120.0,
    max_tokens: int = 4_096,
    max_model_calls: int | None = None,
) -> CardEquivalenceJudge:
    """The semantic-equivalence judge, built here so the CLI layer never imports DSPy.

    `max_model_calls` is the judge's own ceiling, admitted atomically before a slow provider call. A caller
    that spends unattended — the experiment loop's `optimize`, which can run for hours — passes one; the
    interactive baseline does not, because an operator watching a bounded `--max-model-cases` run is the
    ceiling.
    """

    return CardEquivalenceJudge(
        build_metric_lm(
            model_name=model_name,
            api_key=api_key,
            api_base=api_base,
            model_kwargs=model_kwargs or {},
            timeout=timeout,
            max_tokens=max_tokens,
        ),
        max_tokens=int(max_tokens),
        max_model_calls=max_model_calls,
    )


def _percentile(values: Sequence[int], fraction: float) -> int:
    ordered = sorted(values)
    if not ordered:
        return 0
    return ordered[min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))]


def _cluster_bootstrap(values: Sequence[float]) -> dict[str, float] | None:
    """Deterministic interval over cluster means, using the release evaluator's own seed and replicates.

    Sorted first, and that is not cosmetic. A fixed seed draws a fixed *index* sequence, so resampling an
    insertion-ordered list makes the published interval — and therefore `report_sha256` — a function of the
    order cases happened to arrive in. `recorded` keeps input order while `runtime_live` re-sorts by
    `(opened_at_ms, case_id)`, so the same corpus scored two ways produced two different intervals in a
    receipt whose entire purpose is comparability.
    """

    if len(values) < 2:
        return None
    ordered = sorted(values)
    rng = random.Random(int(_BOOTSTRAP["seed"]))  # noqa: S311 - deterministic, not cryptographic
    n = len(ordered)
    means = sorted(sum(ordered[rng.randrange(n)] for _ in range(n)) / n for _ in range(int(_BOOTSTRAP["replicates"])))
    alpha = (1 - float(_BOOTSTRAP["confidence"])) / 2
    return {
        "lower": round(means[max(0, math.floor(alpha * len(means)))], 6),
        "upper": round(means[min(len(means) - 1, math.ceil((1 - alpha) * len(means)) - 1)], 6),
    }


def _cluster_macro(results: Sequence[CaseResult], *, failure_as_zero: bool) -> tuple[float | None, int, list[float]]:
    """One connected fact cluster, one vote. Several cases about the same fact must not out-vote a lone one."""

    buckets: dict[str, list[float]] = {}
    for result in results:
        if not result.answered and not failure_as_zero:
            continue
        buckets.setdefault(result.cluster_id, []).append(result.score if result.answered else 0.0)
    if not buckets:
        return None, 0, []
    per_cluster = [statistics.fmean(scores) for scores in buckets.values()]
    return round(statistics.fmean(per_cluster), 6), len(per_cluster), per_cluster


def _action_confusion(results: Sequence[CaseResult]) -> dict[str, Any]:
    """Split by the reviewer's own label. A single agreement rate hides which direction the errors run."""

    table: dict[str, dict[str, int]] = {}
    for result in results:
        label = result.should_push
        if label not in {"must_push", "should_push", "must_hold", "should_hold"}:
            continue
        row = table.setdefault(label, {"n": 0, "reached_reader": 0, "withheld": 0})
        row["n"] += 1
        if result.action in {"push", "escalate"}:
            row["reached_reader"] += 1
        else:
            row["withheld"] += 1
    summary: dict[str, Any] = {}
    for label, row in sorted(table.items()):
        agreed = row["reached_reader"] if label in {"must_push", "should_push"} else row["withheld"]
        summary[label] = {
            **row,
            "agreed": agreed,
            "agreement": round(agreed / row["n"], 6) if row["n"] else None,
        }
    return summary


def _prediction_dimensions(results: Sequence[CaseResult]) -> dict[str, Any]:
    """What the candidate did, not what the corpus contains."""

    table: dict[str, dict[str, int]] = {}
    for result in results:
        for name, outcome in result.dimension_outcomes:
            row = table.setdefault(name, {})
            row[outcome] = row.get(outcome, 0) + 1
    summary: dict[str, Any] = {}
    for name, counts in sorted(table.items()):
        total = sum(counts.values())
        hits = sum(value for outcome, value in counts.items() if outcome.endswith("_hit"))
        summary[name] = {
            **counts,
            "n": total,
            # The reviewer labelled this dimension on `n` cases and left the rest alone. Publishing the
            # silence keeps `n` readable: a dimension scored on 40 of 242 cases is a different claim from one
            # scored on 240, and the rate alone cannot tell them apart.
            "not_labelled": len(results) - total,
            "hit_rate": round(hits / total, 6) if total else None,
        }
    return summary


def _aggregate_component_diagnostics(results: Sequence[CaseResult]) -> dict[str, dict[str, Any]]:
    """Aggregate the exact per-case support emitted by the shared metric ruler."""

    totals: dict[str, dict[str, Any]] = {
        component: {
            "denominator": 0,
            "effective_weight_mass": 0.0,
            "gold_scored_n": 0,
            "labelled_n": 0,
            "field_n": {field: 0 for field in fields},
        }
        for component, fields in COMPONENT_FIELDS.items()
    }
    for result in results:
        for component, fields in COMPONENT_FIELDS.items():
            source = result.component_diagnostics[component]
            target = totals[component]
            target["denominator"] += int(source["denominator"])
            target["effective_weight_mass"] += float(source["effective_weight_mass"])
            target["gold_scored_n"] += int(source["gold_scored_n"])
            target["labelled_n"] += int(source["labelled_n"])
            for field in fields:
                target["field_n"][field] += int(source["field_n"][field])
    for target in totals.values():
        target["effective_weight_mass"] = round(float(target["effective_weight_mass"]), 6)
        labelled_n = int(target["labelled_n"])
        target["gold_coverage"] = round(int(target["gold_scored_n"]) / labelled_n, 6) if labelled_n else None
    return totals


class RouteOutcome(NamedTuple):
    case: BaselineCase
    judgment: Any
    error: str | None
    elapsed_ms: int
    spent: dict[str, int | None]


def _spend_from_partial_trace(program_trace: Any, attempts: int) -> dict[str, int | None]:
    """What a failed execution actually cost, from the trace it managed to record.

    Mirrors the production consumer's `_usage_from_partial_trace`: synthetic entries stay in `call_count`
    for audit, and only entries marked as physical provider calls contribute tokens or cost.
    """

    calls = tuple(getattr(program_trace, "calls", ()) or ())
    physical = tuple(call for call in calls if getattr(call, "physical_provider_call", False))
    costs = [call.provider_cost_microusd for call in physical]
    return {
        "call_count": len(calls) if calls else max(0, int(attempts)),
        "physical_call_count": len(physical),
        "retry_count": sum(1 for call in calls if int(getattr(call, "attempt", 1)) > 1),
        "input_tokens": sum(call.input_tokens for call in physical),
        "output_tokens": sum(call.output_tokens for call in physical),
        "provider_cost_microusd": sum(costs) if costs and all(cost is not None for cost in costs) else None,
    }


async def _run_runtime_route(
    cases: Sequence[BaselineCase],
    judge_program: Any,
) -> list[RouteOutcome]:
    """Execute the production Program route case by case, in deterministic order.

    Sequential on purpose: the primary circuit breaker is per-Program state, so concurrent cases would make
    "was the breaker open?" depend on scheduling rather than on the run.
    """

    outcomes: list[RouteOutcome] = []
    for case in sorted(cases, key=lambda item: (item.episode.context.now_ms, item.episode.case_id)):
        started = time.monotonic()
        spent: dict[str, int | None] = {}
        try:
            judgment = await judge_program.judge(case.episode.context)
            error: str | None = None
        except Exception as exc:
            judgment, error = None, getattr(exc, "code", None) or type(exc).__name__
            # A failed route is the most expensive kind: up to the whole six-call chain budget. Dropping its
            # spend understated `route` and `latency_ms` precisely where the operator most needs them, and
            # left `route.answered_by` not summing to the requested population with nothing saying why.
            # `SemanticJudgeError` carries the partial trace for exactly this reason.
            spent = _spend_from_partial_trace(getattr(exc, "partial_trace", None), getattr(exc, "attempts", 0))
        outcomes.append(RouteOutcome(case, judgment, error, int((time.monotonic() - started) * 1000), spent))
    return outcomes


def run_baseline(
    cases: Sequence[BaselineCase],
    *,
    mode: BaselineMode,
    artifact: ProgramStrategyArtifactV1,
    program_factory: Callable[[ProgramStrategyArtifactV1], dspy.Module] | None = None,
    lm: dspy.LM | None = None,
    judge: CardEquivalenceJudge | None = None,
    semantic_judge: Any = None,
    runtime_identity: Mapping[str, Any] | None = None,
    cohort_scope: str = "unknown",
    num_threads: int = 1,
) -> BaselineReport:
    """Score `cases` and return one content-addressable report. Never writes, never delivers, never promotes."""

    if not cases:
        raise ValueError("news_program_baseline_requires_cases")
    if mode != "recorded":
        # Before the first provider call. Every input to this check is a pure function of `cases`, so a
        # corpus that cannot verify its own policy must cost nothing to reject — not two Predictor calls per
        # case and a report that files the defect as "the route did not answer".
        for case in cases:
            try:
                verify_policy_projection(case.episode.policy_metric)
            except ValueError as exc:
                raise ValueError(f"news_program_baseline_policy_unusable:{case.episode.case_id[:16]}:{exc}") from exc
    # `recorded` measures the judgment and action that already shipped. It never asks a model whether that
    # historical card repaired a failed factual label: there is no candidate repair to verify, and doing so
    # would make the deterministic calibration depend on provider availability. The configured judge remains
    # in the receipt with zero usage; both live modes keep using it for candidate retention and factual repair.
    metric = bind_metric(None if mode == "recorded" else judge)
    strict = bind_metric(None) if judge is not None else None
    examples = [_gold_example(case) if mode == "recorded" else build_compile_example(case.episode) for case in cases]
    by_case = {case.episode.case_id: case for case in cases}

    results: list[CaseResult] = []
    strict_scores: dict[str, float] = {}
    latency: dict[str, Any] = {}
    route: dict[str, Any] = {}

    if mode == "recorded":
        for case, example in zip(cases, examples, strict=True):
            outcome = metric(example, _stored_prediction(case), None, None, None)
            if strict is not None:
                strict_scores[case.episode.case_id] = float(
                    strict(example, _stored_prediction(case), None, None, None).score
                )
            results.append(_case_result(case, outcome, latency_ms=0))

    elif mode == "compile_live":
        if program_factory is None:
            raise ValueError("news_program_baseline_requires_program_factory")
        if lm is None and dspy.settings.lm is None:
            # Without this the run "succeeds": every case raises `No LM is loaded`, `Evaluate` swallows it,
            # and the receipt reads as a measured 0.0 baseline rather than a run that made no requests.
            raise ValueError("news_program_baseline_requires_lm")
        program = program_factory(artifact)
        captured: dict[str, dspy.Prediction] = {}
        # Separate from `captured` so a raise inside the metric — a stale `policy_sha256`, a schema drift —
        # is not filed as `provider_or_program_failure` and read as route unavailability. Both are failures;
        # they have nothing else in common and different people fix them.
        metric_errors: dict[str, str] = {}

        def scored_metric(gold: dspy.Example, pred: dspy.Prediction, *args: Any, **kwargs: Any) -> float:
            case_id = str(gold.get("case_id"))
            try:
                captured[case_id] = metric(gold, pred, None, None, None)
            except Exception as exc:  # a defect in the ruler, not an outcome of the route
                metric_errors[case_id] = _error_code(exc)
                return 0.0
            if strict is not None:
                # The same predictions scored the old way — the judge's verdicts are already cached, so this
                # measures the ruler rather than model noise.
                strict_scores[case_id] = float(strict(gold, pred, None, None, None).score)
            return float(captured[case_id].score)

        evaluate = dspy.Evaluate(
            devset=examples,
            metric=scored_metric,
            num_threads=num_threads,
            display_progress=False,
            failure_score=0.0,
            provide_traceback=True,
            # A baseline must survive the cases it is measuring; `Evaluate` otherwise cancels the whole run.
            max_errors=len(examples) * 10 + 100,
        )
        started = time.monotonic()
        with (
            dspy.context(lm=lm, adapter=DspyStrictJSONAdapter(use_native_function_calling=False))
            if lm
            else (dspy.context(adapter=DspyStrictJSONAdapter(use_native_function_calling=False)))
        ):
            evaluation = evaluate(program)
        wall_ms = int((time.monotonic() - started) * 1000)
        latency = {
            "wall_ms": wall_ms,
            "per_case_mean_ms": round(wall_ms / max(1, len(examples)), 1),
            "num_threads": num_threads,
        }
        for entry in evaluation.results:
            case = by_case[str(entry[0].get("case_id"))]
            outcome = captured.get(case.episode.case_id)
            if outcome is None:
                results.append(
                    _failed_case(case, metric_errors.get(case.episode.case_id, "provider_or_program_failure"))
                )
                continue
            results.append(_case_result(case, outcome, latency_ms=0))

    elif mode == "runtime_live":
        if semantic_judge is None:
            raise ValueError("news_program_baseline_requires_semantic_judge")
        started = time.monotonic()
        executed = asyncio.run(_run_runtime_route(cases, semantic_judge))
        wall_ms = int((time.monotonic() - started) * 1000)
        by_id = {case.episode.case_id: example for case, example in zip(cases, examples, strict=True)}
        # Two populations, published separately. The spec asks for per-answered-case latency; a route that
        # exhausts the chain is also the slowest case there is, so hiding it would understate exactly the
        # tail an operator is bounding the run against.
        answered_latency: list[int] = []
        all_latency: list[int] = []
        retries = 0
        calls = physical = input_tokens = output_tokens = 0
        known_cost = 0
        cost_unknown_n = 0
        routes: dict[str, int] = {}
        for case, judgment, error, elapsed_ms, spent in executed:
            all_latency.append(elapsed_ms)
            if judgment is None:
                retries += int(spent.get("retry_count") or 0)
                calls += int(spent.get("call_count") or 0)
                physical += int(spent.get("physical_call_count") or 0)
                input_tokens += int(spent.get("input_tokens") or 0)
                output_tokens += int(spent.get("output_tokens") or 0)
                spent_cost = spent.get("provider_cost_microusd")
                if spent_cost is None:
                    cost_unknown_n += 1
                else:
                    known_cost += int(spent_cost)
                results.append(
                    _failed_case(
                        case,
                        error or "program_route_failure",
                        latency_ms=elapsed_ms,
                        physical_calls=int(spent.get("physical_call_count") or 0),
                        input_tokens=int(spent.get("input_tokens") or 0),
                        output_tokens=int(spent.get("output_tokens") or 0),
                    )
                )
                continue
            answered_latency.append(elapsed_ms)
            retries += sum(1 for call in judgment.trace.calls if int(getattr(call, "attempt", 1)) > 1)
            usage = judgment.usage
            calls += usage.call_count
            physical += usage.physical_call_count
            input_tokens += usage.input_tokens
            output_tokens += usage.output_tokens
            if usage.provider_cost_microusd is None:
                # Neither the local endpoint nor DeepSeek returns a resolvable price. Counting it as zero
                # would publish a cost the run never proved.
                cost_unknown_n += 1
            else:
                known_cost += usage.provider_cost_microusd
            answering_route = "fallback" if judgment.fallback_from else "primary"
            routes[answering_route] = routes.get(answering_route, 0) + 1
            example = by_id[case.episode.case_id]
            prediction = dspy.Prediction(
                verdict=judgment.verdict.model_dump(mode="json"),
                editorial=judgment.editorial.model_dump(mode="json"),
            )
            try:
                outcome = metric(example, prediction, None, None, None)
                if strict is not None:
                    strict_scores[case.episode.case_id] = float(strict(example, prediction, None, None, None).score)
            except Exception as exc:
                # Unguarded, one bad episode raised *after* `asyncio.run` had already executed every case —
                # destroying a run that had spent up to six real calls per case on the endpoint that also
                # serves production Triage, with no `--out` file written.
                code = _error_code(exc)
                results.append(
                    _failed_case(
                        case,
                        code,
                        latency_ms=elapsed_ms,
                        physical_calls=usage.physical_call_count,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                    )
                )
                continue
            results.append(
                _case_result(
                    case,
                    outcome,
                    latency_ms=elapsed_ms,
                    route=answering_route,
                    physical_calls=usage.physical_call_count,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    provider_cost_microusd=usage.provider_cost_microusd,
                )
            )
        latency = {
            "wall_ms": wall_ms,
            "population": "answered cases; *_with_failures covers every requested case",
            "p50": _percentile(answered_latency, 0.50),
            "p95": _percentile(answered_latency, 0.95),
            "max": max(answered_latency) if answered_latency else 0,
            "p95_with_failures": _percentile(all_latency, 0.95),
            "max_with_failures": max(all_latency) if all_latency else 0,
            "num_threads": 1,
        }
        route = {
            "answered_by": dict(sorted(routes.items())),
            "unanswered_n": sum(1 for outcome in executed if outcome.judgment is None),
            "retry_count": retries,
            "call_count": calls,
            "physical_call_count": physical,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "provider_cost_microusd_known": known_cost,
            "cost_unknown_n": cost_unknown_n,
        }
    else:  # pragma: no cover - Literal keeps this unreachable
        raise ValueError(f"news_program_baseline_mode_invalid:{mode}")

    return _build_report(
        results,
        cases=cases,
        mode=mode,
        artifact=artifact,
        judge=judge,
        strict_scores=strict_scores,
        latency=latency,
        route=route,
        runtime_identity=runtime_identity,
        cohort_scope=cohort_scope,
    )


def _build_report(
    results: Sequence[CaseResult],
    *,
    cases: Sequence[BaselineCase],
    mode: BaselineMode,
    artifact: ProgramStrategyArtifactV1,
    judge: CardEquivalenceJudge | None,
    strict_scores: Mapping[str, float],
    latency: Mapping[str, Any],
    route: Mapping[str, Any],
    runtime_identity: Mapping[str, Any] | None,
    cohort_scope: str = "unknown",
) -> BaselineReport:
    answered = [result for result in results if result.answered]
    failed = [result for result in results if not result.answered]

    # `None`, not `0.0`. "The route answered nothing" is the single most important `runtime_live` result and
    # the predecessor was the only outcome that produced no receipt at all — after up to six real provider
    # calls per case had been paid for. It raised so a reader could not mistake an empty run for a measured
    # zero; a null score says that better, and keeps the per-case error codes, the failure breakdown and the
    # route aggregates that make the run diagnosable.
    case_answered = round(statistics.fmean(result.score for result in answered), 6) if answered else None
    case_zero = round(statistics.fmean(result.score if result.answered else 0.0 for result in results), 6)
    cluster_answered, cluster_n, cluster_values = _cluster_macro(results, failure_as_zero=False)
    cluster_zero, _cluster_zero_n, cluster_zero_values = _cluster_macro(results, failure_as_zero=True)

    failures_by_code: dict[str, int] = {}
    for result in failed:
        code = str(result.error_code or "unknown")
        failures_by_code[code] = failures_by_code.get(code, 0) + 1

    gold_n = sum(result.gold_scored_n for result in answered)
    labelled_n = sum(result.labelled_n for result in answered)
    policy = _policy_identity(cases)
    component_diagnostics = _aggregate_component_diagnostics(answered)
    scores: dict[str, Any] = {
        "case_macro_answered": case_answered,
        "case_macro_failure_as_zero": case_zero,
        "cluster_macro_answered": cluster_answered,
        "cluster_macro_failure_as_zero": cluster_zero,
        "cluster_n": cluster_n,
        "cluster_interval_95": _cluster_bootstrap(cluster_values),
        "cluster_interval_95_failure_as_zero": _cluster_bootstrap(cluster_zero_values),
        "note": (
            "answered-only is quality given an answer; failure-as-zero is the end-to-end lower bound. "
            "They differ by exactly the unanswered cases."
        ),
        "component_denominators": {
            name: diagnostic["denominator"] for name, diagnostic in component_diagnostics.items()
        },
        "component_diagnostics": component_diagnostics,
        "effective_weight_mass_mean": (
            round(statistics.fmean(result.effective_weight_mass for result in answered), 6) if answered else None
        ),
        "objective_guard_distribution": {
            name: sum(result.objective_guard == name for result in answered)
            for name in sorted({result.objective_guard for result in answered})
        },
        "production_rule_distribution": {
            name: sum(result.production_rule == name for result in answered)
            for name in sorted({result.production_rule for result in answered})
        },
    }
    if strict_scores:
        answered_ids = {result.case_id for result in answered}
        relevant = [value for case_id, value in strict_scores.items() if case_id in answered_ids]
        if relevant:
            scores["case_macro_answered_byte_equality"] = round(statistics.fmean(relevant), 6)

    return BaselineReport(
        mode=mode,
        identity={
            "program_version": PROGRAM_VERSION,
            "program_sha256": artifact.program_sha256,
            "factory_id": artifact.factory_id,
            "policy_version": policy["policy_version"],
            "policy_sha256": policy["policy_sha256"],
            "policy_values": policy["policy_values"],
            "policy_source": policy["policy_source"],
            "metric": metric_receipt(bind_metric(judge), review_rubric_version="news_review_v4"),
            "metric_id": METRIC_ID,
            "runtime_model": dict(runtime_identity or {}),
            # `current` is the release-plane population (this Program, this policy, this epoch); `all`
            # drops that and reads every accepted review in the window. Both are legitimate and they answer
            # different questions, so the receipt names which one it read rather than leaving it to the
            # command line that produced it.
            "cohort_scope": cohort_scope,
            "case_root_sha256": canonical_sha(sorted(case.episode.case_id for case in cases)),
            # The id list answers "the same cases?"; only this answers "the same inputs?". Hashing ids alone
            # let one report SHA describe two different corpora — any evidence edit that kept the ids left the
            # published address untouched, which is the one thing a content address must not do.
            "corpus_sha256": canonical_sha(
                [case.model_dump(mode="json") for case in sorted(cases, key=lambda item: item.episode.case_id)]
            ),
            "cluster_root_sha256": canonical_sha(sorted({case.episode.cluster_id for case in cases})),
        },
        execution_scope=_EXECUTION_SCOPE[mode],
        population={
            "requested_n": len(results),
            "answered_n": len(answered),
            "failure_n": len(failed),
            "failure_rate": round(len(failed) / len(results), 6) if results else 0.0,
        },
        scores=scores,
        action_confusion=_action_confusion(answered),
        hard_gates=_hard_gates(answered),
        failures={"by_code": dict(sorted(failures_by_code.items()))},
        # Every requested case, never only the answered ones. Three documents promise this table does not
        # move when the model does; built over `answered` it moved whenever a provider timed out, and an
        # operator reading a changed label distribution concludes the corpus changed under them.
        review_label_distribution=_dimension_tally(cases),
        prediction_dimensions=_prediction_dimensions(answered),
        gold_coverage={
            "gold_scored_n": gold_n,
            "labelled_n": labelled_n,
            "rate": round(gold_n / labelled_n, 6) if labelled_n else None,
            "note": "share of scored dimensions decided against a stated correct value, not against 'any change'",
        },
        retrieval=retrieval_receipt([case.episode for case in cases]),
        semantic_judge=({**judge.stats, **judge.identity} if judge is not None else {}),
        latency_ms=dict(latency),
        route=dict(route),
        cases=tuple(results),
    )


_ABSENT_POLICY: dict[str, Any] = {
    "policy_version": None,
    "policy_sha256": None,
    "policy_values": None,
    "policy_source": None,
}


def _policy_identity(cases: Sequence[BaselineCase]) -> dict[str, Any]:
    """The policy the run actually replayed, or an explicit absence.

    An episode always *carries* a policy — the projection freezes one so `decide()` can be replayed — but
    `recorded` returns before replay, so naming that policy in the identity would claim a dependency the
    number does not have. `CONTRACTS.md` says `recorded` publishes a null policy; before this it published
    today's configured arm, which is exactly the ambient-state confusion #150 exists to remove.

    Fails closed on disagreement: a report covering two policies cannot honestly name one.
    """

    if all(case.recorded_decision_result for case in cases):
        return dict(_ABSENT_POLICY)
    seen: dict[str, dict[str, Any]] = {}
    for case in cases:
        projection = dict(case.episode.policy_metric or {})
        sha = str(projection.get("policy_sha256") or "")
        if sha:
            seen[sha] = {
                "policy_version": str(projection.get("policy_version") or ""),
                "policy_values": dict(projection.get("policy_values") or {}),
                # Where the values came from. `active_arm_manifest` over an `--all-cohorts` window means
                # today's rules replayed on a retired corpus — a real question, but not "the policy that arm
                # ran", and the receipt must not let a verified hash imply otherwise.
                "policy_source": str(projection.get("policy_source") or "unknown"),
            }
    if not seen:
        return dict(_ABSENT_POLICY)
    if len(seen) > 1:
        raise ValueError(f"news_program_baseline_policy_not_uniform:{len(seen)}")
    sha, payload = next(iter(seen.items()))
    if canonical_sha(dict(payload["policy_values"])) != sha:
        # Recomputed rather than forwarded. The projection's own pair is what the metric verifies per example;
        # publishing it unchecked let a tampered corpus put mismatched values and SHA into the receipt.
        raise ValueError("news_program_baseline_policy_identity_mismatch")
    return {"policy_sha256": sha, **payload}


def _error_code(exc: Exception) -> str:
    """A failure code an operator can act on.

    `metric_error:ValueError` said nothing; the message it swallowed said
    `news_program_metric_policy_sha256_mismatch:6f59f3a6!=1c64d060`.
    """

    code = getattr(exc, "code", None)
    if code:
        return str(code)
    detail = str(exc).strip().splitlines()[0][:80] if str(exc).strip() else type(exc).__name__
    return f"metric_error:{detail}"


def _failed_case(
    case: BaselineCase,
    error_code: str,
    *,
    latency_ms: int = 0,
    physical_calls: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> CaseResult:
    return CaseResult(
        case_id=case.episode.case_id,
        cluster_id=case.episode.cluster_id,
        stratum=case.episode.stratum,
        score=0.0,
        action="",
        should_push=str(case.episode.accepted_review.get("should_push") or "uncertain"),
        feedback="",
        error_code=error_code,
        latency_ms=latency_ms,
        physical_calls=physical_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _case_result(
    case: BaselineCase,
    outcome: dspy.Prediction,
    *,
    latency_ms: int,
    route: str | None = None,
    physical_calls: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    provider_cost_microusd: int | None = None,
) -> CaseResult:
    return CaseResult(
        case_id=case.episode.case_id,
        cluster_id=case.episode.cluster_id,
        stratum=case.episode.stratum,
        score=float(outcome.score),
        action=str(getattr(outcome, "production_action", "") or ""),
        should_push=str(case.episode.accepted_review.get("should_push") or "uncertain"),
        feedback=str(outcome.feedback or ""),
        hard_gate=str(getattr(outcome, "hard_gate", "") or ""),
        production_rule=str(getattr(outcome, "production_rule", "") or ""),
        production_throttled_by=str(getattr(outcome, "production_throttled_by", "") or ""),
        objective_guard=str(getattr(outcome, "objective_guard", "none") or "none"),
        component_scores=dict(getattr(outcome, "component_scores", {}) or {}),
        component_denominators={
            str(name): int(value) for name, value in dict(getattr(outcome, "component_denominators", {}) or {}).items()
        },
        component_diagnostics={str(name): dict(value) for name, value in dict(outcome.component_diagnostics).items()},
        effective_weight_mass=float(getattr(outcome, "effective_weight_mass", 0.0) or 0.0),
        gold_scored_n=int(getattr(outcome, "gold_scored_n", 0) or 0),
        labelled_n=int(getattr(outcome, "labelled_n", 0) or 0),
        dimension_outcomes=tuple(getattr(outcome, "dimension_outcomes", ()) or ()),
        latency_ms=latency_ms,
        route=route,
        physical_calls=physical_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        provider_cost_microusd=provider_cost_microusd,
    )


def compile_program_factory(artifact: ProgramStrategyArtifactV1) -> dspy.Module:
    """The exact graph GEPA optimizes, so a baseline and a candidate score the same object."""

    return DspyCompileProgram(artifact)


def build_baseline_cases(episodes: Sequence[Mapping[str, Any]], *, action_source: str) -> tuple[BaselineCase, ...]:
    """Project the evaluator's episode dicts into scored cases.

    ``action_source='recorded'`` reads the complete persisted decision projection; ``'policy'`` omits it so
    the metric runs the frozen policy over the candidate judgment.
    """

    cases: list[BaselineCase] = []
    for raw in episodes:
        payload = dict(raw)
        recorded = payload.pop("recorded_decision_result", None)
        # Loader-only keys. `DevelopmentEpisode` forbids extras on purpose: the compiler-visible projection is
        # a contract, and a baseline convenience field must not silently widen it.
        payload.pop("event_id", None)
        episode = DevelopmentEpisode.model_validate(payload)
        cases.append(
            BaselineCase(
                episode=episode,
                recorded_decision_result=dict(recorded) if action_source == "recorded" and recorded else None,
            )
        )
    return tuple(cases)


__all__ = [
    "BASELINE_SCHEMA",
    "BaselineCase",
    "BaselineMode",
    "BaselineReport",
    "CaseResult",
    "TriageContext",
    "TriageVerdict",
    "build_baseline_cases",
    "build_judge",
    "build_metric_lm",
    "compile_program_factory",
    "render_model_evidence_json",
    "run_baseline",
]
