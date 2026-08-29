"""`news learning run`: the one recommended GEPA path, end to end (#253 §7 Phase C).

An operator used to reach a terminal report through three commands, three output files and three sets of
flags whose values had to agree — the judge model, the corpus bound, the dataset SHA — with nothing but
care to make them agree. The three steps were right; the composition was the operator's to do by hand, and
#225 is what that costs: a standalone baseline reading `0.0` beside an in-run seed reading `0.475`, with no
artifact anywhere that said whether the two numbers even described the same experiment.

This command is that composition and nothing more. It calls the same `readiness`, the same
`baseline --dataset --mode compile_live` and the same `optimize` an operator can still call one at a time,
into one directory, and then publishes `run_summary.json` — the projection that answers whether the two
Stable numbers are comparable, and what to do next.

It defines no second Objective Plan, Metric, split, budget or optimizer. What it does own is the wiring
those three commands could not check about each other:

- the equivalence judge is the configured compiler reflection route, not a name retyped per command, so the
  standalone baseline and GEPA are scored by one ruler by construction;
- the corpus bound is checked against readiness *before* anything is spent, instead of failing after the
  baseline has already paid for a partial corpus;
- a corpus readiness has already refused skips the baseline entirely and goes straight to the terminal
  report `optimize` produces for free.

It registers nothing, accepts nothing, promotes nothing and deploys nothing.
"""

from __future__ import annotations

import json
from argparse import Namespace
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .news_learning_baseline import _handle_learning_baseline, _handle_learning_readiness
from .news_learning_documents import _write_json

_READINESS_FILE = "readiness.json"
_BASELINE_FILE = "baseline-compile-live.json"
_OPTIMIZATION_DIR = "optimization"
_OPTIMIZATION_FILE = "optimization_report.json"
_CANDIDATE_FILE = "prompt_candidate.json"
_SUMMARY_FILE = "run_summary.json"


def _handle_learning_run(args: Namespace, settings: Any, stable: Any) -> tuple[int, dict[str, Any]]:
    """Readiness, standalone baseline, optimization, summary — in that order, into one directory."""

    from tracefold.news.learning.run_summary import build_run_summary

    out = Path(str(args.out))
    out.mkdir(parents=True, exist_ok=True)
    # A directory is the record of *one* run. Every artifact of a previous one goes first, because a run
    # that stops early — a corpus readiness refuses, a provider failure in the baseline — otherwise leaves
    # a fresh `readiness.json` beside the last run's report and candidate, with no summary to reveal that
    # they came from different corpora. `optimize` clears its own stale candidate for the same reason.
    for stale in (
        out / _SUMMARY_FILE,
        out / _BASELINE_FILE,
        out / _OPTIMIZATION_DIR / _OPTIMIZATION_FILE,
        out / _OPTIMIZATION_DIR / _CANDIDATE_FILE,
    ):
        stale.unlink(missing_ok=True)
    development = str(args.development).strip()
    # Resolved before the corpus work rather than after it: an unconfigured reflection route is the one
    # identity that would otherwise turn a planned run into an error only once readiness and the baseline
    # had already been paid for.
    judge_model = _reflection_judge_model(settings)
    task_route = _task_route(settings)

    readiness = _readiness(settings, stable, out=out, development=development)
    baseline: dict[str, Any] | None = None
    if str(readiness.get("outcome")) == "ready":
        _require_full_corpus_budget(args, readiness)
        baseline = _baseline(args, settings, stable, out=out, development=development, judge_model=judge_model)
    # Run even on a corpus readiness refused: `optimize` rebuilds the same plan and returns a `REJECTED`
    # terminal report before it touches an endpoint, so the summary's terminal is one the optimizer
    # actually produced rather than a verdict this command invented from a readiness outcome.
    optimization = _optimize(args, settings, stable, out=out, development=development)

    candidate = out / _OPTIMIZATION_DIR / _CANDIDATE_FILE
    summary = build_run_summary(
        development_dataset_sha=development,
        readiness=readiness,
        baseline=baseline,
        optimization=optimization,
        task_route=task_route,
        artifacts={
            "readiness": _READINESS_FILE,
            "baseline": _BASELINE_FILE if baseline is not None else None,
            "optimization": f"{_OPTIMIZATION_DIR}/{_OPTIMIZATION_FILE}",
            "prompt_candidate": f"{_OPTIMIZATION_DIR}/{_CANDIDATE_FILE}" if candidate.is_file() else None,
        },
    )
    _write_json(str(out / _SUMMARY_FILE), summary)
    # The summary is written whatever the verdict — a run whose two Stable numbers are not comparable is
    # exactly the run an operator has to be able to read — but it is never *quoted* as a comparison: the
    # exit code separates "no candidate" from "this pair of numbers proves nothing".
    if summary["baseline"]["same_population"] is False:
        return 2, {
            "ok": False,
            "error": {
                "code": "news_learning_run_population_identity_mismatch",
                "path": str(out / _SUMMARY_FILE),
                "mismatched_checks": [
                    check["name"] for check in summary["baseline"]["population_checks"] if check["status"] == "mismatch"
                ],
            },
        }
    advance = summary["optimization"]["terminal"] == "ADVANCE"
    return (0 if advance else 1), {"ok": advance, "data": _stdout_summary(summary, path=out / _SUMMARY_FILE)}


def _stdout_summary(summary: dict[str, Any], *, path: Path) -> dict[str, Any]:
    """The summary an operator reads in the terminal: everything but the twelve-row check table.

    Same convention `readiness` uses for its per-case dispositions. The rows are the evidence behind
    `same_population` and they belong in the file; printing them would bury the four numbers this command
    exists to show behind a wall of digests.
    """

    baseline = dict(summary["baseline"])
    baseline["population_checks_written_to"] = str(path)
    del baseline["population_checks"]
    return {"path": str(path), **summary, "baseline": baseline}


def _readiness(settings: Any, stable: Any, *, out: Path, development: str) -> dict[str, Any]:
    """The zero-call explanation, written to the run directory and read back whole.

    The handler returns a summary with `case_dispositions` stripped; the file is the report. Reading the
    file back is what keeps this command a composition rather than a second reporting path.
    """

    path = out / _READINESS_FILE
    code, payload = _handle_learning_readiness(Namespace(development=development, out=str(path)), settings, stable)
    if code != 0:
        raise ValueError(_error_code(payload, fallback="news_learning_run_readiness_failed"))
    return _read(path)


def _require_full_corpus_budget(args: Namespace, readiness: dict[str, Any]) -> None:
    """Refuse a bound that cannot cover the corpus, before the first provider call rather than after.

    `baseline --dataset` already refuses a partial corpus — a truncated run would publish split roots for
    cases it never scored — but it refuses after projecting the corpus and inside a command that has
    already been paid for by the operator's attention. Readiness has just counted the representatives for
    free, so the same refusal is available here for nothing.
    """

    required = int(readiness.get("objective", {}).get("optimizer_case_n") or 0)
    bound = int(getattr(args, "max_baseline_model_cases", 0) or 0)
    if bound < required:
        raise ValueError(f"news_learning_run_baseline_budget_below_corpus:{bound}<{required}")


def _baseline(
    args: Namespace,
    settings: Any,
    stable: Any,
    *,
    out: Path,
    development: str,
    judge_model: str,
) -> dict[str, Any]:
    """The standalone `compile_live` baseline over the frozen corpus, judged by the compiler reflection route.

    `--action-source` is left empty so the baseline derives the only value its mode admits, and the window
    arguments are absent rather than zero: a dataset-bound run refuses to also carry a moving window, and
    passing `0` would read as one.
    """

    path = out / _BASELINE_FILE
    code, payload = _handle_learning_baseline(
        Namespace(
            dataset=development,
            mode="compile_live",
            action_source="",
            from_ms=None,
            to_ms=None,
            all_cohorts=False,
            max_model_cases=int(args.max_baseline_model_cases),
            semantic_judge=judge_model,
            limit=int(args.max_baseline_model_cases),
            out=str(path),
        ),
        settings,
        stable,
    )
    if code != 0:
        raise ValueError(_error_code(payload, fallback="news_learning_run_baseline_failed"))
    return _read(path)


def _optimize(args: Namespace, settings: Any, stable: Any, *, out: Path, development: str) -> dict[str, Any]:
    """The one optimization, with the operator's declared budget, into `<out>/optimization/`.

    A non-`ADVANCE` exit is not an error here: `NO_OP` and `REJECTED` are complete terminal answers and the
    summary is written for all three. Only a raised refusal — an unconfigured route, a stale Program —
    stops the run.
    """

    from .news_learning_experiment import handle_research

    directory = out / _OPTIMIZATION_DIR
    code, payload = handle_research(
        Namespace(
            learning_command="optimize",
            development=development,
            out=str(directory),
            max_metric_calls=int(args.max_metric_calls),
            max_task_model_calls=int(args.max_task_model_calls),
            max_reflection_model_calls=int(args.max_reflection_model_calls),
            max_metric_judge_model_calls=int(args.max_metric_judge_model_calls),
            max_cost_microusd=int(args.max_cost_microusd),
            max_call_cost_microusd=int(args.max_call_cost_microusd),
            max_wall_clock_seconds=int(args.max_wall_clock_seconds),
            seed=int(args.seed),
        ),
        settings,
        stable,
    )
    if code not in {0, 1}:
        raise ValueError(_error_code(payload, fallback="news_learning_run_optimize_failed"))
    return _read(directory / _OPTIMIZATION_FILE)


def _reflection_judge_model(settings: Any) -> str:
    """The one equivalence judge both legs are scored by, taken from configuration rather than a flag.

    `optimize` builds its metric judge on `llm.news_compiler_reflection` and cannot be told otherwise;
    `baseline --dataset` takes the model by name and refuses anything that does not resolve to that same
    route. Deriving the name here removes the last way the two can be given different rulers — a typo — and
    is what makes the metric receipts of the two reports comparable byte for byte.
    """

    reflection = getattr(settings.llm, "news_compiler_reflection", None)
    if reflection is None or not bool(getattr(reflection, "configured", False)):
        raise ValueError("news_learning_optimize_reflection_not_configured")
    return str(reflection.model)


def _task_route(settings: Any) -> dict[str, str]:
    """The task endpoint this run composes, fingerprinted the two ways the two reports fingerprint it.

    The baseline records `configured_endpoint_model_v3` and the optimizer records
    `model_execution_identity.v1` over the same endpoint, so the digests differ by construction. Resolving
    the endpoint once here and computing both is what lets the summary check each report against the host
    this run actually used instead of comparing two incomparable hashes.
    """

    from tracefold.app.learning_runtime import _endpoint_model_sha256, compose_news_program_runtime
    from tracefold.news.learning.contracts import endpoint_fingerprint

    composition = compose_news_program_runtime(settings)
    if not composition.program_configured:
        raise ValueError("news_program_baseline_compile_route_not_configured")
    endpoint = composition.event_semantics_primary
    return {
        "model": str(endpoint.model_name),
        "baseline_endpoint_sha256": _endpoint_model_sha256(endpoint),
        "optimizer_endpoint_fingerprint": endpoint_fingerprint(str(endpoint.api_base)),
    }


def _error_code(payload: Mapping[str, Any], *, fallback: str) -> str:
    """The refusal's own vocabulary, whether the handler returned a code or a code plus its detail.

    `_handle_learning_baseline` answers an empty optimizer corpus with a mapping — a code and the blocking
    reasons behind it. Stringifying that put a Python dict repr inside the CLI's `error` field, where every
    other refusal in this plane puts a stable code an operator can grep for.
    """

    error = payload.get("error")
    if isinstance(error, Mapping):
        return str(error.get("code") or fallback)
    return str(error or fallback)


def _read(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"news_learning_run_artifact_not_a_mapping:{path}")
    return document


__all__ = ["_handle_learning_run"]
