"""#253: the one recommended GEPA path, composed in one process.

The interesting behaviour is not that three commands run in order — it is what the composition can now
refuse that three hand-typed commands could not: two different judges, and a corpus bound that does not
cover the corpus. The separate comparability summary was deleted in #343: one process, one dataset SHA
and one configured judge route make same-population true by construction.
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

_DATASET = "d" * 64
_EPISODE_ROOT = "e" * 64
_OPTIMIZER_ROOT = "c" * 64
_TRAIN_ROOT = "1" * 64
_SELECTION_ROOT = "2" * 64
_PROGRAM = "9" * 64
_BASELINE_REPORT = "b" * 64
_OPTIMIZATION_REPORT = "a" * 64
_METRIC = {"schema": "tracefold.news.compile_metric_receipt.v4", "metric_id": "m5", "weights": {"final_action": 0.45}}


# The coverage block the readiness report below publishes, agreeing with its own `corpus` counts and with
# the rule `_dataset_counts` applies: every cluster is boundary *or* retention, so the two sum to
# `independent_cluster_n`. What this fixture is for is the forwarding, so the numbers are a realistic
# corpus rather than a passing one — and its `natural_day_n` of 1 over a 21 h window is the shape #259
# stops refusing.
_COVERAGE = {
    "case_n": 84,
    "independent_cluster_n": 71,
    "boundary_cluster_n": 21,
    "retention_cluster_n": 50,
    "negative_cluster_n": 26,
    "safety_cluster_n": 7,
    "stratum_n": 4,
    "eligible_event_n": 612,
    "natural_day_n": 1,
    "window_duration_hours": 21.0,
}


def _readiness(**updates: Any) -> dict[str, Any]:
    report = {
        "schema": "tracefold.news.gepa_readiness_report.v2",
        "outcome": "ready",
        "blocking_reasons": [],
        "identity": {
            "development_dataset_sha": _DATASET,
            "episode_projection_root_sha256": _EPISODE_ROOT,
            "episode_count": 84,
            "program_sha256": _PROGRAM,
        },
        "coverage": dict(_COVERAGE),
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
    """A minimal stand-in: `run` only checks the leg ran and re-reads the file it wrote.

    Nothing in `news_learning_run.py` reads the report's fields any more (#343 deleted the
    comparability summary), so a fuller fake would drift from `program_baseline_report.v3`
    unnoticed while implying cross-checks that no longer exist.
    """

    report: dict[str, Any] = {
        "schema_id": "tracefold.news.program_baseline_report.v3",
        "report_sha256": _BASELINE_REPORT,
        "mode": "compile_live",
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
            "learning_epoch": "bundle_00000000",
            "review_rubric_version": "news_review_v6",
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
        "model_identities": {"task": {"model": "qwen3-30b", "endpoint_fingerprint": "5" * 64}},
        "trajectory": {"val_aggregate_scores": [0.437, 0.401], "best_idx": 0, "num_full_val_evals": 2},
        "usage": {
            "metric_calls": 96,
            "task_model_calls": 192,
            "reflection_model_calls": 7,
            "metric_judge_model_calls": 88,
            "metric_judge_attempts": 88,
            "metric_judge_failures": 0,
            "actual_cost_microusd": 412_000,
            "metric_judge_cost_imputed": True,
            "transport_failures": 1,
            "transport_retries": 1,
        },
    }
    report.update(updates)
    return copy.deepcopy(report)


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


def test_the_three_legs_run_in_order_into_one_directory_and_share_one_judge(monkeypatch: Any, tmp_path: Path) -> None:
    """The composition an operator used to do by hand, with the two agreements it could not check.

    The judge model is the configured reflection route rather than a name retyped per command, which is
    what makes the ruler the two Stable numbers were measured with the same one. The judge's own call
    ceiling is deliberately *not* forwarded: an under-sized one would score cases zero rather than raise.
    """

    legs = _Legs(readiness=_readiness(), baseline=_baseline(), optimization=_optimization())
    _install(monkeypatch, legs)

    code, payload = run_commands._handle_learning_run(_run_args(tmp_path), SimpleNamespace(), SimpleNamespace())

    assert [name for name, _args in legs.calls] == ["readiness", "baseline", "optimize"]
    baseline_args = dict(legs.calls[1][1]._get_kwargs())
    assert baseline_args["semantic_judge"] == "deepseek-v4-pro"
    assert "max_metric_judge_model_calls" not in baseline_args
    assert (baseline_args["mode"], baseline_args["dataset"]) == ("compile_live", _DATASET)
    # A dataset-bound baseline refuses to also carry a moving window, and `0` would read as one.
    assert baseline_args["from_ms"] is None and baseline_args["to_ms"] is None
    run_root = tmp_path / "run-1"
    assert sorted(path.name for path in run_root.iterdir()) == [
        "baseline-compile-live.json",
        "optimization",
        "readiness.json",
    ]
    # `NO_OP` is a complete, successful experiment — and it is not a candidate, so it is not exit 0.
    assert code == 1 and payload["ok"] is False
    assert payload["data"]["outcome"] == "NO_OP"
    assert payload["data"]["optimization"].endswith("optimization/optimization_report.json")


def test_a_corpus_readiness_already_refused_skips_the_baseline_and_still_ends_in_a_terminal_report(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A blocked plan costs nothing twice.

    `baseline --dataset` refuses a blocked plan outright, so running it would only convert a free answer
    into an error. `optimize` rebuilds the same plan and returns `REJECTED` before touching an endpoint,
    which is why the terminal outcome is one the optimizer produced rather than one this command inferred
    from a readiness outcome.
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
    assert payload["data"]["outcome"] == "REJECTED"
    assert payload["data"]["baseline"] is None
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
    assert payload["data"]["outcome"] == "ADVANCE"
    # An `ADVANCE` is a proposal. Nothing here registers, accepts, promotes or deploys it.
    assert payload["data"]["prompt_candidate"] == str(run_root / "optimization" / "prompt_candidate.json")


def test_reusing_a_run_directory_never_leaves_a_previous_baseline_beside_a_current_run(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A directory is the record of one run.

    The second run here is over a corpus readiness refuses, so it never produces a baseline — and the
    first run's baseline must not be left behind to be read as this run's `before` number.
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
    assert second.calls[0][0] == "readiness"


def test_an_aborted_run_leaves_no_artifact_of_the_previous_one_in_the_directory(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The stale pair with no summary to reveal it.

    A run that stops between the clear and the optimization — here on the corpus bound — must not leave a
    fresh `readiness.json` sitting beside the last run's report and candidate. Every artifact of the
    previous run goes first, so a half-finished directory is visibly half-finished.
    """

    first = _Legs(
        readiness=_readiness(),
        baseline=_baseline(),
        optimization=_optimization(outcome="ADVANCE", reasons=[], candidate_sha256="7" * 64),
    )
    _install(monkeypatch, first)

    def with_candidate(args: Namespace, settings: Any, stable: Any) -> tuple[int, dict[str, Any]]:
        result = first.on_optimize(args, settings, stable)
        _write(Path(args.out) / "prompt_candidate.json", {"schema_version": "news_prompt_candidate_v1"})
        return result

    monkeypatch.setattr(
        "tracefold.app.cli.commands.news_learning_experiment.handle_research",
        with_candidate,
    )
    run_commands._handle_learning_run(_run_args(tmp_path), SimpleNamespace(), SimpleNamespace())
    run_root = tmp_path / "run-1"
    assert (run_root / "optimization" / "prompt_candidate.json").exists()

    second = _Legs(readiness=_readiness(), baseline=_baseline(), optimization=_optimization())
    _install(monkeypatch, second)
    with pytest.raises(ValueError, match="news_learning_run_baseline_budget_below_corpus"):
        run_commands._handle_learning_run(
            _run_args(tmp_path, max_baseline_model_cases=64), SimpleNamespace(), SimpleNamespace()
        )

    assert (run_root / "readiness.json").exists()
    for cleared in ("baseline-compile-live.json",):
        assert not (run_root / cleared).exists(), cleared
    for cleared in ("optimization_report.json", "prompt_candidate.json"):
        assert not (run_root / "optimization" / cleared).exists(), cleared


def test_a_composed_handler_refusal_keeps_its_own_code_instead_of_a_dict_repr(monkeypatch: Any, tmp_path: Path) -> None:
    """`baseline` answers an empty optimizer corpus with a code *and* its blocking reasons.

    Stringifying that mapping put a Python dict repr where every other refusal in this plane puts a stable
    code an operator can grep for.
    """

    legs = _Legs(readiness=_readiness(), baseline=_baseline(), optimization=_optimization())
    _install(monkeypatch, legs)

    def refuse(_args: Namespace, _settings: Any, _stable: Any) -> tuple[int, dict[str, Any]]:
        return 2, {
            "ok": False,
            "error": {
                "code": "news_program_baseline_dataset_has_no_optimizer_corpus",
                "blocking_reasons": ["train_target_missing"],
            },
        }

    monkeypatch.setattr(run_commands, "_handle_learning_baseline", refuse)

    with pytest.raises(ValueError, match=r"^news_program_baseline_dataset_has_no_optimizer_corpus$"):
        run_commands._handle_learning_run(_run_args(tmp_path), SimpleNamespace(), SimpleNamespace())


def test_an_out_path_naming_a_directory_that_does_not_exist_yet_is_written_not_refused(
    tmp_path: Path,
) -> None:
    """`freeze --out DIR/development.json` runs before `run` creates `DIR`, and seals the corpus first.

    A missing parent used to be an `OSError` — outside the `(ValueError, PermissionError, RuntimeError)`
    the CLI translates — raised after the dataset was already persisted, so the operator lost the SHA to a
    traceback with no way to recover it but to freeze again.
    """

    from tracefold.app.cli.commands.news_learning_documents import _write_json

    target = tmp_path / "run-1" / "development.json"
    _write_json(str(target), {"artifact_sha": "d" * 64})

    assert json.loads(target.read_text(encoding="utf-8")) == {"artifact_sha": "d" * 64}
