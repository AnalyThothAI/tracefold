from __future__ import annotations

import asyncio
import copy
import traceback
from typing import Any

import dspy
import pytest
from pydantic import BaseModel

from tracefold.news.program.lm import (
    AuditedConfiguredLM,
    LMCallContext,
    LMCallLedger,
    LMOutputTruncatedError,
    RecordedLM,
    RecordedLMMiss,
    RuntimeModelIdentity,
    ScriptedLM,
    lm_request_projection,
    lm_request_sha256,
    program_json_adapter,
)
from tracefold.news.program.runtime import PROGRAM_VERSION

_SHA = "a" * 64


class _Answer(BaseModel):
    value: int


class _AnswerSignature(dspy.Signature):
    question: str = dspy.InputField()
    answer: _Answer = dspy.OutputField()


def _audited(
    steps: list[Any],
    *,
    mode: str = "json_schema",
    ledger: LMCallLedger | None = None,
) -> tuple[AuditedConfiguredLM, ScriptedLM, LMCallLedger]:
    actual_ledger = ledger or LMCallLedger()
    delegate = ScriptedLM(steps, structured_output=mode)  # type: ignore[arg-type]
    identity = RuntimeModelIdentity.issue(provider="scripted", model=delegate.model)
    return (
        AuditedConfiguredLM(
            delegate,
            structured_output=mode,  # type: ignore[arg-type]
            runtime_identity=identity,
            predictor="event_semantics",
            route="primary",
            model_binding="primary",
            ledger=actual_ledger,
        ),
        delegate,
        actual_ledger,
    )


def _predict(lm: dspy.BaseLM) -> dspy.Prediction:
    with dspy.context(adapter=program_json_adapter()):
        return dspy.Predict(_AnswerSignature)(question="six times seven?", lm=lm)


def _recorded_lm(
    recordings: dict[str, dict[str, Any] | None],
    *,
    model: str,
) -> RecordedLM:
    return RecordedLM(
        {key: value for key, value in recordings.items() if value is not None},
        model=model,
        runtime_identity=RuntimeModelIdentity.issue(provider="scripted", model=model),
        model_binding="primary",
    )


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("json_schema", "schema"),
        ("json_object", "object"),
        ("prompt_json", "prompt"),
    ],
)
def test_native_json_adapter_capability_modes_are_one_public_typed_seam(mode: str, expected: str) -> None:
    lm, delegate, ledger = _audited([{"answer": {"value": 42}}], mode=mode)

    with ledger.scope(LMCallContext(PROGRAM_VERSION, _SHA, _SHA)):
        prediction = _predict(lm)

    assert prediction.answer == _Answer(value=42)
    assert len(delegate.requests) == 1
    response_format = delegate.requests[0].config.response_format
    if expected == "schema":
        assert isinstance(response_format, type) and issubclass(response_format, BaseModel)
    elif expected == "object":
        assert response_format == {"type": "json_object"}
    else:
        assert response_format is None
    assert ledger.receipts[0].terminal_disposition == "provider_success"


def test_schema_parse_fallback_records_two_physical_terminal_calls() -> None:
    lm, delegate, ledger = _audited(["not-json", {"answer": {"value": 42}}])

    with ledger.scope(LMCallContext(PROGRAM_VERSION, _SHA, _SHA)):
        prediction = _predict(lm)

    assert prediction.answer.value == 42
    assert len(delegate.requests) == 2
    assert [receipt.attempt for receipt in ledger.receipts] == [1, 2]
    assert [receipt.terminal_disposition for receipt in ledger.receipts] == [
        "adapter_parse_error",
        "provider_success",
    ]
    assert isinstance(delegate.requests[0].config.response_format, type)
    assert delegate.requests[1].config.response_format == {"type": "json_object"}


def test_external_admission_refuses_second_format_attempt_before_receipt_or_provider() -> None:
    admitted = 0

    def before_call() -> None:
        nonlocal admitted
        if admitted == 1:
            raise dspy.LMConfigurationError("news_program_metric_budget_exhausted")
        admitted += 1

    ledger = LMCallLedger(before_call=before_call)
    lm, delegate, _ = _audited(["not-json", {"answer": {"value": 42}}], ledger=ledger)

    with pytest.raises(dspy.LMConfigurationError), ledger.scope(LMCallContext(PROGRAM_VERSION, _SHA, _SHA)):
        _predict(lm)

    assert admitted == 1
    assert len(delegate.requests) == 1
    assert len(ledger.receipts) == 1
    assert ledger.receipts[0].terminal_disposition == "adapter_parse_error"


def test_provider_error_does_not_trigger_json_fallback_and_scrubs_secret() -> None:
    secret = "sk-abcdefghijklmnopqrstu"
    lm, delegate, ledger = _audited([dspy.LMServerError(f"provider echoed {secret}", status=503, code="unavailable")])

    with pytest.raises(dspy.LMServerError), ledger.scope(LMCallContext(PROGRAM_VERSION, _SHA, _SHA)):
        _predict(lm)

    assert len(delegate.requests) == 1
    receipt = ledger.receipts[0]
    assert receipt.terminal_disposition == "provider_error"
    assert receipt.error_code == "news_program_lm_unavailable"
    assert receipt.error_detail == "provider echoed [redacted]"
    assert secret not in repr(receipt.recording)


def test_provider_error_public_exception_and_recording_are_fully_sanitized() -> None:
    secret = "sk-abcdefghijklmnopqrstu"
    error = dspy.LMAuthError(
        "密" * 90 + f" api_key={secret}",
        code=secret,
        model=secret,
        provider=secret,
        provider_code=secret,
        status=401,
    )
    lm, _, ledger = _audited([error])

    with pytest.raises(dspy.LMAuthError) as captured, ledger.scope(LMCallContext(PROGRAM_VERSION, _SHA, _SHA)):
        _predict(lm)

    public = captured.value
    rendered = "".join(traceback.format_exception(public))
    receipt = ledger.receipts[0]
    assert secret not in rendered
    assert secret not in repr(receipt.recording)
    assert public.__suppress_context__ is True
    assert public.model == "scripted/test"
    assert public.provider == "scripted"
    assert public.provider_code is None
    assert len(public.message.encode("utf-8")) <= 200
    assert receipt.error_code == "news_program_lm_auth"


def test_unexpected_exception_suppresses_secret_bearing_cause() -> None:
    secret = "sk-abcdefghijklmnopqrstu"
    lm, _, ledger = _audited([RuntimeError(f"failed with {secret}")])

    with pytest.raises(dspy.LMUnexpectedError) as captured, ledger.scope(LMCallContext(PROGRAM_VERSION, _SHA, _SHA)):
        _predict(lm)

    assert secret not in "".join(traceback.format_exception(captured.value))
    assert captured.value.__suppress_context__ is True
    assert secret not in repr(ledger.receipts[0].recording)


def test_scope_reconciles_non_exception_base_exception_as_abandoned_provider_call() -> None:
    lm, _, ledger = _audited([KeyboardInterrupt()])

    with pytest.raises(KeyboardInterrupt), ledger.scope(LMCallContext(PROGRAM_VERSION, _SHA, _SHA)):
        _predict(lm)

    assert len(ledger.receipts) == 1
    assert ledger.receipts[0].terminal_disposition == "provider_error"
    assert ledger.receipts[0].error_code == "news_program_lm_scope_abandoned"


def test_domain_failure_reclassifies_latest_success_without_synthetic_call() -> None:
    lm, _, ledger = _audited([{"answer": {"value": 42}}])

    with ledger.scope(LMCallContext(PROGRAM_VERSION, _SHA, _SHA)):
        _predict(lm)
        changed = ledger.domain_failure("news_program_answer_domain_invalid")

    assert len(ledger.receipts) == 1
    assert changed.terminal_disposition == "domain_validation_error"
    assert ledger.receipts[0].error_code == "news_program_answer_domain_invalid"
    assert ledger.first_terminal_error == ledger.receipts[0]


def test_late_completion_reclassifies_latest_success_without_synthetic_call() -> None:
    lm, _, ledger = _audited([{"answer": {"value": 42}}])

    with ledger.scope(LMCallContext(PROGRAM_VERSION, _SHA, _SHA)):
        _predict(lm)
        changed = ledger.late_completion()

    assert len(ledger.receipts) == 1
    assert changed.terminal_disposition == "late_completion"
    assert changed.error_code == "news_program_route_deadline"


def test_receipt_converts_to_program_call_trace_with_physical_usage() -> None:
    response = dspy.LMResponse.from_text(
        '{"answer":{"value":42}}',
        model="scripted/test-actual",
        usage={
            "input_tokens": 11,
            "output_tokens": 7,
            "total_tokens": 18,
            "cache_read_tokens": 3,
            "prompt_tokens_details": {"cached_tokens": 5},
        },
        cost=0.0000125,
    )
    lm, _, ledger = _audited([response])

    with ledger.scope(LMCallContext(PROGRAM_VERSION, _SHA, _SHA)):
        _predict(lm)

    trace = ledger.receipts[0].to_program_call_trace()
    assert trace.physical_provider_call is True
    assert trace.input_tokens == 11
    assert trace.output_tokens == 7
    assert trace.cached_tokens == 5
    assert trace.total_tokens == 18
    assert trace.provider_cost_microusd == 13
    assert trace.model == "scripted/test-actual"
    assert trace.invocation_sha256 == ledger.receipts[0].invocation_sha256


def test_recorded_lm_replays_exact_success_and_never_falls_through() -> None:
    lm, delegate, ledger = _audited([{"answer": {"value": 42}}])
    with ledger.scope(LMCallContext(PROGRAM_VERSION, _SHA, _SHA)):
        _predict(lm)
    receipt = ledger.receipts[0]
    assert receipt.recording is not None
    replay = _recorded_lm({receipt.request_sha256: receipt.recording}, model=delegate.model)

    response = replay(request=delegate.requests[0])
    assert isinstance(response, dspy.LMResponse)
    assert response.text == '{"answer":{"value":42}}'

    different = dspy.LMRequest.from_call(
        model=delegate.model,
        messages=[{"role": "user", "content": "different"}],
    )
    with pytest.raises(RecordedLMMiss):
        replay(request=different)


def test_request_and_invocation_addresses_bind_endpoint_and_predictor_slot() -> None:
    request = dspy.LMRequest.from_call(
        model="scripted/test",
        messages=[{"role": "user", "content": "same"}],
    )
    endpoint_a = "1" * 64
    endpoint_b = "2" * 64

    assert lm_request_sha256(request, endpoint_fingerprint=endpoint_a, model_binding="slot.a") != lm_request_sha256(
        request,
        endpoint_fingerprint=endpoint_b,
        model_binding="slot.a",
    )
    assert lm_request_sha256(request, endpoint_fingerprint=endpoint_a, model_binding="slot.a") != lm_request_sha256(
        request,
        endpoint_fingerprint=endpoint_a,
        model_binding="slot.b",
    )

    def invoke(endpoint: str, binding: str) -> tuple[str, str, dict[str, Any]]:
        delegate = ScriptedLM([{"answer": {"value": 42}}])
        ledger = LMCallLedger()
        lm = AuditedConfiguredLM(
            delegate,
            structured_output="json_schema",
            runtime_identity=RuntimeModelIdentity.issue(
                provider="scripted",
                model=delegate.model,
                model_sha256=endpoint,
            ),
            predictor="event_semantics",
            route="primary",
            model_binding=binding,
            ledger=ledger,
        )
        with ledger.scope(LMCallContext(PROGRAM_VERSION, _SHA, _SHA)):
            _predict(lm)
        receipt = ledger.receipts[0]
        assert receipt.recording is not None
        return receipt.request_sha256, receipt.invocation_sha256, receipt.recording

    request_a, invocation_a, recording_a = invoke(endpoint_a, "slot.a")
    request_b, invocation_b, _ = invoke(endpoint_b, "slot.a")
    assert request_a != request_b
    assert invocation_a != invocation_b
    with pytest.raises(ValueError, match="news_program_recording_runtime_identity_mismatch"):
        RecordedLM(
            {request_a: recording_a},
            model="scripted/test",
            runtime_identity=RuntimeModelIdentity.issue(
                provider="scripted",
                model="scripted/test",
                model_sha256=endpoint_b,
            ),
            model_binding="slot.a",
        )


def test_recorded_lm_rejects_legacy_identity_and_malformed_typed_terminal() -> None:
    lm, delegate, ledger = _audited([{"answer": {"value": 42}}])
    with ledger.scope(LMCallContext(PROGRAM_VERSION, _SHA, _SHA)):
        _predict(lm)
    receipt = ledger.receipts[0]
    assert receipt.recording is not None

    legacy = copy.deepcopy(receipt.recording)
    legacy.pop("request_identity")
    with pytest.raises(ValueError):
        _recorded_lm({receipt.request_sha256: legacy}, model=delegate.model)

    malformed = copy.deepcopy(receipt.recording)
    assert isinstance(malformed["response"], dict)
    malformed["response"]["truncated"] = "false"
    malformed["response"]["unexpected"] = "must reject"
    with pytest.raises(ValueError):
        _recorded_lm({receipt.request_sha256: malformed}, model=delegate.model)

    coerced_cost = copy.deepcopy(receipt.recording)
    assert isinstance(coerced_cost["response"], dict)
    coerced_cost["response"]["cost"] = "0.01"
    with pytest.raises(ValueError):
        _recorded_lm({receipt.request_sha256: coerced_cost}, model=delegate.model)


@pytest.mark.parametrize(
    "error",
    [
        dspy.LMRateLimitError("slow down", status=429, retry_after=2.0),
        dspy.LMServerError("unavailable", status=503),
        dspy.LMTimeoutError("timed out"),
    ],
)
def test_recorded_lm_replays_safe_provider_error(error: dspy.LMError) -> None:
    lm, delegate, ledger = _audited([error])
    with pytest.raises(type(error)), ledger.scope(LMCallContext(PROGRAM_VERSION, _SHA, _SHA)):
        _predict(lm)
    receipt = ledger.receipts[0]
    assert receipt.recording is not None
    replay = _recorded_lm({receipt.request_sha256: receipt.recording}, model=delegate.model)

    with pytest.raises(type(error)) as captured:
        replay(request=delegate.requests[0])

    assert captured.value.status == error.status
    assert captured.value.retry_after == error.retry_after


def test_recorded_lm_preserves_schema_invalid_fallback_sequence() -> None:
    lm, delegate, ledger = _audited(["not-json", {"answer": {"value": 42}}])
    with ledger.scope(LMCallContext(PROGRAM_VERSION, _SHA, _SHA)):
        _predict(lm)
    recordings = {
        receipt.request_sha256: receipt.recording for receipt in ledger.receipts if receipt.recording is not None
    }
    replay = _recorded_lm(recordings, model=delegate.model)

    prediction = _predict(replay)

    assert prediction.answer.value == 42
    assert len(replay.requests) == 2


def test_truncation_is_one_provider_answer_and_replay_preserves_it() -> None:
    response = dspy.LMResponse.from_text('{"answer":', model="scripted/test")
    response.outputs[0] = response.output.model_copy(update={"finish_reason": "length", "truncated": True})
    lm, delegate, ledger = _audited([response])

    with pytest.raises(LMOutputTruncatedError), ledger.scope(LMCallContext(PROGRAM_VERSION, _SHA, _SHA)):
        _predict(lm)

    receipt = ledger.receipts[0]
    assert len(delegate.requests) == 1
    assert receipt.terminal_disposition == "provider_success"
    assert receipt.finish_reason == "length"
    assert receipt.error_code == "news_program_lm_output_truncated"
    assert receipt.recording is not None
    replay = _recorded_lm({receipt.request_sha256: receipt.recording}, model=delegate.model)
    with pytest.raises(LMOutputTruncatedError):
        replay(request=delegate.requests[0])

    replay = _recorded_lm({receipt.request_sha256: receipt.recording}, model=delegate.model)
    replay_ledger = LMCallLedger()
    audited_replay = AuditedConfiguredLM(
        replay,
        structured_output="json_schema",
        runtime_identity=RuntimeModelIdentity.issue(provider="scripted", model=delegate.model),
        predictor="event_semantics",
        route="primary",
        model_binding="primary",
        ledger=replay_ledger,
    )
    with pytest.raises(LMOutputTruncatedError), replay_ledger.scope(LMCallContext(PROGRAM_VERSION, _SHA, _SHA)):
        _predict(audited_replay)
    assert len(replay.requests) == 1
    assert replay_ledger.receipts[0].terminal_disposition == "provider_success"
    assert replay_ledger.receipts[0].finish_reason == "length"


def test_request_projection_normalizes_dynamic_response_schema_without_credentials() -> None:
    request = dspy.LMRequest.from_call(
        model="scripted/test",
        messages=[{"role": "user", "content": "hello"}],
        response_format=_Answer,
        extra_body={"thinking": {"type": "disabled"}},
    )

    projection = lm_request_projection(request)

    assert projection["config"]["response_format"]["properties"]["value"]["type"] == "integer"
    assert projection["config"]["extensions"]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "api_key" not in repr(projection)


@pytest.mark.parametrize(
    "unsafe_config",
    [
        {"reasoning": {"effort": "high"}},
        {"prompt_cache": {"enabled": True, "key": "cache-key"}},
        {"extra_body": {"access_token": "credential"}},
        {"extra_body": {"proxy": "https://user:password@example.test/v1"}},
        {"stop": ["api_key=credential"]},
    ],
)
def test_request_projection_rejects_unreviewed_or_secret_shaped_config(
    unsafe_config: dict[str, Any],
) -> None:
    request = dspy.LMRequest.from_call(
        model="scripted/test",
        messages=[{"role": "user", "content": "hello"}],
        **unsafe_config,
    )

    with pytest.raises(dspy.LMConfigurationError):
        lm_request_projection(request)


def test_contextvars_isolate_concurrent_judgments() -> None:
    async def run(value: int) -> tuple[int, str]:
        lm, _, ledger = _audited([{"answer": {"value": value}}])
        with (
            ledger.scope(LMCallContext(PROGRAM_VERSION, _SHA, str(value) * 64)),
            dspy.context(adapter=program_json_adapter()),
        ):
            prediction = await dspy.Predict(_AnswerSignature).acall(question="value?", lm=lm)
        return prediction.answer.value, ledger.receipts[0].invocation_sha256

    async def main() -> tuple[tuple[int, str], tuple[int, str]]:
        first, second = await asyncio.gather(run(1), run(2))
        return first, second

    first, second = asyncio.run(main())

    assert first[0] == 1
    assert second[0] == 2
    assert first[1] != second[1]
