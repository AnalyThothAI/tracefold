"""The native DSPy News Program: three Predictors with deterministic business rules between them."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

import dspy  # type: ignore[import-untyped]
from pydantic import BaseModel

from ..artifact_identity import canonical_json
from ..models import TriageVerdict
from ..taxonomy import ModelTaxonomyV1, NewsTaxonomyV1, source_authority_from_evidence
from .artifact import NewsProgramStateV1, render_model_evidence_json, validate_program_instruction
from .assembly import contradicted_primary_symbols, normalize_restates, restatement_index_error
from .contracts import EditorialEnvelope, ProgramNormalizationTrace, ReaderCardSemanticView, TriageContext
from .lm import (
    LMDelegateProgramError,
    LMOutputTruncatedError,
    active_predictor_disposition,
    mark_active_domain_failure,
    program_json_adapter,
)
from .runtime import PREDICTOR_NAMES, PROGRAM_CONTEXT_UPPER_TOKENS, PredictorName
from .seed import seed_instruction
from .signatures import (
    EventSemantics,
    EventSemanticsSignature,
    EventTaxonomySignature,
    ReaderCard,
    ReaderCardSignature,
)

# (event_semantics_instruction, taxonomy_instruction, reader_card_instruction) -> rejection code or None.
type CandidateGuard = Callable[[str, str, str], str | None]


class ProgramOutputError(ValueError):
    """A declared model-output/domain rejection that routing may degrade."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class NativeProgramResult(dspy.Prediction):  # type: ignore[misc]
    """DSPy-compatible output consumed by both GEPA metrics and production routing."""

    instruction_rejected: str | None
    semantics: EventSemantics | None
    taxonomy: ModelTaxonomyV1 | None
    card: ReaderCard | None
    verdict: TriageVerdict | None
    editorial: EditorialEnvelope | None
    normalizations: tuple[ProgramNormalizationTrace, ...]


@dataclass(frozen=True, slots=True)
class _PreparedRun:
    context: TriageContext
    semantics_evidence_json: str
    taxonomy_evidence_json: str
    card_evidence_json: str


def _prepare(context: TriageContext | Mapping[str, Any]) -> _PreparedRun:
    typed = context if isinstance(context, TriageContext) else TriageContext.model_validate(context)
    if typed.prepared_evidence is None:
        raise ValueError("news_program_archived_input_requires_explicit_adaptation")
    return _PreparedRun(
        context=typed,
        semantics_evidence_json=render_model_evidence_json(
            typed.event_semantics_payload(), predictor="event_semantics"
        ),
        taxonomy_evidence_json=render_model_evidence_json(typed.taxonomy_payload(), predictor="taxonomy"),
        card_evidence_json=render_model_evidence_json(typed.reader_card_payload(), predictor="reader_card"),
    )


def _reader_card_semantic_view(semantics: EventSemantics) -> ReaderCardSemanticView:
    return ReaderCardSemanticView(
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


def _demote_contradicted_primaries(
    semantics: EventSemantics,
    candidates: Mapping[str, Sequence[str]],
) -> EventSemantics:
    """Turn a primary the shown catalogue row contradicts into a `mentioned` name (#675 PR-3).

    The demotion, not a rejection: the Event is still *about* something, and the card keeps the name it
    was given. What it loses is the claim that this is the instrument the reader would act on, which is
    the claim `single_name_without_instrument` and the storyline key both read. The rule and its measured
    scope live in `assembly.contradicted_primary_symbols`; this applies it and keeps asset order.
    """

    contradicted = frozenset(
        contradicted_primary_symbols([asset.model_dump(mode="json") for asset in semantics.assets], candidates)
    )
    if not contradicted:
        return semantics
    assets = tuple(
        asset.model_copy(update={"role": "mentioned"}) if asset.symbol in contradicted else asset
        for asset in semantics.assets
    )
    return semantics.model_copy(update={"assets": assets})


def _normalize_and_validate_semantics(
    raw_semantics: Any,
    *,
    told_count: int,
    catalog_candidates: Mapping[str, Sequence[str]] | None = None,
) -> tuple[EventSemantics, tuple[ProgramNormalizationTrace, ...]]:
    try:
        semantics = EventSemantics.model_validate(raw_semantics)
        semantics = _demote_contradicted_primaries(semantics, catalog_candidates or {})
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
        mark_active_domain_failure(code, predictor="event_semantics")
        raise ProgramOutputError(code) from exc


def _taxonomy_call_failure_code(exc: Exception) -> str | None:
    """The `news_program_*` code for a taxonomy call this Program may degrade, or ``None`` to re-raise.

    Only the three failures that are about *this model call* degrade: a provider refusal, a truncated
    completion, and an adapter that could not parse the answer after its one format fallback. A delegate
    defect carried through DSPy's catch boundary is a Tracefold bug wearing an LM error's clothes, and
    anything else is unknown — both keep ending the route.
    """

    if isinstance(exc, LMDelegateProgramError):
        return None
    if isinstance(exc, LMOutputTruncatedError):
        return "news_program_output_truncated"
    if isinstance(exc, dspy.LMError):
        raw = str(getattr(exc, "code", "") or "lm_error")
        return raw if raw.startswith("news_program_") else f"news_program_lm_{raw}"
    if active_predictor_disposition("taxonomy") == "adapter_parse_error":
        return "news_program_adapter_parse_error"
    return None


def _validate_taxonomy(raw_taxonomy: Any) -> tuple[ModelTaxonomyV1 | None, str | None]:
    """The taxonomy Predictor's typed answer, or the code that says why there is none.

    Separate from `_assemble` since #651 §5.3: the four taxonomy axes and the reader's card are two
    different products of two different Predictors, and validating them together meant a rejected label
    threw away a card the reader could have had. The ledger receipt is attributed to `taxonomy` by name
    rather than by recency, because the ReaderCard call still runs after this one.
    """

    try:
        return ModelTaxonomyV1.model_validate(raw_taxonomy), None
    except ValueError as exc:
        code = str(exc) if str(exc).startswith("news_program_") else "news_program_taxonomy_domain_validation_error"
        mark_active_domain_failure(code, predictor="taxonomy")
        return None, code


def _assemble(
    semantics: EventSemantics,
    taxonomy: ModelTaxonomyV1 | None,
    raw_card: Any,
    *,
    context: TriageContext,
    told_count: int,
    normalizations: tuple[ProgramNormalizationTrace, ...],
    taxonomy_error_code: str | None,
) -> NativeProgramResult:
    try:
        card = ReaderCard.model_validate(raw_card)
        visible_refs = (
            {
                span.ref_id
                for span in (*context.prepared_evidence.current_evidence, *context.prepared_evidence.related_evidence)
            }
            if context.prepared_evidence
            else set()
        )
        if not set(card.source_refs).issubset(visible_refs):
            raise ValueError("news_program_source_ref_invalid")
        error = restatement_index_error(
            novelty=semantics.novelty,
            restates=semantics.restates,
            told_count=told_count,
        )
        if error is not None:
            raise ValueError(error)
        verdict = TriageVerdict.model_validate(
            {
                "novelty": semantics.novelty,
                "restates": semantics.restates,
                "assets": [asset.model_dump(mode="json") for asset in semantics.assets],
                "direction": semantics.direction,
                "scope": semantics.scope,
                "magnitude": semantics.magnitude,
                "confidence": semantics.confidence,
                "audience": semantics.audience,
                "headline_zh": card.headline_zh.strip(),
                "why_zh": card.why_zh.strip(),
            }
        )
        return NativeProgramResult(
            instruction_rejected=None,
            semantics=semantics,
            taxonomy=taxonomy,
            card=card,
            verdict=verdict,
            # The authority is read off the frozen evidence whatever the taxonomy Predictor did: it is
            # what `decide()` needs to keep the uncorroborated-escalate rule working on a judgment whose
            # classification is missing.
            editorial=EditorialEnvelope.issue(
                relevance=semantics.relevance,
                source_authority=source_authority_from_evidence(context.evidence),
                taxonomy=None if taxonomy is None else NewsTaxonomyV1.issue(taxonomy),
                taxonomy_error_code=taxonomy_error_code,
            ),
            normalizations=normalizations,
        )
    except ValueError as exc:
        code = str(exc) if str(exc).startswith("news_program_") else "news_program_domain_validation_error"
        mark_active_domain_failure(code, predictor="reader_card")
        raise ProgramOutputError(code) from exc


def _rejected(code: str) -> NativeProgramResult:
    return NativeProgramResult(
        instruction_rejected=code,
        semantics=None,
        taxonomy=None,
        card=None,
        verdict=None,
        editorial=None,
        normalizations=(),
    )


class NativeNewsProgram(dspy.Module):  # type: ignore[misc]
    """Exactly three named Predictors in fixed order, usable by async production and synchronous GEPA."""

    def __init__(
        self,
        state: NewsProgramStateV1,
        *,
        candidate_guard: CandidateGuard | None = None,
    ) -> None:
        super().__init__()
        if state.program_sha256 != state.computed_sha256():
            raise ValueError("news_program_state_hash_mismatch")
        self.state = state
        self.candidate_guard = candidate_guard
        # Attribute order is `named_predictors()` order, which is also execution order. Built from the
        # code-owned seed defaults and then loaded, so a released image goes through DSPy's own
        # `load_state` rather than a second, Tracefold-shaped construction path.
        self.event_semantics = dspy.Predict(
            EventSemanticsSignature.with_instructions(seed_instruction("event_semantics")),
            max_tokens=state.event_semantics.max_tokens,
        )
        self.taxonomy = dspy.Predict(
            EventTaxonomySignature.with_instructions(seed_instruction("taxonomy")),
            max_tokens=state.taxonomy.max_tokens,
        )
        self.reader_card = dspy.Predict(
            ReaderCardSignature.with_instructions(seed_instruction("reader_card")),
            max_tokens=state.reader_card.max_tokens,
        )
        self.load_state(state.predictor_documents())
        self._verify_loaded_state(state)

    def _check_input_budget(self, prepared: _PreparedRun) -> None:
        # UTF-8 byte count is a deliberately conservative token upper estimate. It
        # includes native instructions, demos, JSON schema, evidence and output reserve;
        # no final slicing is allowed. The extra reserve covers adapter framing and semantics.
        for name, evidence in (
            ("event_semantics", prepared.semantics_evidence_json),
            ("taxonomy", prepared.taxonomy_evidence_json),
            ("reader_card", prepared.card_evidence_json),
        ):
            predictor = getattr(self, name)
            document = canonical_json(
                {
                    "instructions": str(predictor.signature.instructions),
                    "demos": [dict(demo) for demo in predictor.demos],
                    "evidence": evidence,
                    "schema": predictor.signature.model_json_schema(),
                }
            )
            upper = (
                len(document.encode("utf-8")) + self.state.predictor_state(cast(PredictorName, name)).max_tokens + 8192
            )
            if upper > PROGRAM_CONTEXT_UPPER_TOKENS:
                raise ValueError("news_program_input_budget_exceeded")

    def _verify_loaded_state(self, state: NewsProgramStateV1) -> None:
        """Re-read what DSPy actually loaded and refuse anything the round trip did not reproduce.

        `Signature.load_state` applies saved prefixes and descriptions positionally and leaves the field
        names, types and ordering to the code Signature, so "the document loaded" and "the document is the
        Program" are two different statements. This is the second one.
        """

        loaded = dict(self.named_predictors())
        if tuple(loaded) != PREDICTOR_NAMES:
            raise ValueError("news_program_state_predictor_set_invalid")
        for predictor in PREDICTOR_NAMES:
            predict = loaded[predictor]
            if str(predict.signature.instructions) != state.instruction_for(predictor):
                raise ValueError(f"news_program_state_instruction_not_loaded:{predictor}")
            if tuple(dict(demo) for demo in predict.demos) != state.demos_for(predictor):
                raise ValueError(f"news_program_state_demos_not_loaded:{predictor}")
            if predict.lm is not None:
                raise ValueError(f"news_program_state_lm_route_forbidden:{predictor}")

    def _candidate_rejection(self) -> str | None:
        instructions = tuple(str(getattr(self, name).signature.instructions) for name in PREDICTOR_NAMES)
        try:
            for instruction in instructions:
                validate_program_instruction(instruction)
        except ValueError as exc:
            return str(exc)
        if self.candidate_guard is None:
            return None
        code = self.candidate_guard(*instructions)
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
            catalog_candidates={row.symbol: row.classes for row in prepared.context.gate.catalog_candidates},
        )
        semantics_json = canonical_json(_reader_card_semantic_view(semantics).model_dump(mode="json"))
        return semantics, normalizations, semantics_json

    def _taxonomy_answer(
        self,
        prepared: _PreparedRun,
        lm: dspy.BaseLM | None,
    ) -> tuple[ModelTaxonomyV1 | None, str | None]:
        """The taxonomy Predictor's answer, or the code that says why this judgment has none.

        The call is made inside the try because a taxonomy failure no longer ends the judgment: a provider
        refusal, a truncated completion and an unparseable answer are all as survivable as a rejected
        label, and the ReaderCard call after this one is what the reader actually receives.
        """

        try:
            prediction = self.taxonomy(evidence_json=prepared.taxonomy_evidence_json, lm=lm)
        except Exception as exc:
            code = _taxonomy_call_failure_code(exc)
            if code is None:
                raise
            return None, code
        return _validate_taxonomy(prediction.taxonomy)

    async def _ataxonomy_answer(
        self,
        prepared: _PreparedRun,
        lm: dspy.BaseLM | None,
    ) -> tuple[ModelTaxonomyV1 | None, str | None]:
        """`_taxonomy_answer` on the production async path; same boundary, DSPy's own async entry point."""

        try:
            prediction = await self.taxonomy.acall(evidence_json=prepared.taxonomy_evidence_json, lm=lm)
        except Exception as exc:
            code = _taxonomy_call_failure_code(exc)
            if code is None:
                raise
            return None, code
        return _validate_taxonomy(prediction.taxonomy)

    @staticmethod
    def _result(
        prediction: dspy.Prediction,
        *,
        prepared: _PreparedRun,
        semantics: EventSemantics,
        taxonomy: ModelTaxonomyV1 | None,
        taxonomy_error_code: str | None,
        normalizations: tuple[ProgramNormalizationTrace, ...],
    ) -> NativeProgramResult:
        return _assemble(
            semantics,
            taxonomy,
            prediction.card,
            context=prepared.context,
            told_count=len(prepared.context.told.entries),
            normalizations=normalizations,
            taxonomy_error_code=taxonomy_error_code,
        )

    def forward(
        self,
        context: TriageContext | Mapping[str, Any],
        *,
        event_lm: dspy.BaseLM | None = None,
        taxonomy_lm: dspy.BaseLM | None = None,
        card_lm: dspy.BaseLM | None = None,
    ) -> NativeProgramResult:
        rejection = self._candidate_rejection()
        if rejection is not None:
            return _rejected(rejection)
        prepared = _prepare(context)
        self._check_input_budget(prepared)
        with dspy.context(adapter=program_json_adapter()):
            semantics_prediction = self.event_semantics(
                evidence_json=prepared.semantics_evidence_json,
                lm=event_lm,
            )
            # EventSemantics is validated before the next physical call because ReaderCard reads its
            # output: there is no card to assemble without it, and the route restarts on a fallback.
            semantics, normalizations, semantics_json = self._semantics(semantics_prediction, prepared)
            taxonomy, taxonomy_error_code = self._taxonomy_answer(prepared, taxonomy_lm)
            card_prediction = self.reader_card(
                evidence_json=prepared.card_evidence_json,
                semantics_json=semantics_json,
                lm=card_lm,
            )
        return self._result(
            card_prediction,
            prepared=prepared,
            semantics=semantics,
            taxonomy=taxonomy,
            taxonomy_error_code=taxonomy_error_code,
            normalizations=normalizations,
        )

    async def aforward(
        self,
        context: TriageContext | Mapping[str, Any],
        *,
        event_lm: dspy.BaseLM | None = None,
        taxonomy_lm: dspy.BaseLM | None = None,
        card_lm: dspy.BaseLM | None = None,
    ) -> NativeProgramResult:
        rejection = self._candidate_rejection()
        if rejection is not None:
            return _rejected(rejection)
        prepared = _prepare(context)
        self._check_input_budget(prepared)
        with dspy.context(adapter=program_json_adapter()):
            semantics_prediction = await self.event_semantics.acall(
                evidence_json=prepared.semantics_evidence_json,
                lm=event_lm,
            )
            # EventSemantics is validated before the next physical call because ReaderCard reads its
            # output: there is no card to assemble without it, and the route restarts on a fallback.
            semantics, normalizations, semantics_json = self._semantics(semantics_prediction, prepared)
            taxonomy, taxonomy_error_code = await self._ataxonomy_answer(prepared, taxonomy_lm)
            card_prediction = await self.reader_card.acall(
                evidence_json=prepared.card_evidence_json,
                semantics_json=semantics_json,
                lm=card_lm,
            )
        return self._result(
            card_prediction,
            prepared=prepared,
            semantics=semantics,
            taxonomy=taxonomy,
            taxonomy_error_code=taxonomy_error_code,
            normalizations=normalizations,
        )


__all__ = ["CandidateGuard", "NativeNewsProgram", "NativeProgramResult", "ProgramOutputError"]
