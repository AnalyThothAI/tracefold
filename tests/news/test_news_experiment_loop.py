"""The operator's fast loop: a frozen run directory, a paired comparison, and an unpromotable proposal.

Every fixture here builds its cases through `snapshot._case`, the projection the real command uses. The
first version of this file constructed `ExperimentCase(...)` by hand, and so tested a shape the snapshot
can never write: the hand-built case carried no `recorded_decision_result`, and neither did the code, so
the whole suite stayed green while `compare` died on its first case with
`news_program_baseline_recorded_decision_missing`. Two of the tests below exist only to run the arms.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tracefold.news.learning.baseline import BaselineReport, CaseResult
from tracefold.news.learning.compiler import gepa as gepa_core
from tracefold.news.learning.compiler import root as compiler_root
from tracefold.news.learning.compiler.security import CompileRecordV1, GepaRunResult, validate_compile_record
from tracefold.news.learning.experiment import optimize as optimize_module
from tracefold.news.learning.experiment.compare import (
    answered_case_scores,
    baseline_cases,
    compare_report,
    failed_case_ids,
    merged_report,
    pending_cases,
    score_arm,
)
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
from tracefold.news.learning.experiment.snapshot import _case as _snapshot_case
from tracefold.news.program.artifact import ProgramStrategyPatchV1, load_stable_program_artifact

from .test_news_baseline_modes import _case as _baseline_case

_RECORDED_DECISION = {"final": "push", "rule": "trade_relevance", "rule_baseline": "push", "throttled_by": None}
_ACCEPTED_REVIEW = {
    "should_push": "should_push",
    "dimensions": {"factual_fidelity": "fail", "headline_fidelity": "pass", "magnitude": "pass"},
    "novelty": {"judgment": "new_fact", "duplicate_of": ""},
}


def _episode(index: int) -> dict[str, Any]:
    """A raw `baseline_episodes` row, loader-only keys and all — what `snapshot` actually receives."""

    payload = _baseline_case(index).episode.model_dump(mode="json")
    payload.update(
        case_id=f"{index:064x}",
        cluster_id=f"cluster-{index % 3}",
        stratum="delivered",
        accepted_review=dict(_ACCEPTED_REVIEW),
        event_id=f"event-{index}",
        recorded_decision_result=dict(_RECORDED_DECISION),
    )
    return payload


def _case(index: int, **overrides: Any) -> ExperimentCase:
    payload = _episode(index)
    payload.update(overrides)
    return _snapshot_case(payload)


def _manifest(cases: list[ExperimentCase], **overrides: Any) -> ExperimentRunManifest:
    values: dict[str, Any] = {
        "name": "news-24h",
        "window": ExperimentWindow(from_ms=1_787_000_000_000, to_ms=1_787_086_400_000),
        "parent_program_sha256": load_stable_program_artifact().program_sha256,
        "program_version": "news_semantic_program_v5",
        "policy_sha256": "b" * 64,
        "case_count": len(cases),
        "accepted_case_count": sum(1 for case in cases if case.accepted),
        "case_root_sha256": case_root_sha256(cases),
        "created_at_ms": 1_787_086_400_000,
    }
    values.update(overrides)
    return ExperimentRunManifest.issue(**values)


def _sha_of(parts: list[str]) -> str:
    import hashlib

    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _arm(scores: dict[str, float], *, cluster_of: dict[str, str], failed: tuple[str, ...] = ()) -> BaselineReport:
    return BaselineReport(
        mode="recorded",
        identity={
            "mode": "recorded",
            "compile_task_model": "qwen3.8-27b",
            # Batch-scoped by construction: two batches of one run never agree on these, which is why the
            # merge compares model identity only.
            "case_root_sha256": _sha_of(sorted(scores)),
            "corpus_sha256": _sha_of([*sorted(scores), "corpus"]),
            "cluster_root_sha256": _sha_of(sorted(set(cluster_of.values()))),
        },
        execution_scope=("recorded",),
        population={"cases": len(scores), "answered": len(scores) - len(failed)},
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
                error_code="provider_unavailable" if case_id in failed else None,
            )
            for case_id, score in scores.items()
        ),
    )


# --- the arms actually run ----------------------------------------------------------------------------


def test_the_recorded_arm_scores_a_frozen_case() -> None:
    """The regression that mattered: `compare`'s first arm used to raise on its first case.

    `snapshot` stripped `recorded_decision_result` and `baseline_cases` rebuilt `BaselineCase` by hand
    without it, so `run_baseline` refused every recorded case — and no test noticed, because no test called
    `run_baseline` at all. Both sides now go through `build_baseline_cases`, the release plane's own
    projection.
    """

    report = score_arm([_case(0)], mode="recorded", artifact=load_stable_program_artifact(), cohort_scope="experiment")

    assert [case.case_id for case in report.cases] == [f"{0:064x}"]
    assert report.cases[0].error_code is None


def test_the_student_arm_gets_the_program_factory_run_baseline_requires() -> None:
    """`compile_live` refuses without a factory, and the caller is not the one who should remember it.

    Left to the CLI it was forgotten, and `--student` being required meant no invocation escaped
    `news_program_baseline_requires_program_factory`. `score_arm` picks it from the mode now, so reaching a
    provider error here — rather than the factory error — is the whole assertion.
    """

    report = score_arm(
        [_case(0)],
        mode="compile_live",
        artifact=load_stable_program_artifact(),
        lm=object(),
        cohort_scope="experiment",
    )

    # It built the graph and tried to answer. A bogus LM is a provider failure, which this plane publishes
    # as an outcome rather than an absence — the point is that it got that far at all.
    assert [case.case_id for case in report.cases] == [f"{0:064x}"]
    assert report.cases[0].error_code is not None


# --- the run directory is addressed by what it froze --------------------------------------------------


def test_a_run_root_is_reproducible_from_the_directory_it_addresses(tmp_path: Path) -> None:
    """Write order and read order are different orders, so the root cannot depend on either.

    A snapshot writes in `(opened_at_ms, case_id)` — wall clock — and `cases()` reads back in filename
    order, which is the case sha. Hashing the caller's sequence produced a root nothing could recompute.
    """

    cases = [_case(index) for index in (4, 0, 2)]
    run = ExperimentRun(tmp_path / "run", create=True)
    for case in cases:
        run.write_case(case)

    assert case_root_sha256(cases) == case_root_sha256(list(run.cases()))
    assert case_root_sha256(cases) == case_root_sha256(list(reversed(cases)))


def test_a_run_is_addressed_by_the_cases_it_froze(tmp_path: Path) -> None:
    cases = [_case(index) for index in range(4)]
    first = _manifest(cases)
    assert _manifest(list(cases)).run_sha256 == first.run_sha256
    assert _manifest([*cases[:3], _case(99)]).run_sha256 != first.run_sha256

    run = ExperimentRun(tmp_path / "run", create=True)
    run.write_manifest(first)
    assert run.manifest() == first


def test_a_manifest_whose_identity_was_edited_is_refused() -> None:
    payload = _manifest([_case(0)]).model_dump(mode="json")
    payload["case_count"] += 1
    with pytest.raises(ValidationError, match="news_experiment_run_hash_mismatch"):
        ExperimentRunManifest.model_validate(payload)


def test_a_case_id_cannot_escape_the_run_directory() -> None:
    """`case_sha256` is a filename. Unconstrained, `write_compared` wrote outside the run root."""

    with pytest.raises(ValidationError):
        _case(0, case_id="../../../pwned")


def test_a_run_directory_refuses_a_path_that_escapes_or_follows_a_link(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="news_experiment_run_path_invalid"):
        ExperimentRun(tmp_path / ".." / "elsewhere", create=True)

    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    with pytest.raises(ValueError, match="news_experiment_run_path_invalid"):
        ExperimentRun(tmp_path / "link", create=True)


def test_a_read_command_never_conjures_the_run_it_was_given(tmp_path: Path) -> None:
    """Only `snapshot` creates. `compare`/`optimize` used to mkdir and then report a missing manifest."""

    with pytest.raises(ValueError, match="news_experiment_run_directory_missing"):
        ExperimentRun(tmp_path / "never-snapshotted")
    assert not (tmp_path / "never-snapshotted").exists()


def test_a_case_file_that_is_a_symlink_is_not_a_case(tmp_path: Path) -> None:
    run = ExperimentRun(tmp_path / "run", create=True)
    run.write_case(_case(0))
    (run.cases_dir / "planted.json").symlink_to(run.cases_dir / f"{0:064x}.json")
    with pytest.raises(ValueError, match="news_experiment_run_file_invalid"):
        list(run.cases())


# --- resume ------------------------------------------------------------------------------------------


def test_resume_answers_only_what_a_previous_run_left(tmp_path: Path) -> None:
    run = ExperimentRun(tmp_path / "run", create=True)
    cases = [_case(index) for index in range(5)]
    for case in cases:
        run.write_case(case)
    for case in (cases[0], cases[3]):
        run.write_compared(case.case_sha256, {"case_sha256": case.case_sha256, "scores": {}})

    assert [case.case_sha256 for case in pending_cases(run, resume=True)] == [
        cases[1].case_sha256,
        cases[2].case_sha256,
        cases[4].case_sha256,
    ]
    assert len(pending_cases(run, resume=False)) == 5


def test_a_case_the_provider_never_answered_is_asked_again() -> None:
    """`if scores` could never be False — a provider failure scores `0.0`, not nothing.

    So a timed-out case was recorded as answered, skipped by every later pass, and its full delta ranked as
    a semantic regression. Failure is still published; it is just not a reason to stop asking.
    """

    cluster_of = {"answered": "c0", "timed-out": "c0"}
    arms = {
        "recorded": _arm({"answered": 1.0, "timed-out": 1.0}, cluster_of=cluster_of),
        "student": _arm({"answered": 0.8, "timed-out": 0.0}, cluster_of=cluster_of, failed=("timed-out",)),
    }
    report = compare_report(run_sha256="a" * 64, arms=arms)

    assert failed_case_ids(arms) == ("timed-out",)
    assert set(answered_case_scores(report, failed_case_ids=failed_case_ids(arms))) == {"answered"}


def test_a_resumed_report_covers_the_whole_run_not_the_last_batch() -> None:
    """`--resume` scores only the pending batch, so a report rebuilt from it alone described 4% of a run."""

    cluster_of = {"a": "c0", "b": "c0", "c": "c1"}
    first = compare_report(
        run_sha256="a" * 64,
        arms={
            "recorded": _arm({"a": 1.0, "b": 1.0}, cluster_of=cluster_of),
            "student": _arm({"a": 0.5, "b": 0.5}, cluster_of=cluster_of),
        },
    )
    second = compare_report(
        run_sha256="a" * 64,
        arms={
            "recorded": _arm({"c": 1.0}, cluster_of=cluster_of),
            "student": _arm({"c": 0.2}, cluster_of=cluster_of),
        },
    )
    merged = merged_report(previous=first, current=second)

    assert set(merged["cases"]) == {"a", "b", "c"}
    assert {row["cluster_id"] for row in merged["failure_clusters"]} == {"c0", "c1"}


def test_two_batches_of_one_run_merge_even_though_their_corpus_roots_differ() -> None:
    """`identity` mixes which models answered with which cases they answered about.

    Only the first is what two resumed passes have to agree on — `case_root_sha256`, `corpus_sha256` and
    `cluster_root_sha256` are scoped to the batch by construction. Comparing the whole dict made every
    normal second `--resume` raise, and it raised *after* the batch had been marked compared, so the next
    pass skipped results nothing had recorded.
    """

    cluster_of = {"a": "c0", "b": "c0"}
    first = compare_report(run_sha256="a" * 64, arms={"recorded": _arm({"a": 1.0}, cluster_of=cluster_of)})
    second = compare_report(run_sha256="a" * 64, arms={"recorded": _arm({"b": 0.4}, cluster_of=cluster_of)})

    assert first["arms"]["recorded"]["case_root_sha256"] != second["arms"]["recorded"]["case_root_sha256"]
    assert set(merged_report(previous=first, current=second)["cases"]) == {"a", "b"}


def test_two_passes_run_against_different_models_are_not_one_report() -> None:
    cluster_of = {"a": "c0"}
    current = compare_report(run_sha256="a" * 64, arms={"recorded": _arm({"a": 1.0}, cluster_of=cluster_of)})
    previous = dict(current)
    previous["arms"] = {
        "recorded": {**dict(current["arms"]["recorded"]), "compile_task_model": "someone-else"},
    }
    with pytest.raises(ValueError, match="news_experiment_report_arm_identity_changed"):
        merged_report(previous=previous, current=current)


def test_a_merged_report_recomputes_its_headline_numbers_over_every_case() -> None:
    """`scores` and `population` used to be carried from the last batch alone.

    So a 500-case run processed twenty at a time published headline accuracy for its final twenty while
    carrying all five hundred rows and the whole run's identity. The batch report addresses are kept by
    name rather than one of them being passed off as the merged view's.
    """

    cluster_of = {"a": "c0", "b": "c0", "c": "c0"}
    first = compare_report(run_sha256="a" * 64, arms={"recorded": _arm({"a": 1.0, "b": 1.0}, cluster_of=cluster_of)})
    second = compare_report(
        run_sha256="a" * 64,
        arms={"recorded": _arm({"c": 0.4}, cluster_of=cluster_of, failed=("c",))},
    )
    merged = merged_report(previous=first, current=second)

    assert merged["population"]["recorded"] == {"requested_n": 3, "answered_n": 3}
    assert merged["scores"]["recorded"]["mean"] == round((1.0 + 1.0 + 0.4) / 3, 6)
    assert "report_sha256" not in merged
    assert len(merged["batch_report_sha256"]) == 2


# --- only an accepted review can score or train anything ----------------------------------------------


def test_a_snapshot_case_is_accepted_by_construction() -> None:
    """`baseline_episodes` reaches a case through an acceptance row, so this is a fact, not a guess.

    It used to read `bool(accepted_review)`, which `_project_episodes` always fills — a constant wearing the
    costume of a check, and the reason `--events-from` could never find anything to draft.
    """

    assert _case(0).accepted is True
    assert _case(0, accepted_review={}).accepted is True


def test_both_the_metric_and_the_optimizer_see_one_projection() -> None:
    cases = [_case(0), _case(1)]
    scored = baseline_cases(cases)

    assert [case.episode.case_id for case in scored] == [episode.case_id for episode in accepted_episodes(cases)]
    # Threaded, not dropped: this is the field whose absence made the recorded arm unrunnable.
    assert all(case.recorded_decision_result for case in scored)


def test_optimizing_a_snapshot_nobody_reviewed_is_refused() -> None:
    with pytest.raises(ValueError, match="news_experiment_optimize_requires_accepted_reviews"):
        optimize_snapshot(
            run_sha256="a" * 64,
            base_program=load_stable_program_artifact(),
            cases=[],
            task_lm=None,
            reflection_lm=None,
            judge=object(),
            max_metric_calls=4,
            seed=7,
            review_rubric_version="news_review_v4",
        )


# --- failure clusters are a work queue ----------------------------------------------------------------


def test_failure_clusters_rank_a_broad_regression_above_a_single_bad_case() -> None:
    cluster_of = {"a": "broad", "b": "broad", "c": "broad", "d": "broad", "e": "single"}
    report = compare_report(
        run_sha256="a" * 64,
        arms={
            "recorded": _arm({"a": 1.0, "b": 1.0, "c": 1.0, "d": 1.0, "e": 1.0}, cluster_of=cluster_of),
            "student": _arm({"a": 0.8, "b": 0.8, "c": 0.8, "d": 0.8, "e": 0.3}, cluster_of=cluster_of),
        },
    )
    assert [row["cluster_id"] for row in report["failure_clusters"]] == ["broad", "single"]


def test_an_improvement_never_climbs_the_work_queue() -> None:
    cluster_of = {"a": "better", "b": "worse"}
    report = compare_report(
        run_sha256="a" * 64,
        arms={
            "recorded": _arm({"a": 0.2, "b": 1.0}, cluster_of=cluster_of),
            "student": _arm({"a": 0.9, "b": 0.4}, cluster_of=cluster_of),
        },
    )
    ranked = {row["cluster_id"]: row["attention"] for row in report["failure_clusters"]}
    assert ranked["better"] == 0.0 and ranked["worse"] < 0.0


# --- both planes optimize through one core ------------------------------------------------------------


def test_both_planes_hold_the_same_optimization_core_and_the_same_student() -> None:
    """Function identity was not enough, and the gap it missed falsified the PR's headline claim.

    `run_gepa` defaulted `student_factory` to the plain `DspyCompileProgram`; the trusted compiler passed
    `_FeedbackCompileProgram`, whose `_rekey_trace` is what lets GEPA's reflective dataset find anything at
    all. Same function, different student — the fast loop would have burned its whole budget proposing
    nothing while reporting the same algorithm. The student is the core's own default now.
    """

    assert optimize_module.run_gepa is gepa_core.run_gepa is compiler_root.run_gepa
    default = inspect.signature(gepa_core.run_gepa).parameters["student_factory"].default
    assert default is gepa_core._FeedbackCompileProgram
    assert callable(getattr(default, "_rekey_trace", None))


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


def test_optimize_hands_the_core_the_projection_the_metric_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def _fake(**kwargs: Any) -> GepaRunResult:
        seen.update(kwargs)
        return _stub_result()

    monkeypatch.setattr(optimize_module, "run_gepa", _fake)
    candidate = optimize_snapshot(
        run_sha256="a" * 64,
        base_program=load_stable_program_artifact(),
        cases=[_case(0), _case(2)],
        task_lm=None,
        reflection_lm=None,
        judge=object(),
        max_metric_calls=12,
        seed=7,
        review_rubric_version="news_review_v4",
    )

    assert [episode.case_id for episode in seen["episodes"]] == [f"{0:064x}", f"{2:064x}"]
    assert seen["review_rubric_version"] == "news_review_v4"
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
