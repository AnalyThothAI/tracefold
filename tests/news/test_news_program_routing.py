"""Availability, audit and physical-call contracts around the native DSPy Program."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import dspy  # type: ignore[import-untyped]
import pytest

from tracefold.news.program import routing as routing_module
from tracefold.news.program.artifact import NewsProgramStateV1, build_code_owned_program_state
from tracefold.news.program.contracts import ProgramCallTrace, ProgramTrace, SemanticJudgeError, TriageContext
from tracefold.news.program.lm import AuditedConfiguredLM, RuntimeModelIdentity, ScriptedLM
from tracefold.news.program.module import NativeNewsProgram
from tracefold.news.program.routing import RoutedSemanticJudge, RouteLMs
from tracefold.news.program.runtime import PROGRAM_VERSION
from tracefold.news.taxonomy import source_authority_from_evidence


def _semantics(**updates: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "novelty": "new_fact",
        "restates": -1,
        "assets": [{"symbol": "BTC", "market_type": "spot", "role": "primary"}],
        "direction": "bullish",
        "scope": "single_name",
        "magnitude": 2,
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
    return {"semantics": value}


def _taxonomy() -> dict[str, Any]:
    return {
        "taxonomy": {
            "subject_codes": ["medtop:20001279"],
            "event_family": "market_access",
            "change_state": "announced",
            "assertion_status": "confirmed",
        }
    }


def _card() -> dict[str, Any]:
    return {"card": {"headline_zh": "比特币上线新交易市场", "why_zh": "新增现货入口扩大了可交易范围。"}}


def _context() -> TriageContext:
    return TriageContext.from_card(
        {
            "event_id": "event-1",
            "evidence_version": 3,
            "evidence_sha256": "a" * 64,
            "focus_fact_id": "fact-1",
            "reporting_origin": "wire",
            "provenance": ["1018"],
            "leader_title": "BTC listed on Example Exchange",
            "raw_first_line": "$BTC listing",
            "leader_description": "Trading starts tomorrow.",
            "opened_at_ms": 1_000_000,
            "member_count": 1,
            "dedupe_family": "listing",
            "provider_metadata": {"coins": [{"symbol": "BTC", "grade": "A"}]},
            "queue_priority": "normal",
            "asset_class": "crypto",
            "grounded_assets": ["BTC"],
            "storyline_key": "asset:BTC",
        },
        watchlist=("BTC",),
        told_rows=(),
        now_ms=1_010_000,
        queue_lag_ms=10_000,
    )


def _audited(
    steps: list[Any],
    *,
    artifact: NewsProgramStateV1,
    predictor: str,
    route: str,
) -> AuditedConfiguredLM:
    model = f"scripted/{route}-{predictor}"
    delegate = ScriptedLM(steps, model=model)
    binding = getattr(getattr(artifact, predictor).model_bindings, route)
    return AuditedConfiguredLM(
        delegate,
        structured_output="json_schema",
        runtime_identity=RuntimeModelIdentity.issue(provider="scripted", model=model),
        predictor=predictor,
        route=route,
        model_binding=binding,
    )


def _route(
    artifact: NewsProgramStateV1,
    *,
    route: str,
    semantics: list[Any],
    cards: list[Any],
    taxonomies: list[Any] | None = None,
) -> RouteLMs:
    # The taxonomy Predictor runs between the two (#501); by default it answers every call it is asked.
    return RouteLMs(
        event_semantics=_audited(semantics, artifact=artifact, predictor="event_semantics", route=route),
        taxonomy=_audited(
            taxonomies if taxonomies is not None else [_taxonomy()] * 8,
            artifact=artifact,
            predictor="taxonomy",
            route=route,
        ),
        reader_card=_audited(cards, artifact=artifact, predictor="reader_card", route=route),
    )


def test_common_primary_success_is_exactly_three_physical_calls() -> None:
    artifact = build_code_owned_program_state()
    judge = RoutedSemanticJudge(
        NativeNewsProgram(artifact),
        primary=_route(artifact, route="primary", semantics=[_semantics()], cards=[_card()]),
    )

    judgment = asyncio.run(judge.judge(_context()))

    assert judgment.usage.call_count == judgment.usage.physical_call_count == 3
    assert [call.predictor for call in judgment.trace.calls] == ["event_semantics", "taxonomy", "reader_card"]
    assert all(call.terminal_disposition == "provider_success" for call in judgment.trace.calls)
    assert all(call.request_sha256 and call.invocation_sha256 for call in judgment.trace.calls)
    assert all(call.output_sha256 and call.validated_output for call in judgment.trace.calls)
    assert judgment.trace.taxonomy_sha256 is not None
    assert judgment.editorial.taxonomy.event_family == "market_access"
    assert judgment.trace.answering_route == "primary"
    assert judgment.fallback_from is None


def test_route_composition_rejects_unwrapped_base_lm_that_cannot_audit_calls() -> None:
    artifact = build_code_owned_program_state()
    bare = ScriptedLM([_semantics(), _taxonomy(), _card()])

    with pytest.raises(TypeError, match="news_program_route_lm_invalid"):
        RoutedSemanticJudge(
            NativeNewsProgram(artifact),
            primary=RouteLMs(event_semantics=bare, taxonomy=bare, reader_card=bare),
        )


def test_every_trace_rejects_unaddressed_or_unsettled_physical_call() -> None:
    incomplete = ProgramCallTrace(
        predictor="event_semantics",
        route="primary",
        attempt=1,
        request_sha256="a" * 64,
        input_sha256="b" * 64,
        model_binding="event_semantics.primary",
        physical_provider_call=True,
    )

    with pytest.raises(ValueError, match="news_program_native_call_audit_incomplete"):
        ProgramTrace(
            program_version=PROGRAM_VERSION,
            program_sha256="c" * 64,
            context_sha256="d" * 64,
            envelope_sha256="e" * 64,
            calls=(incomplete,),
        )


@pytest.mark.parametrize(
    ("field_name", "invalid"),
    (
        ("program_version", "retired-program"),
        ("program_sha256", ""),
        ("context_sha256", "not-a-sha"),
    ),
)
def test_program_trace_rejects_retired_or_unaddressed_identity(field_name: str, invalid: str) -> None:
    values = {
        "program_version": PROGRAM_VERSION,
        "program_sha256": "c" * 64,
        "context_sha256": "d" * 64,
        "envelope_sha256": "e" * 64,
    }
    values[field_name] = invalid

    with pytest.raises(ValueError):
        ProgramTrace.model_validate(values)


@pytest.mark.parametrize(
    ("trace_field", "judgment_field"),
    (
        ("event_semantics_sha256", None),
        ("taxonomy_sha256", None),
        ("reader_card_sha256", None),
        ("answering_route", None),
        (None, "answering_model"),
    ),
)
def test_successful_judgment_requires_complete_answer_identity(
    trace_field: str | None, judgment_field: str | None
) -> None:
    artifact = build_code_owned_program_state()
    judge = RoutedSemanticJudge(
        NativeNewsProgram(artifact),
        primary=_route(artifact, route="primary", semantics=[_semantics()], cards=[_card()]),
    )
    judgment = asyncio.run(judge.judge(_context()))
    payload = judgment.model_dump(mode="json")
    if trace_field is not None:
        payload["trace"][trace_field] = None
    if judgment_field is not None:
        payload[judgment_field] = None

    with pytest.raises(ValueError, match="news_program_judgment_trace_identity_mismatch"):
        type(judgment).model_validate(payload)


@pytest.mark.parametrize(("answering_route", "fallback_from"), (("fallback", None), ("primary", "failure")))
def test_successful_judgment_route_matches_fallback_cause(answering_route: str, fallback_from: str | None) -> None:
    artifact = build_code_owned_program_state()
    judge = RoutedSemanticJudge(
        NativeNewsProgram(artifact),
        primary=_route(artifact, route="primary", semantics=[_semantics()], cards=[_card()]),
    )
    judgment = asyncio.run(judge.judge(_context()))
    payload = judgment.model_dump(mode="json")
    payload["fallback_from"] = fallback_from
    payload["trace"]["fallback_from"] = fallback_from
    payload["trace"]["answering_route"] = answering_route

    with pytest.raises(ValueError, match="news_program_judgment_trace_identity_mismatch"):
        type(judgment).model_validate(payload)


def test_stock_json_adapter_format_fallback_is_audited_and_route_stays_bounded() -> None:
    artifact = build_code_owned_program_state()
    judge = RoutedSemanticJudge(
        NativeNewsProgram(artifact),
        primary=_route(
            artifact,
            route="primary",
            semantics=["not-json", _semantics()],
            taxonomies=["not-json", _taxonomy()],
            cards=["not-json", _card()],
        ),
    )

    judgment = asyncio.run(judge.judge(_context()))

    assert judgment.usage.physical_call_count == 6
    assert [call.terminal_disposition for call in judgment.trace.calls] == [
        "adapter_parse_error",
        "provider_success",
        "adapter_parse_error",
        "provider_success",
        "adapter_parse_error",
        "provider_success",
    ]
    assert [call.attempt for call in judgment.trace.calls] == [1, 2, 1, 2, 1, 2]


def test_provider_failure_falls_back_and_restarts_from_event_semantics() -> None:
    artifact = build_code_owned_program_state()
    judge = RoutedSemanticJudge(
        NativeNewsProgram(artifact),
        primary=_route(
            artifact,
            route="primary",
            semantics=[dspy.LMServerError("unavailable", code="server")],
            cards=[],
        ),
        fallback=_route(artifact, route="fallback", semantics=[_semantics()], cards=[_card()]),
    )

    judgment = asyncio.run(judge.judge(_context()))

    assert [(call.route, call.predictor) for call in judgment.trace.calls] == [
        ("primary", "event_semantics"),
        ("fallback", "event_semantics"),
        ("fallback", "taxonomy"),
        ("fallback", "reader_card"),
    ]
    assert judgment.trace.calls[0].terminal_disposition == "provider_error"
    assert judgment.trace.answering_route == "fallback"
    assert judgment.fallback_from == "news_program_lm_server"


@pytest.mark.parametrize(
    "error",
    [AssertionError("assertion defect"), RuntimeError("runtime defect"), MemoryError("memory defect")],
)
def test_unknown_program_exception_propagates_without_fallback(error: BaseException) -> None:
    artifact = build_code_owned_program_state()
    primary = _route(artifact, route="primary", semantics=[error], cards=[])
    fallback = _route(artifact, route="fallback", semantics=[_semantics()], cards=[_card()])
    judge = RoutedSemanticJudge(NativeNewsProgram(artifact), primary=primary, fallback=fallback)

    with pytest.raises(type(error)) as captured:
        asyncio.run(judge.judge(_context()))

    assert captured.value is error
    assert fallback.event_semantics._delegate.requests == []


def test_domain_invalid_semantics_fail_closed_without_novelty_default() -> None:
    artifact = build_code_owned_program_state()
    judge = RoutedSemanticJudge(
        NativeNewsProgram(artifact),
        primary=_route(
            artifact,
            route="primary",
            semantics=[_semantics(novelty="restatement", restates=4)],
            cards=[],
        ),
    )

    with pytest.raises(SemanticJudgeError) as caught:
        asyncio.run(judge.judge(_context()))

    assert caught.value.code == "news_program_restatement_index_invalid"
    assert caught.value.output_failure is True
    assert caught.value.attempts == 1
    assert caught.value.partial_trace is not None
    assert caught.value.partial_trace.calls[0].terminal_disposition == "domain_validation_error"


def test_truncated_provider_answer_remains_provider_success_and_skips_format_retry() -> None:
    artifact = build_code_owned_program_state()
    response = dspy.LMResponse.from_text('{"semantics":', model="scripted/truncated")
    response.outputs[0] = response.output.model_copy(update={"finish_reason": "length", "truncated": True})
    judge = RoutedSemanticJudge(
        NativeNewsProgram(artifact),
        primary=_route(artifact, route="primary", semantics=[response], cards=[]),
    )

    with pytest.raises(SemanticJudgeError) as caught:
        asyncio.run(judge.judge(_context()))

    assert caught.value.code == "news_program_output_truncated"
    assert caught.value.output_failure is True
    assert caught.value.partial_trace is not None
    assert len(caught.value.partial_trace.calls) == 1
    call = caught.value.partial_trace.calls[0]
    assert call.terminal_disposition == "provider_success"
    assert call.error_code == "news_program_lm_output_truncated"
    assert call.finish_reason == "length"


def test_dual_provider_failure_returns_one_complete_partial_trace() -> None:
    artifact = build_code_owned_program_state()
    judge = RoutedSemanticJudge(
        NativeNewsProgram(artifact),
        primary=_route(
            artifact,
            route="primary",
            semantics=[dspy.LMRateLimitError("busy", code="rate_limit")],
            cards=[],
        ),
        fallback=_route(
            artifact,
            route="fallback",
            semantics=[dspy.LMServerError("down", code="server")],
            cards=[],
        ),
    )

    with pytest.raises(SemanticJudgeError) as caught:
        asyncio.run(judge.judge(_context()))

    error = caught.value
    assert error.code == "news_program_lm_server"
    assert error.primary_code == "news_program_lm_rate_limit"
    assert error.attempts == 2
    assert error.partial_trace is not None
    assert [call.route for call in error.partial_trace.calls] == ["primary", "fallback"]


def test_primary_breaker_opens_after_three_retryable_failures_and_skips_a_physical_call() -> None:
    artifact = build_code_owned_program_state()
    primary = _route(
        artifact,
        route="primary",
        semantics=[dspy.LMServerError("down", code="server") for _ in range(3)],
        cards=[],
    )
    fallback = _route(
        artifact,
        route="fallback",
        semantics=[_semantics() for _ in range(4)],
        cards=[_card() for _ in range(4)],
    )
    judge = RoutedSemanticJudge(NativeNewsProgram(artifact), primary=primary, fallback=fallback)

    judgments = [asyncio.run(judge.judge(_context())) for _ in range(4)]

    assert [item.fallback_from for item in judgments] == [
        "news_program_lm_server",
        "news_program_lm_server",
        "news_program_lm_server",
        "primary_circuit_open",
    ]
    assert [item.usage.physical_call_count for item in judgments] == [4, 4, 4, 3]
    assert all(call.route == "fallback" for call in judgments[-1].trace.calls)


def test_compile_mode_without_primary_breaker_attempts_every_independent_case() -> None:
    artifact = build_code_owned_program_state()
    primary = _route(
        artifact,
        route="primary",
        semantics=[dspy.LMServerError("down", code="server") for _ in range(4)],
        cards=[],
    )
    judge = RoutedSemanticJudge(
        NativeNewsProgram(artifact),
        primary=primary,
        route_deadline_seconds=None,
        primary_breaker_enabled=False,
    )

    errors: list[SemanticJudgeError] = []
    for _ in range(4):
        with pytest.raises(SemanticJudgeError) as caught:
            asyncio.run(judge.judge(_context()))
        errors.append(caught.value)

    assert all(error.code == "news_program_lm_server" for error in errors)
    assert all(error.partial_trace is not None and len(error.partial_trace.calls) == 1 for error in errors)


class _CancelledLM(dspy.BaseLM):
    """Typed provider spy that never answers and observes route cancellation."""

    forward_contract = "typed_lm"

    def __init__(self) -> None:
        super().__init__("scripted/cancelled", cache=False, num_retries=0)
        self.requests: list[dspy.LMRequest] = []
        self.cancelled = False

    def forward(self, request: dspy.LMRequest) -> dspy.LMResponse:
        raise AssertionError("production route must use the async LM entry")

    async def aforward(self, request: dspy.LMRequest) -> dspy.LMResponse:
        self.requests.append(request)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")


class _LateLM(_CancelledLM):
    """Provider spy that suppresses cancellation and returns one late answer."""

    def __init__(self, answer: dict[str, Any]) -> None:
        super().__init__()
        self.model = "scripted/late"
        self.answer = answer

    async def aforward(self, request: dspy.LMRequest) -> dspy.LMResponse:
        self.requests.append(request)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
        return dspy.LMResponse.from_text(
            json.dumps(self.answer, ensure_ascii=False),
            model=self.model,
        )


def _wrap_provider_spy(
    delegate: dspy.BaseLM,
    *,
    artifact: NewsProgramStateV1,
) -> AuditedConfiguredLM:
    return AuditedConfiguredLM(
        delegate,
        structured_output="json_schema",
        runtime_identity=RuntimeModelIdentity.issue(provider="scripted", model=delegate.model),
        predictor="event_semantics",
        route="primary",
        model_binding=artifact.event_semantics.model_bindings.primary,
    )


def test_route_deadline_cancels_the_physical_call_and_closes_one_terminal_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = build_code_owned_program_state()
    delegate = _CancelledLM()
    primary = RouteLMs(
        event_semantics=_wrap_provider_spy(delegate, artifact=artifact),
        taxonomy=_audited([_taxonomy()], artifact=artifact, predictor="taxonomy", route="primary"),
        reader_card=_audited([_card()], artifact=artifact, predictor="reader_card", route="primary"),
    )
    judge = RoutedSemanticJudge(NativeNewsProgram(artifact), primary=primary)
    monkeypatch.setattr(routing_module, "PROGRAM_ROUTE_DEADLINE_SECONDS", 0.05)

    with pytest.raises(SemanticJudgeError) as caught:
        asyncio.run(judge.judge(_context()))

    assert delegate.cancelled is True
    assert len(delegate.requests) == 1
    assert caught.value.code == "news_program_route_deadline"
    assert caught.value.partial_trace is not None
    assert len(caught.value.partial_trace.calls) == 1
    assert caught.value.partial_trace.calls[0].terminal_disposition == "timeout_cancelled"


def test_provider_answer_after_deadline_is_reconciled_as_late_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = build_code_owned_program_state()
    delegate = _LateLM(_semantics())
    primary = RouteLMs(
        event_semantics=_wrap_provider_spy(delegate, artifact=artifact),
        taxonomy=_audited([_taxonomy()], artifact=artifact, predictor="taxonomy", route="primary"),
        reader_card=_audited([_card()], artifact=artifact, predictor="reader_card", route="primary"),
    )
    judge = RoutedSemanticJudge(NativeNewsProgram(artifact), primary=primary)
    monkeypatch.setattr(routing_module, "PROGRAM_ROUTE_DEADLINE_SECONDS", 0.05)

    with pytest.raises(SemanticJudgeError) as caught:
        asyncio.run(judge.judge(_context()))

    assert delegate.cancelled is True
    assert len(delegate.requests) == 1
    assert caught.value.code == "news_program_route_deadline"
    assert caught.value.partial_trace is not None
    assert len(caught.value.partial_trace.calls) == 1
    assert caught.value.partial_trace.calls[0].terminal_disposition == "late_completion"


def _taxonomy_only_failure(taxonomies: list[Any]) -> tuple[Any, RouteLMs]:
    """One judgment whose taxonomy Predictor fails and whose other two Predictors answer."""

    artifact = build_code_owned_program_state()
    fallback = _route(artifact, route="fallback", semantics=[_semantics()], cards=[_card()])
    judge = RoutedSemanticJudge(
        NativeNewsProgram(artifact),
        primary=_route(
            artifact,
            route="primary",
            semantics=[_semantics()],
            taxonomies=taxonomies,
            cards=[_card()],
        ),
        fallback=fallback,
    )
    return asyncio.run(judge.judge(_context())), fallback


def test_a_taxonomy_provider_failure_keeps_the_card_and_never_restarts_the_route() -> None:
    """#651 §5.3. The fallback re-runs all three Predictors, so it is the right answer only when the
    judgment has nothing to publish. A taxonomy failure leaves a complete verdict, a complete card and the
    code-owned source authority `decide()` reads, so spending a second EventSemantics and a second
    ReaderCard call on it would buy the reader nothing."""

    judgment, fallback = _taxonomy_only_failure([dspy.LMServerError("unavailable", code="server")])

    assert judgment.trace.answering_route == "primary"
    assert judgment.fallback_from is None
    assert fallback.event_semantics._delegate.requests == []
    assert [(call.predictor, call.terminal_disposition) for call in judgment.trace.calls] == [
        ("event_semantics", "provider_success"),
        ("taxonomy", "provider_error"),
        ("reader_card", "provider_success"),
    ]
    assert judgment.verdict.headline_zh == "比特币上线新交易市场"
    assert judgment.editorial.taxonomy is None
    assert judgment.editorial.taxonomy_status == "unavailable"
    assert judgment.editorial.taxonomy_error_code == "news_program_lm_server"
    # The authority is computed from the evidence and is unaffected by the Predictor that failed.
    assert judgment.editorial.source_authority == source_authority_from_evidence(_context().evidence)
    # Per-predictor outcome at judgment altitude: no taxonomy hash, and the code that says why.
    assert judgment.trace.taxonomy_sha256 is None
    assert judgment.trace.taxonomy_error_code == "news_program_lm_server"
    assert judgment.trace.reader_card_sha256 is not None


def test_a_truncated_taxonomy_answer_is_named_on_the_judgment_it_did_not_stop() -> None:
    response = dspy.LMResponse.from_text('{"taxonomy":', model="scripted/truncated")
    response.outputs[0] = response.output.model_copy(update={"finish_reason": "length", "truncated": True})

    judgment, fallback = _taxonomy_only_failure([response])

    assert fallback.taxonomy._delegate.requests == []
    assert judgment.editorial.taxonomy_status == "unavailable"
    assert judgment.editorial.taxonomy_error_code == "news_program_output_truncated"
    taxonomy_call = next(call for call in judgment.trace.calls if call.predictor == "taxonomy")
    assert taxonomy_call.error_code == "news_program_lm_output_truncated"
    assert taxonomy_call.finish_reason == "length"


def test_an_unparseable_taxonomy_answer_costs_the_label_and_nothing_else() -> None:
    """The adapter's own one format fallback still runs; only the third call is the ReaderCard's."""

    judgment, _ = _taxonomy_only_failure(["not-json", "not-json"])

    assert [(call.predictor, call.attempt, call.terminal_disposition) for call in judgment.trace.calls] == [
        ("event_semantics", 1, "provider_success"),
        ("taxonomy", 1, "adapter_parse_error"),
        ("taxonomy", 2, "adapter_parse_error"),
        ("reader_card", 1, "provider_success"),
    ]
    assert judgment.editorial.taxonomy_error_code == "news_program_adapter_parse_error"
    assert judgment.usage.physical_call_count == 4


def test_a_card_failure_after_a_taxonomy_failure_still_fails_the_whole_judgment() -> None:
    """The reader's copy is the product. Losing the classification is survivable; losing the card is not."""

    artifact = build_code_owned_program_state()
    judge = RoutedSemanticJudge(
        NativeNewsProgram(artifact),
        primary=_route(
            artifact,
            route="primary",
            semantics=[_semantics()],
            taxonomies=[dspy.LMServerError("unavailable", code="server")],
            cards=[dspy.LMServerError("unavailable", code="server")],
        ),
    )

    with pytest.raises(SemanticJudgeError) as caught:
        asyncio.run(judge.judge(_context()))

    assert caught.value.failing_predictor == "reader_card"
