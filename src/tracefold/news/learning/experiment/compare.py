"""Score one frozen snapshot under each arm and say where they differ, per case and per cluster.

Three arms answer three different questions and are never averaged together: `recorded` is what
production actually shipped, `student` is the local route the optimizer will run against, `teacher` is
the larger model used as a reference. A case with no accepted review is scored by nobody — it appears in
the report as unlabelled, because inventing accuracy for it is the one thing this loop must not do.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from typing import Any

from ..baseline import BaselineCase, BaselineMode, BaselineReport, run_baseline
from ..metric import DevelopmentEpisode
from .run import ExperimentCase, ExperimentRun

COMPARE_REPORT_SCHEMA = "tracefold.news.experiment_compare_report.v1"


def pending_cases(run: ExperimentRun, *, resume: bool) -> tuple[ExperimentCase, ...]:
    """What still needs answering. `--resume` is a directory listing, not a stored cursor.

    It resumes *between* invocations, not inside one: an arm scores its whole batch in a single
    `run_baseline` call, so a run that dies halfway leaves nothing behind and starts that batch over. What
    `--max-model-cases 20 --resume` buys is the loop an operator actually runs on a single-slot local
    route — twenty cases now, the next twenty later, and neither pass re-spending the other's calls.
    """

    done = set(run.compared()) if resume else set()
    return tuple(case for case in run.cases() if case.case_sha256 not in done)


def baseline_cases(cases: Sequence[ExperimentCase]) -> list[BaselineCase]:
    """Frozen cases as the baseline harness takes them, so both planes score the same objects."""

    return [BaselineCase(episode=DevelopmentEpisode.model_validate(case.episode)) for case in cases if case.accepted]


def compare_report(
    *,
    run_sha256: str,
    arms: Mapping[str, BaselineReport],
    unlabelled_case_ids: Sequence[str],
) -> dict[str, Any]:
    """Per-case deltas and the failure clusters worth an operator's attention.

    Clusters are ranked by size times mean regression, because a large cluster that is slightly worse and
    a single case that is much worse are not the same problem, and a flat list of per-case deltas hides
    which one you are looking at.
    """

    per_case: dict[str, dict[str, float | None]] = {}
    for arm, report in arms.items():
        for case in report.cases:
            per_case.setdefault(case.case_id, {})[arm] = case.score
    clusters: dict[str, list[float]] = {}
    cluster_of = {case.case_id: case.cluster_id for report in arms.values() for case in report.cases}
    baseline_arm = "recorded" if "recorded" in arms else next(iter(arms))
    for case_id, scores in per_case.items():
        reference = scores.get(baseline_arm)
        student = scores.get("student")
        if reference is None or student is None:
            continue
        clusters.setdefault(cluster_of.get(case_id, "unknown"), []).append(student - reference)
    ranked: list[dict[str, Any]] = [
        {
            "cluster_id": cluster_id,
            "case_count": len(deltas),
            "mean_delta": round(statistics.fmean(deltas), 6),
            # Size times mean regression, so a large cluster that is slightly worse outranks one case
            # that is much worse. Improvements score zero here: this list is a work queue, not a summary.
            "attention": round(len(deltas) * min(0.0, statistics.fmean(deltas)), 6),
        }
        for cluster_id, deltas in clusters.items()
    ]
    ranked.sort(key=lambda row: (float(row["attention"]), -int(row["case_count"])))
    return {
        "schema": COMPARE_REPORT_SCHEMA,
        "run_sha256": run_sha256,
        "arms": {arm: report.identity for arm, report in arms.items()},
        # Both means, never one: the answered-only mean says how good an answer is, the failure-as-zero
        # mean is the end-to-end lower bound, and reporting only the first is what let 29 unanswered cases
        # turn 0.482 into 0.587.
        "scores": {arm: report.scores for arm, report in arms.items()},
        "population": {arm: report.population for arm, report in arms.items()},
        "report_sha256": {arm: report.report_sha256 for arm, report in arms.items()},
        "cases": per_case,
        "failure_clusters": ranked,
        # Named, not silently dropped: a case nobody reviewed cannot contribute accuracy, and a report
        # that omitted them would read as if the whole window had been measured.
        "unlabelled_case_ids": list(unlabelled_case_ids),
    }


def answered_case_scores(report: Mapping[str, Any]) -> dict[str, dict[str, float | None]]:
    """The cases an arm actually answered, and only those.

    This is what `--resume` is allowed to remember. Recording an empty result for a case nobody could
    score would make the next resumed pass skip it forever — including after `draft-reviews` produced a
    rubric for it and a human accepted one, which is the whole point of the loop.
    """

    return {case_id: scores for case_id, scores in dict(report.get("cases") or {}).items() if scores}


def score_arm(
    cases: Sequence[ExperimentCase],
    *,
    mode: BaselineMode,
    **kwargs: Any,
) -> BaselineReport:
    """One arm, through the baseline harness the release plane already uses."""

    return run_baseline(baseline_cases(cases), mode=mode, **kwargs)


__all__ = [
    "COMPARE_REPORT_SCHEMA",
    "answered_case_scores",
    "baseline_cases",
    "compare_report",
    "pending_cases",
    "score_arm",
]
