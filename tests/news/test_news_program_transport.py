"""The Program's own model transport (#306 Phase 3).

This replaces the `test_real_dspy_33_*` family in `test_news_semantic_program.py`. Those tests drove DSPy's
LM and asserted that the private surface under it held three audit contracts: one trace entry per physical
attempt, `finish_reason` deciding the retry, and no silent format downgrade issuing a second call. The
contracts did not change; what changed is that they are now properties of a request this repository
composes, so they can be asserted against an HTTP transport instead of against a framework's internals.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from tests.news.test_news_semantic_program import _card, _context, _semantics
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.program.artifact import load_stable_program_artifact
from tracefold.news.program.graph import NewsSemanticProgram, predictor_spec
from tracefold.news.program.signatures import EventSemantics
from tracefold.news.program.transport import (
    ChatCompletionsPredictorAdapter,
    PredictorAdapterError,
    PredictorRequest,
    chat_request_body,
    provider_call_metrics,
    response_format,
    system_message,
    user_message,
    wire_model_name,
)


def _spec(predictor: str = "event_semantics") -> Any:
    artifact = load_stable_program_artifact()
    return predictor_spec(artifact.predictor_state(predictor))


def _completion(
    content: str,
    *,
    model: str = "resolved-model",
    finish_reason: str = "stop",
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}],
        "usage": usage if usage is not None else {"prompt_tokens": 13, "completion_tokens": 2, "total_tokens": 15},
    }


def _adapter(handler: Any, *, model_name: str = "openai/test", max_tokens: int = 1200) -> Any:
    return ChatCompletionsPredictorAdapter(
        model_name=model_name,
        api_key="secret",
        api_base="https://provider.invalid/v1",
        timeout=5,
        max_tokens=max_tokens,
        transport=httpx.MockTransport(handler),
    )


def _request(adapter: Any, *, predictor: str = "event_semantics", inputs: dict[str, Any] | None = None) -> Any:
    runtime = adapter.runtime_identity(f"{predictor}.primary")
    return PredictorRequest(
        program_version="test",
        program_sha256="a" * 64,
        context_sha256="b" * 64,
        predictor=predictor,
        route="primary",
        attempt=1,
        model_binding=f"{predictor}.primary",
        runtime_provider=runtime.provider,
        runtime_model=runtime.model,
        runtime_model_sha256=runtime.model_sha256,
        runtime_binding_sha256=runtime.binding_sha256,
        inputs=inputs if inputs is not None else {"evidence_json": "{}"},
    )


# --- the request this repository composes ---------------------------------------------------------


def test_the_system_message_is_the_instruction_plus_the_output_contract() -> None:
    spec = _spec()
    rendered = system_message(spec.instruction, output_field=spec.output_field, output_model=spec.output_model)

    assert rendered.startswith(spec.instruction)
    # The contract describes the wire envelope, not the judgment, which is why it is appended by the
    # transport rather than expected inside a text an optimizer may rewrite.
    assert "# OUTPUT CONTRACT" in rendered[len(spec.instruction) :]
    assert '"semantics"' in rendered and "EventSemantics" in rendered


def test_the_user_message_carries_the_bounded_fields_in_the_specs_fixed_order() -> None:
    rendered = user_message(("evidence_json", "semantics_json"), {"semantics_json": "S", "evidence_json": "E"})

    assert rendered == "## evidence_json\nE\n\n## semantics_json\nS"


def test_an_unexpected_or_missing_input_field_is_refused_rather_than_sent() -> None:
    with pytest.raises(PredictorAdapterError, match="input_fields_invalid"):
        user_message(("evidence_json",), {"evidence_json": "E", "smuggled": "X"})
    with pytest.raises(PredictorAdapterError, match="input_fields_invalid"):
        user_message(("evidence_json", "semantics_json"), {"evidence_json": "E"})


def test_the_response_format_is_built_from_the_model_the_answer_is_validated_against() -> None:
    """One schema, so the constraint the provider gets and the check the code runs cannot drift."""

    fmt = response_format("semantics", EventSemantics)

    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "EventSemantics"
    envelope = fmt["json_schema"]["schema"]
    assert envelope["required"] == ["semantics"] and envelope["additionalProperties"] is False
    assert envelope["properties"]["semantics"] == EventSemantics.model_json_schema()


def test_the_client_library_prefix_is_not_part_of_the_model_name_on_the_wire() -> None:
    assert wire_model_name("openai/qwen3-30b") == "qwen3-30b"
    # A genuine vendor-qualified identifier is not a prefix and survives untouched.
    assert wire_model_name("Qwen/Qwen3-30B") == "Qwen/Qwen3-30B"
    assert wire_model_name("deepseek-v4-chat") == "deepseek-v4-chat"


def test_operator_configured_extra_body_reaches_the_request_body() -> None:
    """`app/llm.py` disables thinking per model family through `extra_body`; that has to survive the move.

    Without it a Qwen build on llama.cpp spends its whole `max_tokens` budget reasoning and answers
    nothing, which is exactly the failure that made every early judge verdict truncate.
    """

    spec = _spec()
    adapter = _adapter(lambda request: httpx.Response(200, json={}))
    adapter._extra_body = {"chat_template_kwargs": {"enable_thinking": False}}

    body = adapter.request_body(spec, {"evidence_json": "{}"})

    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["model"] == "test"
    assert body["temperature"] == 0 and body["stream"] is False
    assert body["max_tokens"] == spec.max_tokens


def test_the_request_body_is_the_same_envelope_for_every_structured_call() -> None:
    spec = _spec("reader_card")
    adapter = _adapter(lambda request: httpx.Response(200, json={}))

    assert adapter.request_body(spec, {"evidence_json": "E", "semantics_json": "S"}) == chat_request_body(
        model="openai/test",
        instruction=spec.instruction,
        field_order=spec.input_fields,
        values={"evidence_json": "E", "semantics_json": "S"},
        output_field="card",
        output_model=spec.output_model,
        max_tokens=spec.max_tokens,
    )


# --- one invoke is one physical attempt -----------------------------------------------------------


def test_one_invoke_is_exactly_one_provider_request_carrying_the_credential_once() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_completion(json.dumps({"semantics": _semantics()})))

    adapter = _adapter(handler)
    response = asyncio.run(adapter.invoke(_request(adapter), _spec()))

    assert len(seen) == 1
    assert str(seen[0].url) == "https://provider.invalid/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer secret"
    assert response.output == {"semantics": _semantics()}
    assert response.model == "resolved-model"
    assert (response.finish_reason, response.input_tokens, response.output_tokens) == ("stop", 13, 2)
    assert response.model_sha256 == canonical_sha({"provider": "openai", "model": "resolved-model"})
    assert response.runtime_binding_sha256 == adapter.runtime_identity("event_semantics.primary").binding_sha256


def test_usage_reports_cached_tokens_and_leaves_an_unpriced_call_unpriced() -> None:
    """Neither endpoint this project runs on returns a resolvable price.

    Reporting zero there would make a metered optimization run look free and let the overspend check pass
    a run that was not within budget.
    """

    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "prompt_tokens_details": {"cached_tokens": 40},
    }
    metrics = provider_call_metrics(_completion("{}", usage=usage))

    assert (metrics.input_tokens, metrics.output_tokens, metrics.total_tokens) == (100, 20, 120)
    assert metrics.cached_tokens == 40
    assert metrics.provider_cost_microusd is None

    priced = provider_call_metrics({**_completion("{}", usage={**usage, "cost": 0.000012}), "model": "m"})
    assert priced.provider_cost_microusd == 12


@pytest.mark.parametrize(("status", "retryable"), [(429, True), (503, True), (400, False), (401, False)])
def test_an_http_error_is_classified_rather_than_retried_inside_the_transport(status: int, retryable: bool) -> None:
    adapter = _adapter(lambda request: httpx.Response(status, json={"error": "no"}))

    with pytest.raises(PredictorAdapterError) as caught:
        asyncio.run(adapter.invoke(_request(adapter), _spec()))

    assert caught.value.code == f"news_program_provider_http_{status}"
    assert caught.value.retryable is retryable
    # The retry decision belongs to the graph, which is the only thing that can charge it to a budget.
    assert caught.value.output_failure is False


def test_a_transport_failure_is_retryable_and_carries_no_provider_observation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("provider unreachable", request=request)

    adapter = _adapter(handler)
    with pytest.raises(PredictorAdapterError) as caught:
        asyncio.run(adapter.invoke(_request(adapter), _spec()))

    assert caught.value.retryable is True
    # Nothing answered, so nothing reported usage: a settle-up here would charge a call that never happened.
    assert caught.value.provider_observation is None


def test_a_truncated_reply_is_named_truncation_and_keeps_its_exact_usage() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json=_completion('{"semantics":', finish_reason="length")))

    with pytest.raises(PredictorAdapterError) as caught:
        asyncio.run(adapter.invoke(_request(adapter), _spec()))

    assert caught.value.code == "news_program_output_truncated"
    assert caught.value.finish_reason == "length"
    observation = caught.value.provider_observation
    assert observation is not None
    assert (observation.input_tokens, observation.output_tokens, observation.total_tokens) == (13, 2, 15)


def test_an_unparseable_reply_that_was_not_truncated_is_a_different_failure() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json=_completion("not json at all")))

    with pytest.raises(PredictorAdapterError) as caught:
        asyncio.run(adapter.invoke(_request(adapter), _spec()))

    assert caught.value.code == "news_program_provider_output_not_json"
    assert caught.value.output_failure is True
    assert caught.value.finish_reason == "stop"


# --- the graph's contracts, over the real transport -----------------------------------------------


@pytest.mark.parametrize(("finish_reason", "expected_attempts"), [("stop", 2), ("length", 1)])
def test_finish_reason_decides_whether_the_graph_asks_again(finish_reason: str, expected_attempts: int) -> None:
    """A truncated answer is not a transient one: asking the same question again truncates again."""

    from tracefold.news.program.contracts import SemanticJudgeError

    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=_completion("{}", finish_reason=finish_reason))

    adapter = _adapter(handler)
    program = NewsSemanticProgram(load_stable_program_artifact(), primary_adapter=adapter)

    with pytest.raises(SemanticJudgeError) as caught:
        asyncio.run(program.judge(_context()))

    assert len(calls) == expected_attempts
    assert caught.value.attempts == expected_attempts
    assert caught.value.partial_trace is not None
    # One trace entry per physical attempt, which is the contract the private DSPy surface used to buy.
    assert len(caught.value.partial_trace.calls) == expected_attempts
    for call in caught.value.partial_trace.calls:
        assert call.physical_provider_call is True
        assert (call.input_tokens, call.output_tokens, call.total_tokens) == (13, 2, 15)
        assert call.finish_reason == finish_reason


def test_the_two_predictors_are_asked_in_order_each_with_its_own_instruction() -> None:
    artifact = load_stable_program_artifact()
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        answer = _semantics() if len(bodies) == 1 else _card()
        field = "semantics" if len(bodies) == 1 else "card"
        return httpx.Response(200, json=_completion(json.dumps({field: answer})))

    adapter = _adapter(handler)
    judgment = asyncio.run(NewsSemanticProgram(artifact, primary_adapter=adapter).judge(_context()))

    assert len(bodies) == 2
    assert bodies[0]["messages"][0]["content"].startswith(artifact.event_semantics_instruction)
    assert bodies[1]["messages"][0]["content"].startswith(artifact.reader_card_instruction)
    # The untrusted Event JSON is still delimited, which #306 keeps explicitly.
    assert "<tracefold-untrusted-event-json-v1>" in bodies[0]["messages"][1]["content"]
    assert "## semantics_json" in bodies[1]["messages"][1]["content"]
    assert judgment.usage.physical_call_count == 2
    assert judgment.trace.answering_route == "primary"
