from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

NewsTrustTier = Literal["authoritative", "trusted", "standard", "low"]
NewsSourceRole = Literal["original_publisher", "trusted_aggregator"]
NewsProvenanceStatus = Literal["verified", "attributed", "unknown"]
NewsVerificationStatus = Literal["corroborated", "trusted", "attributed", "unverified"]
NewsStoryPhase = Literal["breaking", "developing", "sustained", "fading"]
NewsAnalysisStatus = Literal["pending", "available", "failed", "unavailable"]

ARTICLE_IDENTITY_VERSION = "news_article_identity_v1"
STORY_IDENTITY_VERSION = "news_story_identity_v1"
STORY_LIFECYCLE_VERSION = "news_story_lifecycle_v1"
STORY_IMPORTANCE_VERSION = "news_story_importance_v1"
NEWS_ANALYSIS_PROMPT_VERSION = "news_story_analysis_v2"
NEWS_ANALYSIS_WORKFLOW_VERSION = "news_story_analysis_workflow_v2"
NEWS_ANALYSIS_SCHEMA_VERSION = "news_story_analysis_schema_v1"


class ExactNewsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewsAnalysisContract(ExactNewsModel):
    model: str
    prompt_version: str = NEWS_ANALYSIS_PROMPT_VERSION
    workflow_version: str = NEWS_ANALYSIS_WORKFLOW_VERSION
    schema_version: str = NEWS_ANALYSIS_SCHEMA_VERSION

    @field_validator("model", "prompt_version", "workflow_version", "schema_version", mode="before")
    @classmethod
    def require_contract_text(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("news_analysis_contract_text_required")
        return normalized


class NewsSourceDefinition(ExactNewsModel):
    source_id: str
    name: str
    feed_url: str
    source_domain: str
    source_role: NewsSourceRole
    trust_tier: NewsTrustTier
    source_chain_id: str
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

    @field_validator("source_domain", "default_language", mode="after")
    @classmethod
    def normalize_lower_text(cls, value: str) -> str:
        return value.lower()


class NewsFeedEntry(ExactNewsModel):
    guid: str | None = None
    link: str | None = None
    title: str
    summary: str = ""
    published_at_ms: int | None = Field(default=None, ge=0)
    language: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title", mode="before")
    @classmethod
    def require_title(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("news_feed_entry_title_required")
        return normalized

    @field_validator("guid", "link", "language", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: Any) -> str | None:
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


class NewsArticleFact(ExactNewsModel):
    article_id: str
    source_id: str
    identity_version: str = ARTICLE_IDENTITY_VERSION
    identity_method: Literal["canonical_url", "source_guid", "title_time_bucket"]
    identity_key: str
    source_guid: str | None
    canonical_url: str | None
    title: str
    snippet: str
    published_at_ms: int
    first_seen_at_ms: int
    last_seen_at_ms: int
    language: str
    origin_url: str | None
    origin_domain: str | None
    origin_name: str | None
    provenance_status: NewsProvenanceStatus
    content_hash: str
    source_entry: dict[str, Any]


class StoryMatch(ExactNewsModel):
    story_id: str
    match_method: str
    match_score: float = Field(ge=0, le=1)
    reason: dict[str, Any]


class NewsAnalysisEvidence(ExactNewsModel):
    story_id: str
    evidence_set_hash: str
    title: str
    snippet: str
    verification_status: NewsVerificationStatus
    phase: NewsStoryPhase
    importance_score: int = Field(ge=0, le=100)
    source_count: int = Field(ge=0)
    article_count: int = Field(ge=0)
    trusted_source_count: int = Field(ge=0)
    independent_origin_count: int = Field(ge=0)
    articles: tuple[dict[str, Any], ...]


class NewsStoryAnalysisDraft(ExactNewsModel):
    what_happened: str
    why_it_matters: str
    political_impact: str
    economic_market_impact: str
    confirmed_facts: tuple[str, ...] = ()
    disagreements_unknowns: tuple[str, ...] = ()
    next_checkpoint: str
    evidence_references: tuple[str, ...]

    @field_validator(
        "what_happened",
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

    @field_validator("evidence_references", mode="after")
    @classmethod
    def require_evidence_references(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
        if not normalized:
            raise ValueError("news_story_analysis_evidence_references_required")
        return normalized


class NewsStoryAnalysisResult(ExactNewsModel):
    draft: NewsStoryAnalysisDraft
    receipt: dict[str, Any] = Field(default_factory=dict)


class NewsStoryAnalyzer:
    async def analyze(self, evidence: NewsAnalysisEvidence) -> NewsStoryAnalysisResult:
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


def source_definition(value: NewsSourceDefinition | Mapping[str, Any] | Any) -> NewsSourceDefinition:
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
    "NEWS_ANALYSIS_PROMPT_VERSION",
    "NEWS_ANALYSIS_SCHEMA_VERSION",
    "NEWS_ANALYSIS_WORKFLOW_VERSION",
    "STORY_IDENTITY_VERSION",
    "STORY_IMPORTANCE_VERSION",
    "STORY_LIFECYCLE_VERSION",
    "NewsAnalysisContract",
    "NewsAnalysisEvidence",
    "NewsAnalysisStatus",
    "NewsArticleFact",
    "NewsFeedEntry",
    "NewsFeedFetch",
    "NewsFeedReader",
    "NewsProvenanceStatus",
    "NewsSourceDefinition",
    "NewsSourceRole",
    "NewsStoryAnalysisDraft",
    "NewsStoryAnalysisResult",
    "NewsStoryAnalyzer",
    "NewsStoryPhase",
    "NewsTrustTier",
    "NewsVerificationStatus",
    "StoryMatch",
    "source_definition",
]
