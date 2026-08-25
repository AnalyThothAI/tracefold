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
    payload = dict(episode)
    # `baseline_episodes` carries two loader-only keys that `DevelopmentEpisode` forbids; the frozen case
    # keeps the episode in exactly the shape the metric and the optimizer accept.
    payload.pop("recorded_decision_result", None)
    event_id = str(payload.pop("event_id", "") or "")
    review = dict(payload.get("accepted_review") or {})
    return ExperimentCase(
        case_sha256=str(payload["case_id"]),
        cluster_id=str(payload["cluster_id"]),
        stratum=str(payload.get("stratum") or "unknown"),
        event_id=event_id,
        episode=payload,
        # An accepted review is the only thing that can score a case. A case without one is still frozen,
        # because it is what `draft-reviews` exists to propose against — it just cannot produce accuracy.
        accepted=bool(review),
    )


__all__ = ["snapshot_window"]
