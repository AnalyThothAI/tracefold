"""One exact EventUpdate contract. Stored old documents are not coerced into it."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..taxonomy import SourceAuthority
from .identity import digest, identity


class Exact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


Mode = Literal[
    "observation",
    "decision",
    "commitment",
    "conditional_threat",
    "guidance",
    "forecast",
    "commentary",
    "promotion",
    "unknown",
]
Phase = Literal["proposed", "announced", "ordered", "effective", "executing", "completed", "cancelled", "unknown"]
Relation = Literal[
    "equivalent",
    "adds_information",
    "real_world_change",
    "corrects",
    "conflicts",
    "unrelated",
    "unresolved",
]
# What kind of new content a claim states. Attribution, intent, forecasts, commentary and promotion are the
# claim's `mode`; a repeat is its relation to earlier claims. Neither is a content kind.
ContentKind = Literal[
    "state_change",
    "official_measure",
    "new_quantity",
    "level_crossed",
    "period_record",
    "quantified_flow",
    "schedule",
    "other",
]
# `possible_new` is a claim whose relation to at least one supplied prior claim was not established. It is
# adopted content and a notification candidate, but never a public catalyst: an unresolved comparison is not
# evidence that the world changed.
ChangeKind = Literal[
    "new_fact",
    "possible_new",
    "parameter_change",
    "phase_change",
    "scope_change",
    "correction",
    "conflict",
    "evidence_change",
    "restatement",
]

_RELATION_CHANGE_KINDS: dict[str, frozenset[ChangeKind | None]] = {
    "equivalent": frozenset({None}),
    "unrelated": frozenset({None}),
    "unresolved": frozenset({None}),
    "adds_information": frozenset({"new_fact", "scope_change", "parameter_change"}),
    "real_world_change": frozenset({"phase_change", "parameter_change", "scope_change", "new_fact"}),
    "corrects": frozenset({"correction"}),
    "conflicts": frozenset({"conflict"}),
}


class Quantity(Exact):
    name: str = Field(min_length=1)
    # The source's exact decimal text is retained. Unit conversion belongs to code,
    # never to string similarity or a model's arithmetic.
    value: str = Field(min_length=1, pattern=r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
    unit: str = Field(min_length=1)
    period: str | None = None


class Source(Exact):
    publisher_id: str = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    artifact_revision: str = Field(min_length=1)
    # Populated by ingestion from known provenance, not generated from a URL/name.
    origin_id: str | None = None
    attribution: str | None = None
    published_at_ms: int | None = Field(default=None, ge=0)
    first_available_at_ms: int = Field(ge=0)
    url: str | None = None
    # Code-owned classification of the provenance (the News source-authority classifier), supplied by the
    # store. It is not part of evidence identity and never comes from a model.
    source_authority: SourceAuthority = "unknown"


class Evidence(Exact):
    ref: str = Field(min_length=1)
    text: str = Field(min_length=1)
    source: Source

    @classmethod
    def issue(cls, text: str, source: Source) -> Evidence:
        return cls(ref=cls.source_ref(text, source), text=text, source=source)

    @staticmethod
    def source_ref(text: str, source: Source) -> str:
        # Provenance corrections change evidence identity; re-observation time
        # does not. The store preserves the first availability clock on replay.
        return identity(
            "ev",
            source.publisher_id,
            source.artifact_id,
            source.artifact_revision,
            source.origin_id,
            source.attribution,
            source.published_at_ms,
            text,
        )

    @model_validator(mode="after")
    def check_ref(self) -> Evidence:
        if self.ref != self.source_ref(self.text, self.source):
            raise ValueError("news_evidence_identity_mismatch")
        return self


class Citation(Exact):
    evidence_ref: str = Field(min_length=1)
    quote: str = Field(min_length=1)


class Asset(Exact):
    symbol: str = Field(min_length=1)
    market_type: Literal["crypto", "equity", "commodity", "index", "forex", "fund", "unknown"]
    role: Literal["primary", "mentioned"]


class ClaimFields(Exact):
    subject: str = Field(min_length=1)
    action: str = Field(min_length=1)
    object: str = ""
    speaker: str | None = None
    conditions: tuple[str, ...] = ()
    quantities: tuple[Quantity, ...] = ()
    effective_at: str | None = None
    occurred_at: str | None = None
    statistical_period: str | None = None
    polarity: Literal["affirmative", "negative", "unknown"] = "unknown"
    mode: Mode = "unknown"
    # A non-action claim can use None. A future effective_at never changes phase.
    phase: Phase | None = None
    # The notification policy's reading of the content. It is not part of claim identity: a different
    # reading of the same proposition on a rerun must not manufacture a new claim.
    content_kind: ContentKind = "other"
    assets: tuple[Asset, ...] = ()


class DraftClaim(Exact):
    slot: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    fields: ClaimFields
    citations: tuple[Citation, ...] = Field(min_length=1)


class IdentityHint(Exact):
    key: Literal["subject_id", "object_id", "country", "tenor", "period"]
    value: str = Field(min_length=1)
    evidence_ref: str
    surface: str = Field(min_length=1)


class Claim(Exact):
    ref: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    fields: ClaimFields
    citations: tuple[Citation, ...] = Field(min_length=1)
    first_available_at_ms: int = Field(ge=0)
    known_identity: tuple[IdentityHint, ...] = ()
    # Code-owned ancestry of this adopted occurrence, not a model-generated ID.
    # Persist the closure here: head.changes describes only the latest revision,
    # so a later evidence update must not erase an occurrence's earlier changes.
    # Roots have no antecedents; these refs remain local to recalled Event claims.
    antecedent_refs: tuple[str, ...] = ()


class PriorClaim(Exact):
    event_id: str
    content_revision: str
    claim: Claim


class RelationDraft(Exact):
    slot: str
    previous_ref: str
    relation: Relation
    change_kind: ChangeKind | None = None

    @model_validator(mode="after")
    def check_kind(self) -> RelationDraft:
        if self.change_kind not in _RELATION_CHANGE_KINDS[self.relation]:
            raise ValueError("news_relation_change_kind_mismatch")
        return self


class SupportDraft(Exact):
    slot: str
    evidence_ref: str
    relation: Literal["supports", "refutes", "reports", "not_addressed", "unresolved"]


class ImplicationDraft(Exact):
    slots: tuple[str, ...] = Field(min_length=1)
    channel: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    conditions: tuple[str, ...] = ()
    origin: Literal["reported_causality", "system_hypothesis"]


class ReadTarget(Exact):
    ref: str = Field(min_length=1)
    action: Literal["read_current_artifact", "load_prior_statement", "read_matching_release"]
    description: str = Field(min_length=1)


class OpenQuestion(Exact):
    question: str = Field(min_length=1)
    slots: tuple[str, ...] = Field(min_length=1)
    target_ref: str | None = None


class Extraction(Exact):
    # No maximum claim count: the backend batches; it never drops the tail.
    claims: tuple[DraftClaim, ...]
    topics: tuple[str, ...] = ()
    relations: tuple[RelationDraft, ...] = ()
    supports: tuple[SupportDraft, ...] = ()
    implications: tuple[ImplicationDraft, ...] = ()
    open_questions: tuple[OpenQuestion, ...] = ()

    @model_validator(mode="after")
    def unique_slots(self) -> Extraction:
        slots = {claim.slot for claim in self.claims}
        if len(slots) != len(self.claims):
            raise ValueError("news_duplicate_claim_slot")
        referenced = {row.slot for row in self.relations} | {row.slot for row in self.supports}
        referenced |= {slot for row in self.implications for slot in row.slots}
        referenced |= {slot for row in self.open_questions for slot in row.slots}
        if not referenced <= slots:
            raise ValueError("news_unknown_claim_slot")
        return self


class EvidenceRelation(Exact):
    claim_ref: str
    evidence_ref: str
    relation: Literal["supports", "refutes", "reports", "not_addressed", "unresolved"]


class Change(Exact):
    kind: ChangeKind
    current_ref: str
    previous_ref: str | None = None
    previous_content_ref: str | None = None
    # The established claim relation behind this change, when one exists. `possible_new` carries
    # `unresolved`; an evidence change or a first report carries none.
    relation: Relation | None = None

    @model_validator(mode="after")
    def check_previous(self) -> Change:
        if (self.previous_ref is None) != (self.previous_content_ref is None):
            raise ValueError("news_change_previous_identity_incomplete")
        if self.kind == "possible_new" and (self.previous_ref is None or self.relation != "unresolved"):
            raise ValueError("news_possible_new_requires_unresolved_prior")
        return self


class Implication(Exact):
    claim_refs: tuple[str, ...]
    channel: str
    explanation: str
    conditions: tuple[str, ...]
    origin: Literal["reported_causality", "system_hypothesis"]


class KnowledgeGap(Exact):
    question: str
    claim_refs: tuple[str, ...]
    target_ref: str | None = None


def content_material(
    event_id: str,
    claim_refs: Iterable[str],
    retired_claim_refs: Iterable[str],
    evidence_relations: Iterable[EvidenceRelation],
) -> dict[str, object]:
    """The business material of one adopted revision.

    Wording, program, observation/adoption clocks, topic labels and explanatory prose do not manufacture a
    new business revision. Evidence is identified by immutable source refs; changed support relations are
    genuine source updates.
    """

    relations = sorted(
        (row.model_dump(mode="json") for row in evidence_relations),
        key=lambda row: (row["claim_ref"], row["evidence_ref"], row["relation"]),
    )
    return {
        "event_id": event_id,
        "claims": sorted(claim_refs),
        "retired_claim_refs": sorted(retired_claim_refs),
        "evidence_relations": relations,
    }


class EventUpdate(Exact):
    schema_version: Literal["news_event_update_v1"] = "news_event_update_v1"
    event_id: str
    input_revision: int = Field(ge=1)
    content_revision: str
    previous_content_revision: str | None
    adopted_at_ms: int = Field(ge=0)
    topics: tuple[str, ...]
    claims: tuple[Claim, ...]
    evidence: tuple[Evidence, ...]
    evidence_relations: tuple[EvidenceRelation, ...]
    retired_claim_refs: tuple[str, ...] = ()
    changes: tuple[Change, ...]
    implications: tuple[Implication, ...] = ()
    open_questions: tuple[KnowledgeGap, ...] = ()

    @property
    def ref(self) -> str:
        return identity("update", self.event_id, self.content_revision)

    def content_material(self) -> dict[str, object]:
        return content_material(
            self.event_id,
            (claim.ref for claim in self.claims),
            self.retired_claim_refs,
            self.evidence_relations,
        )

    @model_validator(mode="after")
    def check_links(self) -> EventUpdate:
        claims = {claim.ref for claim in self.claims}
        evidence = {item.ref for item in self.evidence}
        if len(claims) != len(self.claims) or len(evidence) != len(self.evidence):
            raise ValueError("news_update_duplicate_reference")
        if self.content_revision != digest(self.content_material()):
            raise ValueError("news_content_revision_mismatch")
        if not set(self.retired_claim_refs) <= claims:
            raise ValueError("news_retired_claim_missing")
        for claim in self.claims:
            if not {citation.evidence_ref for citation in claim.citations} <= evidence:
                raise ValueError("news_update_citation_missing")
        for relation in self.evidence_relations:
            if relation.claim_ref not in claims or relation.evidence_ref not in evidence:
                raise ValueError("news_update_relation_missing")
        for change in self.changes:
            if change.current_ref not in claims:
                raise ValueError("news_change_current_missing")
        return self


class FrozenInput(Exact):
    schema_version: Literal["news_event_input_v1"] = "news_event_input_v1"
    event_id: str
    revision: int = Field(ge=1)
    # All revisions produced by a single optional read retain this lineage ID.
    lineage_id: str = Field(min_length=1)
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    prior: tuple[PriorClaim, ...] = ()
    read_targets: tuple[ReadTarget, ...] = ()
    focus_claim_refs: tuple[str, ...] = ()
    identity_hints: tuple[IdentityHint, ...] = ()

    @property
    def evidence_sha(self) -> str:
        return digest(sorted((item.model_dump(mode="json") for item in self.evidence), key=lambda row: row["ref"]))

    @model_validator(mode="after")
    def unique_input_refs(self) -> FrozenInput:
        for rows in (self.evidence, self.read_targets):
            refs = [row.ref for row in rows]
            if len(refs) != len(set(refs)):
                raise ValueError("news_input_duplicate_reference")
        evidence = {item.ref: item for item in self.evidence}
        for hint in self.identity_hints:
            if hint.evidence_ref not in evidence or hint.surface not in evidence[hint.evidence_ref].text:
                raise ValueError("news_identity_hint_not_grounded")
        prior = [row.claim.ref for row in self.prior]
        if len(prior) != len(set(prior)):
            raise ValueError("news_input_duplicate_prior_reference")
        return self


class PublicUpdate(Exact):
    """Deterministic, claim-scoped public facts for Trading; never a ReaderCard.

    `superseded_claim_refs` (catalyst_delta only) are earlier claims a real-world, parameter or phase change
    replaced. `retired_claim_refs` (source_update only) are earlier claims a correction retired. Both are
    subsets of `affected_claim_refs`, so a consumer amends only research that cited them.
    """

    schema_version: Literal["news_public_update_v1"] = "news_public_update_v1"
    update_id: str
    kind: Literal["catalyst_delta", "source_update"]
    event_id: str
    content_revision: str
    claim_refs: tuple[str, ...]
    claims: tuple[Claim, ...]
    evidence: tuple[Evidence, ...]
    changes: tuple[Change, ...]
    evidence_relations: tuple[EvidenceRelation, ...] = ()
    previous_content_refs: tuple[str, ...] = ()
    affected_claim_refs: tuple[str, ...] = ()
    superseded_claim_refs: tuple[str, ...] = ()
    retired_claim_refs: tuple[str, ...] = ()
    first_available_at_ms: int = Field(ge=0)
    semantic_completed_at_ms: int = Field(ge=0)
    text: str

    @staticmethod
    def identity_for(event_id: str, content_revision: str, kind: str, claim_refs: tuple[str, ...]) -> str:
        return identity("public", event_id, content_revision, kind, sorted(claim_refs))

    @model_validator(mode="after")
    def check_public_identity(self) -> PublicUpdate:
        if set(self.claim_refs) != {claim.ref for claim in self.claims}:
            raise ValueError("news_public_claim_refs_mismatch")
        if self.update_id != self.identity_for(self.event_id, self.content_revision, self.kind, self.claim_refs):
            raise ValueError("news_public_identity_mismatch")
        if self.kind == "source_update" and (not self.previous_content_refs or not self.affected_claim_refs):
            raise ValueError("news_source_update_target_missing")
        if any(change.kind == "possible_new" for change in self.changes):
            raise ValueError("news_public_possible_new_not_publishable")
        affected = set(self.affected_claim_refs)
        if not set(self.superseded_claim_refs) <= affected or not set(self.retired_claim_refs) <= affected:
            raise ValueError("news_public_scoped_refs_not_affected")
        if self.kind == "source_update" and self.superseded_claim_refs:
            raise ValueError("news_source_update_supersedes_claims")
        if self.kind == "catalyst_delta" and self.retired_claim_refs:
            raise ValueError("news_catalyst_delta_retires_claims")
        return self
