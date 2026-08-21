from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tracefold.app import learning_runtime
from tracefold.app.cli.commands import news as news_commands
from tracefold.app.cli.commands.news import _handle_learning, _learning_program_judges
from tracefold.app.cli.parser import build_parser
from tracefold.news.agents.programs import candidates as candidate_programs
from tracefold.news.agents.semantic_program import load_stable_program_artifact


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


def test_canary_catalog_isolates_a_malformed_compiled_document(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        candidate_programs,
        "COMPILED_CANDIDATE_DOCUMENTS",
        ({"target": "program", "candidate_arm": {"program_sha256": "bad"}},),
    )

    assert candidate_programs.compiled_canary_candidates() == {}


def test_emergency_canary_trip_does_not_load_stable_or_parse_candidate_catalog(monkeypatch: Any) -> None:
    activation_id = "1" * 32

    class _News:
        def __init__(self) -> None:
            self.state = "active"

        def canary_status(self) -> dict[str, Any]:
            return {
                "state": self.state,
                "activation": {"activation_id": activation_id},
                "assignments": {"stable": 0, "candidate": 0},
            }

        def transition_canary(self, **kwargs: Any) -> bool:
            assert kwargs["activation_id"] == activation_id
            assert kwargs["target_state"] == "tripped"
            self.state = "tripped"
            return True

    class _Repos:
        def __init__(self) -> None:
            self.news = _News()

        @contextmanager
        def transaction(self):
            yield

    repos = _Repos()

    @contextmanager
    def fake_repositories(_settings: Any):
        yield repos

    def unexpected_stable(_settings: Any):
        raise AssertionError("emergency rollback must not load the Program catalog")

    monkeypatch.setattr(news_commands, "load_settings", lambda **_kwargs: object())
    monkeypatch.setattr(learning_runtime, "active_arm_manifest", unexpected_stable)
    monkeypatch.setattr("tracefold.app.repositories.repositories", fake_repositories)
    monkeypatch.setattr(
        candidate_programs,
        "COMPILED_CANDIDATE_DOCUMENTS",
        ({"target": "program", "candidate_arm": {"program_sha256": "bad"}},),
    )

    code, payload = _handle_learning(
        SimpleNamespace(
            learning_command="canary",
            canary_command="trip",
            candidate=None,
            activation=activation_id,
            reason="operator_rollback",
        )
    )

    assert code == 0
    assert payload["data"]["state"] == "tripped"


def test_policy_candidate_gets_arm_local_program_adapter_and_breaker_state() -> None:
    class _Rows:
        @staticmethod
        def fetchall() -> list[object]:
            return []

    class _Connection:
        @staticmethod
        def execute(query: str) -> _Rows:
            assert "news_model_recordings" in query
            return _Rows()

    artifact = load_stable_program_artifact()
    stable = SimpleNamespace(program_sha256=artifact.program_sha256, bundle_sha="1" * 64)
    candidate_arm = SimpleNamespace(program_sha256=artifact.program_sha256, bundle_sha="2" * 64)
    candidate = SimpleNamespace(candidate_arm=candidate_arm)

    judges = _learning_program_judges(
        _Connection(),
        settings=None,
        stable=stable,
        candidate=candidate,
        artifact_paths={},
        live=False,
    )
    stable_judge = judges[("stable", stable.bundle_sha)]
    candidate_judge = judges[("candidate", candidate_arm.bundle_sha)]

    assert stable_judge is not candidate_judge
    assert stable_judge.primary_adapter is not candidate_judge.primary_adapter
    assert stable_judge.primary_adapter.requests is not candidate_judge.primary_adapter.requests
    for _ in range(artifact.execution.primary_breaker_failures):
        stable_judge._record_primary_failure()
    assert stable_judge._primary_open_until > 0
    assert candidate_judge._primary_open_until == 0
