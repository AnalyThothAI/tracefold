from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Protocol, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

STORY_COMPARISON_VERSION = "news_story_comparison_v2"
STORY_FEATURE_VERSION = "news_story_features_v2"
STORY_GROUNDED_PROVIDER_VERSION = "news_story_grounded_provider_v2"
STORY_EVENT_POLICY_VERSION = "news_story_event_policy_v2"
STORY_JACCARD_VERSION = "news_story_jaccard_v2"
STORY_CLUSTERING_VERSION = "news_story_fixed_anchor_v2"
STORY_IDENTITY_VERSION = "news_story_identity_v2"
STORY_SELECTOR_VERSION = "news_story_public_selector_v2"
STORY_PROJECTION_VERSION = "news_story_projection_v2"
CLASSIFIER_VERSION = "worldmonitor_keyword_classifier_0e8785c"
IMPORTANCE_VERSION = "worldmonitor_importance_0e8785c_reporting_origin"
BRIEF_PROMPT_VERSION = "worldmonitor_public_insights_prompt_0e8785c"
BRIEF_WORKFLOW_VERSION = "worldmonitor_public_insights_workflow_0e8785c"
BRIEF_COMPOSER_VERSION = "worldmonitor_public_insights_composer_0e8785c"
BRIEF_SCHEMA_VERSION = "worldmonitor_public_insights_schema_v3"
NEWS_LOCALE = "en"

INSIGHTS_SYNTHESIS_PARSE = "INSIGHTS_SYNTHESIS_PARSE"
INSIGHTS_SYNTHESIS_GATE = "INSIGHTS_SYNTHESIS_GATE"
INSIGHTS_SYNTHESIS_MISSING_CLUSTER = "INSIGHTS_SYNTHESIS_MISSING_CLUSTER"
INSIGHTS_SYNTHESIS_PROVIDER = "INSIGHTS_SYNTHESIS_PROVIDER"

ThreatLevel = Literal["critical", "high", "medium", "low", "info"]
EventCategory = Literal[
    "conflict",
    "protest",
    "disaster",
    "diplomatic",
    "economic",
    "terrorism",
    "cyber",
    "health",
    "environmental",
    "military",
    "crime",
    "infrastructure",
    "tech",
    "general",
]
PublicInsightsThreatLevel = Literal["critical", "high", "elevated", "moderate"]
PublicInsightsCategory = Literal[
    "conflict",
    "violence",
    "unrest",
    "geopolitical",
    "crisis",
    "natural_disaster",
    "political",
    "economic",
    "general",
]


class ExactNewsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewsFeedExpectedError(RuntimeError):
    """A bounded public-feed failure safe for durable source retry."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


class NewsSourceDefinition(ExactNewsModel):
    source_id: str
    name: str
    tier: int = Field(ge=1, le=4)
    lang: str = "en"
    source_kind: Literal["rss", "opennews"]
    enabled: bool = True
    feed_url: str | None = None
    memberships: tuple[str, ...] = ()
    refresh_interval_seconds: int = Field(default=1800, ge=1)

    @field_validator(
        "source_id",
        "name",
        "lang",
        mode="before",
    )
    @classmethod
    def require_text(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("news_source_text_required")
        return normalized

    @field_validator("feed_url", mode="before")
    @classmethod
    def normalize_feed_url(cls, value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @field_validator("memberships", mode="before")
    @classmethod
    def normalize_memberships(cls, value: Any) -> tuple[str, ...]:
        if not isinstance(value, list | tuple):
            raise ValueError("news_source_memberships_invalid")
        memberships = tuple(str(item or "").strip().lower() for item in value)
        if any(not item for item in memberships) or len(set(memberships)) != len(memberships):
            raise ValueError("news_source_memberships_invalid")
        return memberships

    @model_validator(mode="after")
    def enforce_source_shape(self) -> Self:
        if self.source_kind == "opennews":
            if self.feed_url is not None or self.memberships:
                raise ValueError("opennews_source_shape_invalid")
            return self
        parsed = urlsplit(str(self.feed_url or "").strip())
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("news_rss_feed_url_invalid")
        if not self.memberships:
            raise ValueError("news_rss_memberships_required")
        return self


class NewsFeedEntry(ExactNewsModel):
    guid: str | None = None
    link: str | None = None
    title: str | None = None
    description: str = ""
    published_at_ms: int | None = None
    language: str | None = None
    reporting_origin: str | None = None


class NewsFeedFetch(ExactNewsModel):
    status_code: int
    entries: tuple[NewsFeedEntry, ...] = ()
    entries_seen: int = Field(default=0, ge=0)
    gate_counts: dict[str, int] = Field(default_factory=dict)
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False


class NewsFeedReader(Protocol):
    def fetch_wire(
        self,
        *,
        source: NewsSourceDefinition,
        etag: str | None,
        last_modified: str | None,
    ) -> object: ...

    def close(self) -> None: ...


class NewsClassification(ExactNewsModel):
    level: ThreatLevel
    category: EventCategory
    confidence: float = Field(ge=0, le=1)
    source: Literal["keyword", "keyword-historical-downgrade"]


class NewsBriefStory(ExactNewsModel):
    story_id: str
    primary_title: str
    primary_source: str
    primary_link: str | None
    primary_published_at_ms: int
    source_count: int = Field(ge=1)
    unique_source_count: int = Field(ge=1)
    sources: tuple[str, ...]
    last_updated_ms: int
    member_titles: tuple[str, ...]
    source_tier: int = Field(ge=1, le=4)
    upstream_importance_score: float
    entity_corroboration: bool
    corroboration_source_count: int = Field(ge=0)
    importance_score: float
    effective_importance_score: float
    is_alert: bool
    threat_level: PublicInsightsThreatLevel
    category: PublicInsightsCategory


class NewsBriefStoryLine(ExactNewsModel):
    n: int = Field(ge=1)
    text: str


class NewsBriefSource(ExactNewsModel):
    title: str
    source: str
    url: str
    published_at_ms: int | None = None


class NewsBriefSynthesisResult(ExactNewsModel):
    brief_kind: Literal["l1", "l2", "none"]
    quality: Literal["ok", "degraded"]
    world_brief: str
    brief_story_lines: tuple[NewsBriefStoryLine, ...]
    sources: tuple[NewsBriefSource, ...]
    provider: str
    model: str
    validation: dict[str, Any]

    @model_validator(mode="after")
    def enforce_kind_shape(self) -> Self:
        if self.brief_kind == "l1":
            if (
                self.quality != "ok"
                or not self.world_brief.strip()
                or not self.provider.strip()
                or not self.model.strip()
                or not self.brief_story_lines
                or len(self.brief_story_lines) != len(self.sources)
            ):
                raise ValueError("news_brief_l1_shape_invalid")
        elif self.brief_kind == "l2":
            if (
                self.quality != "degraded"
                or not self.world_brief.strip()
                or not self.provider.strip()
                or not self.model.strip()
                or self.brief_story_lines
                or len(self.sources) > 1
            ):
                raise ValueError("news_brief_l2_shape_invalid")
        elif (
            self.quality != "degraded"
            or self.world_brief
            or self.provider
            or self.model
            or self.brief_story_lines
            or len(self.sources) > 1
        ):
            raise ValueError("news_brief_none_shape_invalid")
        return self


class NewsBriefPublisher(Protocol):
    def publish(self, stories: Sequence[NewsBriefStory]) -> NewsBriefSynthesisResult: ...

    def close(self) -> None: ...


__all__ = [
    "BRIEF_COMPOSER_VERSION",
    "BRIEF_PROMPT_VERSION",
    "BRIEF_SCHEMA_VERSION",
    "BRIEF_WORKFLOW_VERSION",
    "CLASSIFIER_VERSION",
    "IMPORTANCE_VERSION",
    "INSIGHTS_SYNTHESIS_GATE",
    "INSIGHTS_SYNTHESIS_MISSING_CLUSTER",
    "INSIGHTS_SYNTHESIS_PARSE",
    "INSIGHTS_SYNTHESIS_PROVIDER",
    "NEWS_LOCALE",
    "STORY_IDENTITY_VERSION",
    "EventCategory",
    "NewsBriefPublisher",
    "NewsBriefSource",
    "NewsBriefStory",
    "NewsBriefStoryLine",
    "NewsBriefSynthesisResult",
    "NewsClassification",
    "NewsFeedEntry",
    "NewsFeedExpectedError",
    "NewsFeedFetch",
    "NewsFeedReader",
    "NewsSourceDefinition",
    "PublicInsightsCategory",
    "PublicInsightsThreatLevel",
    "ThreatLevel",
]
