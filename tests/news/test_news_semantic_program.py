from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib.resources
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import dspy
import pytest

from tracefold.news.agents import semantic_program as semantic_program_module
from tracefold.news.agents.semantic_program import (
    PROGRAM_DEMO_JSON_MAX_BYTES,
    PROGRAM_DEMOS_MAX,
    PROGRAM_DEPENDENCY_LOCK_SHA256,
    PROGRAM_INSTRUCTION_MAX_BYTES,
    TOLD_MAX,
    TOLD_SAME_KEY_MAX,
    CompileProvenance,
    DspyCompileProgram,
    DspyNewsSemanticProgram,
    DspyPredictorAdapter,
    EventSemantics,
    PredictorAdapterError,
    PredictorRequest,
    PredictorResponse,
    ProgramArtifact,
    ProgramArtifactCodec,
    ProviderCallObservation,
    RecordReplayPredictorAdapter,
    ScriptedPredictorAdapter,
    SemanticProgramError,
    TriageContext,
    load_program_artifact,
    load_stable_program_artifact,
)
from tracefold.news.artifact_identity import canonical_json, canonical_sha


def _semantics(**updates: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "novelty": "new_fact",
        "restates": -1,
        "event_type": "listing",
        "assets": [{"symbol": "BTC", "market_type": "spot", "role": "primary"}],
        "direction": "bullish",
        "scope": "single_name",
        "magnitude": 1,
        "actionable": True,
        "confidence": 0.8,
        "decision": "push",
        "audience": "crypto",
    }
    value.update(updates)
    return value


def _card(**updates: Any) -> dict[str, Any]:
    value = {"headline_zh": "比特币出现新进展", "title_zh": "比特币出现新进展", "why_zh": "值得关注。"}
    value.update(updates)
    return value


def _context(*, told_rows: list[dict[str, Any]] | None = None) -> TriageContext:
    return TriageContext.from_card(
        {
            "event_id": "event-secret",
            "evidence_version": 2,
            "evidence_sha256": "a" * 64,
            "focus_fact_id": "fact-secret",
            "reporting_origin": "wire",
            "provenance": ["1018"],
            "leader_title": "BTC listed on Example Exchange",
            "raw_first_line": "$BTC listing",
            "leader_description": "Trading starts tomorrow.",
            "opened_at_ms": 1_000_000,
            "member_count": 2,
            "family": "listing",
            "provider_score_max": 90,
            "provider_metadata": {"coins": [{"symbol": "BTC", "grade": "A"}]},
            "priority": "normal",
            "asset_class": "crypto",
            "grounded_assets": ["BTC"],
            "storyline_key": "asset:BTC",
        },
        watchlist=("BTC",),
        told_rows=told_rows or [],
        now_ms=1_010_000,
        queue_lag_ms=10_000,
    )


def _evidence_json() -> str:
    return canonical_json(_context().model_payload())


def _program(
    primary: ScriptedPredictorAdapter,
    *,
    fallback: ScriptedPredictorAdapter | None = None,
    artifact: ProgramArtifact | None = None,
) -> DspyNewsSemanticProgram:
    return DspyNewsSemanticProgram(
        artifact or load_stable_program_artifact(),
        primary_adapter=primary,
        fallback_adapter=fallback,
    )


def _artifact_with_execution(**updates: Any) -> ProgramArtifact:
    base = load_stable_program_artifact()
    data = base.model_dump(mode="json")
    data["execution"].update(updates)
    manifest = {
        key: value for key, value in data.items() if key not in {"program_sha256", "event_semantics", "reader_card"}
    }
    data["program_sha256"] = canonical_sha(manifest)
    return ProgramArtifact.model_validate(data)


def _artifact_documents_with_demo(
    predictor: str,
    demo: dict[str, Any],
) -> tuple[str, str]:
    manifest_document, state_document = ProgramArtifactCodec.encode(load_stable_program_artifact())
    manifest = json.loads(manifest_document)
    state = json.loads(state_document)
    state[predictor]["demos"] = [demo]
    state[predictor]["demos_sha256"] = canonical_sha([demo])
    manifest["state_sha256"] = canonical_sha(state)
    manifest["program_sha256"] = canonical_sha(
        {key: value for key, value in manifest.items() if key != "program_sha256"}
    )
    return canonical_json(manifest), canonical_json(state)


def test_builtin_artifact_is_registered_and_canonical() -> None:
    artifact = load_stable_program_artifact()
    assert load_program_artifact(artifact.program_sha256) == artifact
    manifest, state = ProgramArtifactCodec.encode(artifact)
    assert ProgramArtifactCodec.decode(manifest, state) == artifact
    assert artifact.program_sha256 == artifact.computed_sha256()
    assert artifact.state_sha256 == artifact.computed_state_sha256()


def test_stable_artifact_encodes_the_restatement_index_contract() -> None:
    restates_schema = EventSemantics.model_json_schema()["properties"]["restates"]
    assert restates_schema["description"] == (
        "Visible event_status.told index if and only if novelty is restatement; -1 for new_fact or progression."
    )
    instruction = load_stable_program_artifact().event_semantics.instruction
    assert (
        "Set restates to a visible told index if and only if novelty is restatement. "
        "new_fact and progression always use -1, even when progression follows a prior card."
    ) in instruction


def test_packaged_dependency_lock_identity_matches_the_source_lock() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    assert hashlib.sha256((repository_root / "uv.lock").read_bytes()).hexdigest() == PROGRAM_DEPENDENCY_LOCK_SHA256


def test_built_wheel_loads_the_image_carried_program_without_a_repository_root(tmp_path: Path) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    wheel_dir = tmp_path / "dist"
    built = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir)],
        cwd=repository_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    wheel = next(wheel_dir.glob("tracefold-*.whl"))
    unpacked = tmp_path / "unpacked-wheel"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(unpacked)
    installed_path = tmp_path / "installed-wheel"
    installed_path.symlink_to(unpacked, target_is_directory=True)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(installed_path)
    loaded = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "from tracefold.news.agents import semantic_program as module; "
                "print(Path(module.__file__).resolve()); "
                "print(module.load_stable_program_artifact().program_sha256)"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert loaded.returncode == 0, loaded.stderr
    module_path, program_sha = loaded.stdout.splitlines()
    assert Path(module_path).is_relative_to(unpacked)
    assert program_sha == load_stable_program_artifact().program_sha256


def test_codec_rejects_noncanonical_documents_and_nonfinite_numbers() -> None:
    artifact = load_stable_program_artifact()
    manifest_document, state_document = ProgramArtifactCodec.encode(artifact)
    manifest = json.loads(manifest_document)
    state = json.loads(state_document)

    with pytest.raises(ValueError, match="manifest_json_noncanonical"):
        ProgramArtifactCodec.decode(json.dumps(manifest, indent=2), state_document)
    with pytest.raises(ValueError, match="state_json_noncanonical"):
        ProgramArtifactCodec.decode(manifest_document, canonical_json(state) + "\n\n")

    state["event_semantics"]["max_tokens"] = float("nan")
    with pytest.raises(ValueError, match="state_json_invalid"):
        ProgramArtifactCodec.decode(manifest_document, canonical_json(state))
    state["event_semantics"]["max_tokens"] = float("inf")
    with pytest.raises(ValueError, match="state_json_invalid"):
        ProgramArtifactCodec.decode(manifest_document, canonical_json(state))


def test_codec_rejects_coercive_state_that_cannot_round_trip_exactly() -> None:
    manifest_document, state_document = ProgramArtifactCodec.encode(load_stable_program_artifact())
    state = json.loads(state_document)
    state["event_semantics"]["max_tokens"] = str(state["event_semantics"]["max_tokens"])

    with pytest.raises(ValueError, match="artifact_round_trip_mismatch"):
        ProgramArtifactCodec.decode(manifest_document, canonical_json(state))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda manifest: manifest.update({"parent_program_sha256": "f" * 64}),
        lambda manifest: manifest["compile_receipt"].update({"accepted_by": "unaccepted_candidate"}),
    ],
)
def test_baseline_parent_and_receipt_semantics_cannot_be_recombined(mutate: Any) -> None:
    manifest_document, state_document = ProgramArtifactCodec.encode(load_stable_program_artifact())
    manifest = json.loads(manifest_document)
    mutate(manifest)
    manifest["program_sha256"] = canonical_sha(
        {key: value for key, value in manifest.items() if key != "program_sha256"}
    )

    with pytest.raises(ValueError, match="artifact_schema_invalid"):
        ProgramArtifactCodec.decode(canonical_json(manifest), state_document)


def test_context_hides_audit_ids_from_both_predictors() -> None:
    context = _context(
        told_rows=[
            {
                "event_id": "told-secret",
                "at_ms": 1_005_000,
                "storyline_key": "asset:BTC",
                "magnitude": 1,
                "direction": "bullish",
                "headline_zh": "旧卡片",
            }
        ]
    )
    visible = json.dumps(context.model_payload(), ensure_ascii=False)
    assert "event-secret" not in visible
    assert "fact-secret" not in visible
    assert "told-secret" not in visible
    assert "a" * 64 not in visible
    assert context.told.entries[0].event_id == "told-secret"


def test_context_caps_every_variable_length_model_input() -> None:
    card = {
        "event_id": "event-capped",
        "evidence_version": 1,
        "evidence_sha256": "e" * 64,
        "focus_fact_id": "fact-capped",
        "leader_title": "Bounded input",
        "opened_at_ms": 1_000_000,
        "storyline_key": "theme:bounded",
        "provenance": [f"strategy-{index}" for index in range(40)],
        "grounded_assets": [f"ASSET{index}" for index in range(40)],
    }
    context = TriageContext.from_card(
        card,
        watchlist=[f"WATCH{index}" for index in range(100)],
        told_rows=(),
        now_ms=1_010_000,
        queue_lag_ms=0,
    )

    assert context.evidence.strategies == tuple(f"strategy-{index}" for index in range(16))
    assert context.gate.grounded_assets == tuple(f"ASSET{index}" for index in range(16))
    assert context.watchlist == tuple(f"WATCH{index}" for index in range(64))
    payload = context.model_payload()
    assert len(payload["event"]["strategies"]) == 16
    assert len(payload["gate"]["grounded_assets"]) == 16
    assert len(payload["gate"]["watchlist"]) == 64


def test_told_selection_reserves_same_key_and_preserves_cross_key() -> None:
    rows = [
        {
            "event_id": f"same-{index}",
            "at_ms": 1_009_000 - index,
            "storyline_key": "asset:BTC",
            "magnitude": 1,
            "direction": "bullish",
            "headline_zh": "same",
        }
        for index in range(10)
    ] + [
        {
            "event_id": f"other-{index}",
            "at_ms": 1_008_000 - index,
            "storyline_key": "macro:rates",
            "magnitude": 1,
            "direction": "neutral",
            "headline_zh": "other",
        }
        for index in range(10)
    ]
    entries = _context(told_rows=rows).told.entries
    assert len(entries) == TOLD_MAX
    assert sum(entry.event_id.startswith("same-") for entry in entries) == TOLD_SAME_KEY_MAX
    assert [entry.i for entry in entries] == list(range(TOLD_MAX))


def test_two_predictors_assemble_one_verdict_and_trace_usage() -> None:
    adapter = ScriptedPredictorAdapter(
        [
            PredictorResponse(
                output=_semantics(),
                model="semantic-model",
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                provider_cost_microusd=17,
            ),
            PredictorResponse(
                output=_card(),
                model="reader-model",
                input_tokens=12,
                output_tokens=6,
                total_tokens=18,
                provider_cost_microusd=19,
            ),
        ]
    )
    judgment = asyncio.run(_program(adapter).judge(_context()))
    assert judgment.verdict.title_zh == ""
    assert judgment.answering_model == "reader-model"
    assert judgment.usage.call_count == 2
    assert judgment.usage.physical_call_count == 2
    assert judgment.usage.total_tokens == 33
    assert judgment.usage.provider_cost_microusd == 36
    assert [call.predictor for call in judgment.trace.calls] == ["event_semantics", "reader_card"]
    assert judgment.trace.calls[0].validated_output == _semantics()
    assert judgment.trace.calls[1].upstream_sha256 == judgment.trace.event_semantics_sha256


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update({"program_sha256": "f" * 64}),
        lambda value: value["trace"].update({"verdict_sha256": "f" * 64}),
        lambda value: value["usage"].update({"total_tokens": 999}),
        lambda value: value.update({"fallback_from": "forged"}),
    ],
)
def test_semantic_judgment_rejects_verdict_trace_and_usage_divergence(mutate: Any) -> None:
    judgment = asyncio.run(_program(ScriptedPredictorAdapter([_semantics(), _card()])).judge(_context()))
    data = judgment.model_dump(mode="json")
    mutate(data)

    with pytest.raises(ValueError, match=r"judgment_(trace_identity|usage)_mismatch"):
        type(judgment).model_validate(data)


def test_partial_provider_cost_never_looks_like_complete_program_cost() -> None:
    adapter = ScriptedPredictorAdapter(
        [
            PredictorResponse(output=_semantics(), provider_cost_microusd=17),
            PredictorResponse(output=_card(), provider_cost_microusd=None),
        ]
    )

    judgment = asyncio.run(_program(adapter).judge(_context()))

    assert judgment.trace.calls[0].provider_cost_microusd == 17
    assert judgment.trace.calls[1].provider_cost_microusd is None
    assert judgment.usage.provider_cost_microusd is None


def test_one_retry_is_shared_by_route_and_fallback_restarts_graph() -> None:
    primary = ScriptedPredictorAdapter([_semantics(), {"bad": True}, {"bad": True}])
    fallback = ScriptedPredictorAdapter([_semantics(direction="neutral"), _card()])
    judgment = asyncio.run(_program(primary, fallback=fallback).judge(_context()))
    assert judgment.fallback_from == "news_program_reader_card_invalid"
    assert [request.predictor for request in primary.requests] == [
        "event_semantics",
        "reader_card",
        "reader_card",
    ]
    assert [request.predictor for request in fallback.requests] == ["event_semantics", "reader_card"]
    assert judgment.usage.call_count == 5


def test_cross_field_semantics_retry_reenters_semantics_before_reader_card() -> None:
    adapter = ScriptedPredictorAdapter(
        [
            _semantics(novelty="progression", restates=0),
            _semantics(novelty="progression", restates=-1),
            _card(),
        ]
    )
    context = _context(
        told_rows=[
            {
                "event_id": "prior-card",
                "at_ms": 1_005_000,
                "storyline_key": "asset:BTC",
                "magnitude": 1,
                "direction": "bullish",
                "headline_zh": "比特币此前进展",
            }
        ]
    )

    judgment = asyncio.run(_program(adapter).judge(context))

    assert [(request.predictor, request.attempt) for request in adapter.requests] == [
        ("event_semantics", 1),
        ("event_semantics", 2),
        ("reader_card", 1),
    ]
    assert judgment.verdict.novelty == "progression"
    assert judgment.verdict.restates == -1


def test_exhausted_cross_field_semantics_retry_never_calls_reader_card() -> None:
    adapter = ScriptedPredictorAdapter(
        [
            _semantics(novelty="progression", restates=0),
            _semantics(novelty="progression", restates=0),
            _card(),
        ]
    )

    with pytest.raises(SemanticProgramError) as caught:
        asyncio.run(_program(adapter).judge(_context()))

    assert caught.value.code == "news_program_non_restatement_index_invalid"
    assert [(request.predictor, request.attempt) for request in adapter.requests] == [
        ("event_semantics", 1),
        ("event_semantics", 2),
    ]


def test_chain_budget_is_six_calls_and_reports_partial_trace() -> None:
    primary = ScriptedPredictorAdapter([_semantics(), {"bad": True}, {"bad": True}])
    fallback = ScriptedPredictorAdapter([_semantics(), {"bad": True}, {"bad": True}])
    with pytest.raises(SemanticProgramError) as caught:
        asyncio.run(_program(primary, fallback=fallback).judge(_context()))
    assert caught.value.attempts == 6
    assert caught.value.output_failure is True
    assert caught.value.partial_trace is not None
    assert len(caught.value.partial_trace.calls) == 6


def test_truncation_never_fast_retries() -> None:
    adapter = ScriptedPredictorAdapter([PredictorResponse(output={"bad": True}, finish_reason="length"), _semantics()])
    with pytest.raises(SemanticProgramError) as caught:
        asyncio.run(_program(adapter).judge(_context()))
    assert caught.value.code == "news_program_output_truncated"
    assert caught.value.attempts == 1
    assert len(adapter.requests) == 1


def test_missing_novelty_defaults_only_after_retry_exhaustion() -> None:
    missing = _semantics()
    missing.pop("novelty")
    adapter = ScriptedPredictorAdapter([missing, missing, _card()])
    judgment = asyncio.run(_program(adapter).judge(_context()))
    assert judgment.verdict.novelty == "new_fact"
    assert judgment.trace.novelty_defaulted is True
    assert judgment.usage.call_count == 3


def test_record_replay_uses_full_request_identity_and_real_assembler() -> None:
    scripted = ScriptedPredictorAdapter([_semantics(), _card()])
    original = asyncio.run(_program(scripted).judge(_context()))
    responses = [
        PredictorResponse(
            output=_semantics(),
            model="resolved-replay-sem",
            runtime_binding_sha256=scripted.requests[0].runtime_binding_sha256,
        ),
        PredictorResponse(
            output=_card(),
            model="resolved-replay-card",
            runtime_binding_sha256=scripted.requests[1].runtime_binding_sha256,
        ),
    ]
    recordings = {
        request.request_sha256: {
            "request": {**request.model_dump(mode="json"), "request_sha256": request.request_sha256},
            "response": response.model_dump(mode="json"),
        }
        for request, response in zip(scripted.requests, responses, strict=True)
    }
    replay = RecordReplayPredictorAdapter(recordings)
    repeated = asyncio.run(
        DspyNewsSemanticProgram(load_stable_program_artifact(), primary_adapter=replay).judge(_context())
    )
    assert repeated.verdict == original.verdict
    assert repeated.answering_model == "resolved-replay-card"
    assert [request.request_sha256 for request in replay.requests] == list(recordings)

    with pytest.raises(SemanticProgramError) as caught:
        asyncio.run(
            DspyNewsSemanticProgram(
                load_stable_program_artifact(), primary_adapter=RecordReplayPredictorAdapter({})
            ).judge(_context())
        )
    assert caught.value.code == "news_program_recording_missing"


def test_output_failure_does_not_open_primary_transport_breaker() -> None:
    artifact = _artifact_with_execution(primary_breaker_failures=1)
    primary = ScriptedPredictorAdapter([{"bad": True}, {"bad": True}, _semantics(), _card()])
    fallback = ScriptedPredictorAdapter([_semantics(), _card()])
    program = _program(primary, fallback=fallback, artifact=artifact)
    first = asyncio.run(program.judge(_context()))
    second = asyncio.run(program.judge(_context()))
    assert first.fallback_from == "news_program_event_semantics_invalid"
    assert second.fallback_from is None
    assert second.trace.answering_route == "primary"
    assert len(primary.requests) == 4


def test_transport_failure_opens_only_instance_local_primary_breaker() -> None:
    artifact = _artifact_with_execution(primary_breaker_failures=1)
    primary = ScriptedPredictorAdapter(
        [
            PredictorAdapterError("provider_busy", retryable=True),
            PredictorAdapterError("provider_busy", retryable=True),
        ]
    )
    fallback = ScriptedPredictorAdapter([_semantics(), _card(), _semantics(), _card()])
    program = _program(primary, fallback=fallback, artifact=artifact)
    first = asyncio.run(program.judge(_context()))
    second = asyncio.run(program.judge(_context()))
    assert first.fallback_from == "provider_busy"
    assert second.fallback_from == "primary_circuit_open"
    assert len(primary.requests) == 2
    assert second.usage.call_count == 2
    assert second.usage.physical_call_count == 2
    assert all(call.physical_provider_call for call in second.trace.calls)

    isolated_primary = ScriptedPredictorAdapter([_semantics(), _card()])
    isolated = asyncio.run(_program(isolated_primary, artifact=artifact).judge(_context()))
    assert isolated.trace.answering_route == "primary"


@pytest.mark.parametrize(
    "error_type",
    [
        dspy.LMTransportError,
        dspy.LMServerError,
        dspy.LMTimeoutError,
        dspy.LMRateLimitError,
    ],
)
def test_dspy_transient_lm_errors_are_retryable(error_type: type[Exception]) -> None:
    assert semantic_program_module._is_retryable_exception(error_type("provider failed")) is True


@pytest.mark.parametrize(
    "error_type",
    [
        dspy.LMAuthError,
        dspy.LMInvalidRequestError,
        dspy.ContextWindowExceededError,
    ],
)
def test_dspy_permanent_lm_errors_are_not_retryable(error_type: type[Exception]) -> None:
    assert semantic_program_module._is_retryable_exception(error_type(message="request rejected")) is False


def test_usage_distinguishes_synthetic_trace_entries_from_provider_attempts() -> None:
    class UnresolvedIdentityAdapter:
        def runtime_identity(self, model_binding: str) -> Any:
            del model_binding
            raise PredictorAdapterError("identity_unavailable")

        async def invoke(self, request: Any, predictor: Any) -> PredictorResponse:
            del request, predictor
            raise AssertionError("invoke must not run")

    judgment = asyncio.run(
        DspyNewsSemanticProgram(
            load_stable_program_artifact(),
            primary_adapter=UnresolvedIdentityAdapter(),
            fallback_adapter=ScriptedPredictorAdapter([_semantics(), _card()]),
        ).judge(_context())
    )

    assert judgment.usage.call_count == 3
    assert judgment.usage.physical_call_count == 2
    assert [call.physical_provider_call for call in judgment.trace.calls] == [False, True, True]


def test_transport_error_trace_has_elapsed_time_without_forged_provider_metadata() -> None:
    class SlowTransportAdapter:
        def runtime_identity(self, model_binding: str) -> Any:
            return ScriptedPredictorAdapter([]).runtime_identity(model_binding)

        async def invoke(self, request: Any, predictor: Any) -> PredictorResponse:
            del request, predictor
            await asyncio.sleep(0.002)
            raise ConnectionError("provider unavailable")

    with pytest.raises(SemanticProgramError) as caught:
        asyncio.run(
            DspyNewsSemanticProgram(load_stable_program_artifact(), primary_adapter=SlowTransportAdapter()).judge(
                _context()
            )
        )

    assert caught.value.partial_trace is not None
    assert len(caught.value.partial_trace.calls) == 2
    for call in caught.value.partial_trace.calls:
        assert call.latency_ms > 0
        assert call.provider is None
        assert call.model is None
        assert call.provider_cost_microusd is None


def test_per_predictor_adapter_bindings_are_explicit() -> None:
    semantics_adapter = ScriptedPredictorAdapter([_semantics()], model_name="semantic-only")
    reader_adapter = ScriptedPredictorAdapter([_card()], model_name="reader-only")
    judgment = asyncio.run(
        DspyNewsSemanticProgram(
            load_stable_program_artifact(),
            primary_adapter={
                "event_semantics.primary": semantics_adapter,
                "reader_card.primary": reader_adapter,
            },
        ).judge(_context())
    )
    assert judgment.answering_model == "reader-only"
    assert len(semantics_adapter.requests) == len(reader_adapter.requests) == 1


def test_route_deadline_is_shared_and_audited() -> None:
    class SlowAdapter:
        def runtime_identity(self, model_binding: str) -> Any:
            return ScriptedPredictorAdapter([]).runtime_identity(model_binding)

        async def invoke(self, request: Any, predictor: Any) -> PredictorResponse:
            del request, predictor
            await asyncio.sleep(2)
            return PredictorResponse(output=_semantics())

    artifact = _artifact_with_execution(route_deadline_seconds=1)
    with pytest.raises(SemanticProgramError) as caught:
        asyncio.run(DspyNewsSemanticProgram(artifact, primary_adapter=SlowAdapter()).judge(_context()))
    assert caught.value.code == "news_program_route_deadline"
    assert caught.value.retryable is True
    assert caught.value.attempts == 1
    assert caught.value.partial_trace is not None
    assert caught.value.partial_trace.calls[0].error_code == "news_program_route_deadline"
    assert caught.value.partial_trace.calls[0].latency_ms >= 900


def test_reader_card_deadline_is_attributed_to_reader_predictor() -> None:
    class SlowReaderAdapter:
        def runtime_identity(self, model_binding: str) -> Any:
            return ScriptedPredictorAdapter([]).runtime_identity(model_binding)

        async def invoke(self, request: Any, predictor: Any) -> PredictorResponse:
            del request, predictor
            await asyncio.sleep(2)
            return PredictorResponse(output=_card())

    artifact = _artifact_with_execution(route_deadline_seconds=1)
    with pytest.raises(SemanticProgramError) as caught:
        asyncio.run(
            DspyNewsSemanticProgram(
                artifact,
                primary_adapter={
                    "event_semantics.primary": ScriptedPredictorAdapter([_semantics()]),
                    "reader_card.primary": SlowReaderAdapter(),
                },
            ).judge(_context())
        )
    assert caught.value.partial_trace is not None
    assert [call.predictor for call in caught.value.partial_trace.calls] == [
        "event_semantics",
        "reader_card",
    ]
    assert caught.value.partial_trace.calls[-1].error_code == "news_program_route_deadline"
    assert caught.value.partial_trace.calls[-1].latency_ms >= 900


def test_runtime_lm_factory_rejects_retry_or_cache_override() -> None:
    with pytest.raises(ValueError, match="runtime_model_kwargs_owned"):
        DspyPredictorAdapter.from_runtime(
            model_name="openai/test",
            api_key="secret",
            api_base="https://provider.invalid/v1",
            timeout=5,
            max_tokens=100,
            model_kwargs={"cache": True},
        )


def test_dspy_adapter_fails_closed_without_an_exact_provider_response() -> None:
    class FakePrediction:
        def toDict(self) -> dict[str, Any]:
            return _semantics()

        def get_lm_usage(self) -> dict[str, dict[str, Any]]:
            return {
                "openai/test": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                    "prompt_tokens_details": {"cached_tokens": 3},
                    "response_cost": 0.000012,
                }
            }

    class FakePredictor:
        async def acall(self, **inputs: Any) -> FakePrediction:
            assert inputs == {"evidence_json": "{}"}
            return FakePrediction()

    adapter = DspyPredictorAdapter.from_runtime(
        model_name="openai/test",
        api_key="secret",
        api_base="https://provider.invalid/v1",
        timeout=5,
        max_tokens=100,
    )
    runtime = adapter.runtime_identity("event_semantics.primary")
    request = PredictorRequest(
        program_version="test",
        program_sha256="a" * 64,
        context_sha256="b" * 64,
        predictor="event_semantics",
        route="primary",
        attempt=1,
        signature_sha256="c" * 64,
        instruction_sha256="d" * 64,
        demos_sha256="e" * 64,
        adapter_sha256="f" * 64,
        model_binding="event_semantics.primary",
        runtime_provider=runtime.provider,
        runtime_model=runtime.model,
        runtime_model_sha256=runtime.model_sha256,
        runtime_binding_sha256=runtime.binding_sha256,
        inputs={"evidence_json": "{}"},
    )
    with pytest.raises(PredictorAdapterError, match="provider_metadata_unavailable"):
        asyncio.run(adapter.invoke(request, FakePredictor()))  # type: ignore[arg-type]


def test_dspy_adapter_observes_each_exact_33_provider_response_under_concurrency() -> None:
    """Provider metadata belongs to the in-flight call, never shared ``LM.history[-1]``."""

    from litellm import ModelResponse

    class FakePrediction:
        def __init__(self, marker: str) -> None:
            self._marker = marker

        def toDict(self) -> dict[str, Any]:
            return _semantics(event_type=self._marker)

        def get_lm_usage(self) -> dict[str, Any]:
            # DSPy usage aggregation intentionally omits finish reason and cost.
            return {}

    class FakePredictor:
        async def acall(self, **inputs: Any) -> FakePrediction:
            marker = str(inputs["evidence_json"])
            await dspy.settings.lm.acall(messages=[{"role": "user", "content": marker}])
            return FakePrediction(marker)

    adapter = DspyPredictorAdapter.from_runtime(
        model_name="openai/test",
        api_key="secret",
        api_base="https://provider.invalid/v1",
        timeout=5,
        max_tokens=100,
    )

    async def fake_aforward(*, prompt: Any = None, messages: Any = None, **kwargs: Any) -> ModelResponse:
        del prompt, kwargs
        marker = str(messages[0]["content"])
        if marker == "slow":
            await asyncio.sleep(0.02)
        tokens = 11 if marker == "slow" else 23
        response = ModelResponse(
            model=f"resolved-{marker}",
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "{}"},
                    "finish_reason": "length" if marker == "slow" else "stop",
                }
            ],
            usage={
                "prompt_tokens": tokens,
                "completion_tokens": 5,
                "total_tokens": tokens + 5,
                "prompt_tokens_details": {"cached_tokens": tokens - 10},
            },
        )
        response._hidden_params["response_cost"] = tokens / 1_000_000
        return response

    adapter._lm.aforward = fake_aforward  # type: ignore[method-assign]

    def request(marker: str) -> PredictorRequest:
        return PredictorRequest(
            program_version="test",
            program_sha256="a" * 64,
            context_sha256=canonical_sha(marker),
            predictor="event_semantics",
            route="primary",
            attempt=1,
            signature_sha256="c" * 64,
            instruction_sha256="d" * 64,
            demos_sha256="e" * 64,
            adapter_sha256="f" * 64,
            model_binding="event_semantics.primary",
            runtime_provider="openai",
            runtime_model="openai/test",
            runtime_model_sha256=canonical_sha({"provider": "openai", "model": "openai/test"}),
            runtime_binding_sha256=canonical_sha(
                {
                    "provider": "openai",
                    "model": "openai/test",
                    "model_sha256": canonical_sha({"provider": "openai", "model": "openai/test"}),
                }
            ),
            inputs={"evidence_json": marker},
        )

    async def run() -> tuple[PredictorResponse, PredictorResponse]:
        slow, fast = await asyncio.gather(
            adapter.invoke(request("slow"), FakePredictor()),  # type: ignore[arg-type]
            adapter.invoke(request("fast"), FakePredictor()),  # type: ignore[arg-type]
        )
        return slow, fast

    slow, fast = asyncio.run(run())
    assert (slow.finish_reason, slow.input_tokens, slow.cached_tokens, slow.provider_cost_microusd) == (
        "length",
        11,
        1,
        11,
    )
    assert (fast.finish_reason, fast.input_tokens, fast.cached_tokens, fast.provider_cost_microusd) == (
        "stop",
        23,
        13,
        23,
    )


def test_request_and_strict_replay_are_bound_to_one_runtime_model_identity() -> None:
    model_a_sha = canonical_sha({"provider": "openai", "model": "model-a"})
    model_b_sha = canonical_sha({"provider": "openai", "model": "model-b"})
    shared = {
        "program_version": "test",
        "program_sha256": "a" * 64,
        "context_sha256": "b" * 64,
        "predictor": "event_semantics",
        "route": "primary",
        "attempt": 1,
        "signature_sha256": "c" * 64,
        "instruction_sha256": "d" * 64,
        "demos_sha256": "e" * 64,
        "adapter_sha256": "f" * 64,
        "model_binding": "event_semantics.primary",
        "runtime_provider": "openai",
        "inputs": {"evidence_json": "{}"},
    }
    request_a = PredictorRequest(
        **shared,
        runtime_model="model-a",
        runtime_model_sha256=model_a_sha,
        runtime_binding_sha256=canonical_sha({"provider": "openai", "model": "model-a", "model_sha256": model_a_sha}),
    )
    request_b = PredictorRequest(
        **shared,
        runtime_model="model-b",
        runtime_model_sha256=model_b_sha,
        runtime_binding_sha256=canonical_sha({"provider": "openai", "model": "model-b", "model_sha256": model_b_sha}),
    )
    assert request_a.request_sha256 != request_b.request_sha256

    response_a = PredictorResponse(
        output=_semantics(),
        provider="openai",
        model="model-a",
        model_sha256=model_a_sha,
        runtime_binding_sha256=request_a.runtime_binding_sha256,
    )
    replay = RecordReplayPredictorAdapter(
        {
            request_a.request_sha256: {
                "request": {**request_a.model_dump(mode="json"), "request_sha256": request_a.request_sha256},
                "response": response_a.model_dump(mode="json"),
            }
        }
    )
    assert asyncio.run(replay.invoke(request_a, object())) == response_a  # type: ignore[arg-type]
    with pytest.raises(PredictorAdapterError, match="recording_missing"):
        asyncio.run(replay.invoke(request_b, object()))  # type: ignore[arg-type]

    mismatched = response_a.model_copy(update={"runtime_binding_sha256": request_b.runtime_binding_sha256})
    with pytest.raises(ValueError, match="recording_model_identity_mismatch"):
        RecordReplayPredictorAdapter(
            {
                request_a.request_sha256: {
                    "request": {**request_a.model_dump(mode="json"), "request_sha256": request_a.request_sha256},
                    "response": mismatched.model_dump(mode="json"),
                }
            }
        )


def test_real_dspy_33_finish_reason_classifies_parse_failure_as_truncation() -> None:
    from dspy.utils.exceptions import AdapterParseError
    from litellm import ModelResponse

    class TruncatedPredictor:
        async def acall(self, **inputs: Any) -> Any:
            del inputs
            await dspy.settings.lm.acall(messages=[{"role": "user", "content": "truncated"}])
            partial = _semantics()
            partial.pop("novelty")
            raise AdapterParseError(
                "DspyStrictJSONAdapter",
                dspy.Signature("evidence_json -> semantics"),
                '{"semantics":',
                parsed_result={"semantics": partial},  # type: ignore[arg-type]
            )

    adapter = DspyPredictorAdapter.from_runtime(
        model_name="openai/test",
        api_key="secret",
        api_base="https://provider.invalid/v1",
        timeout=5,
        max_tokens=100,
    )

    async def fake_aforward(*, prompt: Any = None, messages: Any = None, **kwargs: Any) -> ModelResponse:
        del prompt, messages, kwargs
        response = ModelResponse(
            model="resolved-test",
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": '{"semantics":'},
                    "finish_reason": "length",
                }
            ],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        )
        response._hidden_params["response_cost"] = 0.00001
        return response

    adapter._lm.aforward = fake_aforward  # type: ignore[method-assign]
    runtime = adapter.runtime_identity("event_semantics.primary")
    request = PredictorRequest(
        program_version="test",
        program_sha256="a" * 64,
        context_sha256="b" * 64,
        predictor="event_semantics",
        route="primary",
        attempt=1,
        signature_sha256="c" * 64,
        instruction_sha256="d" * 64,
        demos_sha256="e" * 64,
        adapter_sha256="f" * 64,
        model_binding="event_semantics.primary",
        runtime_provider=runtime.provider,
        runtime_model=runtime.model,
        runtime_model_sha256=runtime.model_sha256,
        runtime_binding_sha256=runtime.binding_sha256,
        inputs={"evidence_json": "{}"},
    )
    with pytest.raises(PredictorAdapterError) as caught:
        asyncio.run(adapter.invoke(request, TruncatedPredictor()))  # type: ignore[arg-type]
    assert caught.value.code == "news_program_output_truncated"
    assert caught.value.finish_reason == "length"
    observation = caught.value.provider_observation
    assert isinstance(observation, ProviderCallObservation)
    assert (observation.input_tokens, observation.output_tokens, observation.total_tokens) == (7, 3, 10)
    assert observation.provider_cost_microusd == 10
    assert observation.finish_reason == "length"
    assert caught.value.partial_output is not None
    assert adapter._lm.history == []

    reader = ScriptedPredictorAdapter([_card()])
    program = DspyNewsSemanticProgram(
        load_stable_program_artifact(),
        primary_adapter={
            "event_semantics.primary": adapter,
            "reader_card.primary": reader,
        },
    )
    program.event_semantics = TruncatedPredictor()  # type: ignore[assignment]
    with pytest.raises(SemanticProgramError) as program_error:
        asyncio.run(program.judge(_context()))
    assert program_error.value.attempts == 1
    assert program_error.value.code == "news_program_output_truncated"
    assert program_error.value.partial_trace is not None
    assert program_error.value.partial_trace.novelty_defaulted is False
    assert reader.requests == []


@pytest.mark.parametrize(("finish_reason", "expected_attempts"), [("stop", 2), ("length", 1)])
def test_real_dspy_parse_failure_trace_keeps_exact_usage_and_truncation_does_not_retry(
    finish_reason: str,
    expected_attempts: int,
) -> None:
    from litellm import ModelResponse

    adapter = DspyPredictorAdapter.from_runtime(
        model_name="openai/test",
        api_key="secret",
        api_base="https://provider.invalid/v1",
        timeout=5,
        max_tokens=100,
    )

    async def fake_aforward(*, prompt: Any = None, messages: Any = None, **kwargs: Any) -> ModelResponse:
        del prompt, messages, kwargs
        await asyncio.sleep(0.002)
        response = ModelResponse(
            model="resolved-parse-test",
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "{}"},
                    "finish_reason": finish_reason,
                }
            ],
            usage={"prompt_tokens": 13, "completion_tokens": 2, "total_tokens": 15},
        )
        response._hidden_params["response_cost"] = 0.000012
        return response

    adapter._lm.aforward = fake_aforward  # type: ignore[method-assign]
    program = DspyNewsSemanticProgram(load_stable_program_artifact(), primary_adapter=adapter)

    with pytest.raises(SemanticProgramError) as caught:
        asyncio.run(program.judge(_context()))

    assert caught.value.attempts == expected_attempts
    assert caught.value.partial_trace is not None
    assert len(caught.value.partial_trace.calls) == expected_attempts
    for call in caught.value.partial_trace.calls:
        assert call.provider == "openai"
        assert call.model == "resolved-parse-test"
        assert call.model_sha256 == canonical_sha({"provider": "openai", "model": "resolved-parse-test"})
        assert call.latency_ms > 0
        assert (call.input_tokens, call.output_tokens, call.total_tokens) == (13, 2, 15)
        assert call.provider_cost_microusd == 12
        assert call.finish_reason == finish_reason
    assert adapter._lm.history == []


def test_real_adapter_partial_semantics_defaults_novelty_only_after_retry_is_exhausted() -> None:
    from dspy.utils.exceptions import AdapterParseError
    from litellm import ModelResponse

    class PartialSemanticsPredictor:
        async def acall(self, **inputs: Any) -> Any:
            del inputs
            await dspy.settings.lm.acall(messages=[{"role": "user", "content": "partial"}])
            partial = _semantics()
            partial.pop("novelty")
            raise AdapterParseError(
                "DspyStrictJSONAdapter",
                dspy.Signature("evidence_json -> semantics"),
                "safe partial result",
                parsed_result={"semantics": partial},  # type: ignore[arg-type]
            )

    adapter = DspyPredictorAdapter.from_runtime(
        model_name="openai/test",
        api_key="secret",
        api_base="https://provider.invalid/v1",
        timeout=5,
        max_tokens=100,
    )

    async def fake_aforward(*, prompt: Any = None, messages: Any = None, **kwargs: Any) -> ModelResponse:
        del prompt, messages, kwargs
        response = ModelResponse(
            model="resolved-partial-test",
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "{}"},
                    "finish_reason": "stop",
                }
            ],
            usage={"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10},
        )
        response._hidden_params["response_cost"] = 0.000007
        return response

    adapter._lm.aforward = fake_aforward  # type: ignore[method-assign]
    reader = ScriptedPredictorAdapter([_card()])
    program = DspyNewsSemanticProgram(
        load_stable_program_artifact(),
        primary_adapter={
            "event_semantics.primary": adapter,
            "reader_card.primary": reader,
        },
    )
    program.event_semantics = PartialSemanticsPredictor()  # type: ignore[assignment]

    judgment = asyncio.run(program.judge(_context()))

    assert judgment.verdict.novelty == "new_fact"
    assert judgment.verdict.restates == -1
    assert judgment.trace.novelty_defaulted is True
    assert judgment.usage.call_count == 3
    assert judgment.usage.provider_cost_microusd is None
    assert [call.error_code for call in judgment.trace.calls[:2]] == [
        "news_program_dspy_output_adapterparseerror",
        "news_program_novelty_defaulted",
    ]
    assert all(call.provider_cost_microusd == 7 for call in judgment.trace.calls[:2])
    assert adapter._lm.history == []


def test_real_dspy_33_predict_path_returns_exact_response_metadata() -> None:
    from litellm import ModelResponse

    class Signature(dspy.Signature):
        evidence_json: str = dspy.InputField()
        semantics: dict[str, Any] = dspy.OutputField()

    adapter = DspyPredictorAdapter.from_runtime(
        model_name="openai/configured-alias",
        api_key="secret",
        api_base="https://provider.invalid/v1",
        timeout=5,
        max_tokens=100,
    )

    async def fake_aforward(*, prompt: Any = None, messages: Any = None, **kwargs: Any) -> ModelResponse:
        del prompt, messages
        assert kwargs["response_format"] == {"type": "json_object"}
        response = ModelResponse(
            model="resolved-model-2026-08-22",
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps({"semantics": {"ok": True}})},
                    "finish_reason": "stop",
                }
            ],
            usage={"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10},
        )
        response._hidden_params["response_cost"] = 0.000009
        return response

    adapter._lm.aforward = fake_aforward  # type: ignore[method-assign]
    runtime = adapter.runtime_identity("event_semantics.primary")
    request = PredictorRequest(
        program_version="test",
        program_sha256="a" * 64,
        context_sha256="b" * 64,
        predictor="event_semantics",
        route="primary",
        attempt=1,
        signature_sha256="c" * 64,
        instruction_sha256="d" * 64,
        demos_sha256="e" * 64,
        adapter_sha256="f" * 64,
        model_binding="event_semantics.primary",
        runtime_provider=runtime.provider,
        runtime_model=runtime.model,
        runtime_model_sha256=runtime.model_sha256,
        runtime_binding_sha256=runtime.binding_sha256,
        inputs={"evidence_json": "{}"},
    )

    response = asyncio.run(adapter.invoke(request, dspy.Predict(Signature)))
    assert response.output == {"semantics": {"ok": True}}
    assert response.model == "resolved-model-2026-08-22"
    assert (response.finish_reason, response.input_tokens, response.total_tokens) == ("stop", 8, 10)
    assert response.provider_cost_microusd == 9
    assert response.runtime_binding_sha256 == request.runtime_binding_sha256


def test_artifact_rejects_tamper_unknown_binding_and_unregistered_identity(tmp_path: Path) -> None:
    artifact = load_stable_program_artifact()
    manifest_document, state_document = ProgramArtifactCodec.encode(artifact)
    state = json.loads(state_document)
    manifest = json.loads(manifest_document)
    state["event_semantics"]["model_bindings"]["primary"] = "https://evil.invalid/model"
    manifest["state_sha256"] = canonical_sha(state)
    manifest["program_sha256"] = canonical_sha(
        {key: value for key, value in manifest.items() if key != "program_sha256"}
    )
    with pytest.raises(ValueError):
        ProgramArtifactCodec.decode(json.dumps(manifest), json.dumps(state))
    with pytest.raises(ValueError, match="not_registered"):
        load_program_artifact("0" * 64)
    with pytest.raises(ValueError, match="path_invalid"):
        ProgramArtifactCodec.load("../not-an-image")

    image = tmp_path / artifact.program_sha256
    image.mkdir()
    (image / "manifest.json").write_text(manifest_document, encoding="utf-8")
    (image / "state.json").write_text(state_document, encoding="utf-8")
    (image / "unexpected.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="files_invalid"):
        ProgramArtifactCodec.load(str(image))

    symlink_root = tmp_path / "symlink-case"
    symlink_image = symlink_root / artifact.program_sha256
    symlink_image.mkdir(parents=True)
    manifest_source = symlink_root / "reviewed-manifest.json"
    manifest_source.write_text(manifest_document, encoding="utf-8")
    (symlink_image / "manifest.json").symlink_to(manifest_source)
    (symlink_image / "state.json").write_text(state_document, encoding="utf-8")
    with pytest.raises(ValueError, match="files_invalid"):
        ProgramArtifactCodec.load(str(symlink_image))


@pytest.mark.parametrize(
    "unsafe_key",
    [
        "History",
        "request-callback",
        "clientCredential",
        "auth",
        "Authorization",
        "httpHeaders",
        "providerEndpoint",
        "apiBase",
        "baseURL",
        "clientSecret",
        "accessToken",
        "password",
        "modelList",
    ],
)
def test_artifact_recursively_rejects_runtime_and_secret_key_variants(unsafe_key: str) -> None:
    manifest_document, state_document = ProgramArtifactCodec.encode(load_stable_program_artifact())
    state = json.loads(state_document)
    state["event_semantics"]["nestedRuntimeState"] = {unsafe_key: "must-not-load"}

    with pytest.raises(ValueError, match="unsafe_state_key"):
        ProgramArtifactCodec.decode(manifest_document, canonical_json(state))


@pytest.mark.parametrize(
    "demo",
    [
        {"evidence_json": "{}", "semantics": _semantics(), "case_id": "extra"},
        {"evidence_json": "{}"},
        {"evidence_json": "{}", "semantics_json": "{}", "card": _card()},
    ],
)
def test_artifact_demo_fields_are_exact_for_each_known_signature(demo: dict[str, Any]) -> None:
    manifest, state = _artifact_documents_with_demo("event_semantics", demo)

    with pytest.raises(ValueError, match="artifact_schema_invalid"):
        ProgramArtifactCodec.decode(manifest, state)


def test_artifact_bounds_instruction_demo_count_and_canonical_demo_json() -> None:
    def documents(state: dict[str, Any]) -> tuple[str, str]:
        manifest_document, _ = ProgramArtifactCodec.encode(load_stable_program_artifact())
        manifest = json.loads(manifest_document)
        manifest["state_sha256"] = canonical_sha(state)
        manifest["program_sha256"] = canonical_sha(
            {key: value for key, value in manifest.items() if key != "program_sha256"}
        )
        return canonical_json(manifest), canonical_json(state)

    _, state_document = ProgramArtifactCodec.encode(load_stable_program_artifact())
    state = json.loads(state_document)
    state["event_semantics"]["instruction"] = "x" * (PROGRAM_INSTRUCTION_MAX_BYTES + 1)
    state["event_semantics"]["instruction_sha256"] = canonical_sha(state["event_semantics"]["instruction"])
    with pytest.raises(ValueError, match="artifact_schema_invalid"):
        ProgramArtifactCodec.decode(*documents(state))

    state = json.loads(state_document)
    demo = {"evidence_json": "{}", "semantics": _semantics()}
    state["event_semantics"]["demos"] = [demo] * (PROGRAM_DEMOS_MAX + 1)
    state["event_semantics"]["demos_sha256"] = canonical_sha(state["event_semantics"]["demos"])
    with pytest.raises(ValueError, match="artifact_schema_invalid"):
        ProgramArtifactCodec.decode(*documents(state))

    oversized_evidence = canonical_json({"blob": "x" * PROGRAM_DEMO_JSON_MAX_BYTES})
    manifest, state_document = _artifact_documents_with_demo(
        "event_semantics",
        {"evidence_json": oversized_evidence, "semantics": _semantics()},
    )
    with pytest.raises(ValueError, match="artifact_schema_invalid"):
        ProgramArtifactCodec.decode(manifest, state_document)

    manifest, state_document = _artifact_documents_with_demo(
        "event_semantics",
        {"evidence_json": '{ "noncanonical": true }', "semantics": _semantics()},
    )
    with pytest.raises(ValueError, match="artifact_schema_invalid"):
        ProgramArtifactCodec.decode(manifest, state_document)


@pytest.mark.parametrize(
    ("container", "unsafe_key", "unsafe_value"),
    [
        (None, "event_id", "audit-event-must-not-reach-the-model"),
        ("event", "endpoint", "https://evil.invalid"),
        ("gate", "api_key", "sk-test-must-not-load"),
    ],
)
def test_artifact_demo_evidence_rejects_audit_runtime_and_secret_fields(
    container: str | None,
    unsafe_key: str,
    unsafe_value: str,
) -> None:
    evidence = _context().model_payload()
    target = evidence if container is None else evidence[container]
    target[unsafe_key] = unsafe_value
    manifest, state_document = _artifact_documents_with_demo(
        "event_semantics",
        {"evidence_json": canonical_json(evidence), "semantics": _semantics()},
    )

    with pytest.raises(ValueError, match="artifact_schema_invalid"):
        ProgramArtifactCodec.decode(manifest, state_document)


def test_compiled_module_uses_the_same_exact_demo_validation() -> None:
    base = load_stable_program_artifact()
    cold = DspyCompileProgram(base)
    cold.event_semantics.demos = [
        dspy.Example(evidence_json="{}", semantics=_semantics(), unexpected="unsafe").with_inputs("evidence_json")
    ]

    with pytest.raises(ValueError, match="demo_fields_invalid"):
        ProgramArtifactCodec.from_compiled_module(
            cold,
            base_artifact=base,
            compiler="GEPA",
            source="sealed-development-set",
            compile_provenance=CompileProvenance(
                mode="optimizer_candidate",
                development_dataset_sha="1" * 64,
                learning_epoch="epoch-1",
                optimizer="dspy.GEPA@3.3.0/gepa@0.1.1",
                gepa_version="0.1.1",
                metric_sha256="2" * 64,
                optimizer_config_sha256="3" * 64,
                seed=7,
                max_metric_calls=20,
                max_task_model_calls=30,
                max_cost_microusd=10_000,
                metric_calls=12,
                task_model_calls=15,
                reflection_model_calls=3,
                actual_cost_microusd=9_000,
                trajectory_sha256="4" * 64,
                checkpoint_sha256="5" * 64,
            ),
        )


def test_production_registry_and_image_directories_reject_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = load_stable_program_artifact()
    manifest, state = ProgramArtifactCodec.encode(artifact)
    package_root = tmp_path / "agents"
    programs = package_root / "programs"
    programs.mkdir(parents=True)
    monkeypatch.setattr(importlib.resources, "files", lambda package: package_root)

    real_registry = tmp_path / "registry-source.json"
    real_registry.write_text(
        canonical_json({"stable": artifact.program_sha256, "images": [artifact.program_sha256]}) + "\n",
        encoding="utf-8",
    )
    (programs / "registry.json").symlink_to(real_registry)
    with pytest.raises(ValueError, match="artifact_path_invalid"):
        load_program_artifact(artifact.program_sha256)

    (programs / "registry.json").unlink()
    (programs / "registry.json").write_text(real_registry.read_text(encoding="utf-8"), encoding="utf-8")
    external_image = tmp_path / "external-image"
    external_image.mkdir()
    (external_image / "manifest.json").write_text(manifest, encoding="utf-8")
    (external_image / "state.json").write_text(state, encoding="utf-8")
    (programs / artifact.program_sha256).symlink_to(external_image, target_is_directory=True)
    with pytest.raises(ValueError, match="artifact_path_invalid"):
        load_program_artifact(artifact.program_sha256)

    symlinked_package_root = tmp_path / "symlinked-agents"
    symlinked_package_root.mkdir()
    external_programs = tmp_path / "external-programs"
    external_programs.mkdir()
    (symlinked_package_root / "programs").symlink_to(external_programs, target_is_directory=True)
    monkeypatch.setattr(importlib.resources, "files", lambda package: symlinked_package_root)
    with pytest.raises(ValueError, match="registry_path_invalid"):
        load_program_artifact(artifact.program_sha256)


def test_cold_compile_program_round_trips_only_predictor_state() -> None:
    base = load_stable_program_artifact()
    cold = DspyCompileProgram(base)
    assert len(cold.named_predictors()) == 2
    cold.event_semantics.signature = cold.event_semantics.signature.with_instructions(
        "A reviewed candidate instruction"
    )
    cold.event_semantics.demos = [
        dspy.Example(evidence_json=_evidence_json(), semantics=_semantics()).with_inputs("evidence_json")
    ]
    candidate = ProgramArtifactCodec.from_compiled_module(
        cold,
        base_artifact=base,
        compiler="GEPA",
        source="sealed-development-set",
        compile_provenance=CompileProvenance(
            mode="optimizer_candidate",
            development_dataset_sha="1" * 64,
            learning_epoch="epoch-1",
            optimizer="dspy.GEPA@3.3.0/gepa@0.1.1",
            gepa_version="0.1.1",
            metric_sha256="2" * 64,
            optimizer_config_sha256="3" * 64,
            seed=7,
            max_metric_calls=20,
            max_task_model_calls=30,
            max_cost_microusd=10_000,
            metric_calls=12,
            task_model_calls=15,
            reflection_model_calls=3,
            actual_cost_microusd=9_000,
            trajectory_sha256="4" * 64,
            checkpoint_sha256="5" * 64,
        ),
    )
    assert candidate.program_sha256 != base.program_sha256
    assert candidate.parent_program_sha256 == base.program_sha256
    assert candidate.compile_receipt.accepted_by == "unaccepted_candidate"
    manifest, state = ProgramArtifactCodec.encode(candidate)
    assert ProgramArtifactCodec.decode(manifest, state) == candidate
    assert candidate.event_semantics.demos == ({"evidence_json": _evidence_json(), "semantics": _semantics()},)
    candidate_manifest = json.loads(manifest)
    candidate_manifest["parent_program_sha256"] = None
    candidate_manifest["program_sha256"] = canonical_sha(
        {key: value for key, value in candidate_manifest.items() if key != "program_sha256"}
    )
    with pytest.raises(ValueError, match="artifact_schema_invalid"):
        ProgramArtifactCodec.decode(canonical_json(candidate_manifest), state)


def test_adapter_error_is_domain_classified() -> None:
    adapter = ScriptedPredictorAdapter(
        [PredictorAdapterError("provider_busy", retryable=True), PredictorAdapterError("provider_busy", retryable=True)]
    )
    with pytest.raises(SemanticProgramError) as caught:
        asyncio.run(_program(adapter).judge(_context()))
    assert caught.value.retryable is True
    assert caught.value.output_failure is False
    assert caught.value.attempts == 2


def test_no_unsafe_serialization_surface_in_production_module() -> None:
    source = Path(__file__).resolve().parents[2] / "src/tracefold/news/agents/semantic_program.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".", maxsplit=1)[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert imported.isdisjoint({"pickle", "cloudpickle"})
