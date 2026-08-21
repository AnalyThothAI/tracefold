from __future__ import annotations

from pathlib import Path

import pytest

from tracefold.app.cli.parser import build_parser


def test_learning_compile_requires_all_three_budgets_and_seed() -> None:
    args = build_parser().parse_args(
        [
            "news",
            "learning",
            "compile",
            "--development",
            "d" * 64,
            "--artifact-root",
            "programs",
            "--out",
            "proposal.json",
            "--max-metric-calls",
            "30",
            "--max-task-model-calls",
            "90",
            "--max-cost-microusd",
            "500000",
            "--seed",
            "17",
        ]
    )

    assert args.learning_command == "compile"
    assert (args.max_metric_calls, args.max_task_model_calls, args.max_cost_microusd, args.seed) == (
        30,
        90,
        500000,
        17,
    )


def test_learning_evaluation_exposes_program_live_opt_in_not_legacy_model_flag() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "news",
            "learning",
            "evaluate",
            "--development",
            "d" * 64,
            "--candidate",
            "candidate.json",
            "--live-program",
            "--out",
            "report.json",
        ]
    )

    assert args.live_program is True
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "news",
                "learning",
                "evaluate",
                "--development",
                "d" * 64,
                "--candidate",
                "candidate.json",
                "--live-model",
                "--out",
                "report.json",
            ]
        )


def test_learning_cli_has_no_prompt_or_legacy_model_adapter_path() -> None:
    source = (Path(__file__).resolve().parents[2] / "src/tracefold/app/cli/commands/news.py").read_text(
        encoding="utf-8"
    )

    assert 'target == "prompt"' not in source
    assert "RecordReplayModelAdapter" not in source
    assert "LiveTriageModelAdapter" not in source
    assert "configured_chat_model" not in source
