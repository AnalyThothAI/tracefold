from __future__ import annotations

import json
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tracefold.app import learning_runtime
from tracefold.app.cli.commands import news_learning as news_commands
from tracefold.app.cli.commands.news_learning import _handle_learning
from tracefold.app.cli.commands.news_learning_runtime import _learning_program_judges
from tracefold.app.cli.parser import build_parser
from tracefold.news.program.artifact import load_stable_program_artifact
from tracefold.news.program.resources import candidates as candidate_programs
from tracefold.news.program.runtime import PROGRAM_PRIMARY_BREAKER_FAILURES, PROGRAM_VERSION


def test_taxonomy_evaluate_has_one_frozen_input_and_one_report_output() -> None:
    args = build_parser().parse_args(
        ["news", "learning", "taxonomy-evaluate", "--file", "gold.json", "--out", "report.json"]
    )

    assert args.learning_command == "taxonomy-evaluate"
    assert args.file == "gold.json"
    assert args.out == "report.json"


def test_taxonomy_register_pins_candidate_code_and_model_before_holdout() -> None:
    args = build_parser().parse_args(
        [
            "news",
            "learning",
            "taxonomy-register",
            "--taxonomy-program-sha",
            "b" * 64,
            "--taxonomy-model-binding-sha",
            "c" * 64,
        ]
    )

    assert args.learning_command == "taxonomy-register"
    assert not hasattr(args, "tested_git_sha")
    assert args.taxonomy_program_sha == "b" * 64
    assert args.taxonomy_model_binding_sha == "c" * 64


def test_learning_optimize_requires_every_budget_and_takes_no_model_flags() -> None:
    """The one optimization entry point (#202 §7).

    No `--compiler-image`, because there is no image; no `--student`/`--reflection`, because the task LM
    has to be the route production Triage answers on or the number this maximizes predicts nothing. What
    the command line still owns is the spend, which is why every ceiling is required — including the
    per-call one, which is also the rate an unpriced provider call is charged at.
    """

    args = build_parser().parse_args(
        [
            "news",
            "learning",
            "optimize",
            "--development",
            "d" * 64,
            "--out",
            "artifacts/run-1",
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
            "--max-call-cost-microusd",
            "5000",
            "--seed",
            "17",
        ]
    )

    assert args.learning_command == "optimize"
    assert (
        args.max_metric_calls,
        args.max_task_model_calls,
        args.max_reflection_model_calls,
        args.max_metric_judge_model_calls,
        args.max_cost_microusd,
        args.max_call_cost_microusd,
        args.seed,
    ) == (30, 90, 12, 45, 500000, 5000, 17)
    assert not hasattr(args, "compiler_image")
    for dropped in ("--compiler-image", "--student", "--reflection"):
        with pytest.raises(SystemExit):
            build_parser().parse_args(
                ["news", "learning", "optimize", "--development", "d" * 64, "--out", "o", dropped, "x"]
            )


def test_the_retired_compile_and_propose_commands_are_gone_without_an_alias() -> None:
    """#202 §10.5: one hard cut, no compatibility layer.

    They are not deprecated spellings of `optimize` and `register` — `compile` sealed a container against
    a metered proxy and `propose` sealed either a Program or a policy candidate. Leaving either name
    parsing would keep a second lifecycle alive in an operator's muscle memory.
    """

    for retired in (
        ["news", "learning", "compile", "--development", "d" * 64],
        ["news", "learning", "propose", "--development", "d" * 64, "--file", "c.json", "--out", "o.json"],
        ["news", "learning", "experiment", "snapshot", "--out", "runs/a"],
        # #343: the research fast loop, the #300 corpus carry and the strict replay leg are deletions,
        # not deprecations.
        ["news", "learning", "snapshot", "--out", "runs/a"],
        ["news", "learning", "compare", "--run", "runs/a", "--student", "q", "--max-model-cases", "20"],
        [
            "news",
            "learning",
            "migrate-corpus",
            "--from-dataset",
            "d" * 64,
            "--semantic-judge",
            "j",
            "--max-model-cases",
            "90",
            "--out",
            "o",
        ],
        [
            "news",
            "release",
            "evaluate",
            "--development",
            "d" * 64,
            "--candidate",
            "c.json",
            "--verify-recordings",
            "--out",
            "r.json",
        ],
        ["news", "learning", "draft-reviews", "--model", "m", "--out", "d.json", "--events-from", "runs/a"],
    ):
        with pytest.raises(SystemExit):
            build_parser().parse_args(retired)


def test_learning_evaluation_exposes_program_live_opt_in_not_legacy_model_flag() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "news",
            "release",
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
    assert stable_judge.primary.event_semantics is not candidate_judge.primary.event_semantics
    assert stable_judge.primary.reader_card is not candidate_judge.primary.reader_card
    for _ in range(PROGRAM_PRIMARY_BREAKER_FAILURES):
        stable_judge._record_primary_failure()
    assert stable_judge._primary_open_until > 0
    assert candidate_judge._primary_open_until == 0


def test_recorded_slots_group_schema_fallback_and_reject_old_schema() -> None:
    from tracefold.app.cli.commands.news_learning_runtime import _recorded_program_slots
    from tracefold.news.artifact_identity import canonical_sha
    from tracefold.news.program.identity import EXECUTION_ENVELOPE_SHA256
    from tracefold.news.program.lm import RuntimeModelIdentity

    arm = SimpleNamespace(
        program_sha256="a" * 64,
        runtime_model_bindings_sha256="b" * 64,
        envelope_sha256=EXECUTION_ENVELOPE_SHA256,
    )
    identity = RuntimeModelIdentity.issue(
        provider="fixture",
        model="fixture/model",
        model_sha256=canonical_sha({"configured": "fixture/model"}),
    )
    execution_sha = canonical_sha(
        {
            "program_sha256": arm.program_sha256,
            "runtime_model_bindings_sha256": arm.runtime_model_bindings_sha256,
            "envelope_sha256": arm.envelope_sha256,
            "runtime_binding_sha256": identity.binding_sha256,
            "provider": identity.provider,
        }
    )

    def row(response_format: Any) -> dict[str, Any]:
        request = {
            "schema": "tracefold.news.lm_request.v1",
            "model": identity.model,
            "messages": [],
            "tools": [],
            "config": {"response_format": response_format, "extensions": {}},
        }
        request_identity = {
            "schema": "tracefold.news.audited_lm_request.v2",
            "endpoint_fingerprint": identity.model_sha256,
            "model_binding": "event_semantics.primary",
        }
        request_sha = canonical_sha({**request_identity, "request": request})
        terminal = {
            "schema": "tracefold.news.recorded_lm.v1",
            "request_sha256": request_sha,
            "request_identity": request_identity,
            "request": request,
            "response": {
                "model": identity.model,
                "text": "{}",
                "finish_reason": "stop",
                "truncated": False,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "cache_read_tokens": 0,
                },
                "cost": None,
            },
            "error": None,
        }
        return {
            "execution_contract_sha": execution_sha,
            "predictor_name": "event_semantics",
            "route": "primary",
            "provider": identity.provider,
            "model": identity.model,
            "model_sha": identity.model_sha256,
            "request_sha256": request_sha,
            "request": request,
            "response": terminal,
        }

    rows = [row({"type": "object", "properties": {}}), row({"type": "json_object"})]
    slots = _recorded_program_slots(rows, arm=arm)

    assert slots[("event_semantics", "primary")]["identity"] == identity
    assert slots[("event_semantics", "primary")]["modes"] == {"json_schema", "json_object"}
    assert len(slots[("event_semantics", "primary")]["recordings"]) == 2

    rows[0]["response"] = {"output": {"semantics": {}}}
    with pytest.raises(ValueError, match="news_program_recording_schema_unsupported"):
        _recorded_program_slots(rows, arm=arm)


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
            program_version=PROGRAM_VERSION,
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
    monkeypatch.setattr("tracefold.news.learning.dataset.DevelopmentDatasetStore", _Evaluator)

    code, payload = _handle_learning(_readiness_args())
    data = payload["data"]
    assert code == 0 and payload["ok"] is True
    assert data["outcome"] == "insufficient"
    assert data["blocking_reasons"] == ["dataset_agent_cohort_mismatch"]
    # The sections a consumer reads, present and empty rather than absent.
    for section in (
        "coverage",
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
    # `null`, not `0`: a corpus that could not be projected has unknown coverage, and reporting zeros
    # would read as a measured corpus of nothing.
    assert set(data["coverage"].values()) == {None}


def test_readiness_republishes_the_frozen_datasets_own_coverage_counts(monkeypatch: Any) -> None:
    """#259 §5.2: the day count and the window length reach an operator, and gate nothing.

    Readiness is the report that costs no provider call, so it is where an operator finds out how
    concentrated a corpus is before deciding whether to spend on it. The block is the dataset's own sealed
    counts forwarded verbatim — the eligible-Event and cluster-role numbers were measured against
    production at freeze time and cannot be recovered from a projection of the cases that survived.
    """

    counts = {
        "case_n": 168,
        "independent_cluster_n": 141,
        "boundary_cluster_n": 34,
        "retention_cluster_n": 107,
        "negative_cluster_n": 55,
        "safety_cluster_n": 9,
        "stratum_n": 4,
        "eligible_event_n": 733,
        "natural_day_n": 1,
        "window_duration_hours": 21.0,
        # Sealed beside the rest and deliberately outside the coverage block: a list and a nested identity
        # object are not counts an operator scans.
        "strata": ["delivered", "model_drop"],
        "eligibility": {"unit": "agent_bundle_sha"},
    }

    class _Store:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def development_compile_export(self, _sha: str) -> Any:
            return SimpleNamespace(episodes=(), dataset_payload={"counts": counts})

    @contextmanager
    def fake_postgres_connection(_settings: Any, *, role: str):
        assert role == "serve"
        yield object()

    _readiness_settings(monkeypatch)
    monkeypatch.setattr("tracefold.app.repository_session.postgres_connection", fake_postgres_connection)
    monkeypatch.setattr("tracefold.news.learning.dataset.DevelopmentDatasetStore", _Store)

    code, payload = _handle_learning(_readiness_args())
    coverage = payload["data"]["coverage"]

    assert code == 0
    assert coverage["natural_day_n"] == 1
    assert coverage["window_duration_hours"] == 21.0
    assert coverage["independent_cluster_n"] == 141
    assert coverage["eligible_event_n"] == 733
    assert "strata" not in coverage and "eligibility" not in coverage
    assert payload["data"]["schema"] == "tracefold.news.gepa_readiness_report.v2"


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
    monkeypatch.setattr("tracefold.news.learning.dataset.DevelopmentDatasetStore", _Evaluator)

    code, payload = _handle_learning(_readiness_args())
    assert code == 2
    assert payload["error"] == "news_learning_compile_requires_development_dataset"


def _baseline_args(**updates: Any) -> SimpleNamespace:
    args = {
        "learning_command": "baseline",
        "from_ms": 1,
        "to_ms": 2,
        "dataset": "",
        "mode": "recorded",
        "action_source": "",
        "max_model_cases": 0,
        "semantic_judge": "",
        "limit": 10,
        "out": "",
    }
    args.update(updates)
    return SimpleNamespace(**args)


def test_a_dataset_baseline_refuses_to_also_take_a_moving_window(monkeypatch: Any) -> None:
    """A run measures one corpus.

    The frozen dataset and the moving window answer different questions — release evidence and discovery —
    and silently preferring one would publish a report whose window and whose cases came from different
    ones (#199 §5).
    """

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the guard must fail before the corpus is read")

    monkeypatch.setattr(news_commands, "load_settings", lambda **_kwargs: object())
    monkeypatch.setattr(learning_runtime, "active_arm_manifest", lambda _settings: SimpleNamespace())
    monkeypatch.setattr("tracefold.app.repository_session.postgres_connection", refuse)

    code, payload = _handle_learning(_baseline_args(dataset="a" * 64))
    assert code == 2
    assert payload["error"] == "news_program_baseline_dataset_excludes_moving_window"


def test_a_baseline_with_neither_a_dataset_nor_a_window_is_refused(monkeypatch: Any) -> None:
    monkeypatch.setattr(news_commands, "load_settings", lambda **_kwargs: object())
    monkeypatch.setattr(learning_runtime, "active_arm_manifest", lambda _settings: SimpleNamespace())
    code, payload = _handle_learning(_baseline_args(from_ms=None, to_ms=None))
    assert code == 2
    assert payload["error"] == "news_program_baseline_requires_dataset_or_window"


def test_the_parser_leaves_the_window_absent_so_the_handler_can_tell_it_apart_from_zero() -> None:
    parser = build_parser()
    dataset = parser.parse_args(["news", "learning", "baseline", "--dataset", "a" * 64])
    assert (dataset.dataset, dataset.from_ms, dataset.to_ms) == ("a" * 64, None, None)
    window = parser.parse_args(["news", "learning", "baseline", "--from-ms", "0", "--to-ms", "2"])
    assert (window.dataset, window.from_ms, window.to_ms) == ("", 0, 2)


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
    """`runtime_live` spends 2-8 real calls per case, sequentially, on the endpoints that also serve
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

    built: dict[str, Any] = {}

    endpoint = SimpleNamespace(
        model_name="local/qwen-test",
        api_key="unused",
        api_base="http://endpoint.invalid/v1",
        model_kwargs={},
        temperature=0.0,
        structured_output="json_schema",
    )

    class _Composition:
        program_configured = True
        event_semantics_primary = endpoint

        @staticmethod
        def compile_semantic_judge(artifact: Any) -> object:
            built["artifact"] = artifact
            return object()

    monkeypatch.setattr(learning_runtime, "compose_news_program_runtime", lambda _settings: _Composition())

    program, identity = _baseline_model_route(
        "compile_live",
        settings=object(),
        artifact=load_stable_program_artifact(),
    )

    assert program is not None
    assert built["artifact"] == load_stable_program_artifact()
    assert identity["compile_task_model"] == "local/qwen-test"


def test_the_provider_bound_caps_the_corpus_read_rather_than_being_advisory(monkeypatch: Any) -> None:
    seen: dict[str, int] = {}

    class _Evaluator:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def baseline_episodes(self, _window: Any, *, limit: int) -> list[Any]:
            seen["limit"] = limit
            return []

    @contextmanager
    def fake_postgres_connection(_settings: Any, *, role: str):
        assert role == "serve"
        yield object()

    monkeypatch.setattr(news_commands, "load_settings", lambda **_kwargs: object())
    monkeypatch.setattr(learning_runtime, "active_arm_manifest", lambda _settings: SimpleNamespace())
    monkeypatch.setattr("tracefold.app.repository_session.postgres_connection", fake_postgres_connection)
    monkeypatch.setattr("tracefold.news.learning.dataset.DevelopmentDatasetStore", _Evaluator)

    _handle_learning(_baseline_args(mode="runtime_live", action_source="policy", limit=500, max_model_cases=12))
    assert seen["limit"] == 12, "the smaller of the two bounds wins, so --limit cannot widen it"


def test_a_dataset_baseline_will_not_publish_split_roots_for_cases_it_did_not_score(monkeypatch: Any) -> None:
    """`--max-model-cases` truncates; a formal optimizer baseline cannot be truncated.

    The report republishes the Objective Plan's split roots, so a run that scored 2 of 3 optimizer cases
    would publish roots describing a corpus it never measured. The moving-window form stays available for
    a cheap probe and names itself discovery.
    """

    from tracefold.app.cli.commands import news_learning_baseline as baseline_commands
    from tracefold.news.learning.objective import GepaObjectivePlan

    @contextmanager
    def fake_postgres_connection(_settings: Any, *, role: str):
        assert role == "serve"
        yield object()

    monkeypatch.setattr(news_commands, "load_settings", lambda **_kwargs: object())
    monkeypatch.setattr(learning_runtime, "active_arm_manifest", lambda _settings: SimpleNamespace())
    monkeypatch.setattr("tracefold.app.repository_session.postgres_connection", fake_postgres_connection)
    monkeypatch.setattr("tracefold.news.learning.dataset.DevelopmentDatasetStore", lambda *_a, **_k: object())
    monkeypatch.setattr(
        baseline_commands,
        "_dataset_corpus",
        lambda *_args, **_kwargs: (
            tuple({"case_id": f"case-{index}"} for index in range(3)),
            (),
            GepaObjectivePlan(case_n=3, cluster_n=3),
            {},
        ),
    )

    code, payload = _handle_learning(
        _baseline_args(
            dataset="a" * 64,
            from_ms=None,
            to_ms=None,
            mode="compile_live",
            action_source="policy",
            semantic_judge="deepseek-v4-pro",
            max_model_cases=2,
        )
    )
    assert code == 2
    assert payload["error"] == "news_program_baseline_dataset_requires_full_corpus_budget:3"


@pytest.mark.parametrize("mode", ["recorded", "runtime_live"])
def test_dataset_evidence_only_comes_from_the_graph_the_optimizer_runs(monkeypatch: Any, mode: str) -> None:
    """`subsets.development_selection` is the formal before value, so it has to measure the cold graph.

    `recorded` scores the action that shipped while the Objective Plan classifies under a replayed
    `decide()`, so the same report would call a case a control and zero it. `runtime_live` measures the
    four-slot production route with retry, fallback, deadline and circuit — a reliability question, not a
    number a candidate selected on `DspyCompileProgram` can be compared against. Both stay available in
    the moving-window form.
    """

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the guard must fail before the corpus is read")

    monkeypatch.setattr(news_commands, "load_settings", lambda **_kwargs: object())
    monkeypatch.setattr(learning_runtime, "active_arm_manifest", lambda _settings: SimpleNamespace())
    monkeypatch.setattr("tracefold.app.repository_session.postgres_connection", refuse)

    code, payload = _handle_learning(
        _baseline_args(
            dataset="a" * 64,
            from_ms=None,
            to_ms=None,
            mode=mode,
            action_source="recorded" if mode == "recorded" else "policy",
            semantic_judge="deepseek-v4-pro",
            max_model_cases=8,
        )
    )
    assert code == 2
    assert payload["error"] == "news_program_baseline_dataset_requires_compile_live"


def test_a_dataset_baseline_refuses_to_be_judged_by_a_different_ruler(monkeypatch: Any) -> None:
    """`run_gepa` refuses to run without a metric judge; its baseline may not run without one either.

    `bind_metric(None)` compares free-text retention byte-for-byte and fires `factual_contradiction` on
    every failed `factual_fidelity`, so an un-judged baseline is a different ruler wearing the same name.
    """

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the guard must fail before the corpus is read")

    monkeypatch.setattr(news_commands, "load_settings", lambda **_kwargs: object())
    monkeypatch.setattr(learning_runtime, "active_arm_manifest", lambda _settings: SimpleNamespace())
    monkeypatch.setattr("tracefold.app.repository_session.postgres_connection", refuse)

    code, payload = _handle_learning(
        _baseline_args(
            dataset="a" * 64,
            from_ms=None,
            to_ms=None,
            mode="compile_live",
            action_source="policy",
            max_model_cases=8,
        )
    )
    assert code == 2
    assert payload["error"] == "news_program_baseline_dataset_requires_semantic_judge"


def test_a_blocked_objective_plan_does_not_become_an_empty_before_number(monkeypatch: Any) -> None:
    """`subsets.development_selection` is the number this report exists to publish.

    A blocked plan has no split to compute it from, so a `frozen_development` report would carry an empty
    subsets block that reads as a measured zero. `news learning readiness` explains why, for free.
    """

    from tracefold.app.cli.commands import news_learning_baseline as baseline_commands
    from tracefold.news.learning.objective import GepaObjectivePlan

    @contextmanager
    def fake_postgres_connection(_settings: Any, *, role: str):
        assert role == "serve"
        yield object()

    monkeypatch.setattr(news_commands, "load_settings", lambda **_kwargs: object())
    monkeypatch.setattr(learning_runtime, "active_arm_manifest", lambda _settings: SimpleNamespace())
    monkeypatch.setattr("tracefold.app.repository_session.postgres_connection", fake_postgres_connection)
    monkeypatch.setattr("tracefold.news.learning.dataset.DevelopmentDatasetStore", lambda *_a, **_k: object())
    monkeypatch.setattr(
        baseline_commands,
        "_dataset_corpus",
        lambda *_args, **_kwargs: (
            ({"case_id": "case-0"},),
            (),
            GepaObjectivePlan(case_n=1, cluster_n=1, blocking_reasons=("development_selection_target_missing",)),
            {},
        ),
    )

    code, payload = _handle_learning(
        _baseline_args(
            dataset="a" * 64,
            from_ms=None,
            to_ms=None,
            mode="compile_live",
            action_source="policy",
            semantic_judge="deepseek-v4-pro",
            max_model_cases=8,
        )
    )
    assert code == 2
    assert payload["error"] == ("news_program_baseline_dataset_objective_blocked:development_selection_target_missing")


def test_a_live_baseline_reads_only_the_current_cohort(monkeypatch: Any) -> None:
    """A moving-window baseline cannot opt into retired judgment contracts."""

    seen: dict[str, Any] = {}

    class _Evaluator:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def baseline_episodes(self, _window: Any, *, limit: int) -> list[Any]:
            seen["limit"] = limit
            return []

    @contextmanager
    def fake_postgres_connection(_settings: Any, *, role: str):
        assert role == "serve"
        yield object()

    monkeypatch.setattr(news_commands, "load_settings", lambda **_kwargs: object())
    monkeypatch.setattr(learning_runtime, "active_arm_manifest", lambda _settings: SimpleNamespace())
    monkeypatch.setattr("tracefold.app.repository_session.postgres_connection", fake_postgres_connection)
    monkeypatch.setattr("tracefold.news.learning.dataset.DevelopmentDatasetStore", _Evaluator)

    code, payload = _handle_learning(_baseline_args(mode="runtime_live", action_source="policy", max_model_cases=5))
    assert seen["limit"] == 5
    assert code == 2 and payload["error"]["code"] == "news_program_baseline_no_accepted_reviews_in_window"

    # The genuinely forbidden pairing stays blocked.
    code, payload = _handle_learning(_baseline_args(mode="recorded", action_source="policy"))
    assert payload["error"] == "news_program_baseline_recorded_mode_requires_recorded_decision"


def test_a_non_advance_rerun_clears_an_earlier_candidate_from_the_output_directory() -> None:
    """#212 review: the output directory is the record of *one* optimization.

    Driving the real writer, not a re-statement of it: an operator reusing `--out` would otherwise end up
    with this run's rejection report sitting beside a registrable candidate from a previous run, which
    nothing downstream would catch — that stale candidate is perfectly valid on its own terms.
    """

    from tracefold.app.cli.commands.news_learning_experiment import write_run_outputs

    def _result(outcome: str, candidate: Any) -> Any:
        return SimpleNamespace(
            outcome=outcome,
            report=SimpleNamespace(model_dump=lambda mode="json": {"outcome": outcome}),
            candidate=candidate,
        )

    advanced = _result("ADVANCE", SimpleNamespace(model_dump=lambda mode="json": {"schema_version": "x"}))
    rejected = _result("REJECTED", None)

    with tempfile.TemporaryDirectory() as directory:
        out = Path(directory) / "run"

        report_path, candidate_path = write_run_outputs(out, advanced)
        assert candidate_path is not None and Path(candidate_path).exists()
        assert report_path.exists()

        report_path, candidate_path = write_run_outputs(out, rejected)
        assert candidate_path is None
        assert not (out / "prompt_candidate.json").exists()
        assert json.loads(report_path.read_text(encoding="utf-8"))["outcome"] == "REJECTED"
        # 0600, because a run report names the endpoints and the corpus a candidate was built from.
        assert oct(report_path.stat().st_mode)[-3:] == "600"
