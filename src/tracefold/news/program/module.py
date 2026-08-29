"""The native DSPy News Program: two Predictors with deterministic business rules between them."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import dspy  # type: ignore[import-untyped]
from pydantic import BaseModel

from ..artifact_identity import canonical_json
from ..models import TriageVerdict
from ..taxonomy import NewsTaxonomyV1, source_authority_from_evidence
from .artifact import ProgramStrategyArtifactV1, render_model_evidence_json, validate_program_instruction
from .assembly import decision_for, is_actionable, normalize_restates, restatement_index_error
from .contracts import EditorialEnvelope, ProgramNormalizationTrace, ReaderCardSemanticView, TriageContext
from .lm import mark_active_domain_failure, program_json_adapter
from .signatures import EventSemantics, EventSemanticsSignature, ReaderCard, ReaderCardSignature

type CandidateGuard = Callable[[str, str], str | None]


class NativeProgramResult(dspy.Prediction):  # type: ignore[misc]
    """DSPy-compatible output consumed by both GEPA metrics and production routing."""

    instruction_rejected: str | None
    semantics: EventSemantics | None
    card: ReaderCard | None
    verdict: TriageVerdict | None
    editorial: EditorialEnvelope | None
    normalizations: tuple[ProgramNormalizationTrace, ...]


@dataclass(frozen=True, slots=True)
class _PreparedRun:
    context: TriageContext
    semantics_evidence_json: str
    card_evidence_json: str


def _prepare(context: TriageContext | Mapping[str, Any]) -> _PreparedRun:
    typed = context if isinstance(context, TriageContext) else TriageContext.model_validate(context)
    return _PreparedRun(
        context=typed,
        semantics_evidence_json=render_model_evidence_json(
            typed.event_semantics_payload(), predictor="event_semantics"
        ),
        card_evidence_json=render_model_evidence_json(typed.reader_card_payload(), predictor="reader_card"),
    )


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


def _relevance_normalizations(
    raw_semantics: Any,
    semantics: EventSemantics,
) -> tuple[ProgramNormalizationTrace, ...]:
    traces: list[ProgramNormalizationTrace] = []
    for field in ("channels", "affected_markets"):
        if isinstance(raw_semantics, EventSemantics):
            before = raw_semantics.raw_relevance_codes(field)
        elif isinstance(raw_semantics, BaseModel):
            before = None
        elif isinstance(raw_semantics, Mapping) and isinstance(raw_semantics.get("relevance"), Mapping):
            raw = raw_semantics["relevance"].get(field)
            before = (
                tuple(raw) if isinstance(raw, (list, tuple)) and all(isinstance(item, str) for item in raw) else None
            )
        else:
            before = None
        if before is None:
            continue
        after = tuple(getattr(semantics.relevance, field))
        if before != after:
            traces.append(
                ProgramNormalizationTrace(
                    field=field,
                    reason="canonical_set_order",
                    input_value=before,
                    output_value=after,
                )
            )
    return tuple(traces)


def _normalize_and_validate_semantics(
    raw_semantics: Any,
    *,
    told_count: int,
) -> tuple[EventSemantics, tuple[ProgramNormalizationTrace, ...]]:
    try:
        semantics = EventSemantics.model_validate(raw_semantics)
        normalizations = list(_relevance_normalizations(raw_semantics, semantics))
        normalized_restates = normalize_restates(novelty=semantics.novelty, restates=semantics.restates)
        if normalized_restates != semantics.restates:
            normalizations.append(
                ProgramNormalizationTrace(
                    field="restates",
                    reason="non_restatement_index_ignored",
                    input_value=semantics.restates,
                    output_value=normalized_restates,
                )
            )
            semantics = semantics.model_copy(update={"restates": normalized_restates})
        error = restatement_index_error(
            novelty=semantics.novelty,
            restates=semantics.restates,
            told_count=told_count,
        )
        if error is not None:
            raise ValueError(error)
        return semantics, tuple(normalizations)
    except ValueError as exc:
        code = str(exc) if str(exc).startswith("news_program_") else "news_program_domain_validation_error"
        mark_active_domain_failure(code)
        raise


def _assemble(
    semantics: EventSemantics,
    raw_card: Any,
    *,
    context: TriageContext,
    told_count: int,
    normalizations: tuple[ProgramNormalizationTrace, ...],
) -> NativeProgramResult:
    try:
        card = ReaderCard.model_validate(raw_card)
        error = restatement_index_error(
            novelty=semantics.novelty,
            restates=semantics.restates,
            told_count=told_count,
        )
        if error is not None:
            raise ValueError(error)
        relevance = semantics.relevance
        verdict = TriageVerdict.model_validate(
            {
                "novelty": semantics.novelty,
                "restates": semantics.restates,
                "event_type": semantics.event_type,
                "assets": [asset.model_dump(mode="json") for asset in semantics.assets],
                "direction": semantics.direction,
                "scope": semantics.scope,
                "magnitude": semantics.magnitude,
                "actionable": is_actionable(
                    tradability=relevance.tradability,
                    has_channels=bool(relevance.channels),
                    has_affected_markets=bool(relevance.affected_markets),
                ),
                "confidence": semantics.confidence,
                "decision": decision_for(relevance.reader_value),
                "audience": semantics.audience,
                "headline_zh": card.headline_zh.strip(),
                "title_zh": "",
                "why_zh": card.why_zh.strip(),
            }
        )
        taxonomy = NewsTaxonomyV1.issue(
            semantics.taxonomy,
            source_authority=source_authority_from_evidence(context.evidence),
        )
        return NativeProgramResult(
            instruction_rejected=None,
            semantics=semantics,
            card=card,
            verdict=verdict,
            editorial=EditorialEnvelope.issue(editorial_origin="model", relevance=relevance, taxonomy=taxonomy),
            normalizations=normalizations,
        )
    except ValueError as exc:
        code = str(exc) if str(exc).startswith("news_program_") else "news_program_domain_validation_error"
        mark_active_domain_failure(code)
        raise


def _rejected(code: str) -> NativeProgramResult:
    return NativeProgramResult(
        instruction_rejected=code,
        semantics=None,
        card=None,
        verdict=None,
        editorial=None,
        normalizations=(),
    )


class NativeNewsProgram(dspy.Module):  # type: ignore[misc]
    """Exactly two named Predictors, usable by async production and synchronous GEPA evaluation."""

    def __init__(
        self,
        artifact: ProgramStrategyArtifactV1,
        *,
        candidate_guard: CandidateGuard | None = None,
    ) -> None:
        super().__init__()
        if artifact.program_sha256 != artifact.computed_sha256():
            raise ValueError("news_program_artifact_hash_mismatch")
        self.artifact = artifact
        self.candidate_guard = candidate_guard
        self.event_semantics = dspy.Predict(
            EventSemanticsSignature.with_instructions(artifact.event_semantics_instruction),
            max_tokens=artifact.event_semantics.max_tokens,
        )
        self.reader_card = dspy.Predict(
            ReaderCardSignature.with_instructions(artifact.reader_card_instruction),
            max_tokens=artifact.reader_card.max_tokens,
        )

    def _candidate_rejection(self) -> str | None:
        event_instruction = self.event_semantics.signature.instructions
        card_instruction = self.reader_card.signature.instructions
        try:
            validate_program_instruction(event_instruction)
            validate_program_instruction(card_instruction)
        except ValueError as exc:
            return str(exc)
        if self.candidate_guard is None:
            return None
        code = self.candidate_guard(event_instruction, card_instruction)
        if code is not None and (not isinstance(code, str) or not code.strip()):
            raise ValueError("news_program_candidate_guard_result_invalid")
        return code

    @staticmethod
    def _semantics(
        prediction: dspy.Prediction,
        prepared: _PreparedRun,
    ) -> tuple[EventSemantics, tuple[ProgramNormalizationTrace, ...], str]:
        semantics, normalizations = _normalize_and_validate_semantics(
            prediction.semantics,
            told_count=len(prepared.context.told.entries),
        )
        semantics_json = canonical_json(_reader_card_semantic_view(semantics).model_dump(mode="json"))
        return semantics, normalizations, semantics_json

    @staticmethod
    def _result(
        prediction: dspy.Prediction,
        *,
        prepared: _PreparedRun,
        semantics: EventSemantics,
        normalizations: tuple[ProgramNormalizationTrace, ...],
    ) -> NativeProgramResult:
        return _assemble(
            semantics,
            prediction.card,
            context=prepared.context,
            told_count=len(prepared.context.told.entries),
            normalizations=normalizations,
        )

    def forward(
        self,
        context: TriageContext | Mapping[str, Any],
        *,
        event_lm: dspy.BaseLM | None = None,
        card_lm: dspy.BaseLM | None = None,
    ) -> NativeProgramResult:
        rejection = self._candidate_rejection()
        if rejection is not None:
            return _rejected(rejection)
        prepared = _prepare(context)
        with dspy.context(adapter=program_json_adapter()):
            semantics_prediction = self.event_semantics(
                evidence_json=prepared.semantics_evidence_json,
                lm=event_lm,
            )
            semantics, normalizations, semantics_json = self._semantics(semantics_prediction, prepared)
            card_prediction = self.reader_card(
                evidence_json=prepared.card_evidence_json,
                semantics_json=semantics_json,
                lm=card_lm,
            )
        return self._result(
            card_prediction,
            prepared=prepared,
            semantics=semantics,
            normalizations=normalizations,
        )

    async def aforward(
        self,
        context: TriageContext | Mapping[str, Any],
        *,
        event_lm: dspy.BaseLM | None = None,
        card_lm: dspy.BaseLM | None = None,
    ) -> NativeProgramResult:
        rejection = self._candidate_rejection()
        if rejection is not None:
            return _rejected(rejection)
        prepared = _prepare(context)
        with dspy.context(adapter=program_json_adapter()):
            semantics_prediction = await self.event_semantics.acall(
                evidence_json=prepared.semantics_evidence_json,
                lm=event_lm,
            )
            semantics, normalizations, semantics_json = self._semantics(semantics_prediction, prepared)
            card_prediction = await self.reader_card.acall(
                evidence_json=prepared.card_evidence_json,
                semantics_json=semantics_json,
                lm=card_lm,
            )
        return self._result(
            card_prediction,
            prepared=prepared,
            semantics=semantics,
            normalizations=normalizations,
        )


__all__ = ["CandidateGuard", "NativeNewsProgram", "NativeProgramResult"]
