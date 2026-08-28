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
import re
from typing import Any

import httpx
import pytest

from tests.news.test_news_semantic_program import _card, _context, _semantics
from tracefold.news.artifact_identity import canonical_json, canonical_sha
from tracefold.news.program.artifact import load_stable_program_artifact
from tracefold.news.program.graph import NewsSemanticProgram, predictor_spec
from tracefold.news.program.signatures import PREDICTOR_OUTPUT, EventSemantics, ReaderCard
from tracefold.news.program.transport import (
    ChatCompletionsPredictorAdapter,
    PredictorAdapterError,
    PredictorRequest,
    chat_request_body,
    provider_call_metrics,
    provider_error_detail,
    response_format,
    structured_output_mode,
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
    published = EventSemantics.model_json_schema()
    published.pop("$defs")
    assert envelope["properties"]["semantics"] == published


@pytest.mark.parametrize(("field", "model"), [("semantics", EventSemantics), ("card", ReaderCard)])
def test_every_schema_reference_resolves_from_the_envelope_root(field: str, model: Any) -> None:
    """A dangling `$ref` is an unconstrained field wearing a constraint.

    Pydantic emits `{"$ref": "#/$defs/TriageAsset"}` — a pointer from the *document* root — beside a
    `$defs` block. Nesting the model's schema under one envelope key without moving its definitions leaves
    every pointer resolving against an envelope that has none, and a provider answers that with either an
    error or a field it never constrained. Nothing about "the schema is present" catches it.
    """

    envelope = response_format(field, model)["json_schema"]["schema"]
    definitions = set(envelope.get("$defs", {}))
    references = set(re.findall(r'"\$ref": "([^"]+)"', json.dumps(envelope)))

    assert "$defs" not in envelope["properties"][field]
    unresolved = [
        ref for ref in references if not (ref.startswith("#/$defs/") and ref[len("#/$defs/") :] in definitions)
    ]
    assert unresolved == []
    # And the pointer set is not empty for the model that actually nests: a test that passed because the
    # schema had no references at all would prove nothing.
    assert references if model is EventSemantics else True


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


def test_extra_body_cannot_override_a_field_the_transport_composes() -> None:
    """The escape hatch is spread into the body last, so it has to pass the same guard.

    `extra_body` exists for the per-model-family switches `app/llm.py` sets (`thinking`,
    `chat_template_kwargs`). Validated only against the top-level keys, it would silently override the
    JSON-schema constraint or the token ceiling the role identity attests — through the very guard whose
    comment says it prevents that.
    """

    for smuggled in ({"response_format": None}, {"max_tokens": 1}, {"messages": []}):
        with pytest.raises(ValueError, match="runtime_model_kwargs_owned"):
            ChatCompletionsPredictorAdapter(
                model_name="openai/test",
                api_key="k",
                api_base="https://provider.invalid/v1",
                timeout=5,
                max_tokens=100,
                model_kwargs={"extra_body": smuggled},
            )

    # The switches it exists for still pass.
    adapter = ChatCompletionsPredictorAdapter(
        model_name="openai/test",
        api_key="k",
        api_base="https://provider.invalid/v1",
        timeout=5,
        max_tokens=100,
        model_kwargs={"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
    )
    assert adapter.request_body(_spec(), {"evidence_json": "{}"})["chat_template_kwargs"] == {"enable_thinking": False}


def test_an_injected_transport_survives_more_than_one_request() -> None:
    """`build_task_adapter(transport=...)` advertises a reusable seam, so the client must not close it.

    `httpx.AsyncClient.aclose()` closes the transport it holds. Used as a context manager around one
    request, it would close a transport its caller owns, and the second call would fail.
    """

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_completion(json.dumps({"semantics": _semantics()})))

    transport = httpx.MockTransport(handler)
    adapter = _adapter(handler)
    adapter._transport = transport

    for _ in range(3):
        assert asyncio.run(adapter.invoke(_request(adapter), _spec())).output == {"semantics": _semantics()}
    assert len(calls) == 3


# --- endpoint-capable structured output (#310) ----------------------------------------------------


def test_the_structured_output_mode_follows_the_wire_model_name() -> None:
    assert structured_output_mode("deepseek-v4-flash") == "json_object"
    assert structured_output_mode("openai/deepseek-chat") == "json_object"
    assert structured_output_mode("DeepSeek-R2") == "json_object"
    assert structured_output_mode("openai/qwen3-30b") == "json_schema"
    assert structured_output_mode("scripted/test") == "json_schema"


@pytest.mark.parametrize(
    ("model", "expected_response_format"),
    [
        pytest.param("deepseek-v4-flash", {"type": "json_object"}, id="json_object_endpoint"),
        pytest.param("openai/qwen3-30b", None, id="json_schema_endpoint"),
    ],
)
def test_every_endpoint_gets_the_schema_in_the_message_and_only_the_constraint_differs(
    model: str, expected_response_format: dict[str, Any] | None
) -> None:
    """Both modes carry the schema; only `response_format` follows the endpoint (#315).

    This inverts a #313 guard that asserted a `json_schema` endpoint carried *no* inline copy. That guard
    was a migration-period invariant — #313's claim was that the `json_schema` request stayed byte-identical
    to #307's — and its mission ended when #313 merged. It was also, in hindsight, pinning the defect:
    the "redundant" copy is the only channel the schema's field descriptions have, and the endpoint that
    kept it was the one answering correctly.
    """

    spec = _spec()
    body = chat_request_body(
        model=model,
        instruction=spec.instruction,
        field_order=("evidence_json",),
        values={"evidence_json": "{}"},
        output_field=spec.output_field,
        output_model=spec.output_model,
        max_tokens=64,
    )
    schema = response_format(spec.output_field, spec.output_model)["json_schema"]["schema"]

    assert canonical_json(schema) in body["messages"][0]["content"]
    assert body["response_format"] == (
        expected_response_format
        if expected_response_format is not None
        else response_format(spec.output_field, spec.output_model)
    )


def test_a_prompt_json_endpoint_inlines_the_schema_without_unsupported_request_fields() -> None:
    spec = _spec()
    body = chat_request_body(
        model="MiniMax-M3",
        instruction=spec.instruction,
        field_order=("evidence_json",),
        values={"evidence_json": "{}"},
        output_field=spec.output_field,
        output_model=spec.output_model,
        max_tokens=64,
        temperature=1.0,
        structured_output="prompt_json",
    )

    assert body["temperature"] == 1.0
    assert "response_format" not in body
    schema = response_format(spec.output_field, spec.output_model)["json_schema"]["schema"]
    assert canonical_json(schema) in body["messages"][0]["content"]


def test_a_custom_endpoint_can_omit_temperature_without_a_provider_specific_profile() -> None:
    spec = _spec()
    body = chat_request_body(
        model="local/custom-model",
        instruction=spec.instruction,
        field_order=("evidence_json",),
        values={"evidence_json": "{}"},
        output_field=spec.output_field,
        output_model=spec.output_model,
        max_tokens=64,
        temperature=None,
        structured_output="prompt_json",
    )

    assert "temperature" not in body
    assert "response_format" not in body


def test_the_two_modes_differ_in_exactly_one_value() -> None:
    """The messages are identical, so a future edit cannot reintroduce a per-mode prompt by accident."""

    spec = _spec()
    kwargs: dict[str, Any] = dict(
        instruction=spec.instruction,
        field_order=("evidence_json",),
        values={"evidence_json": "{}"},
        output_field=spec.output_field,
        output_model=spec.output_model,
        max_tokens=64,
    )
    schema_mode = chat_request_body(model="m", **kwargs, structured_output="json_schema")
    object_mode = chat_request_body(model="m", **kwargs, structured_output="json_object")

    assert {key: value for key, value in schema_mode.items() if key != "response_format"} == {
        key: value for key, value in object_mode.items() if key != "response_format"
    }
    assert schema_mode["response_format"] != object_mode["response_format"]


# The sentence #307 lost and production broke on. `restates` is a cross-field constraint — a visible
# `event_status.told` index if and only if novelty is restatement — which no structured-output format can
# express: llama.cpp compiles `response_format` into a GBNF grammar, and a grammar constrains shape, not
# meaning. With the description out of the model's view the primary route emitted out-of-range indices on
# roughly a third of judgments (`restatement_index_invalid`: 0 before #307, the dominant failure class
# after). Named here so that deleting a Field description fails with a reason instead of silently.
_RESTATES_DESCRIPTION_SENTENCE = "Visible event_status.told index"


@pytest.mark.parametrize("mode", ["json_schema", "json_object", "prompt_json"])
def test_the_rendered_contract_carries_the_field_descriptions_a_grammar_cannot(mode: str) -> None:
    spec = _spec()
    body = chat_request_body(
        model="m",
        instruction=spec.instruction,
        field_order=("evidence_json",),
        values={"evidence_json": "{}"},
        output_field=spec.output_field,
        output_model=spec.output_model,
        max_tokens=64,
        structured_output=mode,
    )

    assert _RESTATES_DESCRIPTION_SENTENCE in body["messages"][0]["content"]


# Every `event_semantics` call pays for this block, on every route, forever. The bound is ~1.6x the
# measured size, which absorbs an ordinary field addition and refuses the two edits that would quietly
# tax every judgment: an enum grown to hundreds of members, or a description written as prose. It is a
# ceiling, not a pin — the exact bytes are already covered by NEWS_EXECUTION_ENVELOPE_SHA256, and a second
# hash of the same thing would only mean two lines to re-pin. Sized against the offline optimizer's release
# gate, which rejects a candidate whose per-case tokens grow more than 10%.
_OUTPUT_CONTRACT_MAX_BYTES = 6144


@pytest.mark.parametrize("predictor", ["event_semantics", "reader_card"])
def test_the_inlined_contract_stays_within_its_token_budget(predictor: str) -> None:
    output_field, output_model = PREDICTOR_OUTPUT[predictor]
    instruction = "<instruction>"
    rendered = system_message(instruction, output_field=output_field, output_model=output_model)
    contract = rendered[len(instruction) :]

    assert len(contract.encode("utf-8")) <= _OUTPUT_CONTRACT_MAX_BYTES


def test_provider_error_detail_is_bounded_and_secret_scrubbed() -> None:
    assert provider_error_detail(None) is None
    assert provider_error_detail({"error": "down"}) is None
    assert provider_error_detail({"error": {"message": ""}}) is None
    detail = provider_error_detail(
        {"error": {"code": "invalid_request_error", "message": "no sk-" + "a" * 24 + " for you " + "x" * 400}}
    )
    assert detail is not None
    assert detail.startswith("invalid_request_error: no [redacted] for you")
    assert "sk-" not in detail
    assert len(detail) <= 200


def test_a_refused_request_carries_the_providers_own_reason() -> None:
    """#310 was diagnosed with an offline probe because the 400's body was discarded; the error now keeps a
    bounded copy of what the provider actually said."""

    def handler(http_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {"code": "invalid_request_error", "message": "This response_format type is unavailable now"}
            },
        )

    adapter = _adapter(handler)
    with pytest.raises(PredictorAdapterError) as excinfo:
        asyncio.run(adapter.invoke(_request(adapter), _spec()))

    assert excinfo.value.code == "news_program_provider_http_400"
    assert excinfo.value.provider_detail == "invalid_request_error: This response_format type is unavailable now"


def test_a_gateway_aliased_deepseek_route_still_gets_json_object() -> None:
    assert structured_output_mode("accounts/fireworks/models/deepseek-v3") == "json_object"
    assert structured_output_mode("openai/gateway/deepseek-chat") == "json_object"
    assert structured_output_mode("accounts/acme/models/qwen3-30b") == "json_schema"
