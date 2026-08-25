"""`news learning snapshot | compare | optimize`: the research window and the one optimization entry.

This is the one News CLI module that loads the optimizer in process. The release seam — `register`,
`evaluate`, `canary` — still refuses to import DSPy, GEPA or the optimizer, because that process holds
promotion authority. This one does not: it reads the corpus once as `serve`, writes only into a directory
the operator names, and produces at most a proposal.

Until #202 `optimize` lived under an `experiment` group and produced its own candidate type, because a
release candidate could only come out of a sealed container. There is one optimization now and one
candidate contract; what is left of the research loop is the window itself — `snapshot` and `compare`
score a closed window without freezing a dataset, which is a cheaper question, not a second lifecycle.

No Docker, no compiler image, no sandbox, no proxy sidecar, no tariff, no promotion.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tracefold.app.learning_runtime import compose_news_program_runtime
from tracefold.app.llm import configured_lm_endpoint


def handle_research(args: Any, settings: Any, stable: Any) -> tuple[int, dict[str, Any]]:
    action = str(args.learning_command)
    if action == "snapshot":
        return _snapshot(args, settings, stable)
    if action == "compare":
        return _compare(args, settings, stable)
    if action == "optimize":
        return _optimize(args, settings, stable)
    raise ValueError("news_learning_research_action_unsupported")


def _snapshot(args: Any, settings: Any, stable: Any) -> tuple[int, dict[str, Any]]:
    from tracefold.app.repository_session import postgres_connection
    from tracefold.news.learning.contracts import ClosedWindow
    from tracefold.news.learning.dataset import SETTLEMENT_GRACE_MS, DevelopmentDatasetStore
    from tracefold.news.learning.experiment.run import ExperimentRun
    from tracefold.news.learning.experiment.snapshot import freeze_window, project_window

    now_ms = int(time.time() * 1000)
    # Ends at the settlement grace, not at "now": a window whose tail is still settling is not closed, and
    # a snapshot of it would measure a corpus that changes underneath the comparison.
    to_ms = now_ms - SETTLEMENT_GRACE_MS
    window = ClosedWindow(from_ms=to_ms - int(args.hours) * 3_600_000, to_ms=to_ms)
    # Read first, with the connection open. Then close it, and only then create the directory and write:
    # freezing a window is up to 500 fsync'd files, and `docs/DEVELOPMENT.md` forbids holding a connection
    # across one. The directory is created after the read, so a failed read leaves nothing behind.
    with postgres_connection(settings, role="serve") as conn:
        cases = project_window(DevelopmentDatasetStore(conn, stable=stable), window=window, limit=int(args.limit))
    run = ExperimentRun(Path(str(args.out)), create=True)
    manifest = freeze_window(cases, run=run, name=run.root.name, window=window, stable=stable, now_ms=now_ms)
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
    # Merge first, write second. `merged_report` can refuse — a run identity that does not match, arms run
    # against different models — and marking the batch compared before that refusal both errored and made
    # the next `--resume` skip results nothing had recorded.
    merged = merged_report(previous=run.report("report"), current=report)
    for case_sha256, scores in answered_case_scores(report, failed_case_ids=failed).items():
        run.write_compared(case_sha256, {"case_sha256": case_sha256, "scores": scores})
    path = run.write_report("report", merged)
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
    """The one optimization: a frozen development dataset in, a terminal report out.

    The endpoints are the configured ones, not command-line models. That is deliberate: the task LM has to
    be the route production Triage answers on, or the number this maximizes stops predicting anything about
    production, and the reflection and judge roles are the operator's configured compiler endpoint. What
    the command line still owns is the budget, because spending is the operator's decision.
    """

    from tracefold.app.repository_session import postgres_connection
    from tracefold.news.learning.baseline import build_judge
    from tracefold.news.learning.contracts import (
        REFLECTION_MAX_TOKENS,
        REFLECTION_TIMEOUT_SECONDS,
        DevelopmentDatasetRef,
        OptimizationBudget,
    )
    from tracefold.news.learning.dataset import DevelopmentDatasetStore
    from tracefold.news.learning.objective import DevelopmentEpisode
    from tracefold.news.learning.optimizer import (
        FrozenDevelopmentDataset,
        OptimizationConfig,
        build_optimizer_lm,
        optimize,
    )
    from tracefold.news.program.artifact import load_stable_program_artifact
    from tracefold.news.program.runtime import (
        PROGRAM_EVENT_SEMANTICS_MAX_TOKENS,
        PROGRAM_READER_CARD_MAX_TOKENS,
        PROGRAM_ROUTE_DEADLINE_SECONDS,
    )
    from tracefold.news.review.desk import REVIEW_RUBRIC_VERSION
    from tracefold.platform.config.models import news_model_availability

    availability = news_model_availability(settings)
    if not availability.program_configured or not availability.triage_model:
        raise ValueError("news_learning_optimize_model_not_configured")
    parent = load_stable_program_artifact()
    if parent.program_sha256 != stable.program_sha256:
        raise ValueError("news_learning_optimize_stable_program_mismatch")
    # The one database read, before anything is spent, as `serve`. Nothing after this line holds a
    # connection: the optimization has no write credential and no promotion authority (#202 §3.2).
    with postgres_connection(settings, role="serve") as conn:
        export = DevelopmentDatasetStore(conn, stable=stable).development_compile_export(str(args.development))
    dataset = FrozenDevelopmentDataset.bind(
        dataset_payload=export.dataset_payload,
        ref=DevelopmentDatasetRef(
            development_dataset_sha256=export.dataset_sha,
            episode_projection_root_sha256=export.episode_projection_root_sha256,
            episode_count=len(export.episodes),
            learning_epoch_started_at_ms=export.learning_epoch_started_at_ms,
            # Declared on the trusted side. The optimizer records the rubric its corpus was accepted
            # under; it never looks one up, so the review plane stays out of its import graph.
            review_rubric_version=REVIEW_RUBRIC_VERSION,
        ),
        episodes=tuple(DevelopmentEpisode.model_validate(episode) for episode in export.episodes),
        target_runtime_manifest_sha256=stable.runtime_model_bindings_sha256,
        parent_program=parent,
    )
    composition = compose_news_program_runtime(settings)
    task = composition.event_semantics_primary
    configured_reflection = getattr(settings.llm, "news_compiler_reflection", None)
    if configured_reflection is None or not bool(getattr(configured_reflection, "configured", False)):
        raise ValueError("news_learning_optimize_reflection_not_configured")
    reflection = configured_lm_endpoint(
        settings,
        model_name=str(configured_reflection.model),
        api_key=str(configured_reflection.api_key),
        base_url=str(configured_reflection.base_url),
    )
    task_lm = build_optimizer_lm(
        role="task",
        model_name=task.model_name,
        api_key=task.api_key,
        api_base=task.api_base,
        timeout=float(PROGRAM_ROUTE_DEADLINE_SECONDS),
        max_tokens=max(PROGRAM_EVENT_SEMANTICS_MAX_TOKENS, PROGRAM_READER_CARD_MAX_TOKENS),
        model_kwargs=task.model_kwargs,
    )
    reflection_lm = build_optimizer_lm(
        role="reflection",
        model_name=reflection.model_name,
        api_key=reflection.api_key,
        api_base=reflection.api_base,
        timeout=REFLECTION_TIMEOUT_SECONDS,
        max_tokens=REFLECTION_MAX_TOKENS,
        model_kwargs=reflection.model_kwargs,
    )
    judge = build_judge(
        model_name=reflection.model_name,
        api_key=reflection.api_key,
        api_base=reflection.api_base,
        model_kwargs=reflection.model_kwargs,
        # Bound to the declared budget, because `optimize` refuses a judge whose own ceiling is larger:
        # the metric calls the judge directly, so this admission check is the only pre-call bound there is.
        max_model_calls=int(args.max_metric_judge_model_calls),
    )
    result = optimize(
        dataset,
        OptimizationConfig(
            task_lm=task_lm,
            reflection_lm=reflection_lm,
            judge=judge,
            budget=OptimizationBudget(
                max_metric_calls=int(args.max_metric_calls),
                max_task_model_calls=int(args.max_task_model_calls),
                max_reflection_model_calls=int(args.max_reflection_model_calls),
                max_metric_judge_model_calls=int(args.max_metric_judge_model_calls),
                max_cost_microusd=int(args.max_cost_microusd),
                max_call_cost_microusd=int(args.max_call_cost_microusd),
                max_wall_clock_seconds=float(args.max_wall_clock_seconds),
                seed=int(args.seed),
            ),
        ),
    )
    report_path, candidate_path = write_run_outputs(Path(str(args.out)), result)
    # Only `ADVANCE` exits 0. `NO_OP` and `REJECTED` are complete, retained answers rather than crashes —
    # but an operator scripting this is asking "did I get a candidate", and the exit code answers that.
    return (0 if result.outcome == "ADVANCE" else 1), {
        "ok": result.outcome == "ADVANCE",
        "data": {
            "outcome": result.outcome,
            "report": str(report_path),
            "report_sha256": result.report.report_sha256,
            "prompt_candidate": candidate_path,
            "prompt_candidate_sha256": result.report.candidate_sha256,
            "reasons": list(result.report.reasons),
            "development_dataset_sha256": dataset.ref.development_dataset_sha256,
            "usage": result.report.usage,
        },
    }


def write_run_outputs(out: Path, result: Any) -> tuple[Path, str | None]:
    """Write one optimization's terminal artifacts into the operator's directory.

    The directory is the record of *one* run. An operator reusing `--out` would otherwise end up with a
    current rejection report sitting beside a registrable candidate from an earlier run — the easiest
    possible way to register the wrong two instructions, and one nothing downstream would catch, because
    that stale candidate is perfectly valid on its own terms.
    """

    out.mkdir(parents=True, exist_ok=True)
    report_path = out / "optimization_report.json"
    _write_exact_json(report_path, result.report.model_dump(mode="json"))
    candidate_file = out / "prompt_candidate.json"
    if result.candidate is None:
        candidate_file.unlink(missing_ok=True)
        return report_path, None
    _write_exact_json(candidate_file, result.candidate.model_dump(mode="json"))
    return report_path, str(candidate_file)


def _write_exact_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write the exact `canonical_json` bytes the document's own hash was computed over.

    Atomic, no-follow and 0600, so a retained terminal artifact can be re-verified byte for byte and is
    not world-readable in a directory the operator may not have locked down.
    """

    from tracefold.news.artifact_identity import canonical_json

    document = canonical_json(dict(payload))
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(document)
        handle.flush()
        os.fsync(handle.fileno())


def _semantic_judge(settings: Any, *, model: str) -> Any:
    """The equivalence judge for one comparison, or `None` when the operator named no model.

    Optional here and required by `optimize`: a comparison is allowed to be a cheap byte-equality read, but
    it has to say so by not naming a judge rather than by naming one and ignoring it.
    """

    if not model.strip():
        return None
    from tracefold.news.learning.baseline import build_judge

    endpoint = _arm_endpoint(settings, arm="teacher", model=model)
    return build_judge(
        model_name=endpoint.model_name,
        api_key=endpoint.api_key,
        api_base=endpoint.api_base,
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


__all__ = ["handle_research", "write_run_outputs"]
