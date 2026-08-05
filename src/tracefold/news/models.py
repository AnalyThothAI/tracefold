from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

STORY_IDENTITY_VERSION = "worldmonitor_story_identity_f73de5b7"
CLASSIFIER_VERSION = "worldmonitor_keyword_classifier_f73de5b7"
IMPORTANCE_VERSION = "worldmonitor_importance_f73de5b7_reporting_origin"
BRIEF_PROMPT_VERSION = "worldmonitor_top8_zh_v1"
BRIEF_WORKFLOW_VERSION = "worldmonitor_world_brief_v1"
BRIEF_SCHEMA_VERSION = "worldmonitor_world_brief_schema_v1"
NEWS_LOCALE = "zh-CN"

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


class NewsBriefExpectedError(RuntimeError):
    """A typed provider/response failure safe for native Brief retry."""


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
    title: str
    source: str
    url: str | None
    source_count: int
    importance_score: int
    level: ThreatLevel
    category: EventCategory


class NewsBriefDraft(ExactNewsModel):
    lead: str
    lines: tuple[str, ...]
    provider: str
    model: str
    raw_response: str


class NewsBriefPublisher(Protocol):
    def publish(self, stories: Sequence[NewsBriefStory]) -> NewsBriefDraft: ...

    def close(self) -> None: ...


__all__ = [
    "BRIEF_PROMPT_VERSION",
    "BRIEF_SCHEMA_VERSION",
    "BRIEF_WORKFLOW_VERSION",
    "CLASSIFIER_VERSION",
    "IMPORTANCE_VERSION",
    "NEWS_LOCALE",
    "STORY_IDENTITY_VERSION",
    "EventCategory",
    "NewsBriefDraft",
    "NewsBriefPublisher",
    "NewsBriefStory",
    "NewsClassification",
    "NewsFeedEntry",
    "NewsSourceDefinition",
    "ThreatLevel",
]
