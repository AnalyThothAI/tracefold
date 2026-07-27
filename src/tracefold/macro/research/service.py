from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MACRO_RESEARCH_ARTIFACT_SCHEMA_VERSION: Literal["macro_research_artifact_v2"] = "macro_research_artifact_v2"
MACRO_RESEARCH_SCOPE_SCHEMA_VERSION: Literal["macro_research_scope_v2"] = "macro_research_scope_v2"
MACRO_RESEARCH_MAX_SEARCH_RESULTS = 50
MACRO_RESEARCH_MAX_READ_REFS = 20
MACRO_RESEARCH_MAX_CONCEPT_KEYS = 20
MACRO_RESEARCH_MAX_PRIOR_PUBLICATIONS_PER_PAGE = 5
MACRO_RESEARCH_MAX_QUERY_CHARS = 500


class ExactMacroResearchModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MacroResearchIntegrityError(ValueError):
    """Raised only when a frozen scope or artifact envelope loses integrity."""


class FrozenMacroEvidenceScope(ExactMacroResearchModel):
    schema_version: Literal["macro_research_scope_v2"] = MACRO_RESEARCH_SCOPE_SCHEMA_VERSION
    session_date: date
    market_cutoff_ms: int = Field(ge=0)
    sealed_at_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_seal(self) -> FrozenMacroEvidenceScope:
        if self.sealed_at_ms < self.market_cutoff_ms:
            raise ValueError("macro_research_scope_sealed_before_cutoff")
        return self

    @property
    def scope_id(self) -> str:
        identity = f"{self.schema_version}|{self.session_date.isoformat()}|{self.market_cutoff_ms}|{self.sealed_at_ms}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return f"macro-scope:{self.session_date.isoformat()}:{digest[:20]}"


class MacroObservationQuery(ExactMacroResearchModel):
    query: str = Field(default="", max_length=MACRO_RESEARCH_MAX_QUERY_CHARS)
    concept_keys: tuple[str, ...] = Field(
        default=(),
        max_length=MACRO_RESEARCH_MAX_CONCEPT_KEYS,
    )
    start_date: date | None = None
    end_date: date | None = None
    limit: int = Field(default=20, ge=1, le=MACRO_RESEARCH_MAX_SEARCH_RESULTS)
    offset: int = Field(default=0, ge=0)

    @field_validator("query")
    @classmethod
    def strip_query(cls, value: str) -> str:
        return value.strip()

    @field_validator("concept_keys")
    @classmethod
    def validate_concept_keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(str(item).strip() for item in value)
        if any(not item for item in normalized):
            raise ValueError("macro_research_observation_query_empty_concept")
        if len(normalized) != len(set(normalized)):
            raise ValueError("macro_research_observation_query_duplicate_concept")
        return normalized

    @model_validator(mode="after")
    def validate_dates(self) -> MacroObservationQuery:
        if self.start_date is not None and self.end_date is not None and self.start_date > self.end_date:
            raise ValueError("macro_research_observation_query_date_range")
        return self


class MacroObservationVisibility(ExactMacroResearchModel):
    available_at_ms: int = Field(ge=0)
    availability: Literal["exact_source_timestamp", "date_only_system_known"]


class MacroEvidenceCatalog(ExactMacroResearchModel):
    session_date: date
    market_cutoff_ms: int = Field(ge=0)
    sealed_at_ms: int = Field(ge=0)
    concept_keys: tuple[str, ...] = ()
    source_labels: tuple[str, ...] = ()
    observation_count: int = Field(ge=0)
    prior_research_count: int = Field(ge=0)


class MacroEvidenceRecord(ExactMacroResearchModel):
    evidence_ref: str = Field(min_length=1, max_length=500)
    evidence_kind: Literal["observation"]
    source_label: str = Field(min_length=1, max_length=500)
    concept_key: str | None = Field(default=None, max_length=500)
    source_timestamp: str | None = Field(default=None, max_length=500)
    available_at_ms: int = Field(ge=0)
    persisted_at_ms: int = Field(ge=0)
    observed_at: date | None = None
    published_at_ms: None = None
    url: str | None = Field(default=None, max_length=2_048)
    summary: str = Field(min_length=1, max_length=8_000)
    payload: dict[str, Any] = Field(default_factory=dict)
    lineage: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_source_clock_fields(self) -> MacroEvidenceRecord:
        if self.evidence_kind == "observation" and (not self.concept_key or not self.source_timestamp):
            raise ValueError("macro_research_observation_source_clock_fields_required")
        return self


class MacroPriorResearch(ExactMacroResearchModel):
    publication_ref: str = Field(min_length=1, max_length=500)
    session_date: date
    title: str = Field(min_length=1, max_length=500)
    executive_summary: str = Field(min_length=1, max_length=8_000)
    published_at_ms: int = Field(ge=0)


class MacroResearchSection(ExactMacroResearchModel):
    section_id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=500)
    body_markdown: str = Field(min_length=1, max_length=40_000)
    citation_ids: tuple[str, ...] = ()


class MacroResearchGap(ExactMacroResearchModel):
    gap_id: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=1_000)
    details: str = Field(min_length=1, max_length=8_000)
    citation_ids: tuple[str, ...] = ()


class MacroResearchCitation(ExactMacroResearchModel):
    citation_id: str = Field(min_length=1, max_length=100)
    source_type: Literal["observation", "news"]
    source_ref: str = Field(min_length=1, max_length=500)
    source_label: str = Field(min_length=1, max_length=500)
    available_at_ms: int = Field(ge=0)
    observed_at: date | None = None
    published_at_ms: int | None = Field(default=None, ge=0)
    url: str | None = Field(default=None, max_length=2_048)
    lineage: dict[str, Any] = Field(default_factory=dict)


class MacroResearchCitationSelection(ExactMacroResearchModel):
    citation_id: str = Field(min_length=1, max_length=100)
    source_ref: str = Field(min_length=1, max_length=500)


class _MacroResearchArtifactBody(ExactMacroResearchModel):
    schema_version: Literal["macro_research_artifact_v2"] = MACRO_RESEARCH_ARTIFACT_SCHEMA_VERSION
    session_date: date
    market_cutoff_ms: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=500)
    executive_summary: str = Field(min_length=1, max_length=8_000)
    sections: tuple[MacroResearchSection, ...] = Field(min_length=1)
    gaps: tuple[MacroResearchGap, ...] = ()
    reviewer_notes: tuple[str, ...] = ()

    @field_validator("reviewer_notes")
    @classmethod
    def validate_reviewer_notes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(str(item).strip() for item in value)
        if any(not item for item in normalized):
            raise ValueError("macro_research_artifact_empty_reviewer_note")
        return normalized

    def _validate_local_identity(self) -> None:
        _require_unique(
            (section.section_id for section in self.sections),
            "macro_research_artifact_duplicate_section_id",
        )
        _require_unique(
            (gap.gap_id for gap in self.gaps),
            "macro_research_artifact_duplicate_gap_id",
        )


class MacroResearchArtifactDraft(_MacroResearchArtifactBody):
    citations: tuple[MacroResearchCitationSelection, ...] = ()

    @model_validator(mode="after")
    def validate_local_identity_and_citations(self) -> MacroResearchArtifactDraft:
        self._validate_local_identity()
        _require_unique(
            (citation.citation_id for citation in self.citations),
            "macro_research_artifact_duplicate_citation_id",
        )
        citation_ids = {citation.citation_id for citation in self.citations}
        used_ids = {citation_id for section in self.sections for citation_id in section.citation_ids}
        used_ids.update(citation_id for gap in self.gaps for citation_id in gap.citation_ids)
        missing = sorted(used_ids - citation_ids)
        if missing:
            raise ValueError("macro_research_artifact_citation_closure:" + ",".join(missing))
        return self


class MacroResearchArtifact(_MacroResearchArtifactBody):
    citations: tuple[MacroResearchCitation, ...] = ()

    @model_validator(mode="after")
    def validate_local_identity_and_citations(self) -> MacroResearchArtifact:
        self._validate_local_identity()
        _require_unique(
            (citation.citation_id for citation in self.citations),
            "macro_research_artifact_duplicate_citation_id",
        )
        citation_ids = {citation.citation_id for citation in self.citations}
        used_ids = {citation_id for section in self.sections for citation_id in section.citation_ids}
        used_ids.update(citation_id for gap in self.gaps for citation_id in gap.citation_ids)
        missing = sorted(used_ids - citation_ids)
        if missing:
            raise ValueError("macro_research_artifact_citation_closure:" + ",".join(missing))
        return self

    @property
    def source_refs(self) -> frozenset[str]:
        return frozenset(citation.source_ref for citation in self.citations)


class MacroResearchAudit(ExactMacroResearchModel):
    scope_id: str
    deepagents_version: str
    model_name: str
    prompt_version: str
    workflow_version: str
    model_calls: int = Field(ge=0)
    tool_calls: tuple[str, ...] = ()
    subagents: tuple[str, ...] = ()
    verified_source_refs: tuple[str, ...] = ()


class MacroResearchAgentResult(ExactMacroResearchModel):
    artifact: MacroResearchArtifact
    audit: MacroResearchAudit

    @property
    def report_markdown(self) -> str:
        return render_macro_research_markdown(self.artifact)

    @property
    def model_name(self) -> str:
        return self.audit.model_name

    @property
    def prompt_version(self) -> str:
        return self.audit.prompt_version

    @property
    def workflow_version(self) -> str:
        return self.audit.workflow_version


class MacroResearchReadPort(Protocol):
    def catalog(self, *, scope: FrozenMacroEvidenceScope) -> MacroEvidenceCatalog: ...

    def search_observations(
        self,
        *,
        scope: FrozenMacroEvidenceScope,
        query: MacroObservationQuery,
    ) -> Sequence[MacroEvidenceRecord]: ...

    def read_evidence(
        self,
        *,
        scope: FrozenMacroEvidenceScope,
        source_refs: tuple[str, ...],
    ) -> Sequence[MacroEvidenceRecord]: ...

    def prior_research(
        self,
        *,
        scope: FrozenMacroEvidenceScope,
        limit: int,
        offset: int,
    ) -> Sequence[MacroPriorResearch]: ...


class MacroResearchAgent(Protocol):
    async def analyze(
        self,
        scope: FrozenMacroEvidenceScope,
    ) -> MacroResearchAgentResult: ...


def resolve_observation_visibility(
    scope: FrozenMacroEvidenceScope,
    *,
    source_timestamp: str,
    ingested_at_ms: int,
) -> MacroObservationVisibility | None:
    """Resolve point-in-time visibility from source or system-known clocks."""

    if ingested_at_ms < 0 or ingested_at_ms > scope.sealed_at_ms:
        return None
    normalized_timestamp = str(source_timestamp or "").strip()
    exact_ms = _parse_exact_timestamp_ms(normalized_timestamp)
    if exact_ms is not None:
        if exact_ms > scope.market_cutoff_ms:
            return None
        return MacroObservationVisibility(
            available_at_ms=exact_ms,
            availability="exact_source_timestamp",
        )
    source_date = _parse_date_only(normalized_timestamp)
    if source_date is None:
        return None
    if ingested_at_ms > scope.market_cutoff_ms:
        return None
    return MacroObservationVisibility(
        available_at_ms=ingested_at_ms,
        availability="date_only_system_known",
    )


def require_catalog_in_scope(
    scope: FrozenMacroEvidenceScope,
    catalog: MacroEvidenceCatalog,
) -> MacroEvidenceCatalog:
    if catalog.session_date != scope.session_date:
        raise MacroResearchIntegrityError("macro_research_catalog_session_mismatch")
    if catalog.market_cutoff_ms != scope.market_cutoff_ms:
        raise MacroResearchIntegrityError("macro_research_catalog_cutoff_mismatch")
    if catalog.sealed_at_ms != scope.sealed_at_ms:
        raise MacroResearchIntegrityError("macro_research_catalog_seal_mismatch")
    return catalog


def require_evidence_in_scope(
    scope: FrozenMacroEvidenceScope,
    records: Sequence[MacroEvidenceRecord],
) -> tuple[MacroEvidenceRecord, ...]:
    resolved = tuple(records)
    refs = [record.evidence_ref for record in resolved]
    if len(refs) != len(set(refs)):
        raise MacroResearchIntegrityError("macro_research_duplicate_evidence_ref")
    for record in resolved:
        if record.available_at_ms > scope.market_cutoff_ms:
            raise MacroResearchIntegrityError(f"macro_research_future_evidence:{record.evidence_ref}")
        if record.published_at_ms is not None and record.published_at_ms > scope.market_cutoff_ms:
            raise MacroResearchIntegrityError(f"macro_research_future_evidence:{record.evidence_ref}")
        if record.persisted_at_ms > scope.sealed_at_ms:
            raise MacroResearchIntegrityError(f"macro_research_post_seal_evidence:{record.evidence_ref}")
        visibility = resolve_observation_visibility(
            scope,
            source_timestamp=str(record.source_timestamp),
            ingested_at_ms=record.persisted_at_ms,
        )
        if visibility is None:
            raise MacroResearchIntegrityError(f"macro_research_observation_not_visible:{record.evidence_ref}")
        if visibility.available_at_ms != record.available_at_ms:
            raise MacroResearchIntegrityError("macro_research_observation_availability_mismatch:" + record.evidence_ref)
    return resolved


def require_prior_research_in_scope(
    scope: FrozenMacroEvidenceScope,
    records: Sequence[MacroPriorResearch],
) -> tuple[MacroPriorResearch, ...]:
    resolved = tuple(records)
    refs = [record.publication_ref for record in resolved]
    if len(refs) != len(set(refs)):
        raise MacroResearchIntegrityError("macro_research_duplicate_prior_publication")
    for record in resolved:
        if record.session_date >= scope.session_date:
            raise MacroResearchIntegrityError(f"macro_research_non_prior_publication:{record.publication_ref}")
        if record.published_at_ms > scope.sealed_at_ms:
            raise MacroResearchIntegrityError(f"macro_research_post_seal_publication:{record.publication_ref}")
    return resolved


def canonicalize_macro_research_artifact(
    draft: MacroResearchArtifactDraft,
    *,
    disclosed_evidence: Mapping[str, MacroEvidenceRecord],
) -> MacroResearchArtifact:
    citations: list[MacroResearchCitation] = []
    missing: list[str] = []
    for selection in draft.citations:
        record = disclosed_evidence.get(selection.source_ref)
        if record is None:
            missing.append(selection.source_ref)
            continue
        citations.append(
            MacroResearchCitation(
                citation_id=selection.citation_id,
                source_type=record.evidence_kind,
                source_ref=record.evidence_ref,
                source_label=record.source_label,
                available_at_ms=record.available_at_ms,
                observed_at=record.observed_at,
                published_at_ms=record.published_at_ms,
                url=record.url,
                lineage=dict(record.lineage),
            )
        )
    if missing:
        raise MacroResearchIntegrityError("macro_research_artifact_unknown_citations:" + ",".join(sorted(missing)))
    payload = draft.model_dump(mode="python", exclude={"citations"})
    return MacroResearchArtifact.model_validate(
        {
            **payload,
            "citations": [citation.model_dump(mode="python") for citation in citations],
        }
    )


def render_macro_research_markdown(artifact: MacroResearchArtifact) -> str:
    """Flatten the one authoritative ordered-section artifact for export."""

    blocks = [
        f"# {artifact.title}",
        artifact.executive_summary.strip(),
    ]
    for section in artifact.sections:
        blocks.extend(
            (
                f"## {section.title}",
                section.body_markdown.strip(),
            )
        )
        if section.citation_ids:
            blocks.append("引用：" + " ".join(f"[{citation_id}]" for citation_id in section.citation_ids))
    return "\n\n".join(block for block in blocks if block).strip()


def require_artifact_integrity(
    artifact: MacroResearchArtifact,
    *,
    scope: FrozenMacroEvidenceScope,
    verified_evidence_refs: set[str] | frozenset[str],
) -> MacroResearchArtifact:
    if artifact.session_date != scope.session_date:
        raise MacroResearchIntegrityError("macro_research_artifact_session_mismatch")
    if artifact.market_cutoff_ms != scope.market_cutoff_ms:
        raise MacroResearchIntegrityError("macro_research_artifact_cutoff_mismatch")
    missing = sorted(artifact.source_refs - frozenset(verified_evidence_refs))
    if missing:
        raise MacroResearchIntegrityError("macro_research_artifact_unknown_citations:" + ",".join(missing))
    return artifact


def _parse_exact_timestamp_ms(raw: str) -> int | None:
    if "T" not in raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return int(parsed.astimezone(UTC).timestamp() * 1_000)


def _parse_date_only(raw: str) -> date | None:
    if len(raw) != 10:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _require_unique(values: Iterable[str], error: str) -> None:
    resolved = tuple(values)
    if len(resolved) != len(set(resolved)):
        raise ValueError(error)


__all__ = [
    "MACRO_RESEARCH_ARTIFACT_SCHEMA_VERSION",
    "MACRO_RESEARCH_MAX_CONCEPT_KEYS",
    "MACRO_RESEARCH_MAX_PRIOR_PUBLICATIONS_PER_PAGE",
    "MACRO_RESEARCH_MAX_QUERY_CHARS",
    "MACRO_RESEARCH_MAX_READ_REFS",
    "MACRO_RESEARCH_MAX_SEARCH_RESULTS",
    "MACRO_RESEARCH_SCOPE_SCHEMA_VERSION",
    "FrozenMacroEvidenceScope",
    "MacroEvidenceCatalog",
    "MacroEvidenceRecord",
    "MacroObservationQuery",
    "MacroObservationVisibility",
    "MacroPriorResearch",
    "MacroResearchAgent",
    "MacroResearchAgentResult",
    "MacroResearchArtifact",
    "MacroResearchArtifactDraft",
    "MacroResearchAudit",
    "MacroResearchCitation",
    "MacroResearchCitationSelection",
    "MacroResearchGap",
    "MacroResearchIntegrityError",
    "MacroResearchReadPort",
    "MacroResearchSection",
    "canonicalize_macro_research_artifact",
    "render_macro_research_markdown",
    "require_artifact_integrity",
    "require_catalog_in_scope",
    "require_evidence_in_scope",
    "require_prior_research_in_scope",
    "resolve_observation_visibility",
]
