"""Freeze one closed window into a run directory. Reads once, writes nothing back.

The cases are `CandidateEvaluator`'s own episode projection, not a second one. That is the whole point:
the fast loop has to measure the corpus a trusted compile would seal, or the number it reports predicts
nothing about the number a release gate will see.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..contracts import ArmManifest, ClosedWindow
from ..evaluator import CandidateEvaluator
from .run import ExperimentCase, ExperimentRun, ExperimentRunManifest, ExperimentWindow, case_root_sha256


def snapshot_window(
    evaluator: CandidateEvaluator,
    *,
    run: ExperimentRun,
    name: str,
    window: ClosedWindow,
    stable: ArmManifest,
    now_ms: int,
    limit: int = 500,
) -> ExperimentRunManifest:
    """Project the window's accepted reviews into frozen cases and record what was frozen.

    `cohort=False` deliberately: the fast loop's job is to look at everything reviewed in the window,
    including cases produced by an arm that has since been retired. Release eligibility is the release
    plane's question and is applied there, on a frozen dataset, not here.
    """

    episodes = evaluator.baseline_episodes(window, cohort=False, limit=limit)
    cases = [_case(episode) for episode in episodes]
    # Read the whole window *before* touching the directory, then refuse a directory that already holds a
    # run. Snapshotting twice into one directory used to merge two windows: the manifest said 2 cases and
    # `cases()` returned 5, and every downstream `--resume` skipped cases this run never scored.
    if run.manifest_path.exists() or any(run.cases_dir.glob("*.json")):
        raise ValueError("news_experiment_run_directory_not_empty")
    for case in cases:
        run.write_case(case)
    manifest = ExperimentRunManifest.issue(
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


__all__ = ["snapshot_window"]
