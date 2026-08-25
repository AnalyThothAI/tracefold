"""Freeze one closed window into a run directory. Reads once, writes nothing back.

The cases are `CandidateEvaluator`'s own episode projection, not a second one. That is the whole point:
the fast loop has to measure the corpus a trusted compile would seal, or the number it reports predicts
nothing about the number a release gate will see.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..contracts import COMPILE_EPISODE_PROJECTION_SCHEMA, ArmManifest, ClosedWindow
from ..evaluator import CandidateEvaluator
from .run import ExperimentCase, ExperimentRun, ExperimentRunManifest, ExperimentWindow, case_root_sha256


def project_window(
    evaluator: CandidateEvaluator,
    *,
    window: ClosedWindow,
    limit: int = 500,
) -> tuple[ExperimentCase, ...]:
    """Read the window. Everything that needs the database happens here and nowhere else.

    Split from the writing half on purpose: freezing a window means up to 500 fsync'd files, and holding a
    `serve` connection across that is what `docs/DEVELOPMENT.md` forbids. A comment used to claim this
    separation existed while one function did both inside a single `with` block — the claim is the split
    now, not the comment.

    `cohort=False` deliberately: the fast loop's job is to look at everything reviewed in the window,
    including cases produced by an arm that has since been retired. Release eligibility is the release
    plane's question and is applied there, on a frozen dataset, not here.
    """

    return tuple(_case(episode) for episode in evaluator.baseline_episodes(window, cohort=False, limit=limit))


def freeze_window(
    cases: Sequence[ExperimentCase],
    *,
    run: ExperimentRun,
    name: str,
    window: ClosedWindow,
    stable: ArmManifest,
    now_ms: int,
) -> ExperimentRunManifest:
    """Write what `project_window` read. No database, and no second projection."""

    # Refuse a directory that already holds a run. Snapshotting twice into one used to merge two windows:
    # the manifest said 2 cases and `cases()` returned 5, and every later `--resume` skipped cases this
    # run never scored.
    if run.manifest_path.exists() or any(run.cases_dir.glob("*.json")):
        raise ValueError("news_experiment_run_directory_not_empty")
    for case in cases:
        run.write_case(case)
    manifest = ExperimentRunManifest.issue(
        # The projection these cases were frozen under, so a run outlives the shape it was taken in only
        # as an explicit refusal rather than as a corpus that quietly answers the wrong question.
        projection_schema_id=COMPILE_EPISODE_PROJECTION_SCHEMA,
        name=name,
        window=ExperimentWindow(from_ms=window.from_ms, to_ms=window.to_ms),
        parent_program_sha256=stable.program_sha256,
        program_version=stable.program_version,
        policy_sha256=stable.policy_sha256,
        case_count=len(cases),
        accepted_case_count=sum(1 for case in cases if case.accepted),
        case_root_sha256=case_root_sha256(cases),
        created_at_ms=now_ms,
    )
    run.write_manifest(manifest)
    return manifest


def _case(episode: Mapping[str, Any]) -> ExperimentCase:
    """One reviewed case, frozen in exactly the shape `build_baseline_cases` takes.

    The two loader-only keys stay in the payload. They used to be stripped here, which looked tidy and cost
    the `recorded` arm its `recorded_decision_result` — `run_baseline` refuses a recorded case without one,
    and nothing could put it back without re-snapshotting. The release plane hands the raw dicts straight to
    `build_baseline_cases` and lets *it* do the popping; so does this.
    """

    payload = dict(episode)
    return ExperimentCase(
        case_sha256=str(payload["case_id"]),
        cluster_id=str(payload["cluster_id"]),
        stratum=str(payload.get("stratum") or "unknown"),
        event_id=str(payload.get("event_id") or ""),
        episode=payload,
        # `baseline_episodes` reaches these through an acceptance row, so every one of them is reviewed.
        # This flag is not a guess about the payload — `_project_episodes` always writes a non-empty
        # `accepted_review`, so reading `bool(review)` here was a constant wearing the costume of a check.
        accepted=True,
    )


__all__ = ["freeze_window", "project_window"]
