"""Program-native semantic judgment for News V3.

The public Interface is deliberately small: callers submit one immutable
``TriageContext`` to ``SemanticJudge.judge`` and receive one complete
``SemanticJudgment``.  The hidden graph is exactly
``EventSemantics -> SemanticNormalizer -> ReaderCard -> VerdictAssembler``.
Model transport is an Adapter Seam; domain validation, retry/fallback budgets,
identity and audit remain owned by this Module.

Since #306 Phase 3 there is no model framework under it. The two Predictors are
``PredictorSpec`` values — an instruction, the bounded input fields, the output
model and the token ceiling — and ``transport.py`` turns one of those plus one
input mapping into one HTTP request. That also collapsed the separate optimizer
student: what GEPA evaluates is this Module bound to one task endpoint, so "the
optimized bytes are the production bytes" is what the code does rather than what
a refactor-baseline test asserts.

Only canonical JSON state is loadable.  This module never loads pickle,
cloudpickle, arbitrary classes, endpoints, or credentials.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ValidationError

from ..artifact_identity import canonical_json, canonical_sha
from ..models import TriageVerdict
from .artifact import (
    PredictorModelBindings,
    PredictorState,
    ProgramStrategyArtifactCodec,
    ProgramStrategyArtifactV1,
    ProgramStrategyPatchV1,
    apply_program_patch,
    build_code_owned_program_artifact,
    build_predictor_state,
    load_program_artifact,
    load_stable_program_artifact,
    render_model_evidence_json,
    validate_program_instruction,
)
from .assembly import decision_for, is_actionable, restatement_index_error
from .contracts import (
    EditorialEnvelope,
    FrozenEventEvidence,
    ProgramCallTrace,
    ProgramNormalizationTrace,
    ProgramTrace,
    ProgramUsage,
    ReaderCardSemanticView,
    ScoredJudgment,
    SemanticGateContext,
    SemanticJudge,
    SemanticJudgeError,
    SemanticJudgment,
    TradeRelevanceV1,
    TriageContext,
    aggregate_program_usage,
)
from .identity import EXECUTION_ENVELOPE_SHA256
from .runtime import (
    PROGRAM_PRIMARY_BREAKER_FAILURES,
    PROGRAM_PRIMARY_BREAKER_OPEN_SECONDS,
    PROGRAM_ROUTE_DEADLINE_SECONDS,
    PROGRAM_SCHEMA_VERSION,
    PROGRAM_VERSION,
)
from .signatures import (
    PREDICTOR_INPUT_FIELDS,
    PREDICTOR_OUTPUT,
    EventSemantics,
    ReaderCard,
)
from .transport import (
    _TRUNCATED_FINISH_REASONS,
    ChatCompletionsPredictorAdapter,
    PredictorAdapter,
    PredictorAdapterError,
    PredictorRecording,
    PredictorRequest,
    PredictorResponse,
    PredictorSpec,
    ProviderCallMetrics,
    ProviderCallObservation,
    RecordReplayPredictorAdapter,
    RuntimeModelIdentity,
    ScriptedPredictorAdapter,
    _is_retryable_exception,
)


def predictor_spec(state: PredictorState) -> PredictorSpec:
    """One Predictor's complete request shape, derived from its ready-to-execute state."""

    output_field, output_model = PREDICTOR_OUTPUT[state.name]
    return PredictorSpec(
        name=state.name,
        instruction=state.instruction,
        input_fields=PREDICTOR_INPUT_FIELDS[state.name],
        output_field=output_field,
        output_model=output_model,
        max_tokens=state.max_tokens,
    )


def _unwrap_output(output: Mapping[str, Any], field: str) -> Any:
    if field in output:
        if set(output) != {field}:
            raise ValueError("news_program_output_envelope_extra")
        return output[field]
    return dict(output)


def _reader_card_semantic_view(semantics: EventSemantics) -> ReaderCardSemanticView:
    return ReaderCardSemanticView(
        event_type=semantics.event_type,
        assets=semantics.assets,
        direction=semantics.direction,
        magnitude=semantics.magnitude,
        novelty=semantics.novelty,
        restates=semantics.restates,
        scope=semantics.scope,
        channels=semantics.relevance.channels,
        affected_markets=semantics.relevance.affected_markets,
    )


def _assemble(
    semantics: EventSemantics,
    card: ReaderCard,
    *,
    told_count: int,
) -> tuple[TriageVerdict, EditorialEnvelope]:
    error = restatement_index_error(novelty=semantics.novelty, restates=semantics.restates, told_count=told_count)
    if error is not None:
        raise ValueError(error)
    relevance = semantics.relevance
    actionable = is_actionable(
        tradability=relevance.tradability,
        has_channels=bool(relevance.channels),
        has_affected_markets=bool(relevance.affected_markets),
    )
    decision = decision_for(relevance.reader_value)
    verdict = TriageVerdict.model_validate(
        {
            "novelty": semantics.novelty,
            "restates": semantics.restates,
            "event_type": semantics.event_type,
            "assets": [asset.model_dump(mode="json") for asset in semantics.assets],
            "direction": semantics.direction,
            "scope": semantics.scope,
            "magnitude": semantics.magnitude,
            "actionable": actionable,
            "confidence": semantics.confidence,
            "decision": decision,
            "audience": semantics.audience,
            "headline_zh": card.headline_zh.strip(),
            "title_zh": "",
            "why_zh": card.why_zh.strip(),
        }
    )
    return verdict, EditorialEnvelope.issue(editorial_origin="model", relevance=relevance)


def _validate_semantic_context(semantics: EventSemantics, *, told_count: int) -> None:
    error = restatement_index_error(novelty=semantics.novelty, restates=semantics.restates, told_count=told_count)
    if error is not None:
        raise ValueError(error)


def _normalize_semantics(
    semantics: EventSemantics,
) -> tuple[EventSemantics, tuple[ProgramNormalizationTrace, ...]]:
    if semantics.novelty == "restatement" or semantics.restates == -1:
        return semantics, ()
    normalization = ProgramNormalizationTrace(
        field="restates",
        reason="non_restatement_index_ignored",
        input_value=semantics.restates,
        output_value=-1,
    )
    normalized = semantics.model_copy(update={"restates": normalization.output_value})
    return normalized, (normalization,)


def _relevance_normalization_traces(
    raw_output: Mapping[str, Any],
    semantics: EventSemantics,
) -> tuple[ProgramNormalizationTrace, ...]:
    raw_relevance = raw_output.get("relevance")
    if not isinstance(raw_relevance, Mapping):
        return ()
    traces: list[ProgramNormalizationTrace] = []
    for field in ("channels", "affected_markets"):
        raw = raw_relevance.get(field)
        if not isinstance(raw, (list, tuple)) or not all(isinstance(item, str) for item in raw):
            continue
        normalized = tuple(getattr(semantics.relevance, field))
        original = tuple(raw)
        if original != normalized:
            traces.append(
                ProgramNormalizationTrace(
                    field=field,
                    reason="canonical_set_order",
                    input_value=original,
                    output_value=normalized,
                )
            )
    return tuple(traces)


class _CallFailure(Exception):
    def __init__(
        self,
        *,
        code: str,
        retryable: bool,
        output_failure: bool,
        finish_reason: str | None,
        trace: ProgramCallTrace,
        raw_output: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.output_failure = output_failure
        self.finish_reason = finish_reason
        self.trace = trace
        self.raw_output = raw_output
        super().__init__(code)

    @property
    def fast_retryable(self) -> bool:
        return (self.retryable or self.output_failure) and (
            self.finish_reason or ""
        ).casefold() not in _TRUNCATED_FINISH_REASONS


T = TypeVar("T", bound=BaseModel)
AdapterPool = PredictorAdapter | Mapping[str, PredictorAdapter]


class NewsSemanticProgram:
    """Deep Module owning graph execution, validation, budgets and audit."""

    def __init__(
        self,
        artifact: ProgramStrategyArtifactV1,
        *,
        primary_adapter: AdapterPool,
        fallback_adapter: AdapterPool | None = None,
    ) -> None:
        if artifact.program_sha256 != artifact.computed_sha256():
            raise ValueError("news_program_artifact_hash_mismatch")
        self.artifact = artifact
        self.primary_adapter = self._prepare_adapter_pool(primary_adapter, route="primary")
        self.fallback_adapter = (
            self._prepare_adapter_pool(fallback_adapter, route="fallback") if fallback_adapter is not None else None
        )
        self.event_semantics = predictor_spec(artifact.event_semantics)
        self.reader_card = predictor_spec(artifact.reader_card)
        self._primary_failures = 0
        self._primary_open_until = 0.0

    def _prepare_adapter_pool(
        self,
        pool: AdapterPool,
        *,
        route: Literal["primary", "fallback"],
    ) -> AdapterPool:
        if not isinstance(pool, Mapping):
            return pool
        frozen = dict(pool)
        expected = {
            getattr(self.artifact.event_semantics.model_bindings, route),
            getattr(self.artifact.reader_card.model_bindings, route),
        }
        if set(frozen) != expected:
            raise ValueError("news_program_adapter_bindings_invalid")
        if not all(isinstance(adapter, PredictorAdapter) for adapter in frozen.values()):
            raise TypeError("news_program_adapter_protocol_invalid")
        return frozen

    async def judge(self, context: TriageContext) -> SemanticJudgment:
        if not isinstance(context, TriageContext):
            context = TriageContext.model_validate(context)
        started = time.perf_counter()
        context_sha = canonical_sha(context.model_dump(mode="json"))
        semantics_json_input = render_model_evidence_json(
            context.event_semantics_payload(), predictor="event_semantics"
        )
        card_json_input = render_model_evidence_json(context.reader_card_payload(), predictor="reader_card")
        calls: list[ProgramCallTrace] = []
        primary_failure: _CallFailure | None = None
        if time.monotonic() < self._primary_open_until:
            primary_failure = self._circuit_open_failure(context_sha)
        else:
            try:
                result = await self._run_route_with_deadline(
                    route="primary",
                    adapter_pool=self.primary_adapter,
                    context=context,
                    context_sha=context_sha,
                    semantics_evidence_json=semantics_json_input,
                    card_evidence_json=card_json_input,
                    calls=calls,
                )
            except _CallFailure as exc:
                primary_failure = exc
                if exc.retryable and not exc.output_failure:
                    self._record_primary_failure()
            else:
                self._primary_failures = 0
                self._primary_open_until = 0.0
        if primary_failure is not None:
            if self.fallback_adapter is None:
                raise self._public_error(primary_failure, calls, context_sha=context_sha)
            try:
                result = await self._run_route_with_deadline(
                    route="fallback",
                    adapter_pool=self.fallback_adapter,
                    context=context,
                    context_sha=context_sha,
                    semantics_evidence_json=semantics_json_input,
                    card_evidence_json=card_json_input,
                    calls=calls,
                )
            except _CallFailure as fallback_exc:
                raise self._public_error(
                    fallback_exc,
                    calls,
                    context_sha=context_sha,
                    primary_failure=primary_failure,
                ) from fallback_exc
        semantics, card, verdict, editorial, route, answering_model, novelty_defaulted = result
        semantics_sha = canonical_sha(semantics.model_dump(mode="json"))
        card_sha = canonical_sha(card.model_dump(mode="json"))
        verdict_sha = canonical_sha(verdict.model_dump(mode="json"))
        fallback_from = primary_failure.code if primary_failure is not None else None
        trace = self._trace(
            calls,
            context_sha=context_sha,
            event_semantics_sha256=semantics_sha,
            reader_card_sha256=card_sha,
            verdict_sha256=verdict_sha,
            editorial_sha256=editorial.editorial_sha256,
            answering_route=route,
            fallback_from=fallback_from,
            novelty_defaulted=novelty_defaulted,
        )
        usage = self._usage(calls, started=started)
        return SemanticJudgment(
            verdict=verdict,
            editorial=editorial,
            program_version=PROGRAM_VERSION,
            program_sha256=self.artifact.program_sha256,
            trace=trace,
            usage=usage,
            answering_model=answering_model,
            fallback_from=fallback_from,
        )

    def _record_primary_failure(self) -> None:
        self._primary_failures += 1
        if self._primary_failures >= PROGRAM_PRIMARY_BREAKER_FAILURES:
            self._primary_failures = 0
            self._primary_open_until = time.monotonic() + PROGRAM_PRIMARY_BREAKER_OPEN_SECONDS

    def _circuit_open_failure(self, context_sha: str) -> _CallFailure:
        trace = ProgramCallTrace(
            predictor="event_semantics",
            route="primary",
            attempt=1,
            request_sha256=canonical_sha(
                {
                    "program_sha256": self.artifact.program_sha256,
                    "context_sha256": context_sha,
                    "route": "primary",
                    "state": "circuit_open",
                }
            ),
            input_sha256=canonical_sha({"context_sha256": context_sha}),
            model_binding=self.artifact.event_semantics.model_bindings.primary,
            error_code="primary_circuit_open",
        )
        return _CallFailure(
            code="primary_circuit_open",
            retryable=False,
            output_failure=False,
            finish_reason=None,
            trace=trace,
        )

    async def _run_route_with_deadline(
        self,
        *,
        route: Literal["primary", "fallback"],
        adapter_pool: AdapterPool,
        context: TriageContext,
        context_sha: str,
        semantics_evidence_json: str,
        card_evidence_json: str,
        calls: list[ProgramCallTrace],
    ) -> tuple[
        EventSemantics,
        ReaderCard,
        TriageVerdict,
        EditorialEnvelope,
        Literal["primary", "fallback"],
        str | None,
        bool,
    ]:
        route_call_start = len(calls)
        try:
            async with asyncio.timeout(PROGRAM_ROUTE_DEADLINE_SECONDS):
                return await self._run_route(
                    route=route,
                    adapter_pool=adapter_pool,
                    context=context,
                    context_sha=context_sha,
                    semantics_evidence_json=semantics_evidence_json,
                    card_evidence_json=card_evidence_json,
                    calls=calls,
                )
        except TimeoutError as exc:
            if len(calls) > route_call_start and calls[-1].error_code == "news_program_route_deadline":
                trace = calls[-1]
            else:
                state = self.artifact.event_semantics
                trace = ProgramCallTrace(
                    predictor="event_semantics",
                    route=route,
                    attempt=1,
                    request_sha256=canonical_sha(
                        {"program_sha256": self.artifact.program_sha256, "context_sha256": context_sha, "route": route}
                    ),
                    input_sha256=canonical_sha({"evidence_json": semantics_evidence_json}),
                    model_binding=getattr(state.model_bindings, route),
                    error_code="news_program_route_deadline",
                )
                calls.append(trace)
            raise _CallFailure(
                code="news_program_route_deadline",
                retryable=True,
                output_failure=False,
                finish_reason=None,
                trace=trace,
            ) from exc

    @staticmethod
    def _resolve_adapter(
        pool: AdapterPool,
        state: PredictorState,
        route: Literal["primary", "fallback"],
    ) -> PredictorAdapter:
        if not isinstance(pool, Mapping):
            return pool
        binding = getattr(state.model_bindings, route)
        adapter = pool.get(binding)
        if adapter is None:
            raise PredictorAdapterError("news_program_model_binding_unresolved")
        return adapter

    async def _run_route(
        self,
        *,
        route: Literal["primary", "fallback"],
        adapter_pool: AdapterPool,
        context: TriageContext,
        context_sha: str,
        semantics_evidence_json: str,
        card_evidence_json: str,
        calls: list[ProgramCallTrace],
    ) -> tuple[
        EventSemantics,
        ReaderCard,
        TriageVerdict,
        EditorialEnvelope,
        Literal["primary", "fallback"],
        str | None,
        bool,
    ]:
        retry_available = True
        novelty_defaulted = False
        semantics: EventSemantics | None = None
        semantics_attempt = 1
        while semantics is None:
            try:
                semantics = await self._call_predictor(
                    state=self.artifact.event_semantics,
                    spec=self.event_semantics,
                    adapter=self._resolve_adapter(adapter_pool, self.artifact.event_semantics, route),
                    route=route,
                    attempt=semantics_attempt,
                    context_sha=context_sha,
                    inputs={"evidence_json": semantics_evidence_json},
                    upstream_sha=None,
                    output_field="semantics",
                    output_model=EventSemantics,
                    calls=calls,
                )
                semantics, normalizations = _normalize_semantics(semantics)
                if normalizations:
                    calls[-1] = calls[-1].model_copy(
                        update={"normalizations": (*calls[-1].normalizations, *normalizations)}
                    )
                try:
                    _validate_semantic_context(semantics, told_count=len(context.told.entries))
                except ValueError as exc:
                    failed = calls[-1].model_copy(update={"error_code": str(exc)})
                    calls[-1] = failed
                    raise _CallFailure(
                        code=str(exc),
                        retryable=False,
                        output_failure=True,
                        finish_reason=failed.finish_reason,
                        trace=failed,
                        raw_output=semantics.model_dump(mode="json"),
                    ) from exc
            except _CallFailure as exc:
                if retry_available and exc.fast_retryable:
                    retry_available = False
                    semantics = None
                    semantics_attempt += 1
                    continue
                if (
                    exc.output_failure
                    and exc.raw_output is not None
                    and "novelty" not in exc.raw_output
                    and (exc.finish_reason or "").casefold() not in _TRUNCATED_FINISH_REASONS
                ):
                    patched = dict(exc.raw_output)
                    patched.update({"novelty": "new_fact", "restates": -1})
                    try:
                        semantics = EventSemantics.model_validate(patched)
                    except ValidationError:
                        raise exc from None
                    novelty_defaulted = True
                    patched_state = semantics.model_dump(mode="json")
                    normalizations = _relevance_normalization_traces(patched, semantics)
                    calls[-1] = calls[-1].model_copy(
                        update={
                            "error_code": "news_program_novelty_defaulted",
                            "output_sha256": canonical_sha(patched_state),
                            "validated_output": patched_state,
                            "normalizations": normalizations,
                        }
                    )
                    continue
                raise
        semantics_sha = canonical_sha(semantics.model_dump(mode="json"))
        semantic_view = _reader_card_semantic_view(semantics)
        card_attempt = 1
        while True:
            try:
                card = await self._call_predictor(
                    state=self.artifact.reader_card,
                    spec=self.reader_card,
                    adapter=self._resolve_adapter(adapter_pool, self.artifact.reader_card, route),
                    route=route,
                    attempt=card_attempt,
                    context_sha=context_sha,
                    inputs={
                        "evidence_json": card_evidence_json,
                        "semantics_json": canonical_json(semantic_view.model_dump(mode="json")),
                    },
                    upstream_sha=semantics_sha,
                    output_field="card",
                    output_model=ReaderCard,
                    calls=calls,
                )
                break
            except _CallFailure as exc:
                if retry_available and exc.fast_retryable:
                    retry_available = False
                    card_attempt += 1
                    continue
                raise
        try:
            verdict, editorial = _assemble(semantics, card, told_count=len(context.told.entries))
        except (ValidationError, ValueError) as exc:
            last = calls[-1]
            code = (
                str(exc)
                if isinstance(exc, ValueError) and str(exc).startswith("news_program_")
                else "news_program_verdict_invalid"
            )
            raise _CallFailure(
                code=code,
                retryable=False,
                output_failure=True,
                finish_reason=last.finish_reason,
                trace=last.model_copy(update={"error_code": "news_program_verdict_invalid"}),
            ) from exc
        answering_model = next(
            (call.model for call in reversed(calls) if call.route == route and call.predictor == "reader_card"),
            None,
        )
        return semantics, card, verdict, editorial, route, answering_model, novelty_defaulted

    async def _call_predictor(
        self,
        *,
        state: PredictorState,
        spec: PredictorSpec,
        adapter: PredictorAdapter,
        route: Literal["primary", "fallback"],
        attempt: int,
        context_sha: str,
        inputs: dict[str, Any],
        upstream_sha: str | None,
        output_field: str,
        output_model: type[T],
        calls: list[ProgramCallTrace],
    ) -> T:
        model_binding = getattr(state.model_bindings, route)
        try:
            runtime_identity = adapter.runtime_identity(model_binding)
        except PredictorAdapterError as exc:
            input_sha = canonical_sha(inputs)
            request_sha = canonical_sha(
                {
                    "program_version": PROGRAM_VERSION,
                    "program_sha256": self.artifact.program_sha256,
                    "context_sha256": context_sha,
                    "predictor": state.name,
                    "route": route,
                    "attempt": attempt,
                    "model_binding": model_binding,
                    "runtime_identity": "unavailable",
                    "upstream_sha256": upstream_sha,
                    "inputs": inputs,
                }
            )
            call = ProgramCallTrace(
                predictor=state.name,
                route=route,
                attempt=attempt,
                request_sha256=request_sha,
                input_sha256=input_sha,
                model_binding=model_binding,
                upstream_sha256=upstream_sha,
                error_code=exc.code,
            )
            calls.append(call)
            raise _CallFailure(
                code=exc.code,
                retryable=exc.retryable,
                output_failure=exc.output_failure,
                finish_reason=exc.finish_reason,
                trace=call,
            ) from exc
        request = PredictorRequest(
            program_version=PROGRAM_VERSION,
            program_sha256=self.artifact.program_sha256,
            context_sha256=context_sha,
            predictor=state.name,
            route=route,
            attempt=attempt,
            model_binding=model_binding,
            runtime_provider=runtime_identity.provider,
            runtime_model=runtime_identity.model,
            runtime_model_sha256=runtime_identity.model_sha256,
            runtime_binding_sha256=runtime_identity.binding_sha256,
            upstream_sha256=upstream_sha,
            inputs=inputs,
        )
        input_sha = canonical_sha(inputs)
        adapter_started = time.perf_counter()
        try:
            response = await adapter.invoke(request, spec)
        except asyncio.CancelledError:
            elapsed = max(0, round((time.perf_counter() - adapter_started) * 1000))
            call = ProgramCallTrace(
                predictor=state.name,
                route=route,
                attempt=attempt,
                request_sha256=request.request_sha256,
                input_sha256=input_sha,
                model_binding=request.model_binding,
                physical_provider_call=True,
                runtime_provider=request.runtime_provider,
                runtime_model=request.runtime_model,
                runtime_model_sha256=request.runtime_model_sha256,
                runtime_binding_sha256=request.runtime_binding_sha256,
                upstream_sha256=upstream_sha,
                latency_ms=elapsed,
                error_code="news_program_route_deadline",
            )
            calls.append(call)
            raise
        except PredictorAdapterError as exc:
            elapsed = max(0, round((time.perf_counter() - adapter_started) * 1000))
            observation = exc.provider_observation
            if observation is not None and (
                observation.runtime_binding_sha256 != request.runtime_binding_sha256
                or observation.provider != request.runtime_provider
            ):
                observation = None
            call = ProgramCallTrace(
                predictor=state.name,
                route=route,
                attempt=attempt,
                request_sha256=request.request_sha256,
                input_sha256=input_sha,
                model_binding=request.model_binding,
                physical_provider_call=True,
                runtime_provider=request.runtime_provider,
                runtime_model=request.runtime_model,
                runtime_model_sha256=request.runtime_model_sha256,
                runtime_binding_sha256=request.runtime_binding_sha256,
                upstream_sha256=upstream_sha,
                provider=observation.provider if observation is not None else None,
                model=observation.model if observation is not None else None,
                model_sha256=observation.model_sha256 if observation is not None else None,
                latency_ms=observation.latency_ms if observation is not None else elapsed,
                input_tokens=observation.input_tokens if observation is not None else 0,
                output_tokens=observation.output_tokens if observation is not None else 0,
                cached_tokens=observation.cached_tokens if observation is not None else 0,
                total_tokens=observation.total_tokens if observation is not None else 0,
                provider_cost_microusd=(observation.provider_cost_microusd if observation is not None else None),
                finish_reason=(observation.finish_reason if observation is not None else exc.finish_reason),
                error_code=exc.code,
                error_detail=exc.provider_detail,
            )
            calls.append(call)
            # No `raw_output` here on purpose. A provider answer that reached this branch never parsed
            # into an object, so there is no partial verdict for the novelty default to repair; the branch
            # that *can* produce one is the schema-validation failure below, which holds the parsed dict.
            raise _CallFailure(
                code=exc.code,
                retryable=exc.retryable,
                output_failure=exc.output_failure,
                finish_reason=exc.finish_reason,
                trace=call,
            ) from exc
        except Exception as exc:
            elapsed = max(0, round((time.perf_counter() - adapter_started) * 1000))
            code = f"news_program_transport_{type(exc).__name__.casefold()}"
            call = ProgramCallTrace(
                predictor=state.name,
                route=route,
                attempt=attempt,
                request_sha256=request.request_sha256,
                input_sha256=input_sha,
                model_binding=request.model_binding,
                physical_provider_call=True,
                runtime_provider=request.runtime_provider,
                runtime_model=request.runtime_model,
                runtime_model_sha256=request.runtime_model_sha256,
                runtime_binding_sha256=request.runtime_binding_sha256,
                upstream_sha256=upstream_sha,
                latency_ms=elapsed,
                error_code=code,
            )
            calls.append(call)
            raise _CallFailure(
                code=code,
                retryable=_is_retryable_exception(exc),
                output_failure=False,
                finish_reason=None,
                trace=call,
            ) from exc
        if response.runtime_binding_sha256 != request.runtime_binding_sha256:
            code = "news_program_runtime_binding_mismatch"
            call = ProgramCallTrace(
                predictor=state.name,
                route=route,
                attempt=attempt,
                request_sha256=request.request_sha256,
                input_sha256=input_sha,
                model_binding=request.model_binding,
                physical_provider_call=True,
                runtime_provider=request.runtime_provider,
                runtime_model=request.runtime_model,
                runtime_model_sha256=request.runtime_model_sha256,
                runtime_binding_sha256=request.runtime_binding_sha256,
                upstream_sha256=upstream_sha,
                provider=response.provider,
                model=response.model,
                model_sha256=response.model_sha256,
                latency_ms=response.latency_ms,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cached_tokens=response.cached_tokens,
                total_tokens=response.total_tokens,
                provider_cost_microusd=response.provider_cost_microusd,
                finish_reason=response.finish_reason,
                error_code=code,
            )
            calls.append(call)
            raise _CallFailure(
                code=code,
                retryable=False,
                output_failure=False,
                finish_reason=response.finish_reason,
                trace=call,
            )
        raw_output: Mapping[str, Any] | None = None
        output_sha = canonical_sha(response.output)
        finish_reason = response.finish_reason.casefold() if response.finish_reason else None
        call = ProgramCallTrace(
            predictor=state.name,
            route=route,
            attempt=attempt,
            request_sha256=request.request_sha256,
            input_sha256=input_sha,
            model_binding=request.model_binding,
            physical_provider_call=True,
            runtime_provider=request.runtime_provider,
            runtime_model=request.runtime_model,
            runtime_model_sha256=request.runtime_model_sha256,
            runtime_binding_sha256=request.runtime_binding_sha256,
            upstream_sha256=upstream_sha,
            output_sha256=output_sha,
            provider=response.provider,
            model=response.model,
            model_sha256=response.model_sha256,
            latency_ms=response.latency_ms,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cached_tokens=response.cached_tokens,
            total_tokens=response.total_tokens,
            provider_cost_microusd=response.provider_cost_microusd,
            finish_reason=finish_reason,
        )
        if finish_reason in _TRUNCATED_FINISH_REASONS:
            failed = call.model_copy(update={"error_code": "news_program_output_truncated"})
            calls.append(failed)
            raise _CallFailure(
                code="news_program_output_truncated",
                retryable=False,
                output_failure=True,
                finish_reason=finish_reason,
                trace=failed,
            )
        try:
            unwrapped = _unwrap_output(response.output, output_field)
            if isinstance(unwrapped, BaseModel):
                raw_output = unwrapped.model_dump(mode="json")
            elif isinstance(unwrapped, Mapping):
                raw_output = dict(unwrapped)
            else:
                raise TypeError("output_not_object")
            validated = output_model.model_validate(raw_output)
        except (TypeError, ValidationError, ValueError) as exc:
            code = f"news_program_{state.name}_invalid"
            failed = call.model_copy(update={"error_code": code})
            calls.append(failed)
            raise _CallFailure(
                code=code,
                retryable=False,
                output_failure=True,
                finish_reason=finish_reason,
                trace=failed,
                raw_output=raw_output,
            ) from exc
        validated_state = validated.model_dump(mode="json")
        normalizations = (
            _relevance_normalization_traces(raw_output, validated)
            if isinstance(validated, EventSemantics) and raw_output is not None
            else ()
        )
        calls.append(
            call.model_copy(
                update={
                    "output_sha256": canonical_sha(validated_state),
                    "validated_output": validated_state,
                    "normalizations": normalizations,
                }
            )
        )
        return validated

    def _trace(
        self,
        calls: Sequence[ProgramCallTrace],
        *,
        context_sha: str,
        event_semantics_sha256: str | None = None,
        reader_card_sha256: str | None = None,
        verdict_sha256: str | None = None,
        editorial_sha256: str | None = None,
        answering_route: Literal["primary", "fallback"] | None = None,
        fallback_from: str | None = None,
        novelty_defaulted: bool = False,
    ) -> ProgramTrace:
        return ProgramTrace(
            program_version=PROGRAM_VERSION,
            program_sha256=self.artifact.program_sha256,
            context_sha256=context_sha,
            envelope_sha256=EXECUTION_ENVELOPE_SHA256,
            event_semantics_sha256=event_semantics_sha256,
            reader_card_sha256=reader_card_sha256,
            verdict_sha256=verdict_sha256,
            editorial_sha256=editorial_sha256,
            answering_route=answering_route,
            fallback_from=fallback_from,
            novelty_defaulted=novelty_defaulted,
            calls=tuple(calls),
        )

    def _public_error(
        self,
        failure: _CallFailure,
        calls: Sequence[ProgramCallTrace],
        *,
        context_sha: str,
        primary_failure: _CallFailure | None = None,
    ) -> SemanticJudgeError:
        partial_trace = self._trace(
            calls,
            context_sha=context_sha,
            fallback_from=primary_failure.code if primary_failure is not None else None,
        )
        return SemanticJudgeError(
            failure.code,
            retryable=failure.retryable,
            output_failure=failure.output_failure or bool(primary_failure and primary_failure.output_failure),
            attempts=len(calls),
            partial_trace=partial_trace,
            finish_reason=failure.finish_reason,
            failing_predictor=failure.trace.predictor,
            primary_code=primary_failure.code if primary_failure is not None else None,
        )

    @staticmethod
    def _usage(calls: Sequence[ProgramCallTrace], *, started: float) -> ProgramUsage:
        return ProgramUsage(
            wall_latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
            **aggregate_program_usage(calls),
        )


__all__ = [
    "EXECUTION_ENVELOPE_SHA256",
    "PROGRAM_SCHEMA_VERSION",
    "PROGRAM_VERSION",
    "ChatCompletionsPredictorAdapter",
    "EditorialEnvelope",
    "EventSemantics",
    "FrozenEventEvidence",
    "NewsSemanticProgram",
    "PredictorAdapter",
    "PredictorAdapterError",
    "PredictorModelBindings",
    "PredictorRecording",
    "PredictorRequest",
    "PredictorResponse",
    "PredictorSpec",
    "PredictorState",
    "ProgramCallTrace",
    "ProgramNormalizationTrace",
    "ProgramStrategyArtifactCodec",
    "ProgramStrategyArtifactV1",
    "ProgramStrategyPatchV1",
    "ProgramTrace",
    "ProgramUsage",
    "ProviderCallMetrics",
    "ProviderCallObservation",
    "ReaderCard",
    "ReaderCardSemanticView",
    "RecordReplayPredictorAdapter",
    "RuntimeModelIdentity",
    "ScoredJudgment",
    "ScriptedPredictorAdapter",
    "SemanticGateContext",
    "SemanticJudge",
    "SemanticJudgeError",
    "SemanticJudgment",
    "TradeRelevanceV1",
    "TriageContext",
    "apply_program_patch",
    "build_code_owned_program_artifact",
    "build_predictor_state",
    "load_program_artifact",
    "load_stable_program_artifact",
    "predictor_spec",
    "render_model_evidence_json",
    "validate_program_instruction",
]
