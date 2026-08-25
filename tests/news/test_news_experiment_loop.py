"""The operator's fast loop: a frozen run directory, a paired comparison, and an unpromotable proposal.

Four properties are what make this loop safe to run beside a release plane, and each one is asserted here
rather than argued in a docstring: the run is addressed by what it froze, only accepted reviews can score
or train anything, a case nobody judged is named rather than silently dropped, and the winner is not
release evidence. The fifth — that the fast loop optimizes through the same core a trusted compile does —
is what makes the number it reports worth reading at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tracefold.news.learning.baseline import BaselineCase, BaselineReport, CaseResult
from tracefold.news.learning.compiler import gepa as gepa_core
from tracefold.news.learning.compiler import root as compiler_root
from tracefold.news.learning.compiler.gepa import GepaRunResult
from tracefold.news.learning.compiler.security import CompileRecordV1, validate_compile_record
from tracefold.news.learning.experiment import optimize as optimize_module
from tracefold.news.learning.experiment.compare import baseline_cases, compare_report, pending_cases
from tracefold.news.learning.experiment.optimize import (
    ExperimentCandidate,
    accepted_episodes,
    optimize_snapshot,
)
from tracefold.news.learning.experiment.run import (
    ExperimentCase,
    ExperimentRun,
    ExperimentRunManifest,
    ExperimentWindow,
    case_root_sha256,
)
from tracefold.news.program.artifact import ProgramStrategyPatchV1, load_stable_program_artifact

from .test_news_baseline_modes import _case as _baseline_case

_ACCEPTED_REVIEW = {
    "should_push": "should_push",
    "dimensions": {"factual_fidelity": "fail", "headline_fidelity": "pass", "magnitude": "pass"},
    "novelty": {"judgment": "new_fact", "duplicate_of": ""},
}


def _case(index: int, *, accepted: bool = True) -> ExperimentCase:
    """One frozen case built from the same episode shape the baseline harness scores."""

    episode = _baseline_case(index).episode.model_dump(mode="json")
    episode["accepted_review"] = dict(_ACCEPTED_REVIEW) if accepted else {}
    return ExperimentCase(
        case_sha256=f"{index:064x}",
        cluster_id=f"cluster-{index % 3}",
        stratum="delivered",
        event_id=f"{index:064x}",
        episode=episode,
        accepted=accepted,
    )


def _manifest(cases: list[ExperimentCase], **overrides: Any) -> ExperimentRunManifest:
    stable = load_stable_program_artifact()
    values: dict[str, Any] = {
        "name": "news-24h",
        "window": ExperimentWindow(from_ms=1_787_000_000_000, to_ms=1_787_086_400_000),
        "parent_program_sha256": stable.program_sha256,
        "program_version": "news_semantic_program_v5",
        "policy_sha256": "b" * 64,
        "case_count": len(cases),
        "accepted_case_count": sum(1 for case in cases if case.accepted),
        "case_root_sha256": case_root_sha256(cases),
        "created_at_ms": 1_787_086_400_000,
    }
    values.update(overrides)
    return ExperimentRunManifest.issue(**values)


def _report(scores: dict[str, float], *, cluster_of: dict[str, str]) -> BaselineReport:
    """A minimal arm report: what `compare_report` reads, through the real report model."""

    return BaselineReport(
        mode="recorded",
        identity={"mode": "recorded"},
        execution_scope=("recorded",),
        population={"cases": len(scores), "answered": len(scores)},
        scores={"mean": 0.5, "mean_with_failures_as_zero": 0.5},
        action_confusion={},
        hard_gates={},
        failures={},
        review_label_distribution={},
        prediction_dimensions={},
        gold_coverage={},
        retrieval={},
        cases=tuple(
            CaseResult(
                case_id=case_id,
                cluster_id=cluster_of[case_id],
                stratum="delivered",
                score=score,
                action="push",
                should_push="should_push",
                feedback="",
            )
            for case_id, score in scores.items()
        ),
    )


# --- the run directory is addressed by what it froze --------------------------------------------------


def test_a_run_is_addressed_by_the_cases_it_froze(tmp_path: Path) -> None:
    cases = [_case(index) for index in range(4)]
    first = _manifest(cases)
    assert _manifest(list(cases)).run_sha256 == first.run_sha256

    swapped = [*cases[:3], _case(99)]
    assert _manifest(swapped).run_sha256 != first.run_sha256

    run = ExperimentRun(tmp_path / "run")
    run.write_manifest(first)
    assert run.manifest() == first


def test_a_manifest_whose_identity_was_edited_is_refused() -> None:
    payload = _manifest([_case(0)]).model_dump(mode="json")
    payload["case_count"] += 1
    with pytest.raises(ValidationError, match="news_experiment_run_hash_mismatch"):
        ExperimentRunManifest.model_validate(payload)


def test_a_run_cannot_claim_more_accepted_cases_than_it_holds() -> None:
    with pytest.raises(ValidationError, match="news_experiment_accepted_case_count_invalid"):
        _manifest([_case(0)], case_count=1, accepted_case_count=2)


def test_a_run_directory_refuses_a_path_that_escapes_or_follows_a_link(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="news_experiment_run_path_invalid"):
        ExperimentRun(tmp_path / ".." / "elsewhere")

    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    with pytest.raises(ValueError, match="news_experiment_run_path_invalid"):
        ExperimentRun(tmp_path / "link")


def test_a_case_file_that_is_a_symlink_is_not_a_case(tmp_path: Path) -> None:
    run = ExperimentRun(tmp_path / "run")
    run.write_case(_case(0))
    (run.cases_dir / "planted.json").symlink_to(run.cases_dir / f"{0:064x}.json")
    with pytest.raises(ValueError, match="news_experiment_run_file_invalid"):
        list(run.cases())


def test_a_run_without_a_manifest_is_not_a_run(tmp_path: Path) -> None:
    run = ExperimentRun(tmp_path / "run")
    with pytest.raises(ValueError, match="news_experiment_run_manifest_missing"):
        run.manifest()


# --- resume is a directory listing, not a stored cursor -----------------------------------------------


def test_resume_answers_only_what_a_previous_run_left(tmp_path: Path) -> None:
    run = ExperimentRun(tmp_path / "run")
    cases = [_case(index) for index in range(5)]
    for case in cases:
        run.write_case(case)
    run.write_compared(cases[0].case_sha256, {"case_sha256": cases[0].case_sha256, "scores": {}})
    run.write_compared(cases[3].case_sha256, {"case_sha256": cases[3].case_sha256, "scores": {}})

    remaining = [case.case_sha256 for case in pending_cases(run, resume=True)]
    assert remaining == [cases[1].case_sha256, cases[2].case_sha256, cases[4].case_sha256]
    # Without `--resume` the run is answered again from the top: the flag is the only thing that lets a
    # previous partial answer stand, so a rerun can never inherit one by accident.
    assert len(pending_cases(run, resume=False)) == 5


def test_cases_are_read_in_one_fixed_order(tmp_path: Path) -> None:
    run = ExperimentRun(tmp_path / "run")
    for index in (4, 0, 2, 1, 3):
        run.write_case(_case(index))
    assert [case.case_sha256 for case in run.cases()] == sorted(f"{index:064x}" for index in range(5))


# --- only an accepted review can score or train anything ----------------------------------------------


def test_only_accepted_reviews_become_training_episodes() -> None:
    cases = [_case(0), _case(1, accepted=False), _case(2)]
    episodes = accepted_episodes(cases)
    assert [episode.case_id for episode in episodes] == [f"{0:064x}", f"{2:064x}"]


def test_a_case_nobody_judged_is_scored_by_nobody() -> None:
    cases = [_case(0), _case(1, accepted=False)]
    scored: list[BaselineCase] = baseline_cases(cases)
    assert [case.episode.case_id for case in scored] == [f"{0:064x}"]


def test_a_drafted_review_is_a_proposal_and_never_truth() -> None:
    """A draft sitting in the episode is not an acceptance, and `accepted` is what decides.

    `draft-reviews` writes a file; a human accepts it; only then does `baseline_episodes` project it as
    `accepted_review`. A frozen case that carried a drafted rubric and were trained on anyway would let the
    teacher model grade its own homework and call the result gold.
    """

    drafted = _case(7, accepted=False)
    drafted = drafted.model_copy(update={"episode": {**drafted.episode, "accepted_review": _ACCEPTED_REVIEW}})
    assert drafted.accepted is False
    assert accepted_episodes([drafted]) == ()
    assert baseline_cases([drafted]) == []


def test_optimizing_a_snapshot_nobody_reviewed_is_refused() -> None:
    with pytest.raises(ValueError, match="news_experiment_optimize_requires_accepted_reviews"):
        optimize_snapshot(
            run_sha256="a" * 64,
            base_program=load_stable_program_artifact(),
            cases=[_case(0, accepted=False)],
            task_lm=None,
            reflection_lm=None,
            judge=object(),
            max_metric_calls=4,
            seed=7,
            review_rubric_version="news_review_v4",
        )


# --- an unlabelled case is named, not dropped ---------------------------------------------------------


def test_unlabelled_cases_are_named_in_the_report() -> None:
    cluster_of = {"a": "c0", "b": "c0"}
    report = compare_report(
        run_sha256="a" * 64,
        arms={
            "recorded": _report({"a": 1.0, "b": 0.5}, cluster_of=cluster_of),
            "student": _report({"a": 0.5, "b": 0.5}, cluster_of=cluster_of),
        },
        unlabelled_case_ids=["c", "d"],
    )
    assert report["unlabelled_case_ids"] == ["c", "d"]
    # Both means survive the trip. Reporting only the answered-only mean is what turned 0.482 into 0.587.
    assert set(report["scores"]["student"]) >= {"mean", "mean_with_failures_as_zero"}


def test_failure_clusters_rank_a_broad_regression_above_a_single_bad_case() -> None:
    cluster_of = {"a": "broad", "b": "broad", "c": "broad", "d": "broad", "e": "single"}
    recorded = _report({"a": 1.0, "b": 1.0, "c": 1.0, "d": 1.0, "e": 1.0}, cluster_of=cluster_of)
    student = _report({"a": 0.8, "b": 0.8, "c": 0.8, "d": 0.8, "e": 0.3}, cluster_of=cluster_of)
    report = compare_report(
        run_sha256="a" * 64, arms={"recorded": recorded, "student": student}, unlabelled_case_ids=[]
    )
    # -0.2 across four cases outranks -0.7 on one: an operator's next hour is better spent on the pattern.
    assert [row["cluster_id"] for row in report["failure_clusters"]] == ["broad", "single"]


def test_an_improvement_never_climbs_the_work_queue() -> None:
    cluster_of = {"a": "better", "b": "worse"}
    report = compare_report(
        run_sha256="a" * 64,
        arms={
            "recorded": _report({"a": 0.2, "b": 1.0}, cluster_of=cluster_of),
            "student": _report({"a": 0.9, "b": 0.4}, cluster_of=cluster_of),
        },
        unlabelled_case_ids=[],
    )
    ranked = {row["cluster_id"]: row["attention"] for row in report["failure_clusters"]}
    assert ranked["better"] == 0.0
    assert ranked["worse"] < 0.0


# --- both planes optimize through one core ------------------------------------------------------------


def test_both_planes_hold_the_same_optimization_core() -> None:
    """Not "the same algorithm" by inspection — the same function object, in both callers.

    If either plane rebinds its own copy, the number an operator reads in the fast loop stops predicting
    what a trusted compile maximizes, and every comparison this loop produces becomes advisory noise.
    """

    assert optimize_module.run_gepa is gepa_core.run_gepa
    assert compiler_root.run_gepa is gepa_core.run_gepa


def _stub_result(**overrides: Any) -> GepaRunResult:
    values: dict[str, Any] = {
        "patch": ProgramStrategyPatchV1(
            parent_program_sha256=load_stable_program_artifact().program_sha256,
            event_semantics_instruction="Name the comparison base.",
            reader_card_instruction="",
        ),
        "metric": {"schema": "tracefold.news.compile_metric_receipt.v1"},
        "optimizer_config": {
            "schema": "tracefold.news.compile_optimizer_config_receipt.v1",
            "model_identities": {"task": {"role": "task"}, "reflection": {"role": "reflection"}},
        },
        "trajectory": {"schema": "tracefold.news.compile_trajectory_receipt.v1"},
        "checkpoint": {"schema": "tracefold.news.compile_checkpoint_receipt.v2"},
        "split": {"schema": "tracefold.news.compile_split_receipt.v1"},
        "retrieval": {"schema": "tracefold.news.compile_retrieval_receipt.v1"},
        "failure_cluster_ids": ("cluster-0",),
        "target_dimensions": ("factual_fidelity",),
        "metric_calls": 12,
        "train_count": 2,
        "val_count": 1,
    }
    values.update(overrides)
    return GepaRunResult(**values)


def test_optimize_hands_the_core_accepted_episodes_only(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def _fake(**kwargs: Any) -> GepaRunResult:
        seen.update(kwargs)
        return _stub_result()

    monkeypatch.setattr(optimize_module, "run_gepa", _fake)
    cases = [_case(0), _case(1, accepted=False), _case(2), _case(3, accepted=False)]
    candidate = optimize_snapshot(
        run_sha256="a" * 64,
        base_program=load_stable_program_artifact(),
        cases=cases,
        task_lm=None,
        reflection_lm=None,
        judge=object(),
        max_metric_calls=12,
        seed=7,
        review_rubric_version="news_review_v4",
    )
    assert [episode.case_id for episode in seen["episodes"]] == [f"{0:064x}", f"{2:064x}"]
    assert seen["review_rubric_version"] == "news_review_v4"
    assert candidate.parent_program_sha256 == load_stable_program_artifact().program_sha256
    assert candidate.event_semantics_instruction == "Name the comparison base."


# --- the winner is not release evidence ---------------------------------------------------------------


def _candidate(**overrides: Any) -> ExperimentCandidate:
    result = _stub_result()
    values: dict[str, Any] = {
        "run_sha256": "a" * 64,
        "parent_program_sha256": load_stable_program_artifact().program_sha256,
        "event_semantics_instruction": result.patch.event_semantics_instruction,
        "reader_card_instruction": result.patch.reader_card_instruction,
        "task_model": {"role": "task"},
        "reflection_model": {"role": "reflection"},
        "metric_judge_model": {"role": "metric_judge"},
        "optimizer": result.optimizer_config,
        "metric": result.metric,
        "split": result.split,
        "trajectory": result.trajectory,
        "failure_cluster_ids": result.failure_cluster_ids,
        "target_dimensions": result.target_dimensions,
        "metric_calls": result.metric_calls,
        "train_count": result.train_count,
        "val_count": result.val_count,
    }
    values.update(overrides)
    return ExperimentCandidate.issue(**values)


def test_an_experiment_candidate_says_it_cannot_be_promoted() -> None:
    payload = _candidate().model_dump(mode="json")
    assert payload["promotable"] is False
    assert payload["schema_version"] == "tracefold.news.experiment_candidate.v1"
    # Not a near-miss of the release document: it carries no compile record identity at all, so nothing
    # that reads one can mistake it for a compile that happened.
    assert "compile_record_sha256" not in payload


def test_an_experiment_candidate_cannot_be_read_as_a_compile_record() -> None:
    payload = _candidate().model_dump(mode="json")
    with pytest.raises(ValidationError):
        CompileRecordV1.model_validate(payload)
    with pytest.raises(ValidationError):
        validate_compile_record(
            payload,
            parent_program_sha256=str(payload["parent_program_sha256"]),
            program_sha256="c" * 64,
            development_dataset_sha256="d" * 64,
            target_runtime_manifest_sha256="e" * 64,
        )


def test_a_candidate_whose_instruction_was_edited_is_refused() -> None:
    payload = _candidate().model_dump(mode="json")
    payload["event_semantics_instruction"] = "Say whatever you like."
    with pytest.raises(ValidationError, match="news_experiment_candidate_hash_mismatch"):
        ExperimentCandidate.model_validate(payload)


def test_a_candidate_that_names_no_failure_it_targeted_is_refused() -> None:
    with pytest.raises(ValidationError):
        _candidate(failure_cluster_ids=())
    with pytest.raises(ValidationError):
        _candidate(target_dimensions=())
