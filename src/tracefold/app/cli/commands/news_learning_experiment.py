"""The operator's fast loop: `news learning experiment snapshot | compare | optimize`.

This is the one News CLI module that loads the optimizer in process, and it is a separate module for
exactly that reason. The trusted seam — `compile`, `propose`, `evaluate`, `canary` — still refuses to
import DSPy, GEPA or the optimizer, because that process holds database credentials and promotion
authority. This one holds neither: it reads once as `serve`, writes only into a run directory the
operator owns, and produces a candidate that no gate can accept.

No Docker, no compiler image, no sandbox, no proxy sidecar, no tariff, no promotion.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any


def handle_experiment(args: Any, settings: Any, stable: Any) -> tuple[int, dict[str, Any]]:
    action = str(args.experiment_action)
    if action == "snapshot":
        return _snapshot(args, settings, stable)
    if action == "compare":
        return _compare(args, settings, stable)
    if action == "optimize":
        return _optimize(args, settings, stable)
    raise ValueError("news_experiment_action_unsupported")


def _snapshot(args: Any, settings: Any, stable: Any) -> tuple[int, dict[str, Any]]:
    from tracefold.app.repository_session import postgres_connection
    from tracefold.news.learning.contracts import ClosedWindow
    from tracefold.news.learning.evaluator import SETTLEMENT_GRACE_MS, CandidateEvaluator
    from tracefold.news.learning.experiment.run import ExperimentRun
    from tracefold.news.learning.experiment.snapshot import snapshot_window

    now_ms = int(time.time() * 1000)
    # Ends at the settlement grace, not at "now": a window whose tail is still settling is not closed, and
    # a snapshot of it would measure a corpus that changes underneath the comparison.
    to_ms = now_ms - SETTLEMENT_GRACE_MS
    window = ClosedWindow(from_ms=to_ms - int(args.hours) * 3_600_000, to_ms=to_ms)
    run = ExperimentRun(Path(str(args.out)), create=True)
    # `snapshot_window` reads the whole window before it writes anything, so the connection is closed by
    # the time up to 500 fsync'd case files are on disk. `docs/DEVELOPMENT.md` forbids holding one across
    # a file write, and 500 of them is the case that rule was written for.
    with postgres_connection(settings, role="serve") as conn:
        evaluator = CandidateEvaluator(conn, stable=stable, judges={})
        manifest = snapshot_window(
            evaluator,
            run=run,
            name=run.root.name,
            window=window,
            stable=stable,
            now_ms=now_ms,
            limit=int(args.limit),
        )
    return 0, {
        "ok": True,
        "data": {
            "run": str(run.root),
            "run_sha256": manifest.run_sha256,
            "cases": manifest.case_count,
            "accepted_cases": manifest.accepted_case_count,
            "window": manifest.window.model_dump(mode="json"),
            "parent_program_sha256": manifest.parent_program_sha256,
        },
    }


def _compare(args: Any, settings: Any, stable: Any) -> tuple[int, dict[str, Any]]:
    del stable
    from tracefold.news.learning.baseline import BaselineReport, build_runtime_lm
    from tracefold.news.learning.experiment.compare import (
        answered_case_scores,
        compare_report,
        failed_case_ids,
        merged_report,
        pending_cases,
        score_arm,
    )
    from tracefold.news.learning.experiment.run import ExperimentRun
    from tracefold.news.program.artifact import load_program_artifact
    from tracefold.news.program.runtime import (
        PROGRAM_EVENT_SEMANTICS_MAX_TOKENS,
        PROGRAM_READER_CARD_MAX_TOKENS,
        PROGRAM_ROUTE_DEADLINE_SECONDS,
    )

    run = ExperimentRun(Path(str(args.run)))
    manifest = run.manifest()
    artifact = load_program_artifact(manifest.parent_program_sha256)
    # The same judge `optimize` will maximize against. Parsing `--semantic-judge` and then scoring by byte
    # equality is the ruler drift the shared core exists to prevent: the operator reads one number and the
    # optimizer maximizes another.
    judge = _semantic_judge(settings, model=str(getattr(args, "semantic_judge", "") or ""))
    cases = pending_cases(run, resume=bool(args.resume))
    if not cases:
        return 0, {"ok": True, "data": {"run": str(run.root), "note": "every case already answered"}}
    # The student is the single-slot local route that also serves production Triage, so it is bounded and
    # sequential. The bound is required rather than defaulted for the same reason `baseline` requires one.
    bounded = cases[: int(args.max_model_cases)]

    arms: dict[str, BaselineReport] = {
        "recorded": score_arm(bounded, mode="recorded", artifact=artifact, cohort_scope="experiment_run")
    }
    for arm, model in (("student", str(args.student)), ("teacher", str(getattr(args, "teacher", "") or ""))):
        if not model:
            continue
        # Each arm runs the model the operator named, on the credentials of the route that serves that
        # role. Reusing one configured route for both and recording the requested name beside it would
        # have produced two identical arms whose report claimed a student/teacher gap that never ran.
        endpoint = _arm_endpoint(settings, arm=arm, model=model)
        arms[arm] = score_arm(
            bounded,
            mode="compile_live",
            artifact=artifact,
            lm=build_runtime_lm(
                model_name=endpoint.model_name,
                api_key=endpoint.api_key,
                api_base=endpoint.api_base,
                timeout=float(PROGRAM_ROUTE_DEADLINE_SECONDS),
                max_tokens=max(PROGRAM_EVENT_SEMANTICS_MAX_TOKENS, PROGRAM_READER_CARD_MAX_TOKENS),
                model_kwargs=endpoint.model_kwargs,
            ),
            judge=judge,
            runtime_identity=_endpoint_identity(endpoint),
            cohort_scope="experiment_run",
        )
    report = compare_report(run_sha256=manifest.run_sha256, arms=arms)
    failed = failed_case_ids(arms)
    for case_sha256, scores in answered_case_scores(report, failed_case_ids=failed).items():
        run.write_compared(case_sha256, {"case_sha256": case_sha256, "scores": scores})
    # Merged, not overwritten. `--resume` deliberately scores only the pending batch, so a report rebuilt
    # from that batch alone covered the last 20 cases of a 500-case run while carrying the whole run's
    # identity — and `failure_clusters`, which is the operator's work queue, was ranked over 4% of it.
    path = run.write_report("report", merged_report(previous=run.report("report"), current=report))
    return 0, {
        "ok": True,
        "data": {
            "run": str(run.root),
            "report": str(path),
            "scored_cases": len(bounded),
            "unanswered_cases": len(failed),
            "failure_clusters": report["failure_clusters"][:5],
        },
    }


def _optimize(args: Any, settings: Any, stable: Any) -> tuple[int, dict[str, Any]]:
    del stable
    from tracefold.news.learning.baseline import build_judge
    from tracefold.news.learning.compiler.gepa import build_compile_lm, require_model_identity
    from tracefold.news.learning.compiler.security import (
        METRIC_JUDGE_MAX_TOKENS,
        METRIC_JUDGE_TIMEOUT_SECONDS,
        REFLECTION_MAX_TOKENS,
        REFLECTION_TIMEOUT_SECONDS,
    )
    from tracefold.news.learning.experiment.optimize import optimize_snapshot
    from tracefold.news.learning.experiment.run import ExperimentRun
    from tracefold.news.learning.review import REVIEW_RUBRIC_VERSION
    from tracefold.news.program.artifact import load_program_artifact, load_stable_program_artifact
    from tracefold.news.program.runtime import (
        PROGRAM_EVENT_SEMANTICS_MAX_TOKENS,
        PROGRAM_READER_CARD_MAX_TOKENS,
        PROGRAM_ROUTE_DEADLINE_SECONDS,
    )

    run = ExperimentRun(Path(str(args.run)))
    manifest = run.manifest()
    artifact = load_program_artifact(manifest.parent_program_sha256)
    # Before the first provider call, and for the same reason `ProgramCompiler.__init__` checks it: a
    # snapshot taken on Monday against a Program promoted away on Tuesday would spend the whole budget on
    # a superseded parent and emit a candidate the trusted compiler refuses on sight.
    if artifact.program_sha256 != load_stable_program_artifact().program_sha256:
        raise ValueError("news_experiment_parent_program_superseded")
    cases = tuple(run.cases())
    task = _arm_endpoint(settings, arm="student", model=str(args.student))
    reflection = _arm_endpoint(settings, arm="teacher", model=str(args.reflection))
    judge_endpoint = _arm_endpoint(settings, arm="teacher", model=str(args.semantic_judge))
    task_lm = build_compile_lm(
        role="task",
        model_name=task.model_name,
        api_key=task.api_key,
        api_base=task.api_base,
        timeout=float(PROGRAM_ROUTE_DEADLINE_SECONDS),
        max_tokens=max(PROGRAM_EVENT_SEMANTICS_MAX_TOKENS, PROGRAM_READER_CARD_MAX_TOKENS),
        model_kwargs=task.model_kwargs,
    )
    reflection_lm = build_compile_lm(
        role="reflection",
        model_name=reflection.model_name,
        api_key=reflection.api_key,
        api_base=reflection.api_base,
        timeout=REFLECTION_TIMEOUT_SECONDS,
        max_tokens=REFLECTION_MAX_TOKENS,
        model_kwargs=reflection.model_kwargs,
    )
    # Fail before the first provider call, the way the trusted compiler does.
    require_model_identity(task_lm, role="task")
    require_model_identity(reflection_lm, role="reflection")
    # Bounded like the trusted compile's judge. Without `max_model_calls` a run has no ceiling at all:
    # `--max-metric-calls` bounds metric invocations, and each of those drives two task calls plus N judge
    # calls, so the only number an operator could set bounded nothing they were actually spending.
    judge = build_judge(
        model_name=judge_endpoint.model_name,
        api_key=judge_endpoint.api_key,
        api_base=judge_endpoint.api_base,
        timeout=METRIC_JUDGE_TIMEOUT_SECONDS,
        max_tokens=METRIC_JUDGE_MAX_TOKENS,
        model_kwargs=judge_endpoint.model_kwargs,
        max_model_calls=int(args.max_judge_model_calls),
    )
    candidate = optimize_snapshot(
        run_sha256=manifest.run_sha256,
        base_program=artifact,
        cases=cases,
        task_lm=task_lm,
        reflection_lm=reflection_lm,
        judge=judge,
        max_metric_calls=int(args.max_metric_calls),
        seed=int(args.seed),
        review_rubric_version=REVIEW_RUBRIC_VERSION,
    )
    # Through the run directory's own writer: atomic, no-follow, 0600, and the same `canonical_json` bytes
    # `experiment_candidate_sha256` was computed over, so the file can be re-verified byte for byte. The
    # App CLI's plain `open()` helper did none of those and wrote 0644 into a 0700 directory.
    path = run.write_report("experiment_candidate", candidate.model_dump(mode="json"))
    return 0, {
        "ok": True,
        "data": {
            "run": str(run.root),
            "experiment_candidate": str(path),
            "experiment_candidate_sha256": candidate.experiment_candidate_sha256,
            "parent_program_sha256": candidate.parent_program_sha256,
            "train_cases": candidate.train_count,
            "val_cases": candidate.val_count,
            # Said in the output, not only in the file: nothing here may be proposed. Promoting a winner
            # means re-running the trusted compiler, which is the only thing that makes a release candidate.
            "promotable": False,
        },
    }


def _semantic_judge(settings: Any, *, model: str) -> Any:
    """The equivalence judge for one comparison, or `None` when the operator named no model.

    Optional here and required by `optimize`: a comparison is allowed to be a cheap byte-equality read, but
    it has to say so by not naming a judge rather than by naming one and ignoring it.
    """

    if not model.strip():
        return None
    from tracefold.news.learning.baseline import build_judge
    from tracefold.news.learning.compiler.security import METRIC_JUDGE_MAX_TOKENS, METRIC_JUDGE_TIMEOUT_SECONDS

    endpoint = _arm_endpoint(settings, arm="teacher", model=model)
    return build_judge(
        model_name=endpoint.model_name,
        api_key=endpoint.api_key,
        api_base=endpoint.api_base,
        timeout=METRIC_JUDGE_TIMEOUT_SECONDS,
        max_tokens=METRIC_JUDGE_MAX_TOKENS,
        model_kwargs=endpoint.model_kwargs,
    )


def _arm_endpoint(settings: Any, *, arm: str, model: str) -> Any:
    """One operator-named model, on the credentials of the route that serves that role.

    `student` runs where production Triage runs — the local single slot — because the whole question is
    whether the model that will actually ship can do the job. `teacher` and the metric side run on the
    hosted gateway the compiler already uses. The model name alone never selects an endpoint here: the
    same name on two hosts is two different baselines.
    """

    from tracefold.app.learning_runtime import compose_news_program_runtime
    from tracefold.app.llm import configured_lm_endpoint

    if arm == "student":
        composition = compose_news_program_runtime(settings)
        if not composition.program_configured:
            raise ValueError("news_experiment_program_route_not_configured")
        primary = composition.event_semantics_primary
        return configured_lm_endpoint(
            settings,
            model_name=str(model or primary.model_name),
            api_key=str(primary.api_key),
            base_url=str(primary.api_base),
        )
    reflection = getattr(settings.llm, "news_compiler_reflection", None)
    if reflection is None or not bool(getattr(reflection, "configured", False)):
        raise ValueError("news_experiment_reflection_not_configured")
    return configured_lm_endpoint(
        settings,
        model_name=str(model or reflection.model),
        api_key=str(reflection.api_key),
        base_url=str(reflection.base_url),
    )


def _endpoint_identity(endpoint: Any) -> dict[str, Any]:
    """What an arm's report records: the name, and a fingerprint that separates two hosts serving it."""

    from tracefold.app.learning_runtime import _endpoint_model_sha256

    return {
        "compile_task_model": endpoint.model_name,
        "compile_task_endpoint_sha256": _endpoint_model_sha256(endpoint),
    }


__all__ = ["handle_experiment"]
