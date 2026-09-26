"""Source-grounded event understanding, independent of cards and trading decisions.

Models propose claims and relations. This module validates references, assigns
Event-local identities and adopts content without converting a retry, a translation
or a new model into a new real-world action. It does no I/O.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from decimal import Decimal
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .artifact_identity import canonical_json, canonical_sha
from .evidence import EvidenceSpan
from .models import TriageAsset
from .taxonomy import SubjectCode

UPDATE_SCHEMA: Final = "news_event_update_v1"
CLAIM_RELATION_VERSION = "news_claim_relation_v1"
COVERAGE_VERSION = "news_delivered_coverage_v1"

ClaimMode = Literal[
    "observation",
    "decision",
    "commitment",
    "conditional_threat",
    "guidance",
    "forecast",
    "denial",
    "commentary",
    "promotion",
    "calendar",
    "unknown",
]
ActionPhase = Literal[
    "proposed",
    "announced",
    "ordered",
    "effective",
    "executing",
    "completed",
    "cancelled",
    "unknown",
    "not_applicable",
]
ClaimPolarity = Literal["affirmed", "negated", "conditional", "unknown"]
ClaimRelationKind = Literal[
    "equivalent",
    "adds_information",
    "corrects_or_conflicts",
    "unrelated",
    "unresolved",
]
ChangeKind = Literal[
    "new_fact",
    "parameter",
    "phase",
    "scope",
    "correction",
    "retraction",
    "evidence",
    "restatement",
    "unresolved",
]
EvidenceRelationKind = Literal["supports", "refutes", "reports", "not_addressed", "unresolved"]
CoverageKind = Literal["full", "partial", "none", "unresolved", "unavailable"]
ImpactChannel = Literal[
    "supply",
    "demand",
    "financing",
    "market_access",
    "policy_commitment",
    "operational_risk",
    "none",
    "unknown",
]
ReadAction = Literal["read_current_artifact", "load_prior_statement", "read_matching_release", "no_useful_read"]

Ref = Annotated[str, Field(min_length=1, max_length=128)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Exact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def _text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


class EvidenceQuote(Exact):
    evidence_ref: Ref
    quote: str = Field(min_length=1)


class Quantity(Exact):
    name: str = Field(min_length=1)
    value: Decimal
    unit: str = Field(min_length=1)

    @field_validator("value")
    @classmethod
    def _canonical_number(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("news_claim_quantity_nonfinite")
        return value.normalize() if value else Decimal(0)


class OpenClaim(Exact):
    """Open extraction: no importance score, authority verdict or send decision."""

    text: str = Field(min_length=1)
    subject: str | None = None
    action: str | None = None
    object: str | None = None
    speaker: str | None = None
    attributed_to: str | None = None
    condition: str | None = None
    quantities: tuple[Quantity, ...] = ()
    effective_at: str | None = None
    statistical_period: str | None = None
    # Explicit, normalized dimensions only. Missing information stays None;
    # neither a title token nor a URL is proof of any of these fields.
    jurisdiction: str | None = None
    instrument_term: str | None = None
    evidence_quotes: tuple[EvidenceQuote, ...] = Field(min_length=1)
    assets: tuple[TriageAsset, ...] = ()

    @field_validator("text")
    @classmethod
    def _nonblank_text(cls, value: str) -> str:
        normalized = _text(value)
        if not normalized:
            raise ValueError("news_claim_text_empty")
        return normalized


class ClaimDraft(OpenClaim):
    mode: ClaimMode = "unknown"
    phase: ActionPhase = "unknown"
    polarity: ClaimPolarity = "unknown"


class Claim(ClaimDraft):
    claim_id: Digest
    first_available_at_ms: int = Field(ge=0)


class ClaimReference(Exact):
    event_id: Ref
    content_id: Digest
    claim_id: Digest

    @property
    def key(self) -> str:
        return f"{self.event_id}:{self.content_id}:{self.claim_id}"


class PriorClaim(Exact):
    reference: ClaimReference
    claim: Claim

    @model_validator(mode="after")
    def _reference_matches(self) -> PriorClaim:
        if self.reference.claim_id != self.claim.claim_id:
            raise ValueError("news_prior_claim_identity_mismatch")
        return self


class ClaimComparison(Exact):
    current_index: int = Field(ge=0)
    previous: ClaimReference
    relation: ClaimRelationKind
    changes: tuple[ChangeKind, ...] = ()
    # A correction by a publisher is not the same thing as a new real-world
    # reversal. The latter may be a catalyst; the former updates research only.
    cause: Literal["world_action", "source_correction", "evidence_change", "unknown"] = "unknown"


class EvidenceRelationDraft(Exact):
    claim_index: int = Field(ge=0)
    evidence_ref: Ref
    relation: EvidenceRelationKind
    target: Literal["statement", "proposition"] = "statement"


class EvidenceRelation(Exact):
    claim_id: Digest
    evidence_ref: Ref
    relation: EvidenceRelationKind
    target: Literal["statement", "proposition"]


class ClaimChange(Exact):
    current_claim_id: Digest
    previous: ClaimReference | None = None
    kinds: tuple[ChangeKind, ...] = Field(min_length=1)
    cause: Literal["world_action", "source_correction", "evidence_change", "unknown"] = "unknown"


class ImplicationDraft(Exact):
    claim_indices: tuple[int, ...] = Field(min_length=1)
    channel: ImpactChannel
    explanation: str = Field(min_length=1)
    conditions: tuple[str, ...] = ()
    origin: Literal["reported_causal_claim", "system_hypothesis"] = "system_hypothesis"


class Implication(Exact):
    claim_ids: tuple[Digest, ...] = Field(min_length=1)
    channel: ImpactChannel
    explanation: str = Field(min_length=1)
    conditions: tuple[str, ...] = ()
    origin: Literal["reported_causal_claim", "system_hypothesis"]


class ReadTarget(Exact):
    target_id: Ref
    action: ReadAction
    reference: Ref
    description: str = Field(min_length=1)


class OpenQuestionDraft(Exact):
    question: str = Field(min_length=1)
    claim_indices: tuple[int, ...] = ()
    target_id: Ref | None = None


class OpenQuestion(Exact):
    question: str = Field(min_length=1)
    claim_ids: tuple[Digest, ...] = ()
    target_id: Ref | None = None


class ExtractionDraft(Exact):
    topics: tuple[SubjectCode, ...] = ()
    claims: tuple[OpenClaim, ...]
    implications: tuple[ImplicationDraft, ...] = ()
    open_questions: tuple[OpenQuestionDraft, ...] = ()
    # This is only an explicit empty extraction, never a provider failure.
    no_assertion_reason: str | None = None

    @model_validator(mode="after")
    def _empty_extraction_is_explained(self) -> ExtractionDraft:
        if not self.claims and not (self.no_assertion_reason or "").strip():
            raise ValueError("news_claim_extraction_empty_unexplained")
        return self


class UnderstandingDraft(Exact):
    topics: tuple[SubjectCode, ...] = ()
    claims: tuple[ClaimDraft, ...]
    comparisons: tuple[ClaimComparison, ...] = ()
    evidence_relations: tuple[EvidenceRelationDraft, ...] = ()
    implications: tuple[ImplicationDraft, ...] = ()
    open_questions: tuple[OpenQuestionDraft, ...] = ()
    no_assertion_reason: str | None = None

    @model_validator(mode="after")
    def _internal_references_exist(self) -> UnderstandingDraft:
        if not self.claims and not (self.no_assertion_reason or "").strip():
            raise ValueError("news_claim_extraction_empty_unexplained")
        count = len(self.claims)
        indices = [comparison.current_index for comparison in self.comparisons]
        indices.extend(relation.claim_index for relation in self.evidence_relations)
        indices.extend(i for implication in self.implications for i in implication.claim_indices)
        indices.extend(i for question in self.open_questions for i in question.claim_indices)
        if any(index < 0 or index >= count for index in indices):
            raise ValueError("news_claim_output_reference_invalid")
        pairs = [(entry.current_index, entry.previous.key) for entry in self.comparisons]
        if len(pairs) != len(set(pairs)):
            raise ValueError("news_claim_comparison_duplicate")
        return self


class SourceEvidence(Exact):
    evidence_ref: Ref
    source_item_id: str
    source_artifact_id: str
    source: str
    url: str
    text: str
    content_sha256: str
    available_at_ms: int | None
    reported_published_at_ms: int | None

    @classmethod
    def from_span(cls, span: EvidenceSpan) -> SourceEvidence:
        return cls(
            evidence_ref=span.ref_id,
            source_item_id=span.source_item_id,
            source_artifact_id=span.source_artifact_id,
            source=span.source,
            url=span.url,
            text=span.text,
            content_sha256=span.content_sha256,
            available_at_ms=span.available_at_ms,
            reported_published_at_ms=span.reported_published_at_ms,
        )


class EventUpdate(Exact):
    """An adopted interpretation of referenced evidence, not verified world truth."""

    schema_version: Literal["news_event_update_v1"] = UPDATE_SCHEMA
    event_id: Ref
    topics: tuple[SubjectCode, ...] = ()
    claims: tuple[Claim, ...]
    evidence: tuple[SourceEvidence, ...]
    evidence_relations: tuple[EvidenceRelation, ...] = ()
    changes: tuple[ClaimChange, ...] = ()
    implications: tuple[Implication, ...] = ()
    open_questions: tuple[OpenQuestion, ...] = ()
    no_assertion_reason: str | None = None

    @model_validator(mode="after")
    def _references_exist(self) -> EventUpdate:
        claims = {claim.claim_id: claim for claim in self.claims}
        evidence = {source.evidence_ref: source for source in self.evidence}
        if len(claims) != len(self.claims) or len(evidence) != len(self.evidence):
            raise ValueError("news_event_update_duplicate_identity")
        if not claims and not (self.no_assertion_reason or "").strip():
            raise ValueError("news_claim_extraction_empty_unexplained")
        for claim in self.claims:
            for quote in claim.evidence_quotes:
                source = evidence.get(quote.evidence_ref)
                if source is None or quote.quote not in source.text or not quote.quote.strip():
                    raise ValueError("news_claim_quote_not_in_evidence")
        for relation in self.evidence_relations:
            if relation.claim_id not in claims or relation.evidence_ref not in evidence:
                raise ValueError("news_claim_evidence_relation_invalid")
        if any(change.current_claim_id not in claims for change in self.changes):
            raise ValueError("news_claim_change_current_reference_invalid")
        if any(claim_id not in claims for item in self.implications for claim_id in item.claim_ids):
            raise ValueError("news_implication_claim_reference_invalid")
        if any(claim_id not in claims for item in self.open_questions for claim_id in item.claim_ids):
            raise ValueError("news_question_claim_reference_invalid")
        return self

    @property
    def content_id(self) -> str:
        # Changes describe the transition *to* this content. Re-evaluating the same
        # content against itself has no changes, without changing its identity.
        # Topics and explanatory prose may change when the same facts are read
        # again. They belong to the semantic execution, not business freshness.
        return canonical_sha(
            self.model_dump(mode="json", exclude={"changes", "topics", "implications", "open_questions", "evidence"})
        )

    def reference(self, claim_id: str) -> ClaimReference:
        if claim_id not in {claim.claim_id for claim in self.claims}:
            raise ValueError("news_claim_reference_missing")
        return ClaimReference(event_id=self.event_id, content_id=self.content_id, claim_id=claim_id)

    def prior_claims(self) -> tuple[PriorClaim, ...]:
        return tuple(PriorClaim(reference=self.reference(claim.claim_id), claim=claim) for claim in self.claims)

    @property
    def first_available_at_ms(self) -> int | None:
        return min((claim.first_available_at_ms for claim in self.claims), default=None)


def material_differences(current: ClaimDraft, previous: ClaimDraft) -> tuple[str, ...]:
    """Known contradictory dimensions forbid equivalence; missing fields are unknown.

    Free-text entity aliases are left to the relation backend. Numeric comparisons
    are exact Decimal comparisons, never substring overlap or float tolerances.
    """

    differences: list[str] = []
    for field in ("jurisdiction", "instrument_term", "statistical_period", "effective_at"):
        left, right = getattr(current, field), getattr(previous, field)
        if left and right and _text(left).casefold() != _text(right).casefold():
            differences.append(field)
    for field in ("mode", "phase", "polarity"):
        left, right = getattr(current, field), getattr(previous, field)
        if left != "unknown" and right not in {"unknown", left}:
            differences.append(field)
    prior_values = {(_text(q.name).casefold(), _text(q.unit).casefold()): q.value for q in previous.quantities}
    current_keys = {(_text(q.name).casefold(), _text(q.unit).casefold()) for q in current.quantities}
    if current_keys and prior_values and current_keys != set(prior_values):
        differences.append("quantity_dimensions")
    for quantity in current.quantities:
        key = (_text(quantity.name).casefold(), _text(quantity.unit).casefold())
        if key in prior_values and quantity.value != prior_values[key]:
            differences.append("quantities")
    return tuple(sorted(set(differences)))


def _sorted_models[T: BaseModel](values: Sequence[T]) -> tuple[T, ...]:
    by_value = {canonical_json(value.model_dump(mode="json")): value for value in values}
    return tuple(by_value[key] for key in sorted(by_value))


def _draft_identity(claim: ClaimDraft, event_id: str) -> str:
    # No output index, model name, generated card or completion time in this ID.
    payload = claim.model_dump(mode="json")
    payload["evidence_quotes"] = [quote.model_dump(mode="json") for quote in _sorted_models(claim.evidence_quotes)]
    payload["quantities"] = [value.model_dump(mode="json") for value in _sorted_models(claim.quantities)]
    payload["assets"] = [value.model_dump(mode="json") for value in _sorted_models(claim.assets)]
    return canonical_sha({"event_id": event_id, "claim": payload})


def assemble_event_update(
    *,
    event_id: str,
    draft: UnderstandingDraft,
    evidence: Sequence[SourceEvidence],
    first_available_at_ms: int,
    prior: Sequence[PriorClaim] = (),
    previous_update: EventUpdate | None = None,
    read_targets: Sequence[ReadTarget] = (),
) -> EventUpdate:
    """Validate one frozen result and reuse known equivalent Event-local claims."""

    sources = {entry.evidence_ref: entry for entry in evidence}
    if len(sources) != len(evidence):
        raise ValueError("news_event_update_duplicate_evidence")
    prior_by_ref = {entry.reference.key: entry for entry in prior}
    if previous_update is not None:
        if previous_update.event_id != event_id:
            raise ValueError("news_event_update_parent_event_mismatch")
        for entry in previous_update.evidence:
            current = sources.get(entry.evidence_ref)
            if current is not None and (current.text, current.content_sha256) != (entry.text, entry.content_sha256):
                raise ValueError("news_evidence_reference_content_conflict")
            sources[entry.evidence_ref] = entry
        prior_by_ref.update({entry.reference.key: entry for entry in previous_update.prior_claims()})
    target_ids = {target.target_id for target in read_targets}
    for question in draft.open_questions:
        if question.target_id is not None and question.target_id not in target_ids:
            raise ValueError("news_question_read_target_invalid")
    comparisons: dict[int, list[ClaimComparison]] = {}
    for comparison in draft.comparisons:
        previous = prior_by_ref.get(comparison.previous.key)
        if previous is None:
            raise ValueError("news_claim_previous_reference_invalid")
        normalized_comparison = comparison
        if comparison.relation == "equivalent" and material_differences(
            draft.claims[comparison.current_index], previous.claim
        ):
            # A failed semantic comparison is not a reason to discard the news.
            normalized_comparison = comparison.model_copy(update={"relation": "adds_information", "changes": ()})
        comparisons.setdefault(comparison.current_index, []).append(normalized_comparison)

    claims: dict[str, Claim] = {} if previous_update is None else {c.claim_id: c for c in previous_update.claims}
    current_ids: list[str] = []
    changes: list[ClaimChange] = []
    for index, proposed in enumerate(draft.claims):
        for quote in proposed.evidence_quotes:
            source = sources.get(quote.evidence_ref)
            if source is None or not quote.quote.strip() or quote.quote not in source.text:
                raise ValueError("news_claim_quote_not_in_evidence")
        pairs = comparisons.get(index, [])
        equivalents = [pair for pair in pairs if pair.relation == "equivalent"]
        local = sorted(
            (pair for pair in equivalents if pair.previous.event_id == event_id),
            key=lambda pair: pair.previous.key,
        )
        if local:
            claim = prior_by_ref[local[0].previous.key].claim
        else:
            available = min(
                (
                    timestamp
                    for quote in proposed.evidence_quotes
                    if (timestamp := sources[quote.evidence_ref].available_at_ms) is not None
                ),
                default=first_available_at_ms,
            )
            # Cross-Event translations preserve the original proposition's age.
            if equivalents:
                available = min(
                    available, *(prior_by_ref[pair.previous.key].claim.first_available_at_ms for pair in equivalents)
                )
            claim = Claim(
                **proposed.model_dump(),
                claim_id=_draft_identity(proposed, event_id),
                first_available_at_ms=available,
            )
        # Exact structured repetitions preserve their original availability even
        # without a relation question (for example, a redelivered worker job).
        if claim.claim_id in claims:
            claim = claims[claim.claim_id]
        current_ids.append(claim.claim_id)
        claims[claim.claim_id] = claim
        material_pairs = [pair for pair in pairs if pair.relation not in {"equivalent", "unrelated", "unresolved"}]
        if material_pairs:
            for pair in material_pairs:
                kinds = pair.changes or ("new_fact",)
                if pair.cause == "source_correction":
                    kinds = tuple(sorted(set(kinds) - {"new_fact", "parameter", "phase", "scope"})) or ("correction",)
                    if pair.previous.event_id == event_id:
                        claims.pop(pair.previous.claim_id, None)
                changes.append(
                    ClaimChange(current_claim_id=claim.claim_id, previous=pair.previous, kinds=kinds, cause=pair.cause)
                )
        elif equivalents:
            if not local:
                changes.append(
                    ClaimChange(
                        current_claim_id=claim.claim_id, previous=equivalents[0].previous, kinds=("restatement",)
                    )
                )
        elif any(pair.relation == "unresolved" for pair in pairs):
            # An unavailable comparison is neither evidence of equivalence nor
            # proof of a new catalyst. Keep the proposition for readers/research
            # and record the exact dependency that is still unresolved.
            changes.extend(
                ClaimChange(
                    current_claim_id=claim.claim_id,
                    previous=pair.previous,
                    kinds=("unresolved",),
                    cause="unknown",
                )
                for pair in pairs
                if pair.relation == "unresolved"
            )
        elif previous_update is None or claim.claim_id not in {c.claim_id for c in previous_update.claims}:
            changes.append(ClaimChange(current_claim_id=claim.claim_id, kinds=("new_fact",)))

    relations = [] if previous_update is None else list(previous_update.evidence_relations)
    for relation in draft.evidence_relations:
        if relation.evidence_ref not in sources:
            raise ValueError("news_claim_evidence_relation_invalid")
        relations.append(
            EvidenceRelation(
                claim_id=current_ids[relation.claim_index],
                evidence_ref=relation.evidence_ref,
                relation=relation.relation,
                target=relation.target,
            )
        )
    relations = [relation for relation in relations if relation.claim_id in claims]
    referenced_evidence = {quote.evidence_ref for claim in claims.values() for quote in claim.evidence_quotes}
    referenced_evidence.update(relation.evidence_ref for relation in relations)
    return EventUpdate(
        event_id=event_id,
        topics=tuple(sorted(set(draft.topics))),
        claims=tuple(claims[key] for key in sorted(claims)),
        evidence=tuple(sources[key] for key in sorted(referenced_evidence)),
        evidence_relations=_sorted_models(relations),
        changes=_sorted_models(changes),
        implications=_sorted_models(
            [
                Implication(
                    **item.model_dump(exclude={"claim_indices"}),
                    claim_ids=tuple(sorted({current_ids[i] for i in item.claim_indices})),
                )
                for item in draft.implications
            ]
        ),
        open_questions=_sorted_models(
            [
                OpenQuestion(
                    **item.model_dump(exclude={"claim_indices"}),
                    claim_ids=tuple(sorted({current_ids[i] for i in item.claim_indices})),
                )
                for item in draft.open_questions
            ]
        ),
        no_assertion_reason=draft.no_assertion_reason if not claims else None,
    )


def selected_claims(update: EventUpdate, claim_ids: Sequence[str]) -> tuple[Claim, ...]:
    by_id = {claim.claim_id: claim for claim in update.claims}
    if not claim_ids or len(set(claim_ids)) != len(claim_ids) or any(claim_id not in by_id for claim_id in claim_ids):
        raise ValueError("news_selected_claims_invalid")
    return tuple(by_id[claim_id] for claim_id in sorted(claim_ids))


def notification_intent_id(
    *, update: EventUpdate, claim_ids: Sequence[str], channel: str, purpose: str = "news"
) -> str:
    if not channel.strip() or not purpose.strip():
        raise ValueError("news_notification_destination_missing")
    claims = selected_claims(update, claim_ids)
    return canonical_sha(
        {
            "schema": "news_notification_intent_v1",
            "event_id": update.event_id,
            "content_id": update.content_id,
            "claim_ids": [claim.claim_id for claim in claims],
            "channel": channel,
            "purpose": purpose,
        }
    )


def source_text(update: EventUpdate, claim_ids: Sequence[str]) -> str:
    """Deterministic public evidence projection; never generated card copy."""

    evidence = {entry.evidence_ref: entry for entry in update.evidence}
    lines: list[str] = []
    for claim in selected_claims(update, claim_ids):
        lines.append(f"[{claim.claim_id}] {claim.text}")
        lines.append(f"mode={claim.mode}; phase={claim.phase}; polarity={claim.polarity}")
        for name in ("speaker", "attributed_to", "condition", "effective_at", "statistical_period"):
            value = getattr(claim, name)
            if value:
                lines.append(f"{name}={value}")
        for quote in claim.evidence_quotes:
            origin = evidence[quote.evidence_ref]
            lines.append(f"[{quote.evidence_ref}] {origin.source or 'unknown source'}: {quote.quote}")
    return "\n".join(lines)
