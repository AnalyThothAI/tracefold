from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

STORY_IDENTITY_VERSION = "worldmonitor_story_identity_f73de5b7"
CLASSIFIER_VERSION = "worldmonitor_keyword_classifier_f73de5b7"
IMPORTANCE_VERSION = "worldmonitor_importance_f73de5b7_reporting_origin"
BRIEF_PROMPT_VERSION = "worldmonitor_public_insights_prompt_0e8785c"
BRIEF_WORKFLOW_VERSION = "worldmonitor_public_insights_workflow_0e8785c"
BRIEF_COMPOSER_VERSION = "worldmonitor_public_insights_composer_0e8785c"
BRIEF_SCHEMA_VERSION = "worldmonitor_public_insights_schema_v2"
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


class NewsSourceDefinition(ExactNewsModel):
    source_id: str
    name: str
    tier: int = Field(ge=1, le=4)
    lang: str = "en"
    source_kind: Literal["opennews"] = "opennews"
    enabled: bool = True

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


class NewsFeedEntry(ExactNewsModel):
    guid: str | None = None
    link: str | None = None
    title: str | None = None
    description: str = ""
    published_at_ms: int | None = None
    language: str | None = None
    reporting_origin: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


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
    "NewsSourceDefinition",
    "PublicInsightsCategory",
    "PublicInsightsThreatLevel",
    "ThreatLevel",
]
