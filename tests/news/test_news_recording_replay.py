from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any

import pytest

from tracefold.news.agents.semantic_program import (
    DspyNewsSemanticProgram,
    ProgramCallTrace,
    ScriptedPredictorAdapter,
    load_stable_program_artifact,
)
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.recording_replay import (
    RecordingReplayError,
    RecordingReplayMiss,
    ReplayArmSpec,
    load_recording_replay_capability,
)
from tracefold.news.semantic_contract import TriageContext

RUN_SHA = "a" * 64
STABLE_BUNDLE_SHA = "1" * 64
CANDIDATE_BUNDLE_SHA = "2" * 64


def _semantics(**updates: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "novelty": "new_fact",
        "restates": -1,
        "event_type": "listing",
        "assets": [{"symbol": "BTC", "market_type": "spot", "role": "primary"}],
        "direction": "bullish",
        "scope": "single_name",
        "magnitude": 1,
        "confidence": 0.8,
        "audience": "crypto",
        "relevance": {
            "impact_breadth": "single_instrument",
            "tradability": "direct",
            "surprise": "unscheduled",
            "development_delta": "state_change",
            "channels": ["exchange_access"],
            "affected_markets": ["single_asset"],
            "reader_value": "realtime",
        },
    }
    value.update(updates)
    return value


def _card() -> dict[str, Any]:
    return {
        "headline_zh": "比特币出现新进展",
        "why_zh": "值得关注。",
    }


def _context() -> TriageContext:
    return TriageContext.from_card(
        {
            "event_id": "event-replay",
            "evidence_version": 2,
            "evidence_sha256": "b" * 64,
            "focus_fact_id": "fact-replay",
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
            "queue_priority": "normal",
            "asset_class": "crypto",
            "grounded_assets": ["BTC"],
            "storyline_key": "asset:BTC",
        },
        watchlist=("BTC",),
        told_rows=[],
        now_ms=1_010_000,
        queue_lag_ms=10_000,
    )


class _Rows:
    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[Mapping[str, Any]]:
        return list(self._rows)


class _ExactRunConnection:
    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self._rows = rows
        self.requested_run_sha: str | None = None

    def execute(self, query: str, params: tuple[str]) -> _Rows:
        assert "WHERE run_sha = %s" in query
        assert "response IS NOT NULL" not in query
        self.requested_run_sha = params[0]
        return _Rows([row for row in self._rows if row["run_sha"] == params[0]])


def _recording_rows(
    *,
    judgment: Any,
    adapters: Sequence[ScriptedPredictorAdapter],
    run_sha: str = RUN_SHA,
) -> list[dict[str, Any]]:
    requests = {
        (request.route, request.predictor, request.attempt): request
        for adapter in adapters
        for request in adapter.requests
    }
    rows: list[dict[str, Any]] = []
    for arm in ("stable", "candidate"):
        for call_index, call in enumerate(judgment.trace.calls):
            rows.append(
                _recording_row(
                    run_sha=run_sha,
                    arm=arm,
                    call_index=call_index,
                    call=call,
                    request=requests[(call.route, call.predictor, call.attempt)],
                )
            )
    return rows


def _recording_row(
    *,
    run_sha: str,
    arm: str,
    call_index: int,
    call: ProgramCallTrace,
    request: Any,
) -> dict[str, Any]:
    request_payload = request.model_dump(mode="json")
    request_payload.update(
        {
            "request_sha256": request.request_sha256,
            "input_sha256": call.input_sha256,
            "call_index": call_index,
            "runtime_model_bindings_sha256": "c" * 64,
        }
    )
    response = None
    if call.validated_output is not None:
        output_field = "semantics" if call.predictor == "event_semantics" else "card"
        response = {
            "output": {output_field: call.validated_output},
            "provider": call.provider,
            "model": call.model,
            "model_sha256": call.model_sha256,
            "latency_ms": call.latency_ms,
            "input_tokens": call.input_tokens,
            "output_tokens": call.output_tokens,
            "cached_tokens": call.cached_tokens,
            "total_tokens": call.total_tokens,
            "provider_cost_microusd": call.provider_cost_microusd,
            "finish_reason": call.finish_reason,
            "runtime_binding_sha256": call.runtime_binding_sha256,
        }
    identity = {
        "run_sha": run_sha,
        "case_id": "case-1",
        "arm": arm,
        "trial": 1,
        "predictor_name": call.predictor,
        "call_index": call_index,
        "attempt": call.attempt,
        "request_sha256": call.request_sha256,
    }
    provider = call.provider or "unobserved"
    model = call.model or "unobserved"
    return {
        **identity,
        "recording_sha": canonical_sha(identity),
        "route": call.route,
        "response_sha256": canonical_sha(response) if response is not None else None,
        "request": request_payload,
        "response": response,
        "provider": provider,
        "model": model,
        "model_sha": call.model_sha256 or canonical_sha({"provider": provider, "model": model}),
        "latency_ms": call.latency_ms,
        "input_tokens": call.input_tokens,
        "output_tokens": call.output_tokens,
        "cached_tokens": call.cached_tokens,
        "total_tokens": call.total_tokens,
        "provider_cost_microusd": call.provider_cost_microusd,
        "finish_reason": call.finish_reason,
        "error_code": call.error_code,
    }


def _load(rows: Sequence[Mapping[str, Any]], *, run_sha: str = RUN_SHA) -> tuple[Any, _ExactRunConnection]:
    artifact = load_stable_program_artifact()
    conn = _ExactRunConnection(rows)
    capability = load_recording_replay_capability(
        conn,
        run_sha=run_sha,
        arms=(
            ReplayArmSpec(arm="stable", bundle_sha=STABLE_BUNDLE_SHA, artifact=artifact),
            ReplayArmSpec(arm="candidate", bundle_sha=CANDIDATE_BUNDLE_SHA, artifact=artifact),
        ),
    )
    return capability, conn


def test_sealed_replay_loads_null_error_calls_and_reexecutes_retry_and_fallback() -> None:
    artifact = load_stable_program_artifact()
    invalid = {"not": "event semantics"}
    primary = ScriptedPredictorAdapter([invalid, invalid])
    fallback = ScriptedPredictorAdapter([_semantics(), _card()])
    original = asyncio.run(
        DspyNewsSemanticProgram(
            artifact,
            primary_adapter=primary,
            fallback_adapter=fallback,
        ).judge(_context())
    )
    rows = _recording_rows(judgment=original, adapters=(primary, fallback))
    unrelated = [{**rows[0], "run_sha": "d" * 64, "recording_sha": "e" * 64}]
    capability, conn = _load([*rows, *unrelated])

    replayed = [
        asyncio.run(
            capability.judge(
                arm=arm,
                bundle_sha=bundle_sha,
                case_id="case-1",
                trial=1,
                context=_context(),
            )
        )
        for arm, bundle_sha in (("stable", STABLE_BUNDLE_SHA), ("candidate", CANDIDATE_BUNDLE_SHA))
    ]

    assert conn.requested_run_sha == RUN_SHA
    assert sum(row["response"] is None for row in rows) == 4
    assert original.fallback_from == "news_program_event_semantics_invalid"
    for replay in replayed:
        assert replay.verdict == original.verdict
        assert replay.fallback_from == original.fallback_from
        assert [(call.route, call.attempt, call.error_code) for call in replay.trace.calls] == [
            (call.route, call.attempt, call.error_code) for call in original.trace.calls
        ]
    receipt = capability.sealed_receipt()
    assert receipt["run_sha"] == RUN_SHA
    assert receipt["recording_n"] == len(rows)
    assert len(receipt["recording_corpus_root"]) == 64


def test_sealed_replay_reexecutes_program_owned_novelty_default() -> None:
    artifact = load_stable_program_artifact()
    missing_novelty = _semantics()
    missing_novelty.pop("novelty")
    missing_novelty.pop("restates")
    primary = ScriptedPredictorAdapter([missing_novelty, missing_novelty, _card()])
    original = asyncio.run(
        DspyNewsSemanticProgram(
            artifact,
            primary_adapter=primary,
        ).judge(_context())
    )
    rows = _recording_rows(judgment=original, adapters=(primary,))
    capability, _ = _load(rows)

    for arm, bundle_sha in (("stable", STABLE_BUNDLE_SHA), ("candidate", CANDIDATE_BUNDLE_SHA)):
        replay = asyncio.run(
            capability.judge(
                arm=arm,
                bundle_sha=bundle_sha,
                case_id="case-1",
                trial=1,
                context=_context(),
            )
        )
        assert replay.verdict == original.verdict
        assert replay.trace.novelty_defaulted is True
        assert [call.error_code for call in replay.trace.calls] == [
            "news_program_event_semantics_invalid",
            "news_program_novelty_defaulted",
            None,
        ]
    capability.sealed_receipt()


def test_sealed_replay_exposes_an_absent_run_corpus_as_unavailable() -> None:
    capability, conn = _load([])

    with pytest.raises(RecordingReplayMiss, match="news_learning_recording_replay_corpus_missing"):
        capability.assert_for_run(RUN_SHA)

    assert conn.requested_run_sha == RUN_SHA


def test_sealed_replay_rejects_program_v1_artifacts_before_reading_recordings() -> None:
    legacy_artifact: Any = SimpleNamespace(
        schema_version="news_semantic_program_artifact_v1",
        factory_id="tracefold.news.semantic_program.factory_v1",
        program_version="news_semantic_program_v1",
    )
    conn = _ExactRunConnection([])

    with pytest.raises(RecordingReplayError, match="news_learning_recording_replay_program_v1_unsupported"):
        load_recording_replay_capability(
            conn,
            run_sha=RUN_SHA,
            arms=(
                ReplayArmSpec(arm="stable", bundle_sha=STABLE_BUNDLE_SHA, artifact=legacy_artifact),
                ReplayArmSpec(arm="candidate", bundle_sha=CANDIDATE_BUNDLE_SHA, artifact=legacy_artifact),
            ),
        )

    assert conn.requested_run_sha is None


def test_sealed_replay_exposes_an_incomplete_arm_corpus_as_unavailable() -> None:
    artifact = load_stable_program_artifact()
    primary = ScriptedPredictorAdapter([_semantics(), _card()])
    original = asyncio.run(DspyNewsSemanticProgram(artifact, primary_adapter=primary).judge(_context()))
    rows = [row for row in _recording_rows(judgment=original, adapters=(primary,)) if row["arm"] == "stable"]

    capability, _ = _load(rows)

    with pytest.raises(RecordingReplayMiss, match="news_learning_recording_replay_arms_missing"):
        capability.assert_for_run(RUN_SHA)


def test_sealed_replay_exposes_an_absent_case_call_as_unavailable() -> None:
    artifact = load_stable_program_artifact()
    primary = ScriptedPredictorAdapter([_semantics(), _card()])
    original = asyncio.run(DspyNewsSemanticProgram(artifact, primary_adapter=primary).judge(_context()))
    capability, _ = _load(_recording_rows(judgment=original, adapters=(primary,)))

    with pytest.raises(RecordingReplayMiss, match="news_learning_recording_replay_call_missing"):
        asyncio.run(
            capability.judge(
                arm="stable",
                bundle_sha=STABLE_BUNDLE_SHA,
                case_id="case-without-recordings",
                trial=1,
                context=_context(),
            )
        )


def test_sealed_replay_rejects_unreplayable_route_deadline_before_signing() -> None:
    artifact = load_stable_program_artifact()
    primary = ScriptedPredictorAdapter([_semantics(), _card()])
    original = asyncio.run(
        DspyNewsSemanticProgram(
            artifact,
            primary_adapter=primary,
        ).judge(_context())
    )
    rows = _recording_rows(judgment=original, adapters=(primary,))
    rows[0] = {
        **rows[0],
        "response": None,
        "response_sha256": None,
        "error_code": "news_program_route_deadline",
    }

    with pytest.raises(
        RecordingReplayError,
        match="news_learning_recording_replay_outcome_unreplayable:route_deadline",
    ):
        _load(rows)


def test_sealed_replay_rejects_a_null_outcome_without_an_error_identity() -> None:
    artifact = load_stable_program_artifact()
    primary = ScriptedPredictorAdapter([_semantics(), _card()])
    original = asyncio.run(
        DspyNewsSemanticProgram(
            artifact,
            primary_adapter=primary,
        ).judge(_context())
    )
    rows = _recording_rows(judgment=original, adapters=(primary,))
    rows[0] = {
        **rows[0],
        "response": None,
        "response_sha256": None,
        "error_code": None,
    }

    with pytest.raises(RecordingReplayError, match="news_learning_recording_replay_outcome_missing"):
        _load(rows)


def test_sealed_replay_rejects_a_response_whose_content_does_not_match_its_identity() -> None:
    artifact = load_stable_program_artifact()
    primary = ScriptedPredictorAdapter([_semantics(), _card()])
    original = asyncio.run(
        DspyNewsSemanticProgram(
            artifact,
            primary_adapter=primary,
        ).judge(_context())
    )
    rows = _recording_rows(judgment=original, adapters=(primary,))
    rows[0] = {**rows[0], "response_sha256": "f" * 64}

    with pytest.raises(
        RecordingReplayError,
        match="news_learning_recording_replay_response_identity_mismatch",
    ) as caught:
        _load(rows)

    assert not isinstance(caught.value, RecordingReplayMiss)


def test_sealed_replay_keeps_program_identity_mismatch_fail_closed() -> None:
    artifact = load_stable_program_artifact()
    primary = ScriptedPredictorAdapter([_semantics(), _card()])
    original = asyncio.run(DspyNewsSemanticProgram(artifact, primary_adapter=primary).judge(_context()))
    rows = _recording_rows(judgment=original, adapters=(primary,))
    rows[0] = {
        **rows[0],
        "request": {**rows[0]["request"], "program_sha256": "f" * 64},
    }

    with pytest.raises(
        RecordingReplayError,
        match="news_learning_recording_replay_program_identity_mismatch",
    ) as caught:
        _load(rows)

    assert not isinstance(caught.value, RecordingReplayMiss)


def test_sealed_replay_rejects_a_recorded_adapter_identity_not_in_the_signed_request() -> None:
    artifact = load_stable_program_artifact()
    primary = ScriptedPredictorAdapter([_semantics(), _card()])
    original = asyncio.run(DspyNewsSemanticProgram(artifact, primary_adapter=primary).judge(_context()))
    rows = _recording_rows(judgment=original, adapters=(primary,))
    rows[0] = {
        **rows[0],
        "request": {**rows[0]["request"], "adapter_sha256": "f" * 64},
    }
    capability, _ = _load(rows)

    with pytest.raises(
        RecordingReplayError,
        match="news_learning_recording_replay_request_mismatch:adapter_sha256",
    ):
        asyncio.run(
            capability.judge(
                arm="stable",
                bundle_sha=STABLE_BUNDLE_SHA,
                case_id="case-1",
                trial=1,
                context=_context(),
            )
        )


@pytest.mark.parametrize(
    "tamper",
    (
        {"recording_sha": "f" * 64},
        {"case_id": "tampered-case"},
        {"trial": 2},
    ),
)
def test_sealed_replay_keeps_canonical_recording_identity_tamper_fail_closed(
    tamper: Mapping[str, object],
) -> None:
    artifact = load_stable_program_artifact()
    primary = ScriptedPredictorAdapter([_semantics(), _card()])
    original = asyncio.run(DspyNewsSemanticProgram(artifact, primary_adapter=primary).judge(_context()))
    rows = _recording_rows(judgment=original, adapters=(primary,))
    rows[0] = {**rows[0], **tamper}

    with pytest.raises(
        RecordingReplayError,
        match="news_learning_recording_replay_recording_identity_mismatch",
    ) as caught:
        _load(rows)

    assert not isinstance(caught.value, RecordingReplayMiss)
