from __future__ import annotations

import tempfile
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from tracefold.app import learning_runtime
from tracefold.app.cli.commands import news_learning as news_commands
from tracefold.app.cli.commands.news_learning import _handle_learning
from tracefold.app.cli.commands.news_learning_baseline import (
    _desk_lookback_hours,
    _run_window,
    _within_window,
)
from tracefold.app.cli.commands.news_learning_runtime import _learning_program_judges
from tracefold.app.cli.parser import build_parser
from tracefold.news.learning.compiler.security import COMPILE_EPISODE_PROJECTION_SCHEMA
from tracefold.news.learning.experiment.run import (
    ExperimentRun,
    ExperimentRunManifest,
    ExperimentWindow,
)
from tracefold.news.program.artifact import load_stable_program_artifact
from tracefold.news.program.resources import candidates as candidate_programs
from tracefold.news.program.runtime import PROGRAM_PRIMARY_BREAKER_FAILURES


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
            "--max-reflection-model-calls",
            "12",
            "--max-metric-judge-model-calls",
            "45",
            "--max-cost-microusd",
            "500000",
            "--seed",
            "17",
        ]
    )

    assert args.learning_command == "compile"
    assert args.compiler_image == "sha256:" + "1" * 64
    assert (
        args.max_metric_calls,
        args.max_task_model_calls,
        args.max_reflection_model_calls,
        args.max_metric_judge_model_calls,
        args.max_cost_microusd,
        args.seed,
    ) == (
        30,
        90,
        12,
        45,
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
    monkeypatch.setattr("tracefold.app.repository_session.postgres_connection", fake_postgres_connection)
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
    monkeypatch.setattr("tracefold.app.repository_session.repositories", fake_repositories)
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
    for _ in range(PROGRAM_PRIMARY_BREAKER_FAILURES):
        stable_judge._record_primary_failure()
    assert stable_judge._primary_open_until > 0
    assert candidate_judge._primary_open_until == 0


def _readiness_args(**updates: Any) -> SimpleNamespace:
    args = {"learning_command": "readiness", "development": "a" * 64, "out": ""}
    args.update(updates)
    return SimpleNamespace(**args)


def _readiness_settings(monkeypatch: Any) -> None:
    monkeypatch.setattr(news_commands, "load_settings", lambda **_kwargs: object())
    monkeypatch.setattr(
        learning_runtime,
        "active_arm_manifest",
        lambda _settings: SimpleNamespace(
            program_version="news_semantic_program_v5",
            program_sha256="b" * 64,
            bundle_sha="c" * 64,
            policy_sha256="d" * 64,
        ),
    )
    monkeypatch.setattr(
        "tracefold.app.cli.commands.news_learning_baseline._readiness_model_targets",
        lambda _settings: {"task": None, "program_route_configured": False, "compiler_reflection_configured": True},
    )


def test_readiness_reports_a_cohort_mismatch_in_the_same_shape_as_a_real_report(monkeypatch: Any) -> None:
    """One report shape, whatever the answer.

    The blocked path used to return four keys and a differently named output field, so anything parsing
    the report had to special-case it. `dataset_agent_cohort_mismatch` is a #199 §4 blocking reason, not
    an error, so it is an `insufficient` report with every section present and empty.
    """

    class _Evaluator:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def development_compile_export(self, _sha: str) -> Any:
            raise ValueError("news_learning_dataset_agent_cohort_mismatch")

    @contextmanager
    def fake_postgres_connection(_settings: Any, *, role: str):
        assert role == "serve"
        yield object()

    _readiness_settings(monkeypatch)
    monkeypatch.setattr("tracefold.app.repository_session.postgres_connection", fake_postgres_connection)
    monkeypatch.setattr("tracefold.news.learning.evaluator.CandidateEvaluator", _Evaluator)

    code, payload = _handle_learning(_readiness_args())
    data = payload["data"]
    assert code == 0 and payload["ok"] is True
    assert data["outcome"] == "insufficient"
    assert data["blocking_reasons"] == ["dataset_agent_cohort_mismatch"]
    # The sections a consumer reads, present and empty rather than absent.
    for section in (
        "corpus",
        "owner_distribution",
        "objective",
        "split",
        "train",
        "development_selection",
        "retrieval",
        "call_envelope",
        "case_dispositions_written_to",
    ):
        assert section in data, section
    assert data["objective"]["target_case_n"] == 0
    assert data["identity"]["model_targets"]["compiler_reflection_configured"] is True


def test_readiness_lets_a_wrong_dataset_argument_stay_an_error(monkeypatch: Any) -> None:
    """`insufficient` means "this corpus cannot support an optimization". A validation SHA is a typo."""

    class _Evaluator:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def development_compile_export(self, _sha: str) -> Any:
            raise ValueError("news_learning_compile_requires_development_dataset")

    @contextmanager
    def fake_postgres_connection(_settings: Any, *, role: str):
        assert role == "serve"
        yield object()

    _readiness_settings(monkeypatch)
    monkeypatch.setattr("tracefold.app.repository_session.postgres_connection", fake_postgres_connection)
    monkeypatch.setattr("tracefold.news.learning.evaluator.CandidateEvaluator", _Evaluator)

    code, payload = _handle_learning(_readiness_args())
    assert code == 2
    assert payload["error"] == "news_learning_compile_requires_development_dataset"


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
        ("recorded", "policy", "news_program_baseline_recorded_mode_requires_recorded_decision"),
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
    monkeypatch.setattr("tracefold.app.repository_session.postgres_connection", refuse)

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
    monkeypatch.setattr("tracefold.app.repository_session.postgres_connection", refuse)

    for mode in ("compile_live", "runtime_live"):
        code, payload = _handle_learning(_baseline_args(mode=mode, action_source="policy"))
        assert code == 2
        assert payload["error"] == "news_program_baseline_live_mode_requires_max_model_cases"

    # `recorded` reaches no provider, so it needs no such bound: it gets past the guard and on to the corpus
    # read, which is what the stub above refuses.
    with pytest.raises(AssertionError, match="before the corpus is read"):
        _handle_learning(_baseline_args(mode="recorded"))


def test_each_live_mode_builds_its_route_from_the_code_owned_execution_budget(monkeypatch: Any) -> None:
    """The route deadline and token ceilings are code, and `compile_live` has to read them from there.

    They used to be artifact fields, so this branch reached through `artifact.execution` and
    `artifact.route_spec`. #193 deleted both. The parameter is annotated `Any`, so neither mypy nor any
    existing test noticed — the first `--mode compile_live` run would have raised `AttributeError` after
    the corpus read and before scoring a single case.
    """

    from tracefold.app.cli.commands.news_learning_baseline import _baseline_model_route
    from tracefold.news.program.runtime import (
        PROGRAM_EVENT_SEMANTICS_MAX_TOKENS,
        PROGRAM_READER_CARD_MAX_TOKENS,
        PROGRAM_ROUTE_DEADLINE_SECONDS,
    )

    built: dict[str, Any] = {}

    def record_lm(**kwargs: Any) -> object:
        built.update(kwargs)
        return object()

    endpoint = SimpleNamespace(
        model_name="local/qwen-test",
        api_key="unused",
        api_base="http://endpoint.invalid/v1",
        model_kwargs={},
    )
    monkeypatch.setattr(
        learning_runtime,
        "compose_news_program_runtime",
        lambda _settings: SimpleNamespace(
            program_configured=True,
            event_semantics_primary=endpoint,
        ),
    )
    monkeypatch.setattr("tracefold.news.learning.baseline.build_runtime_lm", record_lm)

    lm, judge, identity = _baseline_model_route(
        "compile_live",
        settings=object(),
        artifact=load_stable_program_artifact(),
    )

    assert lm is not None and judge is None
    assert built["timeout"] == float(PROGRAM_ROUTE_DEADLINE_SECONDS)
    assert built["max_tokens"] == max(PROGRAM_EVENT_SEMANTICS_MAX_TOKENS, PROGRAM_READER_CARD_MAX_TOKENS)
    assert identity["compile_task_model"] == "local/qwen-test"


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
    monkeypatch.setattr("tracefold.app.repository_session.postgres_connection", fake_postgres_connection)
    monkeypatch.setattr("tracefold.news.learning.evaluator.CandidateEvaluator", _Evaluator)

    _handle_learning(_baseline_args(mode="runtime_live", action_source="policy", limit=500, max_model_cases=12))
    assert seen["limit"] == 12, "the smaller of the two bounds wins, so --limit cannot widen it"


def test_a_live_baseline_may_read_retired_cohorts_and_says_so(monkeypatch: Any) -> None:
    """What #150 forbids is replaying today's policy over a *stored retired verdict*, and that is
    `--mode recorded --action-source policy`, which the handler already rejects.

    A live mode generates the verdict with today's Program and scores it under today's policy; only the
    evidence and the reviewer's labels are historical, which is the only pairing that can be measured at all —
    every accepted review this project has belongs to a retired cohort, so banning the combination made both
    live modes unrunnable rather than safer. The receipt names the population instead.
    """

    seen: dict[str, Any] = {}

    class _Evaluator:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def baseline_episodes(self, _window: Any, *, cohort: bool, limit: int) -> list[Any]:
            seen["cohort"] = cohort
            seen["limit"] = limit
            return []

    @contextmanager
    def fake_postgres_connection(_settings: Any, *, role: str):
        assert role == "serve"
        yield object()

    monkeypatch.setattr(news_commands, "load_settings", lambda **_kwargs: object())
    monkeypatch.setattr(learning_runtime, "active_arm_manifest", lambda _settings: SimpleNamespace())
    monkeypatch.setattr("tracefold.app.repository_session.postgres_connection", fake_postgres_connection)
    monkeypatch.setattr("tracefold.news.learning.evaluator.CandidateEvaluator", _Evaluator)

    code, payload = _handle_learning(
        _baseline_args(mode="runtime_live", action_source="policy", max_model_cases=5, all_cohorts=True)
    )
    assert seen["cohort"] is False
    assert code == 2 and payload["error"]["code"] == "news_program_baseline_no_accepted_reviews_in_window"

    # The genuinely forbidden pairing stays blocked.
    code, payload = _handle_learning(_baseline_args(mode="recorded", action_source="policy", all_cohorts=True))
    assert payload["error"] == "news_program_baseline_recorded_mode_requires_recorded_decision"


def _experiment_args(action: str, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "command": "news",
        "news_command": "learning",
        "learning_command": "experiment",
        "experiment_action": action,
        "config": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_the_experiment_group_parses_its_three_actions() -> None:
    parser = build_parser()
    snapshot = parser.parse_args(
        ["news", "learning", "experiment", "snapshot", "--hours", "48", "--limit", "300", "--out", "runs/a"]
    )
    assert (snapshot.experiment_action, snapshot.hours, snapshot.limit, snapshot.out) == ("snapshot", 48, 300, "runs/a")

    compare = parser.parse_args(
        [
            "news",
            "learning",
            "experiment",
            "compare",
            "--run",
            "runs/a",
            "--student",
            "qwen3-30b",
            "--max-model-cases",
            "20",
            "--resume",
        ]
    )
    assert (compare.experiment_action, compare.max_model_cases, compare.resume) == ("compare", 20, True)
    # The bound is required, not defaulted: the student route is the same single slot production Triage
    # runs on, so an unbounded comparison is an unbounded load on the live path.
    with pytest.raises(SystemExit):
        parser.parse_args(["news", "learning", "experiment", "compare", "--run", "runs/a", "--student", "q"])

    optimize = parser.parse_args(
        [
            "news",
            "learning",
            "experiment",
            "optimize",
            "--run",
            "runs/a",
            "--student",
            "qwen3-30b",
            "--reflection",
            "deepseek-v4-pro",
            "--semantic-judge",
            "deepseek-v4-pro",
            "--max-metric-calls",
            "60",
            "--max-judge-model-calls",
            "200",
            "--seed",
            "7",
        ]
    )
    assert (optimize.experiment_action, optimize.max_metric_calls, optimize.seed) == ("optimize", 60, 7)
    # The metric-call bound is not a spend bound: each metric call drives two task calls plus N judge
    # calls, so the judge carries its own required ceiling.
    assert optimize.max_judge_model_calls == 200


def test_a_snapshot_ends_at_the_settlement_grace_and_closes_its_connection_first(monkeypatch: Any) -> None:
    """Two properties of one command, both about when things happen.

    A window whose tail is still settling is not closed: the outcome loop keeps writing prices for minutes
    after an Event opens, so `to_ms = now` would freeze cases whose scores change after the file is
    written. And freezing is up to 500 fsync'd files, so the `serve` connection has to be closed before
    the first of them — `docs/DEVELOPMENT.md` forbids holding one across a file write.
    """

    seen: dict[str, Any] = {}
    open_connections: list[str] = []

    class _Evaluator:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

    def fake_project_window(_evaluator: Any, *, window: Any, limit: int) -> tuple[Any, ...]:
        seen["window"] = window
        seen["limit"] = limit
        seen["connection_open_during_read"] = bool(open_connections)
        return ()

    def fake_freeze_window(cases: Any, **kwargs: Any) -> Any:
        seen["connection_open_during_write"] = bool(open_connections)
        seen["now_ms"] = kwargs["now_ms"]
        return SimpleNamespace(
            run_sha256="a" * 64,
            case_count=len(cases),
            accepted_case_count=0,
            window=kwargs["window"],
            parent_program_sha256="b" * 64,
        )

    @contextmanager
    def fake_postgres_connection(_settings: Any, *, role: str):
        assert role == "serve"
        open_connections.append(role)
        try:
            yield object()
        finally:
            open_connections.pop()

    monkeypatch.setattr(news_commands, "load_settings", lambda **_kwargs: object())
    monkeypatch.setattr(learning_runtime, "active_arm_manifest", lambda _settings: SimpleNamespace())
    monkeypatch.setattr("tracefold.app.repository_session.postgres_connection", fake_postgres_connection)
    monkeypatch.setattr("tracefold.news.learning.evaluator.CandidateEvaluator", _Evaluator)
    monkeypatch.setattr("tracefold.news.learning.experiment.snapshot.project_window", fake_project_window)
    monkeypatch.setattr("tracefold.news.learning.experiment.snapshot.freeze_window", fake_freeze_window)
    monkeypatch.setattr("time.time", lambda: 1_787_086_400.0)

    with tempfile.TemporaryDirectory() as directory:
        code, payload = _handle_learning(_experiment_args("snapshot", hours=24, limit=500, out=f"{directory}/run"))

    assert code == 0 and payload["ok"] is True
    now_ms = 1_787_086_400_000
    assert seen["window"].to_ms == now_ms - 10 * 60 * 1000
    assert seen["window"].from_ms == seen["window"].to_ms - 24 * 3_600_000
    assert seen["now_ms"] == now_ms
    assert seen["connection_open_during_read"] is True
    assert seen["connection_open_during_write"] is False


def test_draft_reviews_takes_its_events_from_a_run_when_told_to() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "news",
            "learning",
            "draft-reviews",
            "--model",
            "deepseek-v4-pro",
            "--out",
            "d.json",
            "--events-from",
            "runs/a",
        ]
    )
    assert args.events_from == "runs/a"
    # Still optional: the queue-by-hours form is what a first corpus is grown with, before any run exists.
    assert (
        parser.parse_args(
            ["news", "learning", "draft-reviews", "--model", "deepseek-v4-pro", "--out", "d.json"]
        ).events_from
        == ""
    )


def _run_with_window(root: Any, *, from_ms: int, to_ms: int) -> ExperimentRun:
    run = ExperimentRun(root, create=True)
    run.write_manifest(
        ExperimentRunManifest.issue(
            projection_schema_id=COMPILE_EPISODE_PROJECTION_SCHEMA,
            name="run",
            window=ExperimentWindow(from_ms=from_ms, to_ms=to_ms),
            parent_program_sha256=load_stable_program_artifact().program_sha256,
            program_version="news_semantic_program_v5",
            policy_sha256="b" * 64,
            case_count=0,
            accepted_case_count=0,
            case_root_sha256="c" * 64,
            created_at_ms=to_ms,
        )
    )
    return run


def test_draft_reviews_draws_its_lookback_from_the_run_window(tmp_path: Any) -> None:
    """`--events-from` contributes the window; the ReviewDesk queue still does the selecting.

    It used to read the run's *case list* and draft the ones marked unaccepted. There are none and there
    never can be — `baseline_episodes` reaches a case through an acceptance row — so the bridge always
    raised. Drafting is for the rest of the window, and the desk's stratified sampler picks which.
    """

    opened = 1_787_000_000_000
    run = _run_with_window(tmp_path / "run", from_ms=opened, to_ms=opened + 24 * 3_600_000)

    assert _run_window(str(run.root)) == (opened, opened + 24 * 3_600_000)
    assert _desk_lookback_hours((opened, opened + 1), now_ms=opened + 48 * 3_600_000) == 48
    # Rounded up, so the look-back covers the window's leading edge rather than stopping just inside it.
    assert _desk_lookback_hours((opened, opened + 1), now_ms=opened + 48 * 3_600_000 + 1) == 49
    # No run named leaves `--hours` exactly as it was.
    assert _run_window("") is None


def test_draft_reviews_keeps_only_the_events_inside_the_frozen_window(tmp_path: Any) -> None:
    """The desk's look-back is a width ending at *now*; a run has two edges.

    A snapshot stops at the settlement grace on purpose, so the look-back necessarily reaches past `to_ms`
    and rounds up to an hour before `from_ms`. Without this bound the drafts would grow a corpus for a
    window the run never froze while claiming to target exactly that window.
    """

    window = (1_000_000, 2_000_000)

    assert _within_window({"opened_at_ms": 1_500_000}, window) is True
    assert _within_window({"opened_at_ms": 1_000_000}, window) is True
    assert _within_window({"opened_at_ms": 999_999}, window) is False
    # Half-open at the top: `to_ms` is the settlement grace, and an Event opening exactly there is newer
    # than the window the run froze.
    assert _within_window({"opened_at_ms": 2_000_000}, window) is False
    # No run named means no bound, which is the historical `--hours` behaviour untouched.
    assert _within_window({"opened_at_ms": 0}, None) is True


def test_draft_reviews_refuses_a_run_window_the_desk_cannot_reach(tmp_path: Any) -> None:
    """The desk pages a look-back, and its own bound is 720 hours. Past that, say so."""

    with pytest.raises(ValueError, match="news_review_drafter_run_window_exceeds_desk_lookback"):
        _desk_lookback_hours((1_000, 2_000), now_ms=1_787_000_000_000)
    with pytest.raises(ValueError, match="news_review_drafter_run_window_not_in_the_past"):
        _desk_lookback_hours((1_000, 2_000), now_ms=0)
