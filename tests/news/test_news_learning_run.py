"""#253: the one recommended GEPA path, and the summary that says whether its two numbers are comparable.

The interesting behaviour is not that three commands run in order — it is what the composition can now
refuse that three hand-typed commands could not: two different judges, a corpus bound that does not cover
the corpus, and above all a `standalone` and a `seed` scalar published side by side without anything
asserting they describe the same experiment.
"""

from __future__ import annotations

import copy
import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tracefold.app.cli.commands import news_learning_run as run_commands
from tracefold.app.cli.parser import build_parser
from tracefold.news.learning.run_summary import RUN_SUMMARY_SCHEMA, build_run_summary

_DATASET = "d" * 64
_EPISODE_ROOT = "e" * 64
_OPTIMIZER_ROOT = "c" * 64
_TRAIN_ROOT = "1" * 64
_SELECTION_ROOT = "2" * 64
_PROGRAM = "9" * 64
_BASELINE_REPORT = "b" * 64
_OPTIMIZATION_REPORT = "a" * 64
_METRIC = {"schema": "tracefold.news.compile_metric_receipt.v3", "metric_id": "m4", "weights": {"final_action": 0.45}}
_TASK_ROUTE = {
    "model": "qwen3-30b",
    "baseline_endpoint_sha256": "4" * 64,
    "optimizer_endpoint_fingerprint": "5" * 64,
}


def _readiness(**updates: Any) -> dict[str, Any]:
    report = {
        "schema": "tracefold.news.gepa_readiness_report.v1",
        "outcome": "ready",
        "blocking_reasons": [],
        "identity": {
            "development_dataset_sha": _DATASET,
            "episode_projection_root_sha256": _EPISODE_ROOT,
            "episode_count": 84,
            "program_sha256": _PROGRAM,
        },
        "corpus": {"case_n": 84, "cluster_n": 71},
        "objective": {
            "target_case_n": 19,
            "control_case_n": 46,
            "excluded_case_n": 19,
            "optimizer_case_n": 65,
            "optimizer_cluster_n": 65,
            "optimizer_case_root_sha256": _OPTIMIZER_ROOT,
        },
        "split": {
            "train": {"case_root_sha256": _TRAIN_ROOT, "cluster_n": 46, "case_n": 46},
            "development_selection": {"case_root_sha256": _SELECTION_ROOT, "cluster_n": 19, "case_n": 19},
        },
    }
    report.update(updates)
    return copy.deepcopy(report)


def _baseline(**updates: Any) -> dict[str, Any]:
    report = {
        "schema_id": "tracefold.news.program_baseline_report.v3",
        "report_sha256": _BASELINE_REPORT,
        "mode": "compile_live",
        "identity": {
            "development_dataset_sha": _DATASET,
            "episode_projection_root_sha256": _EPISODE_ROOT,
            "episode_count": 84,
            "program_sha256": _PROGRAM,
            "metric": _METRIC,
            "runtime_model": {
                "compile_task_model": "qwen3-30b",
                "compile_task_endpoint_sha256": _TASK_ROUTE["baseline_endpoint_sha256"],
            },
        },
        "objective": {
            "optimizer_case_n": 65,
            "optimizer_cluster_n": 65,
            "optimizer_case_root_sha256": _OPTIMIZER_ROOT,
            "split": {
                "train": {"case_root_sha256": _TRAIN_ROOT},
                "development_selection": {"case_root_sha256": _SELECTION_ROOT},
            },
        },
        "subsets": {
            "development_selection": {
                "case_n": 19,
                "answered_n": 19,
                "case_macro_failure_as_zero": 0.412,
                "case_macro_answered": 0.412,
            }
        },
    }
    report.update(updates)
    return copy.deepcopy(report)


def _optimization(**updates: Any) -> dict[str, Any]:
    report = {
        "schema_version": "news_optimization_run_report_v1",
        "outcome": "NO_OP",
        "reasons": ["news_program_compile_no_program_change"],
        "candidate_sha256": None,
        "report_sha256": _OPTIMIZATION_REPORT,
        "parent_program_sha256": _PROGRAM,
        "dataset": {
            "development_dataset_sha256": _DATASET,
            "episode_projection_root_sha256": _EPISODE_ROOT,
            "episode_count": 84,
            "learning_epoch": "program_v7",
            "review_rubric_version": "news_review_v4",
        },
        "objective": {
            "episode_projection_root_sha256": _EPISODE_ROOT,
            "optimizer_case_n": 65,
            "optimizer_cluster_n": 65,
            "optimizer_case_root_sha256": _OPTIMIZER_ROOT,
        },
        "split": {
            "train": {"case_root_sha256": _TRAIN_ROOT},
            "development_selection": {"case_root_sha256": _SELECTION_ROOT},
        },
        "metric": _METRIC,
        "model_identities": {
            "task": {"model": "qwen3-30b", "endpoint_fingerprint": _TASK_ROUTE["optimizer_endpoint_fingerprint"]}
        },
        "trajectory": {"val_aggregate_scores": [0.437, 0.401], "best_idx": 0, "num_full_val_evals": 2},
        "usage": {
            "metric_calls": 96,
            "task_model_calls": 192,
            "reflection_model_calls": 7,
            "metric_judge_model_calls": 88,
            "metric_judge_failures": 0,
            "actual_cost_microusd": 412_000,
            "metric_judge_cost_imputed": True,
            "transport_failures": 1,
            "transport_retries": 1,
        },
    }
    report.update(updates)
    return copy.deepcopy(report)


def _summary(**updates: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "development_dataset_sha": _DATASET,
        "readiness": _readiness(),
        "baseline": _baseline(),
        "optimization": _optimization(),
        "task_route": _TASK_ROUTE,
        "artifacts": {"readiness": "readiness.json", "baseline": "baseline-compile-live.json"},
    }
    arguments.update(updates)
    return build_run_summary(**arguments)


def test_the_seed_baseline_comes_from_the_trajectory_and_never_from_the_standalone_run() -> None:
    """#253 §3.2: two numbers, two physical runs, and the summary must not let one stand in for the other.

    `standalone_selection_score` is the `compile_live` report's own development-selection mean;
    `gepa_seed_selection_score` is trajectory index 0, the seed Program's score inside the GEPA run that
    proposed against it. #225 published `0.0` and `0.475` for what looked like one number, and the fix is
    not to reconcile them but to name each one by the field it came from.
    """

    summary = _summary()

    assert summary["schema"] == RUN_SUMMARY_SCHEMA
    assert summary["baseline"]["standalone_selection_score"] == 0.412
    assert summary["baseline"]["gepa_seed_selection_score"] == 0.437
    assert summary["baseline"]["standalone_selection_source"] == (
        "subsets.development_selection.case_macro_failure_as_zero"
    )
    assert summary["baseline"]["gepa_seed_selection_source"] == "trajectory.val_aggregate_scores[0]"
    # Published, not smoothed: a difference between two physical runs is a fact about model execution.
    assert summary["baseline"]["numeric_drift"] == 0.025
    assert summary["baseline"]["numeric_drift_definition"] == ("gepa_seed_selection_score - standalone_selection_score")
    assert summary["baseline"]["same_population"] is True


def test_a_numeric_difference_alone_never_reads_as_a_broken_dataset_identity() -> None:
    """§8: "standalone 与 seed 数值不同必须公开 … 也不能仅凭差异断言 identity 错误"."""

    summary = _summary(optimization=_optimization(trajectory={"val_aggregate_scores": [0.0], "best_idx": 0}))

    assert summary["baseline"]["gepa_seed_selection_score"] == 0.0
    assert summary["baseline"]["numeric_drift"] == -0.412
    assert summary["baseline"]["same_population"] is True
    assert [check["status"] for check in summary["baseline"]["population_checks"]] == ["match"] * 12


@pytest.mark.parametrize(
    ("name", "patch"),
    [
        ("development_dataset_sha", {"identity": {"development_dataset_sha": "f" * 64}}),
        ("episode_projection_root_sha256", {"identity": {"episode_projection_root_sha256": "f" * 64}}),
        ("episode_count", {"identity": {"episode_count": 83}}),
        ("optimizer_case_root_sha256", {"objective": {"optimizer_case_root_sha256": "f" * 64}}),
        ("optimizer_case_n", {"objective": {"optimizer_case_n": 64}}),
        ("optimizer_cluster_n", {"objective": {"optimizer_cluster_n": 64}}),
        ("split_train_case_root_sha256", {"objective": {"split": {"train": {"case_root_sha256": "f" * 64}}}}),
        ("metric_sha256", {"identity": {"metric": {"schema": "other"}}}),
        ("program_sha256", {"identity": {"program_sha256": "f" * 64}}),
        ("task_model", {"identity": {"runtime_model": {"compile_task_model": "other-model"}}}),
        (
            "task_endpoint_resolved_once",
            {"identity": {"runtime_model": {"compile_task_endpoint_sha256": "f" * 64}}},
        ),
    ],
)
def test_any_disagreeing_population_identity_refuses_the_comparison(name: str, patch: dict[str, Any]) -> None:
    """§8: dataset, representative, split, metric, Program or model binding — any one of them, fail closed.

    The two scalars are still published, because they are facts about two runs that happened. What is
    withheld is the claim that they may be subtracted from each other.
    """

    baseline = _baseline()
    for section, updates in patch.items():
        _deep_update(baseline[section], updates)

    summary = _summary(baseline=baseline)

    assert summary["baseline"]["same_population"] is False
    mismatched = [check["name"] for check in summary["baseline"]["population_checks"] if check["status"] == "mismatch"]
    assert name in mismatched
    # Still there — a refused comparison is not a reason to hide the evidence for it.
    assert summary["baseline"]["standalone_selection_score"] == 0.412
    assert summary["baseline"]["gepa_seed_selection_score"] == 0.437


def test_readiness_is_a_checked_party_and_not_a_printed_footnote() -> None:
    """#253 §9 PR-K0: readiness, Baseline and GEPA must use the same representative roots.

    Baseline and GEPA agreeing with each other proves nothing about the report the operator read first.
    A readiness that describes a different representative set means the run was explained as one corpus
    and measured as another, and that is a mismatch even when the two measured legs agree.
    """

    drifted = _readiness()
    drifted["objective"]["optimizer_case_root_sha256"] = "f" * 64
    drifted["split"]["development_selection"]["case_root_sha256"] = "f" * 64

    summary = _summary(readiness=drifted)

    assert summary["baseline"]["same_population"] is False
    mismatched = {check["name"] for check in summary["baseline"]["population_checks"] if check["status"] == "mismatch"}
    assert mismatched == {"optimizer_case_root_sha256", "split_selection_case_root_sha256"}
    row = next(
        check for check in summary["baseline"]["population_checks"] if check["name"] == "optimizer_case_root_sha256"
    )
    assert row["standalone"] == row["gepa"] == _OPTIMIZER_ROOT
    assert row["expected"] == "f" * 64


def test_two_endpoint_digest_schemas_are_each_checked_against_the_route_this_run_composed() -> None:
    """The baseline fingerprints its task endpoint one way and the optimizer another.

    Comparing the two digests directly would fail closed on every honest run — they hash different
    documents over the same endpoint — so each is checked against the endpoint this run resolved instead.
    """

    check = next(
        row for row in _summary()["baseline"]["population_checks"] if row["name"] == "task_endpoint_resolved_once"
    )

    assert check["status"] == "match"
    assert check["standalone"] != check["gepa"]
    assert (check["expected_standalone"], check["expected_gepa"]) == (
        _TASK_ROUTE["baseline_endpoint_sha256"],
        _TASK_ROUTE["optimizer_endpoint_fingerprint"],
    )


def test_a_run_with_no_baseline_leg_is_not_comparable_rather_than_matching_or_mismatching() -> None:
    """A corpus readiness refused never reaches a baseline, and `same_population` must not claim either verdict."""

    summary = _summary(
        readiness=_readiness(outcome="insufficient", blocking_reasons=["train_target_missing"], split=None),
        baseline=None,
        optimization=_optimization(
            outcome="REJECTED",
            reasons=["train_target_missing"],
            split=None,
            metric=None,
            trajectory=None,
        ),
        artifacts={"readiness": "readiness.json", "baseline": None},
    )

    assert summary["run"]["baseline_executed"] is False
    assert summary["baseline"]["same_population"] is None
    assert summary["baseline"]["numeric_drift"] is None
    assert summary["baseline"]["standalone_selection_score"] is None
    assert summary["baseline"]["gepa_seed_selection_score"] is None
    assert {check["status"] for check in summary["baseline"]["population_checks"]} == {"not_comparable"}
    assert summary["next_action"] == "collect_more_gold"


def test_the_future_test_baseline_is_named_and_left_empty_because_this_command_cannot_produce_one() -> None:
    """§3.2 and §7 Phase E: the third baseline exists, is not this one, and is not inferable from it."""

    summary = _summary()

    assert summary["baseline"]["future_test_baseline"] is None
    assert "holdout" in summary["baseline"]["future_test_note"]
    assert "ValidationDataset" in summary["baseline"]["future_test_note"]


@pytest.mark.parametrize(
    ("outcome", "reasons", "expected"),
    [
        ("ADVANCE", [], "future_test"),
        ("NO_OP", ["news_program_compile_no_program_change"], "keep_stable"),
        ("REJECTED", ["news_program_compile_no_verified_failure_clusters"], "collect_more_gold"),
        ("REJECTED", ["development_selection_control_missing"], "collect_more_gold"),
        ("REJECTED", ["news_program_compile_split_requires_two_clusters"], "collect_more_gold"),
        ("REJECTED", ["news_program_compile_cost_budget_exceeded"], "keep_stable"),
        ("REJECTED", ["news_learning_optimize_wall_clock_exhausted"], "keep_stable"),
        ("REJECTED", ["news_program_learned_strategy_rejected"], "keep_stable"),
    ],
)
def test_next_action_separates_a_corpus_that_cannot_answer_from_a_run_that_ran_out(
    outcome: str, reasons: list[str], expected: str
) -> None:
    """`collect_more_gold` is advice about the corpus. Giving it for an exhausted budget would be wrong."""

    candidate = "7" * 64 if outcome == "ADVANCE" else None
    summary = _summary(optimization=_optimization(outcome=outcome, reasons=reasons, candidate_sha256=candidate))

    assert summary["next_action"] == expected
    assert summary["optimization"]["terminal"] == outcome
    assert summary["optimization"]["candidate_sha"] == candidate


def test_the_summary_carries_counts_and_addresses_and_no_business_content() -> None:
    """§8: a projection of retained artifacts — never news text, a Prompt, a case list or a credential."""

    summary = _summary()
    document = json.dumps(summary, ensure_ascii=False)

    assert summary["dataset"] == {
        "development_sha": _DATASET,
        "episode_root": _EPISODE_ROOT,
        "episode_count": 84,
        "accepted_case_n": 84,
        "connected_cluster_n": 71,
        "optimizer_representative_n": 65,
        "optimizer_cluster_n": 65,
        "optimizer_case_root": _OPTIMIZER_ROOT,
        "target_case_n": 19,
        "control_case_n": 46,
        "excluded_case_n": 19,
        "train_root": _TRAIN_ROOT,
        "selection_root": _SELECTION_ROOT,
        "train_cluster_n": 46,
        "selection_cluster_n": 19,
    }
    for forbidden in ("headline", "why_zh", "instruction", "api_key", "case_id", "event_id"):
        assert forbidden not in document
    assert summary["optimization"]["cost"]["actual_cost_microusd"] == 412_000
    assert summary["optimization"]["metric_calls"] == 96
    assert summary["optimization"]["best_selection_score"] == 0.437


def test_a_trajectory_the_run_never_produced_leaves_both_gepa_numbers_absent() -> None:
    """An unspent `REJECTED` publishes no trajectory. Reading `0.0` off an absent one would invent a score."""

    summary = _summary(
        optimization=_optimization(outcome="REJECTED", reasons=["train_target_missing"], trajectory=None)
    )

    assert summary["baseline"]["gepa_seed_selection_score"] is None
    assert summary["optimization"]["best_selection_score"] is None
    assert summary["optimization"]["trajectory_entries"] == 0


def _deep_update(target: dict[str, Any], updates: dict[str, Any]) -> None:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
            continue
        target[key] = value


# --- the composition itself ------------------------------------------------------------------------


def test_the_golden_path_takes_no_judge_flag_so_one_ruler_scores_both_legs() -> None:
    """§7 Phase C: the entry is thin, and the two ways it could be given two rulers are simply absent.

    `--semantic-judge` and `--dataset` are gone: the judge is `llm.news_compiler_reflection`, which is the
    route `optimize` cannot be told to leave, and the corpus is the frozen dataset by construction. The
    budget flags stay, because a spend is the operator's decision and inventing defaults would be a second
    budget.
    """

    args = build_parser().parse_args(
        [
            "news", "learning", "run",
            "--development", _DATASET,
            "--out", "artifacts/run-1",
            "--max-baseline-model-cases", "65",
            "--max-metric-calls", "120",
            "--max-task-model-calls", "400",
            "--max-reflection-model-calls", "40",
            "--max-metric-judge-model-calls", "200",
            "--max-cost-microusd", "800000",
            "--max-call-cost-microusd", "5000",
        ]
    )  # fmt: skip

    assert args.learning_command == "run"
    assert (args.max_baseline_model_cases, args.max_metric_judge_model_calls, args.seed) == (65, 200, 129)
    for absent in ("semantic_judge", "dataset", "mode", "student"):
        assert not hasattr(args, absent), absent
    for required in ("--max-metric-calls", "--max-metric-judge-model-calls", "--max-baseline-model-cases"):
        incomplete = [
            token
            for token in [
                "news", "learning", "run", "--development", _DATASET, "--out", "o",
                "--max-baseline-model-cases", "65", "--max-metric-calls", "120",
                "--max-task-model-calls", "400", "--max-reflection-model-calls", "40",
                "--max-metric-judge-model-calls", "200", "--max-cost-microusd", "800000",
                "--max-call-cost-microusd", "5000",
            ]
        ]  # fmt: skip
        index = incomplete.index(required)
        del incomplete[index : index + 2]
        with pytest.raises(SystemExit):
            build_parser().parse_args(incomplete)


def _run_args(tmp_path: Path, **updates: Any) -> Namespace:
    values: dict[str, Any] = {
        "learning_command": "run",
        "development": _DATASET,
        "out": str(tmp_path / "run-1"),
        "max_baseline_model_cases": 65,
        "max_metric_calls": 120,
        "max_task_model_calls": 400,
        "max_reflection_model_calls": 40,
        "max_metric_judge_model_calls": 200,
        "max_cost_microusd": 800_000,
        "max_call_cost_microusd": 5_000,
        "max_wall_clock_seconds": 14_400,
        "seed": 129,
    }
    values.update(updates)
    return Namespace(**values)


class _Legs:
    """Stand-ins for the three composed handlers that record how they were called and write real files."""

    def __init__(self, *, readiness: dict[str, Any], baseline: dict[str, Any], optimization: dict[str, Any]) -> None:
        self.readiness_report = readiness
        self.baseline_report = baseline
        self.optimization_report = optimization
        self.calls: list[tuple[str, Namespace]] = []

    def on_readiness(self, args: Namespace, _settings: Any, _stable: Any) -> tuple[int, dict[str, Any]]:
        self.calls.append(("readiness", args))
        _write(Path(args.out), self.readiness_report)
        return 0, {"ok": True, "data": {}}

    def on_baseline(self, args: Namespace, _settings: Any, _stable: Any) -> tuple[int, dict[str, Any]]:
        self.calls.append(("baseline", args))
        _write(Path(args.out), self.baseline_report)
        return 0, {"ok": True, "data": {}}

    def on_optimize(self, args: Namespace, _settings: Any, _stable: Any) -> tuple[int, dict[str, Any]]:
        self.calls.append(("optimize", args))
        directory = Path(args.out)
        directory.mkdir(parents=True, exist_ok=True)
        _write(directory / "optimization_report.json", self.optimization_report)
        return (0 if self.optimization_report["outcome"] == "ADVANCE" else 1), {"ok": False, "data": {}}


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _install(monkeypatch: Any, legs: _Legs) -> None:
    monkeypatch.setattr(run_commands, "_handle_learning_readiness", legs.on_readiness)
    monkeypatch.setattr(run_commands, "_handle_learning_baseline", legs.on_baseline)
    monkeypatch.setattr(
        "tracefold.app.cli.commands.news_learning_experiment.handle_research",
        legs.on_optimize,
    )
    monkeypatch.setattr(run_commands, "_reflection_judge_model", lambda _settings: "deepseek-v4-pro")
    monkeypatch.setattr(run_commands, "_task_route", lambda _settings: dict(_TASK_ROUTE))


def test_the_three_legs_run_in_order_into_one_directory_and_share_one_judge(monkeypatch: Any, tmp_path: Path) -> None:
    """The composition an operator used to do by hand, with the two agreements it could not check.

    The judge model is the configured reflection route rather than a name retyped per command, and the
    optimizer's judge ceiling is handed to the baseline as well — which is what makes the two metric
    receipts, and therefore the ruler the two Stable numbers were measured with, byte-identical.
    """

    legs = _Legs(readiness=_readiness(), baseline=_baseline(), optimization=_optimization())
    _install(monkeypatch, legs)

    code, payload = run_commands._handle_learning_run(_run_args(tmp_path), SimpleNamespace(), SimpleNamespace())

    assert [name for name, _args in legs.calls] == ["readiness", "baseline", "optimize"]
    baseline_args = dict(legs.calls[1][1]._get_kwargs())
    assert baseline_args["semantic_judge"] == "deepseek-v4-pro"
    assert baseline_args["max_metric_judge_model_calls"] == 200
    assert (baseline_args["mode"], baseline_args["dataset"]) == ("compile_live", _DATASET)
    # A dataset-bound baseline refuses to also carry a moving window, and `0` would read as one.
    assert baseline_args["from_ms"] is None and baseline_args["to_ms"] is None
    run_root = tmp_path / "run-1"
    assert sorted(path.name for path in run_root.iterdir()) == [
        "baseline-compile-live.json",
        "optimization",
        "readiness.json",
        "run_summary.json",
    ]
    # `NO_OP` is a complete, successful experiment — and it is not a candidate, so it is not exit 0.
    assert code == 1 and payload["ok"] is False
    summary = json.loads((run_root / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["optimization"]["terminal"] == "NO_OP"
    assert summary["next_action"] == "keep_stable"
    assert summary["run"]["artifacts"]["optimization"] == "optimization/optimization_report.json"


def test_a_corpus_readiness_already_refused_skips_the_baseline_and_still_ends_in_a_terminal_report(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A blocked plan costs nothing twice.

    `baseline --dataset` refuses a blocked plan outright, so running it would only convert a free answer
    into an error. `optimize` rebuilds the same plan and returns `REJECTED` before touching an endpoint,
    which is why the terminal in the summary is one the optimizer produced rather than one this command
    inferred from a readiness outcome.
    """

    legs = _Legs(
        readiness=_readiness(outcome="insufficient", blocking_reasons=["train_target_missing"], split=None),
        baseline=_baseline(),
        optimization=_optimization(
            outcome="REJECTED", reasons=["train_target_missing"], split=None, metric=None, trajectory=None
        ),
    )
    _install(monkeypatch, legs)

    code, payload = run_commands._handle_learning_run(_run_args(tmp_path), SimpleNamespace(), SimpleNamespace())

    assert [name for name, _args in legs.calls] == ["readiness", "optimize"]
    assert code == 1
    summary = payload["data"]
    assert summary["run"]["readiness_outcome"] == "insufficient"
    assert summary["run"]["baseline_executed"] is False
    assert summary["optimization"]["terminal"] == "REJECTED"
    assert not (tmp_path / "run-1" / "baseline-compile-live.json").exists()


def test_a_corpus_bound_that_cannot_cover_the_corpus_is_refused_before_the_first_provider_call(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Readiness has already counted the representatives, so the refusal is free rather than half-paid for.

    `baseline --dataset` refuses a truncated corpus too, but only after projecting it inside a command that
    has already started. Here the same refusal happens with the readiness report in hand and nothing spent.
    """

    legs = _Legs(readiness=_readiness(), baseline=_baseline(), optimization=_optimization())
    _install(monkeypatch, legs)

    with pytest.raises(ValueError, match=r"news_learning_run_baseline_budget_below_corpus:64<65"):
        run_commands._handle_learning_run(
            _run_args(tmp_path, max_baseline_model_cases=64), SimpleNamespace(), SimpleNamespace()
        )

    assert [name for name, _args in legs.calls] == ["readiness"]


def test_a_population_mismatch_writes_the_evidence_and_still_refuses_to_call_it_a_comparison(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Fail closed, with the diagnosis on disk. Withholding the summary would withhold the diagnosis too."""

    drifted = _baseline()
    drifted["objective"]["optimizer_case_root_sha256"] = "f" * 64
    legs = _Legs(readiness=_readiness(), baseline=drifted, optimization=_optimization())
    _install(monkeypatch, legs)

    code, payload = run_commands._handle_learning_run(_run_args(tmp_path), SimpleNamespace(), SimpleNamespace())

    assert code == 2 and payload["ok"] is False
    assert payload["error"]["code"] == "news_learning_run_population_identity_mismatch"
    assert payload["error"]["mismatched_checks"] == ["optimizer_case_root_sha256"]
    summary = json.loads((tmp_path / "run-1" / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["baseline"]["same_population"] is False


def test_an_advance_is_the_only_zero_exit_and_names_the_candidate_file_it_wrote(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """`ADVANCE` means "worth testing on future examples", which is the one outcome that has a next step."""

    legs = _Legs(
        readiness=_readiness(),
        baseline=_baseline(),
        optimization=_optimization(outcome="ADVANCE", reasons=[], candidate_sha256="7" * 64),
    )
    _install(monkeypatch, legs)
    run_root = tmp_path / "run-1"

    def with_candidate(args: Namespace, settings: Any, stable: Any) -> tuple[int, dict[str, Any]]:
        result = legs.on_optimize(args, settings, stable)
        _write(Path(args.out) / "prompt_candidate.json", {"schema_version": "news_prompt_candidate_v1"})
        return result

    monkeypatch.setattr(
        "tracefold.app.cli.commands.news_learning_experiment.handle_research",
        with_candidate,
    )

    code, payload = run_commands._handle_learning_run(_run_args(tmp_path), SimpleNamespace(), SimpleNamespace())

    assert code == 0 and payload["ok"] is True
    summary = json.loads((run_root / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["next_action"] == "future_test"
    assert summary["run"]["artifacts"]["prompt_candidate"] == "optimization/prompt_candidate.json"
    # An `ADVANCE` is a proposal. Nothing here registers, accepts, promotes or deploys it.
    assert summary["optimization"]["candidate_sha"] == "7" * 64


# --- one real run, reconciled ----------------------------------------------------------------------


def test_a_real_baseline_and_a_real_optimization_over_one_corpus_reconcile_field_for_field() -> None:
    """The test the hand-built fixtures above cannot be: every path read from artifacts nobody typed.

    A summary is only worth reading if its field addresses are the ones the two reports actually publish,
    and if two runs over one frozen corpus really do agree on dataset, projection, representative set,
    split, ruler and Program. Both objects here are produced by the real builders — `run_baseline` in the
    `compile_live` mode `learning run` uses, and `optimize()` over the same episodes with the same judge —
    so a renamed field or a drifted receipt fails here rather than in an operator's run directory.
    """

    import dspy

    from tracefold.news.artifact_identity import canonical_sha
    from tracefold.news.learning.baseline import BaselineCase, compile_program_factory, run_baseline
    from tracefold.news.learning.contracts import DevelopmentDatasetRef
    from tracefold.news.learning.objective import build_gepa_objective_plan
    from tracefold.news.learning.optimizer import FrozenDevelopmentDataset, OptimizationConfig, optimize
    from tracefold.news.program.artifact import load_stable_program_artifact

    from .test_news_baseline_modes import _ScriptedLM
    from .test_news_gepa_core import _episodes as _corpus
    from .test_news_gepa_core import _FakeGEPA, _MeteredFakeLM
    from .test_news_learning_optimize import _budget, _StampedJudge

    corpus = _corpus()
    payload = {"role": "development", "learning_epoch": "program_v7", "cases": []}
    dataset_sha = canonical_sha({"kind": "dataset", "payload": payload})
    episode_root = canonical_sha([episode.model_dump(mode="json") for episode in corpus])
    plan = build_gepa_objective_plan(corpus)
    artifact = load_stable_program_artifact()
    # One judge object for both legs, which is exactly what `learning run` arranges by taking the model
    # from `llm.news_compiler_reflection` instead of from a flag on each command.
    judge = _StampedJudge()

    baseline = run_baseline(
        tuple(BaselineCase(episode=episode) for episode in plan.optimizer_episodes),
        mode="compile_live",
        artifact=artifact,
        program_factory=compile_program_factory,
        lm=_ScriptedLM(break_card=False),
        judge=judge,
        objective=plan,
        cohort_scope="frozen_development",
        dataset_identity={
            "development_dataset_sha": dataset_sha,
            "episode_projection_root_sha256": episode_root,
            "episode_count": len(corpus),
            "scored_population": "objective_plan_target_and_control",
        },
        retrieval_population=corpus,
        runtime_identity={
            # The same endpoint the optimizer runs on, fingerprinted the way `compile_live` fingerprints it.
            "compile_task_model": "task/model",
            "compile_task_endpoint_sha256": _TASK_ROUTE["baseline_endpoint_sha256"],
        },
    )
    dataset = FrozenDevelopmentDataset.bind(
        ref=DevelopmentDatasetRef(
            development_dataset_sha256=dataset_sha,
            episode_projection_root_sha256=episode_root,
            episode_count=len(corpus),
            learning_epoch_started_at_ms=1_787_549_907_739,
            review_rubric_version="news_review_v4",
        ),
        episodes=corpus,
        dataset_payload=payload,
        target_runtime_manifest_sha256="a" * 64,
    )
    task_lm = _MeteredFakeLM("task/model", cost=0.000002)
    with dspy.context(lm=task_lm):
        result = optimize(
            dataset,
            OptimizationConfig(
                task_lm=task_lm,
                reflection_lm=_MeteredFakeLM("reflection/model", cost=0.000003, role="reflection"),
                judge=judge,
                budget=_budget(),
                optimizer_factory=_FakeGEPA,
                now_ms=lambda: 1_800_000_123_456,
                monotonic=lambda: 0.0,
            ),
        )
    optimization = result.report.model_dump(mode="json")
    # The task endpoint fingerprint the optimizer stamped. `learning run` computes both digests from the
    # endpoint it composed; the summary checks each report against its own digest of that one host.
    route = {
        "model": str(optimization["model_identities"]["task"]["model"]),
        "baseline_endpoint_sha256": _TASK_ROUTE["baseline_endpoint_sha256"],
        "optimizer_endpoint_fingerprint": str(optimization["model_identities"]["task"]["endpoint_fingerprint"]),
    }

    summary = build_run_summary(
        development_dataset_sha=dataset_sha,
        readiness=_real_readiness(plan, dataset_sha=dataset_sha, episode_root=episode_root, corpus=corpus),
        baseline={**baseline.model_dump(mode="json"), "report_sha256": baseline.report_sha256},
        optimization=optimization,
        task_route=route,
        artifacts={"readiness": "readiness.json", "baseline": "baseline-compile-live.json"},
    )

    assert summary["baseline"]["same_population"] is True, [
        check for check in summary["baseline"]["population_checks"] if check["status"] != "match"
    ]
    # Not the same object read twice: one is the report's own subset mean, the other is trajectory index 0.
    assert (
        summary["baseline"]["standalone_selection_score"]
        == (baseline.subsets["development_selection"]["case_macro_failure_as_zero"])
    )
    assert summary["baseline"]["gepa_seed_selection_score"] == optimization["trajectory"]["val_aggregate_scores"][0]
    assert summary["dataset"]["optimizer_representative_n"] == len(plan.optimizer_case_ids)
    assert summary["dataset"]["optimizer_representative_n"] == summary["dataset"]["optimizer_cluster_n"]
    assert summary["dataset"]["train_root"] == plan.split["train"]["case_root_sha256"]
    assert summary["dataset"]["selection_root"] == plan.split["development_selection"]["case_root_sha256"]
    assert summary["optimization"]["terminal"] == result.outcome


def _real_readiness(plan: Any, *, dataset_sha: str, episode_root: str, corpus: Any) -> dict[str, Any]:
    """The readiness report the CLI would have written for this plan, built by its own builder."""

    from tracefold.news.learning.objective import build_readiness_report
    from tracefold.news.program.artifact import load_stable_program_artifact

    return build_readiness_report(
        plan,
        episodes=corpus,
        identity={
            "development_dataset_sha": dataset_sha,
            "episode_projection_root_sha256": episode_root,
            "episode_count": len(corpus),
            "program_sha256": load_stable_program_artifact().program_sha256,
        },
    )


def test_the_baseline_judge_ceiling_is_what_makes_the_two_metric_receipts_one_ruler() -> None:
    """Why `learning run` hands the optimizer's judge ceiling to the baseline as well.

    The ruler both legs are scored by is `compile_metric_receipt.v3`, and the equivalence judge's complete
    role contract — including its own admission ceiling — is inside it. Left unbounded on one side, the two
    receipts hash differently and the population check has to be weakened by an exclusion; given the same
    ceiling they are byte-identical, and the check can compare the whole document.
    """

    from tracefold.news.learning.baseline import build_judge
    from tracefold.news.learning.metric import bind_metric, metric_receipt

    def receipt(*, max_model_calls: int | None) -> str:
        judge = build_judge(
            model_name="deepseek/deepseek-v4-pro",
            api_key="k",
            api_base="https://judge.invalid/v1",
            max_model_calls=max_model_calls,
        )
        return json.dumps(metric_receipt(bind_metric(judge), review_rubric_version="news_review_v4"), sort_keys=True)

    assert receipt(max_model_calls=200) == receipt(max_model_calls=200)
    assert receipt(max_model_calls=200) != receipt(max_model_calls=None)


def test_reusing_a_run_directory_never_leaves_a_previous_baseline_beside_a_current_summary(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The stale pair the population checks could not catch, because both files would look current.

    A directory is the record of one run. The second run here is over a corpus readiness refuses, so it
    never produces a baseline — and the first run's baseline must not be left behind to be read as this
    run's `before` number.
    """

    first = _Legs(readiness=_readiness(), baseline=_baseline(), optimization=_optimization())
    _install(monkeypatch, first)
    run_commands._handle_learning_run(_run_args(tmp_path), SimpleNamespace(), SimpleNamespace())
    assert (tmp_path / "run-1" / "baseline-compile-live.json").exists()

    second = _Legs(
        readiness=_readiness(outcome="insufficient", blocking_reasons=["train_target_missing"], split=None),
        baseline=_baseline(),
        optimization=_optimization(outcome="REJECTED", reasons=["train_target_missing"], trajectory=None),
    )
    _install(monkeypatch, second)
    run_commands._handle_learning_run(_run_args(tmp_path), SimpleNamespace(), SimpleNamespace())

    assert not (tmp_path / "run-1" / "baseline-compile-live.json").exists()
    summary = json.loads((tmp_path / "run-1" / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["run"]["baseline_executed"] is False
    assert summary["baseline"]["standalone_selection_score"] is None


def test_the_terminal_summary_keeps_the_check_table_in_the_file_and_the_numbers_on_screen(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The same convention `readiness` uses: the evidence is in the file, the answer is on stdout."""

    legs = _Legs(readiness=_readiness(), baseline=_baseline(), optimization=_optimization())
    _install(monkeypatch, legs)

    _code, payload = run_commands._handle_learning_run(_run_args(tmp_path), SimpleNamespace(), SimpleNamespace())

    printed = payload["data"]["baseline"]
    assert "population_checks" not in printed
    assert printed["population_checks_written_to"].endswith("run_summary.json")
    assert (printed["standalone_selection_score"], printed["gepa_seed_selection_score"]) == (0.412, 0.437)
    on_disk = json.loads((tmp_path / "run-1" / "run_summary.json").read_text(encoding="utf-8"))
    assert len(on_disk["baseline"]["population_checks"]) == 12
