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
from typing import Any, Literal

import dspy  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field

from ..artifact_identity import canonical_sha
from ..models import TriageVerdict
from ..semantic_contract import TriageContext
from .program_judge import CardEquivalenceJudge
from .program_metric import (
    _CARD_DIMENSIONS,
    _SEMANTICS_DIMENSIONS,
    METRIC_ID,
    DevelopmentEpisode,
    bind_metric,
    build_compile_example,
    metric_receipt,
    retrieval_receipt,
)
from .semantic_program import (
    PROGRAM_VERSION,
    DspyCompileProgram,
    DspyStrictJSONAdapter,
    ProgramArtifact,
    render_model_evidence_json,
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
        "excludes: consumer transaction, advisory lock, stale-evidence re-ask,"
        " degraded wire-card fallback, broker, delivery",
    ),
}


class BaselineCase(BaseModel):
    """One scored case. A superset of `DevelopmentEpisode` by exactly one field: the action that shipped."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    episode: DevelopmentEpisode
    recorded_action: str = ""


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
        return canonical_sha(self.model_dump(mode="json"))


def _gold_example(case: BaselineCase) -> dspy.Example:
    """The metric's gold side. `recorded` mode pins the shipped action; the others let `decide()` run."""

    example = build_compile_example(case.episode)
    if case.recorded_action:
        projection = dict(example.get("policy_metric") or {})
        projection["recorded_action"] = case.recorded_action
        example = example.copy(policy_metric=projection)
    return example


def _stored_prediction(case: BaselineCase) -> dspy.Prediction:
    verdict = dict(case.episode.production_verdict or {})
    return dspy.Prediction(verdict=verdict)


def _dimension_tally(cases: Sequence[BaselineCase]) -> dict[str, Any]:
    tally: dict[str, dict[str, int]] = {}
    for case in cases:
        dimensions = dict(case.episode.accepted_review.get("dimensions") or {})
        for name in (*_SEMANTICS_DIMENSIONS, *_CARD_DIMENSIONS):
            label = str(dimensions.get(name) or "")
            if label not in {"pass", "fail"}:
                continue
            row = tally.setdefault(name, {"pass": 0, "fail": 0})
            row[label] += 1
    return {
        name: {**row, "n": row["pass"] + row["fail"], "pass_rate": round(row["pass"] / (row["pass"] + row["fail"]), 6)}
        for name, row in sorted(tally.items())
    }


def _hard_gate(feedback: str, score: float) -> str | None:
    """Which gate zeroed a case. The metric's feedback is the only place it says so, and an operator reading a
    baseline needs the reason far more than the zero."""

    if score > 0.0:
        return None
    if "must receive this fact" in feedback:
        return "must_push_missed"
    if "must not receive this fact" in feedback:
        return "must_hold_pushed"
    if "factually wrong" in feedback:
        return "factual_fail_unchanged"
    if "must be grounded" in feedback:
        return "ungrounded_primary"
    if "schema-valid" in feedback:
        return "schema_invalid"
    return "scored_zero"


def build_runtime_lm(
    *,
    model_name: str,
    api_key: str,
    api_base: str,
    timeout: float,
    max_tokens: int,
    model_kwargs: Mapping[str, Any] | None = None,
) -> dspy.LM:
    """The provider binding for `--mode live`, kept here so the CLI layer never imports DSPy."""

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
) -> CardEquivalenceJudge:
    """The semantic-equivalence judge, built here so the CLI layer never imports DSPy."""

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
    )


def _percentile(values: Sequence[int], fraction: float) -> int:
    ordered = sorted(values)
    if not ordered:
        return 0
    return ordered[min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))]


def _cluster_bootstrap(values: Sequence[float]) -> dict[str, float] | None:
    """Deterministic interval over cluster means, using the release evaluator's own seed and replicates."""

    if len(values) < 2:
        return None
    rng = random.Random(int(_BOOTSTRAP["seed"]))  # noqa: S311 - deterministic, not cryptographic
    n = len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(int(_BOOTSTRAP["replicates"])))
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
            table.setdefault(name, {})[outcome] = table.setdefault(name, {}).get(outcome, 0) + 1
    summary: dict[str, Any] = {}
    for name, counts in sorted(table.items()):
        total = sum(counts.values())
        hits = counts.get("gold_hit", 0) + counts.get("retention_hit", 0) + counts.get("ungolded_change", 0)
        summary[name] = {**counts, "n": total, "hit_rate": round(hits / total, 6) if total else None}
    return summary


async def _run_runtime_route(
    cases: Sequence[BaselineCase],
    judge_program: Any,
) -> list[tuple[BaselineCase, Any, str | None, int]]:
    """Execute the production Program route case by case, in deterministic order.

    Sequential on purpose: the primary circuit breaker is per-Program state, so concurrent cases would make
    "was the breaker open?" depend on scheduling rather than on the run.
    """

    outcomes: list[tuple[BaselineCase, Any, str | None, int]] = []
    for case in sorted(cases, key=lambda item: (item.episode.context.now_ms, item.episode.case_id)):
        started = time.monotonic()
        try:
            judgment = await judge_program.judge(case.episode.context)
            error: str | None = None
        except Exception as exc:
            judgment, error = None, getattr(exc, "code", None) or type(exc).__name__
        outcomes.append((case, judgment, error, int((time.monotonic() - started) * 1000)))
    return outcomes


def run_baseline(
    cases: Sequence[BaselineCase],
    *,
    mode: BaselineMode,
    artifact: ProgramArtifact,
    program_factory: Callable[[ProgramArtifact], dspy.Module] | None = None,
    lm: dspy.LM | None = None,
    judge: CardEquivalenceJudge | None = None,
    semantic_judge: Any = None,
    runtime_identity: Mapping[str, Any] | None = None,
    num_threads: int = 1,
) -> BaselineReport:
    """Score `cases` and return one content-addressable report. Never writes, never delivers, never promotes."""

    if not cases:
        raise ValueError("news_program_baseline_requires_cases")
    metric = bind_metric(judge)
    strict = bind_metric(None) if judge is not None else None
    examples = [_gold_example(case) for case in cases]
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

        def scored_metric(gold: dspy.Example, pred: dspy.Prediction, *args: Any, **kwargs: Any) -> float:
            case_id = str(gold.get("case_id"))
            captured[case_id] = metric(gold, pred, None, None, None)
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
                results.append(_failed_case(case, "provider_or_program_failure"))
                continue
            results.append(_case_result(case, outcome, latency_ms=0))

    elif mode == "runtime_live":
        if semantic_judge is None:
            raise ValueError("news_program_baseline_requires_semantic_judge")
        started = time.monotonic()
        executed = asyncio.run(_run_runtime_route(cases, semantic_judge))
        wall_ms = int((time.monotonic() - started) * 1000)
        by_id = {case.episode.case_id: example for case, example in zip(cases, examples, strict=True)}
        per_case_latency: list[int] = []
        calls = physical = input_tokens = output_tokens = 0
        known_cost = 0
        cost_unknown_n = 0
        routes: dict[str, int] = {}
        for case, judgment, error, elapsed_ms in executed:
            if judgment is None:
                results.append(_failed_case(case, error or "program_route_failure"))
                continue
            per_case_latency.append(elapsed_ms)
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
            prediction = dspy.Prediction(verdict=judgment.verdict.model_dump(mode="json"))
            outcome = metric(example, prediction, None, None, None)
            if strict is not None:
                strict_scores[case.episode.case_id] = float(strict(example, prediction, None, None, None).score)
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
            "p50": _percentile(per_case_latency, 0.50),
            "p95": _percentile(per_case_latency, 0.95),
            "max": max(per_case_latency) if per_case_latency else 0,
            "num_threads": 1,
        }
        route = {
            "answered_by": dict(sorted(routes.items())),
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
    )


def _build_report(
    results: Sequence[CaseResult],
    *,
    cases: Sequence[BaselineCase],
    mode: BaselineMode,
    artifact: ProgramArtifact,
    judge: CardEquivalenceJudge | None,
    strict_scores: Mapping[str, float],
    latency: Mapping[str, Any],
    route: Mapping[str, Any],
    runtime_identity: Mapping[str, Any] | None,
) -> BaselineReport:
    answered = [result for result in results if result.answered]
    failed = [result for result in results if not result.answered]
    if not answered:
        raise ValueError(f"news_program_baseline_all_cases_failed:{len(failed)}/{len(results)}")

    by_case = {case.episode.case_id: case for case in cases}
    case_answered = round(statistics.fmean(result.score for result in answered), 6)
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
            "state_sha256": artifact.state_sha256,
            "policy_version": policy["policy_version"],
            "policy_sha256": policy["policy_sha256"],
            "policy_values": policy["policy_values"],
            "metric": metric_receipt(bind_metric(judge), review_rubric_version="news_review_v2+v3"),
            "metric_id": METRIC_ID,
            "runtime_model": dict(runtime_identity or {}),
            "case_root_sha256": canonical_sha(sorted(case.episode.case_id for case in cases)),
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
        failures={"by_code": dict(sorted(failures_by_code.items()))},
        review_label_distribution=_dimension_tally([by_case[result.case_id] for result in answered]),
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


def _policy_identity(cases: Sequence[BaselineCase]) -> dict[str, Any]:
    """The exact policy every scored example carried, or an explicit absence for `recorded`.

    Fails closed on disagreement: a report covering two policies cannot honestly name one.
    """

    seen: dict[str, dict[str, Any]] = {}
    for case in cases:
        projection = dict(case.episode.policy_metric or {})
        sha = str(projection.get("policy_sha256") or "")
        if sha:
            seen[sha] = {
                "policy_version": str(projection.get("policy_version") or ""),
                "policy_values": dict(projection.get("policy_values") or {}),
            }
    if not seen:
        return {"policy_version": None, "policy_sha256": None, "policy_values": None}
    if len(seen) > 1:
        raise ValueError(f"news_program_baseline_policy_not_uniform:{len(seen)}")
    sha, payload = next(iter(seen.items()))
    return {"policy_sha256": sha, **payload}


def _failed_case(case: BaselineCase, error_code: str) -> CaseResult:
    return CaseResult(
        case_id=case.episode.case_id,
        cluster_id=case.episode.cluster_id,
        stratum=case.episode.stratum,
        score=0.0,
        action="",
        should_push=str(case.episode.accepted_review.get("should_push") or "uncertain"),
        feedback="",
        error_code=error_code,
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


def compile_program_factory(artifact: ProgramArtifact) -> dspy.Module:
    """The exact graph GEPA optimizes, so a baseline and a candidate score the same object."""

    return DspyCompileProgram(artifact)


def build_baseline_cases(episodes: Sequence[Mapping[str, Any]], *, action_source: str) -> tuple[BaselineCase, ...]:
    """Project the evaluator's episode dicts into scored cases.

    ``action_source='recorded'`` reads the action that actually shipped out of the episode; ``'policy'`` leaves
    it empty so the metric re-runs today's `decide()` over the candidate verdict.
    """

    cases: list[BaselineCase] = []
    for raw in episodes:
        payload = dict(raw)
        recorded = str(payload.pop("recorded_action", "") or "")
        # Loader-only keys. `DevelopmentEpisode` forbids extras on purpose: the compiler-visible projection is
        # a contract, and a baseline convenience field must not silently widen it.
        payload.pop("event_id", None)
        episode = DevelopmentEpisode.model_validate(payload)
        cases.append(BaselineCase(episode=episode, recorded_action=recorded if action_source == "recorded" else ""))
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
