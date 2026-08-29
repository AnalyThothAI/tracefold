"""`news learning optimize`: the one optimization entry.

This is the one News CLI module that loads the optimizer in process. The release seam — `register`,
`evaluate`, `canary` — still refuses to import DSPy, GEPA or the optimizer, because that process holds
promotion authority. This one does not: it reads the corpus once as `serve`, writes only into a directory
the operator names, and produces at most a proposal.

Until #202 `optimize` lived under an `experiment` group and produced its own candidate type; #343 then
deleted the rest of that research loop (`snapshot`/`compare` and the run-directory plane), so the module
name is historical and the surface is exactly one action.

No Docker, no compiler image, no sandbox, no proxy sidecar, no tariff, no promotion.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tracefold.app.learning_runtime import compose_news_program_runtime
from tracefold.app.llm import configured_lm_endpoint


def handle_research(args: Any, settings: Any, stable: Any) -> tuple[int, dict[str, Any]]:
    if str(args.learning_command) == "optimize":
        return _optimize(args, settings, stable)
    raise ValueError("news_learning_research_action_unsupported")


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
        DevelopmentDatasetRef,
        OptimizationBudget,
        epoch_id_for_bundle,
    )
    from tracefold.news.learning.dataset import DevelopmentDatasetStore
    from tracefold.news.learning.objective import DevelopmentEpisode
    from tracefold.news.learning.optimizer import (
        FrozenDevelopmentDataset,
        OptimizationConfig,
        build_reflection_lm,
        build_task_adapter,
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
            learning_epoch=epoch_id_for_bundle(stable.bundle_sha),
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
        request_config=configured_reflection.request,
    )
    task_adapter = build_task_adapter(
        model_name=task.model_name,
        api_key=task.api_key,
        api_base=task.api_base,
        timeout=float(PROGRAM_ROUTE_DEADLINE_SECONDS),
        max_tokens=max(PROGRAM_EVENT_SEMANTICS_MAX_TOKENS, PROGRAM_READER_CARD_MAX_TOKENS),
        model_kwargs=task.model_kwargs,
        temperature=0 if task.temperature is None else task.temperature,
        structured_output=task.structured_output,
    )
    reflection_lm = build_reflection_lm(
        model_name=reflection.model_name,
        api_key=reflection.api_key,
        api_base=reflection.api_base,
        model_kwargs=reflection.model_kwargs,
    )
    judge = build_judge(
        model_name=reflection.model_name,
        api_key=reflection.api_key,
        api_base=reflection.api_base,
        model_kwargs=reflection.model_kwargs,
        temperature=0 if reflection.temperature is None else reflection.temperature,
        structured_output=reflection.structured_output,
        # Bound to the declared budget, because `optimize` refuses a judge whose own ceiling is larger:
        # the metric calls the judge directly, so this admission check is the only pre-call bound there is.
        max_model_calls=int(args.max_metric_judge_model_calls),
    )
    result = optimize(
        dataset,
        OptimizationConfig(
            task_adapter=task_adapter,
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


__all__ = ["handle_research", "write_run_outputs"]
