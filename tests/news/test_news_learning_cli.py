from __future__ import annotations

from contextlib import contextmanager
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
            "--compiler-image",
            "sha256:" + "1" * 64,
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
    assert args.compiler_image == "sha256:" + "1" * 64
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


def test_learning_recording_verification_is_explicit_and_cannot_use_live_program() -> None:
    parser = build_parser()
    base = [
        "news",
        "learning",
        "evaluate",
        "--development",
        "d" * 64,
        "--candidate",
        "candidate.json",
        "--verify-recordings",
        "--out",
        "report.json",
    ]

    args = parser.parse_args(base)

    assert args.verify_recordings is True
    assert args.live_program is False
    with pytest.raises(SystemExit):
        parser.parse_args([*base[:-2], "--live-program", *base[-2:]])


def test_learning_recording_verification_is_not_exposed_by_shadow_and_rejects_canary() -> None:
    parser = build_parser()
    shared = [
        "--development",
        "d" * 64,
        "--validation",
        "v" * 64,
        "--candidate",
        "candidate.json",
        "--verify-recordings",
        "--out",
        "report.json",
    ]

    with pytest.raises(SystemExit):
        parser.parse_args(["news", "learning", "shadow", *shared])

    args = parser.parse_args(["news", "learning", "evaluate", "--stage", "canary", *shared])
    assert args.verify_recordings is True


def test_learning_recording_verification_fails_closed_for_canary_before_loading_replay(
    monkeypatch: Any,
) -> None:
    stable = SimpleNamespace(bundle_sha="1" * 64)
    candidate = SimpleNamespace(candidate_sha="2" * 64)

    @contextmanager
    def fake_postgres_connection(_settings: Any, *, role: str):
        assert role == "workers"
        yield object()

    monkeypatch.setattr(news_commands, "load_settings", lambda **_kwargs: object())
    monkeypatch.setattr(learning_runtime, "active_arm_manifest", lambda _settings: stable)
    monkeypatch.setattr("tracefold.app.repositories.postgres_connection", fake_postgres_connection)
    monkeypatch.setattr(news_commands, "_load_candidate_bundle", lambda _path: (candidate, {}))

    code, payload = _handle_learning(
        SimpleNamespace(
            learning_command="evaluate",
            development="d" * 64,
            validation="v" * 64,
            candidate="candidate.json",
            stage="canary",
            observation_manifest="",
            live_program=False,
            verify_recordings=True,
            out="report.json",
        )
    )

    assert code == 2
    assert payload["error"] == "news_learning_recording_verification_stage_unsupported:canary"


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


def _baseline_args(**updates: Any) -> SimpleNamespace:
    args = {
        "learning_command": "baseline",
        "from_ms": 1,
        "to_ms": 2,
        "mode": "recorded",
        "action_source": "",
        "all_cohorts": False,
        "max_model_cases": 0,
        "semantic_judge": "",
        "limit": 10,
        "out": "",
    }
    args.update(updates)
    return SimpleNamespace(**args)


@pytest.mark.parametrize(
    ("mode", "action_source", "error"),
    [
        ("recorded", "policy", "news_program_baseline_recorded_mode_requires_recorded_action"),
        ("compile_live", "recorded", "news_program_baseline_live_mode_requires_policy_action"),
        ("runtime_live", "recorded", "news_program_baseline_live_mode_requires_policy_action"),
    ],
)
def test_baseline_refuses_a_mode_and_action_source_that_measure_nothing(
    monkeypatch: Any, mode: str, action_source: str, error: str
) -> None:
    """Both directions are wrong, and the second one is the dangerous one because it looks like it works.

    `recorded_action` short-circuits `_production_action`, so a live mode with `--action-source recorded`
    would generate a fresh verdict and score it against the action a *different* verdict shipped. The metric's
    heaviest component (0.50) would be measuring nothing while every `action` in the report read as real.
    """

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the guard must fail before the corpus is read")

    monkeypatch.setattr(news_commands, "load_settings", lambda **_kwargs: object())
    monkeypatch.setattr(learning_runtime, "active_arm_manifest", lambda _settings: SimpleNamespace())
    monkeypatch.setattr("tracefold.app.repositories.postgres_connection", refuse)

    code, payload = _handle_learning(_baseline_args(mode=mode, action_source=action_source))
    assert code == 2
    assert payload["error"] == error


def test_baseline_defaults_each_mode_to_its_only_valid_action_source() -> None:
    parser = build_parser()
    for mode in ("recorded", "compile_live", "runtime_live"):
        args = parser.parse_args(["news", "learning", "baseline", "--from-ms", "1", "--to-ms", "2", "--mode", mode])
        assert args.action_source == "", "the handler resolves the default, so the parser must not guess"
    with pytest.raises(SystemExit):
        parser.parse_args(["news", "learning", "baseline", "--from-ms", "1", "--to-ms", "2", "--mode", "live"])


def test_a_live_baseline_refuses_to_run_without_an_explicit_provider_bound(monkeypatch: Any) -> None:
    """`runtime_live` spends 2-6 real calls per case, sequentially, on the endpoints that also serve
    production Triage. Every other model-spending command in this plane makes its budget required; this one
    defaulted `--limit` to 500, so the unbounded form was the *shortest* form to type."""

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the bound must be checked before the corpus is read")

    monkeypatch.setattr(news_commands, "load_settings", lambda **_kwargs: object())
    monkeypatch.setattr(learning_runtime, "active_arm_manifest", lambda _settings: SimpleNamespace())
    monkeypatch.setattr("tracefold.app.repositories.postgres_connection", refuse)

    for mode in ("compile_live", "runtime_live"):
        code, payload = _handle_learning(_baseline_args(mode=mode, action_source="policy"))
        assert code == 2
        assert payload["error"] == "news_program_baseline_live_mode_requires_max_model_cases"

    # `recorded` reaches no provider, so it needs no such bound: it gets past the guard and on to the corpus
    # read, which is what the stub above refuses.
    with pytest.raises(AssertionError, match="before the corpus is read"):
        _handle_learning(_baseline_args(mode="recorded"))


def test_the_provider_bound_caps_the_corpus_read_rather_than_being_advisory(monkeypatch: Any) -> None:
    seen: dict[str, int] = {}

    class _Evaluator:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def baseline_episodes(self, _window: Any, *, cohort: bool, limit: int) -> list[Any]:
            del cohort
            seen["limit"] = limit
            return []

    @contextmanager
    def fake_postgres_connection(_settings: Any, *, role: str):
        assert role == "serve"
        yield object()

    monkeypatch.setattr(news_commands, "load_settings", lambda **_kwargs: object())
    monkeypatch.setattr(learning_runtime, "active_arm_manifest", lambda _settings: SimpleNamespace())
    monkeypatch.setattr("tracefold.app.repositories.postgres_connection", fake_postgres_connection)
    monkeypatch.setattr("tracefold.news.CandidateEvaluator", _Evaluator)

    _handle_learning(_baseline_args(mode="runtime_live", action_source="policy", limit=500, max_model_cases=12))
    assert seen["limit"] == 12, "the smaller of the two bounds wins, so --limit cannot widen it"


def test_a_live_baseline_refuses_all_cohorts(monkeypatch: Any) -> None:
    """#150 forbids policy replay across retired cohorts, and only a live mode replays policy.

    The `policy_not_uniform` guard cannot catch this: `_project_episodes` stamps every episode with the same
    active arm, so a mixed-cohort run looks perfectly uniform while answering "what would today's rules do to
    yesterday's Program?".
    """

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the guard must fail before the corpus is read")

    monkeypatch.setattr(news_commands, "load_settings", lambda **_kwargs: object())
    monkeypatch.setattr(learning_runtime, "active_arm_manifest", lambda _settings: SimpleNamespace())
    monkeypatch.setattr("tracefold.app.repositories.postgres_connection", refuse)

    for mode in ("compile_live", "runtime_live"):
        code, payload = _handle_learning(
            _baseline_args(mode=mode, action_source="policy", max_model_cases=5, all_cohorts=True)
        )
        assert code == 2
        assert payload["error"] == "news_program_baseline_live_mode_forbids_all_cohorts"

    # `recorded` never replays policy, so the retired-cohort corpus stays available to it.
    with pytest.raises(AssertionError, match="before the corpus is read"):
        _handle_learning(_baseline_args(mode="recorded", all_cohorts=True))
