"""Offline baseline: score the Program the optimizer optimizes, without the optimizer.

Every DSPy tutorial starts by measuring a baseline, and until #143 this repository had none — no
`dspy.Evaluate` anywhere, and the only path that could run the Program at all required a sealed candidate, a
frozen dataset and a container sandbox. So every Prompt and policy edit was a blind edit: four days of
production went through eight policy revisions and six Program images without one "before X, after Y" receipt.

This module is the missing step. It is cold, read-only, needs no sandbox, tariff or container, and shares
`accepted_review_metric` byte-for-byte with the cold optimizer (`program_metric`) so that the number an
operator reads before a RulePack edit is the number GEPA will later try to maximize.

Three case sources, deliberately separate:

``recorded``   Score the verdict production actually persisted, against the action it actually shipped.
               No model request. This is the metric-wiring proof: it is exactly reproducible forever, across
               policy revisions, because it asserts nothing about what today's policy would do.
``replay``     Re-run the Program graph over a recorded Predictor corpus. Assembly, normalization, storyline
               and `decide()` all execute; only the provider is replaced. Deterministic and free.
``live``       Run the Program against the configured provider. The only mode that costs money and time, and
               the only one that can measure a Prompt change.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

import dspy  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field

from ..artifact_identity import canonical_sha
from ..models import TRIAGE_POLICY_VERSION, TriageVerdict
from ..semantic_contract import TriageContext
from ..triage_rules import DEFAULT_POLICY
from .program_metric import (
    _CARD_DIMENSIONS,
    _SEMANTICS_DIMENSIONS,
    METRIC_ID,
    DevelopmentEpisode,
    accepted_review_metric,
    build_compile_example,
    metric_receipt,
    retrieval_receipt,
)
from .semantic_program import (
    PROGRAM_VERSION,
    DspyCompileProgram,
    ProgramArtifact,
    render_model_evidence_json,
)

BaselineMode = Literal["recorded", "replay", "live"]
BASELINE_SCHEMA = "tracefold.news.program_baseline_report.v1"


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


class BaselineReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_id: Literal["tracefold.news.program_baseline_report.v1"] = BASELINE_SCHEMA
    mode: BaselineMode
    identity: dict[str, Any]
    score: float
    case_n: int
    components: dict[str, Any]
    dimensions: dict[str, Any]
    hard_gates: dict[str, int]
    gold_coverage: dict[str, Any]
    retrieval: dict[str, Any]
    provider_failure_n: int = 0
    latency_ms: dict[str, Any] = Field(default_factory=dict)
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


def run_baseline(
    cases: Sequence[BaselineCase],
    *,
    mode: BaselineMode,
    artifact: ProgramArtifact,
    program_factory: Callable[[ProgramArtifact], dspy.Module] | None = None,
    runtime_identity: Mapping[str, Any] | None = None,
    num_threads: int = 1,
) -> BaselineReport:
    """Score `cases` and return one content-addressable report. Never writes, never delivers, never promotes."""

    if not cases:
        raise ValueError("news_program_baseline_requires_cases")
    examples = [_gold_example(case) for case in cases]
    by_case = {case.episode.case_id: case for case in cases}

    results: list[CaseResult] = []
    provider_failures = 0
    latency: dict[str, Any] = {}

    if mode == "recorded":
        for case, example in zip(cases, examples, strict=True):
            outcome = accepted_review_metric(example, _stored_prediction(case), None, None, None)
            results.append(_case_result(case, outcome, latency_ms=0))
    else:
        if program_factory is None:
            raise ValueError("news_program_baseline_requires_program_factory")
        program = program_factory(artifact)
        captured: dict[str, dict[str, Any]] = {}

        def metric(gold: dspy.Example, pred: dspy.Prediction, *args: Any, **kwargs: Any) -> float:
            captured[str(gold.get("case_id"))] = accepted_review_metric(gold, pred, None, None, None)
            return float(captured[str(gold.get("case_id"))].score)

        evaluate = dspy.Evaluate(
            devset=examples,
            metric=metric,
            num_threads=num_threads,
            display_progress=False,
            failure_score=0.0,
            provide_traceback=True,
            # A baseline must survive the cases it is measuring. `Evaluate` cancels the whole run once errors
            # reach `max_errors`, and a Program whose provider truncates on a handful of hard inputs would
            # otherwise report nothing at all instead of reporting a low score and the failure count.
            max_errors=len(examples) * 10 + 100,
        )
        started = time.monotonic()
        evaluation = evaluate(program)
        wall_ms = int((time.monotonic() - started) * 1000)
        latency = {
            "wall_ms": wall_ms,
            "per_case_mean_ms": round(wall_ms / max(1, len(examples)), 1),
            "num_threads": num_threads,
        }
        for entry in evaluation.results:
            example = entry[0]
            case = by_case[str(example.get("case_id"))]
            outcome = captured.get(case.episode.case_id)
            if outcome is None:
                # `Evaluate` swallowed the failure. A provider that did not answer is not evidence that the
                # Program is wrong, so it is counted separately instead of averaged in as a zero.
                provider_failures += 1
                results.append(
                    CaseResult(
                        case_id=case.episode.case_id,
                        cluster_id=case.episode.cluster_id,
                        stratum=case.episode.stratum,
                        score=0.0,
                        action="",
                        should_push=str(case.episode.accepted_review.get("should_push") or "uncertain"),
                        feedback="",
                        error_code="provider_or_program_failure",
                    )
                )
                continue
            # No per-case latency. `Evaluate` owns the program call, so the only interval this loop could
            # observe is the metric's own arithmetic; reporting that as "case latency" would read as a
            # provider timing and be wrong by three orders of magnitude. The wall clock is reported instead.
            results.append(_case_result(case, outcome, latency_ms=0))

    scored = [result for result in results if result.error_code is None]
    score = round(statistics.fmean(result.score for result in scored), 6) if scored else 0.0
    gates: dict[str, int] = {}
    for result in scored:
        reason = _hard_gate(result.feedback, result.score)
        if reason:
            gates[reason] = gates.get(reason, 0) + 1
    gold_n = sum(result.gold_scored_n for result in scored)
    labelled_n = sum(result.labelled_n for result in scored)

    action_hit = action_n = 0
    for result in scored:
        if result.should_push in {"must_push", "should_push", "must_hold", "should_hold"}:
            action_n += 1
            wanted = result.should_push in {"must_push", "should_push"}
            action_hit += int((result.action in {"push", "escalate"}) is wanted)

    identity = {
        "program_version": PROGRAM_VERSION,
        "program_sha256": artifact.program_sha256,
        "state_sha256": artifact.state_sha256,
        "policy_version": TRIAGE_POLICY_VERSION,
        "policy_values": DEFAULT_POLICY.as_dict(),
        "metric": metric_receipt(accepted_review_metric, review_rubric_version="news_review_v2+v3"),
        "metric_id": METRIC_ID,
        "runtime_model": dict(runtime_identity or {}),
        "case_root_sha256": canonical_sha(sorted(case.episode.case_id for case in cases)),
        "cluster_root_sha256": canonical_sha(sorted({case.episode.cluster_id for case in cases})),
    }
    return BaselineReport(
        mode=mode,
        identity=identity,
        score=score,
        case_n=len(scored),
        components={
            "action_agreement": {
                "n": action_n,
                "hit": action_hit,
                "rate": round(action_hit / action_n, 6) if action_n else None,
            },
            "weights": {"final_action": 0.50, "event_semantics": 0.35, "reader_card": 0.15},
        },
        dimensions=_dimension_tally([by_case[result.case_id] for result in scored]),
        hard_gates=dict(sorted(gates.items())),
        gold_coverage={
            "gold_scored_n": gold_n,
            "labelled_n": labelled_n,
            "rate": round(gold_n / labelled_n, 6) if labelled_n else None,
            "note": "share of scored dimensions decided against a stated correct value rather than 'any change scores'",
        },
        retrieval=retrieval_receipt([case.episode for case in cases]),
        provider_failure_n=provider_failures,
        latency_ms=latency,
        cases=tuple(results),
    )


def _case_result(case: BaselineCase, outcome: dspy.Prediction, *, latency_ms: int) -> CaseResult:
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
        latency_ms=latency_ms,
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
    "compile_program_factory",
    "render_model_evidence_json",
    "run_baseline",
]
