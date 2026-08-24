"""Program-native semantic judgment for News V3.

The public Interface is deliberately small: callers submit one immutable
``TriageContext`` to ``SemanticJudge.judge`` and receive one complete
``SemanticJudgment``.  The hidden graph is exactly
``EventSemantics -> SemanticNormalizer -> ReaderCard -> VerdictAssembler``.
Model transport is an Adapter Seam; domain validation, retry/fallback budgets,
identity and audit remain owned by this Module.

Only canonical JSON state is loadable.  This module never loads pickle,
cloudpickle, DSPy Flex state, arbitrary classes, endpoints, or credentials.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from typing import Any, Literal, TypeVar, cast

import dspy  # type: ignore[import-untyped]
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
    code_owned_rule_packs,
    load_program_artifact,
    load_stable_program_artifact,
    render_model_evidence_json,
    render_predictor_instruction,
    validate_learned_instruction,
)
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
from .dspy_adapter import (
    _TRUNCATED_FINISH_REASONS,
    DspyPredictorAdapter,
    DspyStrictJSONAdapter,
    ExactMetadataDspyLM,
    ExactProviderCallCapture,
    ExactProviderMetadata,
    PredictorAdapter,
    PredictorAdapterError,
    PredictorRecording,
    PredictorRequest,
    PredictorResponse,
    ProviderCallObservation,
    RecordReplayPredictorAdapter,
    RuntimeModelIdentity,
    ScriptedPredictorAdapter,
    _is_retryable_exception,
)
from .runtime import (
    PROGRAM_FACTORY_ID,
    PROGRAM_LEARNING_EPOCH,
    PROGRAM_PREDICTOR_MAX_TOKENS,
    PROGRAM_PRIMARY_BREAKER_FAILURES,
    PROGRAM_PRIMARY_BREAKER_OPEN_SECONDS,
    PROGRAM_ROUTE_DEADLINE_SECONDS,
    PROGRAM_SCHEMA_VERSION,
    PROGRAM_VERSION,
    PredictorName,
)
from .signatures import (
    EventSemantics,
    ReaderCard,
)


class _EventSemanticsSignature(dspy.Signature):  # type: ignore[misc]
    evidence_json: str = dspy.InputField(desc="Delimited canonical bounded News evidence JSON")
    semantics: EventSemantics = dspy.OutputField(desc="Strict semantic judgment; no reader prose")


class _ReaderCardSignature(dspy.Signature):  # type: ignore[misc]
    evidence_json: str = dspy.InputField(desc="Delimited canonical bounded News evidence JSON")
    semantics_json: str = dspy.InputField(desc="Validated EventSemantics canonical JSON")
    card: ReaderCard = dspy.OutputField(desc="Concise Chinese reader card")


def _predictor(state: PredictorState, base_signature: type[dspy.Signature]) -> dspy.Predict:
    signature = base_signature.with_instructions(state.instruction)
    predictor = dspy.Predict(signature, temperature=0, max_tokens=state.max_tokens)
    predictor.demos = []
    return predictor


def _signature_shape(signature: type[dspy.Signature]) -> str:
    return canonical_sha(
        {
            name: {
                "annotation": repr(field.annotation),
                "description": field.description,
                "json_schema_extra": field.json_schema_extra,
                "required": field.is_required(),
            }
            for name, field in signature.fields.items()
        }
    )


class _OptimizerOwnedPredictor(dspy.Predict):  # type: ignore[misc]
    """Expose only the bounded advisory instruction as GEPA-mutable Predictor state."""

    def __init__(self, artifact: ProgramStrategyArtifactV1, predictor: PredictorName) -> None:
        self._artifact = artifact
        self._predictor_name = predictor
        self._base_signature = _EventSemanticsSignature if predictor == "event_semantics" else _ReaderCardSignature
        super().__init__(
            # DSPy replaces a literal empty string with its generated default
            # instruction.  One blank character canonicalizes back to an empty
            # instruction, preserving the artifact's genuinely empty baseline.
            self._base_signature.with_instructions(artifact.instruction_for(predictor) or " "),
            temperature=0,
            max_tokens=PROGRAM_PREDICTOR_MAX_TOKENS[predictor],
        )
        self.demos: list[dspy.Example] = []

    def _validate_mutable_surface(self) -> None:
        if _signature_shape(self.signature) != _signature_shape(self._base_signature):
            raise ValueError("news_program_optimizer_signature_mutation_forbidden")
        expected_max_tokens = PROGRAM_PREDICTOR_MAX_TOKENS[self._predictor_name]
        if self.config != {"temperature": 0, "max_tokens": expected_max_tokens} or self.lm is not None:
            raise ValueError("news_program_optimizer_config_mutation_forbidden")
        # A demo is not part of the write-set and there is no bank to draw one from. Refuse here rather than
        # carrying an unreachable DemoBank contract to say the same thing.
        if list(self.demos):
            raise ValueError("news_program_optimizer_demos_forbidden")

    def _runtime_predictor(self) -> dspy.Predict:
        self._validate_mutable_surface()
        # The advisory bounds are applied here, on the optimizer's live proposal, so a rejection surfaces from
        # `forward` where `_FeedbackCompileProgram` can score it and tell the reflection model which bound it
        # hit — rather than only at patch extraction, after the whole run.
        learned = validate_learned_instruction(str(self.signature.instructions or ""))
        return _predictor(build_predictor_state(self._predictor_name, learned), self._base_signature)

    def forward(self, **kwargs: Any) -> dspy.Prediction:
        return cast(dspy.Prediction, self._runtime_predictor()(**kwargs))

    async def aforward(self, **kwargs: Any) -> dspy.Prediction:
        return cast(dspy.Prediction, await self._runtime_predictor().acall(**kwargs))


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
    if semantics.novelty == "restatement":
        if semantics.restates < 0 or semantics.restates >= told_count:
            raise ValueError("news_program_restatement_index_invalid")
    elif semantics.restates != -1:
        raise ValueError("news_program_non_restatement_index_invalid")
    relevance = semantics.relevance
    actionable = (
        relevance.tradability in {"direct", "second_order"}
        and bool(relevance.channels)
        and bool(relevance.affected_markets)
    )
    decision = {
        "escalate": "escalate",
        "realtime": "push",
        "background": "drop",
        "none": "drop",
    }[relevance.reader_value]
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
    if semantics.novelty == "restatement":
        if semantics.restates < 0 or semantics.restates >= told_count:
            raise ValueError("news_program_restatement_index_invalid")
    elif semantics.restates != -1:
        raise ValueError("news_program_non_restatement_index_invalid")


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


class DspyCompileProgram(dspy.Module):  # type: ignore[misc]
    """Cold-only optimizer Module; never used by the production hot path."""

    def __init__(self, artifact: ProgramStrategyArtifactV1) -> None:
        super().__init__()
        if artifact.program_sha256 != artifact.computed_sha256():
            raise ValueError("news_program_artifact_hash_mismatch")
        self.artifact = artifact
        self.event_semantics = _OptimizerOwnedPredictor(artifact, "event_semantics")
        self.reader_card = _OptimizerOwnedPredictor(artifact, "reader_card")

    def forward(self, evidence_json: str, card_evidence_json: str, told_count: int) -> dspy.Prediction:
        semantics_prediction = self.event_semantics(evidence_json=evidence_json)
        semantics = EventSemantics.model_validate(_unwrap_output(semantics_prediction.toDict(), "semantics"))
        semantics, _ = _normalize_semantics(semantics)
        _validate_semantic_context(semantics, told_count=max(0, int(told_count)))
        semantic_view = _reader_card_semantic_view(semantics)
        card_prediction = self.reader_card(
            evidence_json=card_evidence_json,
            semantics_json=canonical_json(semantic_view.model_dump(mode="json")),
        )
        card = ReaderCard.model_validate(_unwrap_output(card_prediction.toDict(), "card"))
        verdict, editorial = _assemble(semantics, card, told_count=max(0, int(told_count)))
        return dspy.Prediction(semantics=semantics, card=card, verdict=verdict, editorial=editorial)


def extract_optimizer_patch(
    compiled: DspyCompileProgram,
    parent: ProgramStrategyArtifactV1,
) -> ProgramStrategyPatchV1:
    """Freeze the two advisory instructions, which is the whole optimizer write-set."""

    if not isinstance(compiled, DspyCompileProgram):
        raise TypeError("news_program_compiled_module_type_invalid")
    if compiled.artifact.program_sha256 != parent.program_sha256:
        raise ValueError("news_program_optimizer_parent_identity_mismatch")

    instructions: dict[PredictorName, str] = {}
    for predictor_name in ("event_semantics", "reader_card"):
        predictor = getattr(compiled, predictor_name)
        if not isinstance(predictor, _OptimizerOwnedPredictor):
            raise ValueError("news_program_optimizer_predictor_type_invalid")
        predictor._validate_mutable_surface()
        instructions[predictor_name] = str(predictor.signature.instructions or "")

    return ProgramStrategyPatchV1.issue(
        parent=parent,
        event_semantics_instruction=instructions["event_semantics"],
        reader_card_instruction=instructions["reader_card"],
    )


class DspyNewsSemanticProgram(dspy.Module):  # type: ignore[misc]
    """Deep Module owning graph execution, validation, budgets and audit."""

    def __init__(
        self,
        artifact: ProgramStrategyArtifactV1,
        *,
        primary_adapter: AdapterPool,
        fallback_adapter: AdapterPool | None = None,
    ) -> None:
        super().__init__()
        if artifact.program_sha256 != artifact.computed_sha256():
            raise ValueError("news_program_artifact_hash_mismatch")
        self.artifact = artifact
        self.primary_adapter = self._prepare_adapter_pool(primary_adapter, route="primary")
        self.fallback_adapter = (
            self._prepare_adapter_pool(fallback_adapter, route="fallback") if fallback_adapter is not None else None
        )
        self.event_semantics = _predictor(artifact.event_semantics, _EventSemanticsSignature)
        self.reader_card = _predictor(artifact.reader_card, _ReaderCardSignature)
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

    async def aforward(self, context: TriageContext) -> SemanticJudgment:
        return await self.judge(context)

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
                    predictor=self.event_semantics,
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
                    predictor=self.reader_card,
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
        predictor: dspy.Predict,
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
            response = await adapter.invoke(request, predictor)
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
            )
            calls.append(call)
            partial_raw_output: Mapping[str, Any] | None = None
            if exc.partial_output is not None:
                try:
                    partial = _unwrap_output(exc.partial_output, output_field)
                except ValueError:
                    partial = None
                if isinstance(partial, Mapping):
                    partial_raw_output = dict(partial)
            raise _CallFailure(
                code=exc.code,
                retryable=exc.retryable,
                output_failure=exc.output_failure,
                finish_reason=exc.finish_reason,
                trace=call,
                raw_output=partial_raw_output,
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
            factory_id=self.artifact.factory_id,
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
    "PROGRAM_FACTORY_ID",
    "PROGRAM_LEARNING_EPOCH",
    "PROGRAM_SCHEMA_VERSION",
    "PROGRAM_VERSION",
    "DspyCompileProgram",
    "DspyNewsSemanticProgram",
    "DspyPredictorAdapter",
    "DspyStrictJSONAdapter",
    "EditorialEnvelope",
    "EventSemantics",
    "ExactMetadataDspyLM",
    "ExactProviderCallCapture",
    "ExactProviderMetadata",
    "FrozenEventEvidence",
    "PredictorAdapter",
    "PredictorAdapterError",
    "PredictorModelBindings",
    "PredictorRecording",
    "PredictorRequest",
    "PredictorResponse",
    "PredictorState",
    "ProgramCallTrace",
    "ProgramNormalizationTrace",
    "ProgramStrategyArtifactCodec",
    "ProgramStrategyArtifactV1",
    "ProgramStrategyPatchV1",
    "ProgramTrace",
    "ProgramUsage",
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
    "code_owned_rule_packs",
    "extract_optimizer_patch",
    "load_program_artifact",
    "load_stable_program_artifact",
    "render_model_evidence_json",
    "render_predictor_instruction",
    "validate_learned_instruction",
]
