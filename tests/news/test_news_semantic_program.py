from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tracefold.news.agents.semantic_program import (
    TOLD_MAX,
    TOLD_SAME_KEY_MAX,
    CompileProvenance,
    DspyCompileProgram,
    DspyNewsSemanticProgram,
    DspyPredictorAdapter,
    PredictorAdapterError,
    PredictorRequest,
    PredictorResponse,
    ProgramArtifact,
    ProgramArtifactCodec,
    RecordReplayPredictorAdapter,
    ScriptedPredictorAdapter,
    SemanticProgramError,
    TriageContext,
    load_program_artifact,
    load_stable_program_artifact,
)
from tracefold.news.artifact_identity import canonical_sha


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


def test_builtin_artifact_is_registered_and_canonical() -> None:
    artifact = load_stable_program_artifact()
    assert load_program_artifact(artifact.program_sha256) == artifact
    manifest, state = ProgramArtifactCodec.encode(artifact)
    assert ProgramArtifactCodec.decode(manifest, state) == artifact
    assert artifact.program_sha256 == artifact.computed_sha256()
    assert artifact.state_sha256 == artifact.computed_state_sha256()


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
    assert judgment.usage.total_tokens == 33
    assert judgment.usage.provider_cost_microusd == 36
    assert [call.predictor for call in judgment.trace.calls] == ["event_semantics", "reader_card"]
    assert judgment.trace.calls[0].validated_output == _semantics()
    assert judgment.trace.calls[1].upstream_sha256 == judgment.trace.event_semantics_sha256


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
    recordings = {
        scripted.requests[0].request_sha256: PredictorResponse(output=_semantics(), model="replay-sem"),
        scripted.requests[1].request_sha256: PredictorResponse(output=_card(), model="replay-card"),
    }
    replay = RecordReplayPredictorAdapter(recordings)
    repeated = asyncio.run(
        DspyNewsSemanticProgram(load_stable_program_artifact(), primary_adapter=replay).judge(_context())
    )
    assert repeated.verdict == original.verdict
    assert repeated.answering_model == "replay-card"
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

    isolated_primary = ScriptedPredictorAdapter([_semantics(), _card()])
    isolated = asyncio.run(_program(isolated_primary, artifact=artifact).judge(_context()))
    assert isolated.trace.answering_route == "primary"


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


def test_reader_card_deadline_is_attributed_to_reader_predictor() -> None:
    class SlowReaderAdapter:
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


def test_dspy_adapter_extracts_nested_cached_tokens_and_explicit_cost() -> None:
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
    response = asyncio.run(
        adapter.invoke(
            PredictorRequest(
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
                inputs={"evidence_json": "{}"},
            ),
            FakePredictor(),  # type: ignore[arg-type]
        )
    )
    assert response.cached_tokens == 3
    assert response.provider_cost_microusd == 12
    assert response.provider == "openai"


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


def test_cold_compile_program_round_trips_only_predictor_state() -> None:
    base = load_stable_program_artifact()
    cold = DspyCompileProgram(base)
    assert len(cold.named_predictors()) == 2
    cold.event_semantics.signature = cold.event_semantics.signature.with_instructions(
        "A reviewed candidate instruction"
    )
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
