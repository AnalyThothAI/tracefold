from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NewsTrustTier = Literal["authoritative", "trusted", "standard", "low"]
NewsSourceRole = Literal[
    "original_publisher",
    "wire_service",
    "official_authority",
    "trusted_aggregator",
]
ContentForm = Literal["report", "analysis", "opinion", "live", "static", "unknown"]
OriginRelation = Literal[
    "originating",
    "independent",
    "syndicated",
    "derived",
    "unresolved",
]
DevelopmentRelation = Literal[
    "initial",
    "follow_up",
    "correction",
    "background",
    "retrospective",
]
EpistemicUse = Literal["fact_evidence", "context", "viewpoint", "non_evidence"]
EvidencePosture = Literal[
    "single_origin_reported",
    "independently_corroborated",
    "primary_source_confirmed",
    "contested",
    "corrected",
    "withdrawn",
]
StoryLifecycle = Literal[
    "emerging",
    "developing",
    "stable",
    "fading",
    "dormant",
    "reactivated",
]
StoryIdentityVerdict = Literal[
    "accept_strong",
    "accept_scored",
    "reject_conflict",
    "ambiguous_new_story",
    "no_candidate_new_story",
    "revision_compatible",
    "revision_identity_ambiguous",
]
NewsAiStatus = Literal["pending", "available", "failed", "unavailable", "insufficient"]
NewsPageFetchStatus = Literal[
    "available",
    "failed",
    "paywalled",
    "robots_denied",
    "unsupported_content",
    "truncated",
]

SOURCE_REGISTRY_VERSION = "news_source_registry_v2"
ARTICLE_IDENTITY_VERSION = "news_article_identity_v2"
STORY_IDENTITY_VERSION = "news_story_identity_v2"
STORY_LIFECYCLE_VERSION = "news_story_lifecycle_v2"
STORY_SCORING_VERSION = "news_story_scoring_v2"
STORY_MEMBER_SEMANTICS_VERSION = "news_story_member_semantics_v2"
BRIEF_GROUPING_VERSION = "news_brief_grouping_v1"
BRIEF_SELECTION_VERSION = "news_brief_selection_v1"
BRIEF_PROMPT_VERSION = "news_global_brief_v2"
BRIEF_WORKFLOW_VERSION = "news_global_brief_workflow_v1"
BRIEF_SCHEMA_VERSION = "news_global_brief_schema_v1"
STORY_ANALYSIS_PROMPT_VERSION = "news_story_analysis_v5"
STORY_ANALYSIS_WORKFLOW_VERSION = "news_story_analysis_workflow_v3"
STORY_ANALYSIS_SCHEMA_VERSION = "news_story_analysis_schema_v2"
NEWS_LOCALE = "zh-CN"


class ExactNewsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewsPublicationContract(ExactNewsModel):
    model: str
    prompt_version: str
    workflow_version: str
    schema_version: str
    locale: str = NEWS_LOCALE

    @field_validator(
        "model",
        "prompt_version",
        "workflow_version",
        "schema_version",
        "locale",
        mode="before",
    )
    @classmethod
    def require_contract_text(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("news_publication_contract_text_required")
        return normalized


def brief_publication_contract(model: str) -> NewsPublicationContract:
    return NewsPublicationContract(
        model=model,
        prompt_version=BRIEF_PROMPT_VERSION,
        workflow_version=BRIEF_WORKFLOW_VERSION,
        schema_version=BRIEF_SCHEMA_VERSION,
    )


def story_analysis_contract(model: str) -> NewsPublicationContract:
    return NewsPublicationContract(
        model=model,
        prompt_version=STORY_ANALYSIS_PROMPT_VERSION,
        workflow_version=STORY_ANALYSIS_WORKFLOW_VERSION,
        schema_version=STORY_ANALYSIS_SCHEMA_VERSION,
    )


class NewsSourceDefinition(ExactNewsModel):
    source_id: str
    name: str
    feed_url: str
    source_domain: str
    source_role: NewsSourceRole
    trust_tier: NewsTrustTier
    source_chain_id: str
    publisher_organization_id: str | None = None
    parent_organization_id: str | None = None
    canonical_domains: tuple[str, ...] = ()
    known_relationships: tuple[dict[str, Any], ...] = ()
    source_quality_factors: dict[str, Any] = Field(default_factory=dict)
    coverage_tags: tuple[str, ...] = ()
    default_language: str = "en"
    enabled: bool = True
    refresh_interval_seconds: int = Field(default=300, ge=1)

    @field_validator(
        "source_id",
        "name",
        "feed_url",
        "source_domain",
        "source_chain_id",
        "default_language",
        mode="before",
    )
    @classmethod
    def require_text(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("news_source_text_required")
        return normalized

    @field_validator("publisher_organization_id", "parent_organization_id", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @field_validator("source_domain", "default_language", mode="after")
    @classmethod
    def normalize_lower_text(cls, value: str) -> str:
        return value.lower()

    @model_validator(mode="after")
    def derive_registry_defaults(self) -> NewsSourceDefinition:
        if self.publisher_organization_id is None:
            self.publisher_organization_id = self.source_chain_id
        if not self.canonical_domains:
            self.canonical_domains = (self.source_domain,)
        if not self.source_quality_factors:
            self.source_quality_factors = {"trust_tier": self.trust_tier}
        return self


class NewsFeedEntry(ExactNewsModel):
    guid: str | None = None
    link: str | None = None
    title: str | None = None
    summary: str = ""
    published_at_ms: int | None = Field(default=None, ge=0)
    language: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("guid", "link", "title", "language", mode="before")
    @classmethod
    def normalize_optional_entry_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None


class NewsFeedFetch(ExactNewsModel):
    status_code: int = Field(ge=100, le=599)
    entries: tuple[NewsFeedEntry, ...] = ()
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False


class NewsPageFetch(ExactNewsModel):
    status: NewsPageFetchStatus
    fetched_at_ms: int = Field(ge=0)
    http_status: int | None = Field(default=None, ge=100, le=599)
    content_type: str | None = None
    content_hash: str | None = None
    extracted_text: str | None = None
    byte_count: int | None = Field(default=None, ge=0)
    failure_reason: str | None = None
    final_url: str


class AdmittedFeedObservation(ExactNewsModel):
    observation_id: str
    source_id: str
    source_entry_key: str
    observation_revision_hash: str
    source_guid: str | None
    raw_url: str
    normalized_url: str
    title: str
    summary: str
    source_published_at_ms: int
    observed_at_ms: int
    language: str
    raw_entry: dict[str, Any]


class ArticleIdentityFeatures(ExactNewsModel):
    revision_id: str
    article_id: str
    identity_version: str = ARTICLE_IDENTITY_VERSION
    language: str
    normalized_title: str
    normalized_lead: str
    content_fingerprint: str
    lexical_signature: str
    event_key: str
    named_event_keys: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()
    actor_entities: tuple[str, ...] = ()
    target_entities: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    event_objects: tuple[str, ...] = ()
    locations: tuple[str, ...] = ()
    stages: tuple[str, ...] = ()
    quantities: tuple[dict[str, Any], ...] = ()
    tokens: tuple[str, ...] = ()
    bigrams: tuple[str, ...] = ()
    chargrams: tuple[str, ...] = ()
    extraction_receipt: dict[str, Any] = Field(default_factory=dict)
    feature_hash: str

    def storage_features(self) -> dict[str, Any]:
        return {
            "entities": list(self.entities),
            "actor_entities": list(self.actor_entities),
            "target_entities": list(self.target_entities),
            "actions": list(self.actions),
            "event_objects": list(self.event_objects),
            "locations": list(self.locations),
            "stages": list(self.stages),
            "quantities": list(self.quantities),
            "tokens": list(self.tokens),
            "bigrams": list(self.bigrams),
            "chargrams": list(self.chargrams),
        }


class StoryCandidate(ExactNewsModel):
    story_id: str
    channel_hits: tuple[str, ...]
    member_score: float = Field(ge=0, le=1)
    core_score: float = Field(ge=0, le=1)
    final_score: float = Field(ge=0, le=1)
    hard_conflicts: tuple[str, ...] = ()
    strong_proofs: tuple[str, ...] = ()
    reason: dict[str, Any] = Field(default_factory=dict)


class StoryIdentityDecision(ExactNewsModel):
    verdict: StoryIdentityVerdict
    selected_story_id: str | None = None
    match_method: str
    match_score: float = Field(ge=0, le=1)
    runner_up_margin: float = Field(ge=0, le=1)
    candidates: tuple[StoryCandidate, ...] = ()
    reason: dict[str, Any] = Field(default_factory=dict)


class MemberSemantics(ExactNewsModel):
    content_form: ContentForm
    origin_relation: OriginRelation
    development_relation: DevelopmentRelation
    epistemic_use: EpistemicUse
    reporting_origin_id: str | None
    origin_confidence: float = Field(ge=0, le=1)
    reason: dict[str, Any] = Field(default_factory=dict)


class StoryAnalysisEvidence(ExactNewsModel):
    story_id: str
    material_evidence_hash: str
    title: str
    snippet: str
    event_core: dict[str, Any]
    evidence_posture: EvidencePosture
    evidence_factors: dict[str, Any]
    impact_profile: dict[str, Any]
    material_change: str
    articles: tuple[dict[str, Any], ...]


class BriefEvidenceBundle(ExactNewsModel):
    selection_snapshot_id: str
    selection_fingerprint: str
    evidence_bundle_hash: str
    cutoff_at_ms: int
    stories: tuple[dict[str, Any], ...]
    narrative_groups: tuple[dict[str, Any], ...]
    selection_policy_version: str


class EvidenceBackedFact(ExactNewsModel):
    text: str
    evidence_references: tuple[str, ...] = Field(min_length=1)

    @field_validator("text", mode="before")
    @classmethod
    def require_fact_text(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("news_fact_text_required")
        return normalized

    @field_validator("evidence_references", mode="after")
    @classmethod
    def normalize_fact_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(item.strip() for item in value if item.strip()))
        if not normalized:
            raise ValueError("news_fact_evidence_references_required")
        return normalized


class ConditionalTransmission(ExactNewsModel):
    condition: str
    mechanism: str
    possible_effect: str
    confidence: Literal["low", "medium", "high"]

    @field_validator("condition", "mechanism", "possible_effect", mode="before")
    @classmethod
    def require_transmission_text(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("news_transmission_text_required")
        return normalized


class StoryAnalysisDraft(ExactNewsModel):
    what_happened: tuple[EvidenceBackedFact, ...] = Field(min_length=1)
    why_it_matters: str
    political_impact: str
    economic_market_impact: str
    disagreements_unknowns: tuple[str, ...] = ()
    transmission_scenarios: tuple[ConditionalTransmission, ...] = ()
    next_checkpoint: str

    @field_validator(
        "why_it_matters",
        "political_impact",
        "economic_market_impact",
        "next_checkpoint",
        mode="before",
    )
    @classmethod
    def require_analysis_text(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("news_story_analysis_text_required")
        return normalized


class BriefItemDraft(ExactNewsModel):
    story_id: str
    what_happened: tuple[EvidenceBackedFact, ...] = Field(min_length=1)
    why_it_matters: str
    transmission_scenarios: tuple[ConditionalTransmission, ...] = ()
    uncertainties: tuple[str, ...] = ()
    watchpoints: tuple[str, ...] = ()

    @field_validator(
        "story_id",
        "why_it_matters",
        mode="before",
    )
    @classmethod
    def require_brief_item_text(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("news_brief_item_text_required")
        return normalized


class GlobalBriefDraft(ExactNewsModel):
    headline: str
    executive_summary: str
    items: tuple[BriefItemDraft, ...]
    narratives: tuple[str, ...] = ()
    global_watchpoints: tuple[str, ...] = ()

    @field_validator("headline", "executive_summary", mode="before")
    @classmethod
    def require_brief_text(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("news_brief_text_required")
        return normalized


class AiPublicationResult(ExactNewsModel):
    payload: dict[str, Any]
    receipt: dict[str, Any] = Field(default_factory=dict)


class NewsAiPublisher:
    async def synthesize_brief(self, evidence: BriefEvidenceBundle) -> AiPublicationResult:
        raise NotImplementedError

    async def analyze_story(self, evidence: StoryAnalysisEvidence) -> AiPublicationResult:
        raise NotImplementedError

    async def repair(
        self,
        *,
        publication_kind: Literal["brief", "story_analysis"],
        evidence: BriefEvidenceBundle | StoryAnalysisEvidence,
        validation_errors: tuple[str, ...],
    ) -> AiPublicationResult:
        raise NotImplementedError


class NewsFeedReader:
    def fetch(
        self,
        *,
        source: NewsSourceDefinition,
        etag: str | None,
        last_modified: str | None,
    ) -> NewsFeedFetch:
        raise NotImplementedError

    def close(self) -> None:
        return None


class NewsPageReader:
    extractor_version: str

    def fetch(self, *, url: str) -> NewsPageFetch:
        raise NotImplementedError

    def close(self) -> None:
        return None


def source_definition(
    value: NewsSourceDefinition | Mapping[str, Any] | Any,
) -> NewsSourceDefinition:
    if isinstance(value, NewsSourceDefinition):
        return value
    if isinstance(value, Mapping):
        return NewsSourceDefinition.model_validate(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return NewsSourceDefinition.model_validate(model_dump())
    raise TypeError("news_source_definition_required")


__all__ = [
    "ARTICLE_IDENTITY_VERSION",
    "BRIEF_GROUPING_VERSION",
    "BRIEF_PROMPT_VERSION",
    "BRIEF_SCHEMA_VERSION",
    "BRIEF_SELECTION_VERSION",
    "BRIEF_WORKFLOW_VERSION",
    "NEWS_LOCALE",
    "SOURCE_REGISTRY_VERSION",
    "STORY_ANALYSIS_PROMPT_VERSION",
    "STORY_ANALYSIS_SCHEMA_VERSION",
    "STORY_ANALYSIS_WORKFLOW_VERSION",
    "STORY_IDENTITY_VERSION",
    "STORY_LIFECYCLE_VERSION",
    "STORY_MEMBER_SEMANTICS_VERSION",
    "STORY_SCORING_VERSION",
    "AdmittedFeedObservation",
    "AiPublicationResult",
    "ArticleIdentityFeatures",
    "BriefEvidenceBundle",
    "BriefItemDraft",
    "ContentForm",
    "DevelopmentRelation",
    "EpistemicUse",
    "EvidencePosture",
    "GlobalBriefDraft",
    "MemberSemantics",
    "NewsAiPublisher",
    "NewsAiStatus",
    "NewsFeedEntry",
    "NewsFeedFetch",
    "NewsFeedReader",
    "NewsPublicationContract",
    "NewsSourceDefinition",
    "NewsSourceRole",
    "NewsTrustTier",
    "OriginRelation",
    "StoryAnalysisDraft",
    "StoryAnalysisEvidence",
    "StoryCandidate",
    "StoryIdentityDecision",
    "StoryIdentityVerdict",
    "StoryLifecycle",
    "brief_publication_contract",
    "source_definition",
    "story_analysis_contract",
]
