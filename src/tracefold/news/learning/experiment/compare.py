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

from ..baseline import (
    BaselineCase,
    BaselineMode,
    BaselineReport,
    build_baseline_cases,
    run_baseline,
)
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


def baseline_cases(cases: Sequence[ExperimentCase]) -> tuple[BaselineCase, ...]:
    """Frozen cases through the release plane's own projection, so both planes score the same objects.

    `build_baseline_cases` rather than a local `BaselineCase(...)`: it is what strips the loader-only keys
    and threads `recorded_decision_result` into the case, and reimplementing it here is what left the
    `recorded` arm raising `news_program_baseline_recorded_decision_missing` on its first case.
    """

    return build_baseline_cases([case.episode for case in cases if case.accepted], action_source="recorded")


def compare_report(
    *,
    run_sha256: str,
    arms: Mapping[str, BaselineReport],
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
    cluster_of = {case.case_id: case.cluster_id for report in arms.values() for case in report.cases}
    return {
        "cluster_of": cluster_of,
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
        "failure_clusters": _ranked_clusters(per_case, cluster_of=cluster_of),
        # Said once, plainly: this corpus is the window's *reviewed* Events and nothing else, because that
        # is the only population an accepted review can score. The rest of the window is what
        # `draft-reviews --events-from` exists for. A per-case `unlabelled_case_ids` list used to sit here
        # and was necessarily always empty — a field that can only ever say one thing is not evidence.
        "corpus": "accepted_reviews_only",
    }


def answered_case_scores(report: Mapping[str, Any], *, failed_case_ids: Sequence[str]) -> dict[str, Any]:
    """What `--resume` is allowed to remember: the cases every arm actually got an answer for.

    `if scores` was the first attempt and could never be False — `CaseResult.score` is a non-optional float
    and a provider failure scores `0.0`, so a timed-out case was recorded as answered, skipped forever by
    the next resumed pass, and its full delta ranked as a semantic regression. Failure is an outcome the
    report still publishes; it is just not a reason to stop asking.
    """

    failed = set(failed_case_ids)
    return {case_id: scores for case_id, scores in dict(report.get("cases") or {}).items() if case_id not in failed}


def failed_case_ids(arms: Mapping[str, BaselineReport]) -> tuple[str, ...]:
    """Every case that any arm failed to get an answer for, by the arm's own error code."""

    return tuple(
        sorted({case.case_id for report in arms.values() for case in report.cases if case.error_code is not None})
    )


def _ranked_clusters(
    per_case: Mapping[str, Mapping[str, float | None]], *, cluster_of: Mapping[str, str]
) -> list[dict[str, Any]]:
    """The failure clusters worth an operator's attention, ranked over whatever cases it is given."""

    clusters: dict[str, list[float]] = {}
    for case_id, scores in per_case.items():
        reference, student = scores.get("recorded"), scores.get("student")
        if reference is None or student is None:
            continue
        clusters.setdefault(str(cluster_of.get(case_id) or "unknown"), []).append(student - reference)
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
    return ranked


# Corpus-scoped, so they differ between two batches of one run by construction. `identity` mixes "which
# models answered" with "which cases they answered about", and only the first is what two resumed passes
# have to agree on. Comparing the whole dict made every normal second `--resume` raise.
_BATCH_SCOPED_IDENTITY_KEYS = frozenset({"case_root_sha256", "cluster_root_sha256", "corpus_sha256"})


def execution_identity(identity: Mapping[str, Any]) -> dict[str, Any]:
    """Which models answered, with the corpus roots that name *this batch* dropped."""

    return {key: value for key, value in dict(identity).items() if key not in _BATCH_SCOPED_IDENTITY_KEYS}


def _merged_scores(per_case: Mapping[str, Mapping[str, float | None]], *, arm: str) -> dict[str, Any]:
    """Both means over every case merged so far, recomputed rather than carried from one batch.

    `run_baseline` computes these over the batch it scored. Keeping the latest batch's copy is how a
    500-case run reported headline accuracy for its last twenty while publishing all five hundred rows.
    Both means, never one: the answered-only mean says how good an answer is, the failure-as-zero mean is
    the end-to-end lower bound, and publishing only the first is what let 29 unanswered cases turn 0.482
    into 0.587.
    """

    scores = [row.get(arm) for row in per_case.values() if arm in row]
    answered = [float(score) for score in scores if score is not None]
    return {
        "mean": round(statistics.fmean(answered), 6) if answered else None,
        "mean_with_failures_as_zero": round(sum(answered) / len(scores), 6) if scores else None,
    }


def merged_report(*, previous: Mapping[str, Any] | None, current: Mapping[str, Any]) -> dict[str, Any]:
    """One report over everything answered so far, not just this pass.

    `--resume` scores only the cases a previous pass left, which is the point — but it made the report a
    view of the last batch while still stamped with the whole run's `run_sha256`. Per-case scores merge
    (this pass wins for a case it re-answered), the clusters and both means are recomputed over the union,
    and the arm identities are checked on their model fields alone: two passes run against different
    models are not one report, while two batches of one run are.
    """

    if previous is None:
        return dict(current)
    if previous.get("run_sha256") != current.get("run_sha256"):
        raise ValueError("news_experiment_report_run_mismatch")
    previous_arms = {arm: execution_identity(identity) for arm, identity in dict(previous.get("arms") or {}).items()}
    current_arms = {arm: execution_identity(identity) for arm, identity in dict(current.get("arms") or {}).items()}
    if previous_arms != current_arms:
        raise ValueError("news_experiment_report_arm_identity_changed")
    cases = {**dict(previous.get("cases") or {}), **dict(current.get("cases") or {})}
    # The cluster map merges too. Re-ranking the union against only this pass's map dropped every earlier
    # case into `unknown`, which is the same "report describes one batch" defect one level down.
    cluster_of = {**dict(previous.get("cluster_of") or {}), **dict(current.get("cluster_of") or {})}
    merged = dict(current)
    merged["cases"] = cases
    merged["cluster_of"] = cluster_of
    merged["failure_clusters"] = _ranked_clusters(cases, cluster_of=cluster_of)
    merged["scores"] = {arm: _merged_scores(cases, arm=arm) for arm in current_arms}
    merged["population"] = {
        arm: {
            "requested_n": sum(1 for row in cases.values() if arm in row),
            "answered_n": sum(1 for row in cases.values() if row.get(arm) is not None),
        }
        for arm in current_arms
    }
    # A batch report's address names the batch it scored, so a merged view has no single one. Both are
    # kept by name rather than one of them being passed off as the whole run's.
    merged["batch_report_sha256"] = [
        *list(previous.get("batch_report_sha256") or [previous.get("report_sha256")]),
        current.get("report_sha256"),
    ]
    merged.pop("report_sha256", None)
    return merged


def score_arm(
    cases: Sequence[ExperimentCase],
    *,
    mode: BaselineMode,
    **kwargs: Any,
) -> BaselineReport:
    """One arm, through the baseline harness the release plane already uses.

    Both live modes take the same argument now: #306 Phase 3 collapsed the separate optimizer student, so
    `compile_live` is the production graph bound to one endpoint and the caller passes it as
    `semantic_judge` exactly as `runtime_live` does.
    """

    return run_baseline(baseline_cases(cases), mode=mode, **kwargs)


__all__ = [
    "COMPARE_REPORT_SCHEMA",
    "answered_case_scores",
    "baseline_cases",
    "compare_report",
    "pending_cases",
    "score_arm",
]
