"""The one public candidate path: zero-call readiness followed by one optimization."""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tracefold.app.cli.commands import news_learning_run as run_commands
from tracefold.app.cli.parser import build_parser

_DATASET = "d" * 64


def _readiness(**updates: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema": "tracefold.news.gepa_readiness_report.v3",
        "objective": {"compilable": True, "blockers": []},
        "development_profile": {"ready": True, "blockers": []},
    }
    report.update(updates)
    return report


def _optimization(**updates: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema_version": "news_optimization_run_report_v3",
        "outcome": "NO_OP",
        "reasons": ["news_program_compile_no_program_change"],
        "candidate_sha256": None,
    }
    report.update(updates)
    return report


def _run_args(tmp_path: Path, **updates: Any) -> Namespace:
    values: dict[str, Any] = {
        "development": _DATASET,
        "out": str(tmp_path / "run-1"),
        "max_metric_calls": 120,
        "max_task_model_calls": 400,
        "max_reflection_model_calls": 40,
        "max_cost_microusd": 800_000,
        "max_call_cost_microusd": 5_000,
        "max_wall_clock_seconds": 14_400,
        "seed": 129,
    }
    values.update(updates)
    return Namespace(**values)


class _Legs:
    def __init__(self, *, readiness: dict[str, Any], optimization: dict[str, Any]) -> None:
        self.readiness_report = readiness
        self.optimization_report = optimization
        self.calls: list[tuple[str, Namespace]] = []

    def on_readiness(self, args: Namespace, _settings: Any, _stable: Any) -> tuple[int, dict[str, Any]]:
        self.calls.append(("readiness", args))
        _write(Path(args.out), self.readiness_report)
        return 0, {"ok": True, "data": {}}

    def on_optimize(self, args: Namespace, _settings: Any, _stable: Any) -> tuple[int, dict[str, Any]]:
        self.calls.append(("optimize", args))
        directory = Path(args.out)
        directory.mkdir(parents=True, exist_ok=True)
        _write(directory / "optimization_report.json", self.optimization_report)
        code = 0 if self.optimization_report["outcome"] == "ADVANCE" else 1
        return code, {"ok": code == 0, "data": {}}


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _install(monkeypatch: Any, legs: _Legs) -> None:
    monkeypatch.setattr(run_commands, "_handle_learning_readiness", legs.on_readiness)
    monkeypatch.setattr(
        "tracefold.app.cli.commands.news_learning_experiment.execute_optimization",
        legs.on_optimize,
    )


def test_run_parser_is_the_only_candidate_route_and_keeps_explicit_budgets() -> None:
    args = build_parser().parse_args(
        [
            "news", "learning", "run",
            "--development", _DATASET,
            "--out", "artifacts/run-1",
            "--max-metric-calls", "120",
            "--max-task-model-calls", "400",
            "--max-reflection-model-calls", "40",
            "--max-cost-microusd", "800000",
            "--max-call-cost-microusd", "5000",
        ]
    )  # fmt: skip

    assert args.learning_command == "run"
    assert args.seed == 129
    assert not hasattr(args, "max_metric_judge_model_calls")
    for absent in ("semantic_judge", "dataset", "mode", "max_baseline_model_cases"):
        assert not hasattr(args, absent), absent


def test_readiness_then_one_optimization_write_the_only_run_artifacts(monkeypatch: Any, tmp_path: Path) -> None:
    legs = _Legs(readiness=_readiness(), optimization=_optimization())
    _install(monkeypatch, legs)

    code, payload = run_commands._handle_learning_run(_run_args(tmp_path), SimpleNamespace(), SimpleNamespace())

    assert [name for name, _args in legs.calls] == ["readiness", "optimize"]
    assert code == 1 and payload["ok"] is False
    assert sorted(path.name for path in (tmp_path / "run-1").iterdir()) == ["optimization", "readiness.json"]
    assert payload["data"]["outcome"] == "NO_OP"
    assert "baseline" not in payload["data"]


def test_insufficient_readiness_refuses_before_the_optimizer_leg(monkeypatch: Any, tmp_path: Path) -> None:
    legs = _Legs(
        readiness=_readiness(
            objective={"compilable": True, "blockers": []},
            development_profile={"ready": False, "blockers": ["development_calibration_missing"]},
        ),
        optimization=_optimization(outcome="REJECTED", reasons=["train_target_missing"]),
    )
    _install(monkeypatch, legs)

    with pytest.raises(ValueError, match="news_learning_run_readiness_blocked:development_calibration_missing"):
        run_commands._handle_learning_run(_run_args(tmp_path), SimpleNamespace(), SimpleNamespace())

    assert [name for name, _args in legs.calls] == ["readiness"]


def test_advance_is_the_only_zero_exit_and_names_its_candidate(monkeypatch: Any, tmp_path: Path) -> None:
    legs = _Legs(readiness=_readiness(), optimization=_optimization(outcome="ADVANCE", reasons=[]))
    _install(monkeypatch, legs)

    def with_candidate(args: Namespace, settings: Any, stable: Any) -> tuple[int, dict[str, Any]]:
        result = legs.on_optimize(args, settings, stable)
        _write(Path(args.out) / "prompt_candidate.json", {"schema_version": "news_prompt_candidate_v2"})
        return result

    monkeypatch.setattr(
        "tracefold.app.cli.commands.news_learning_experiment.execute_optimization",
        with_candidate,
    )

    code, payload = run_commands._handle_learning_run(_run_args(tmp_path), SimpleNamespace(), SimpleNamespace())

    assert code == 0 and payload["ok"] is True
    assert payload["data"]["prompt_candidate"].endswith("optimization/prompt_candidate.json")


def test_nonempty_run_directory_is_refused_before_readiness(monkeypatch: Any, tmp_path: Path) -> None:
    legs = _Legs(readiness=_readiness(), optimization=_optimization())
    _install(monkeypatch, legs)
    out = tmp_path / "run-1"
    out.mkdir()
    (out / "foreign.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="news_learning_run_out_must_be_new_empty_directory"):
        run_commands._handle_learning_run(_run_args(tmp_path), SimpleNamespace(), SimpleNamespace())

    assert legs.calls == []


def test_an_existing_empty_run_directory_is_valid(monkeypatch: Any, tmp_path: Path) -> None:
    legs = _Legs(readiness=_readiness(), optimization=_optimization())
    _install(monkeypatch, legs)
    (tmp_path / "run-1").mkdir()

    run_commands._handle_learning_run(_run_args(tmp_path), SimpleNamespace(), SimpleNamespace())

    assert [name for name, _args in legs.calls] == ["readiness", "optimize"]
