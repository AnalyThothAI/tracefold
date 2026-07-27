from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

SOURCE_INVENTORY_VERSION = "worldmonitor_full_intel_plus_crypto_f73de5b7"
STORY_IDENTITY_VERSION = "worldmonitor_story_identity_f73de5b7"
CLASSIFIER_VERSION = "worldmonitor_keyword_classifier_f73de5b7"
IMPORTANCE_VERSION = "worldmonitor_importance_f73de5b7_physical_source"
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


class ExactNewsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewsSourceDefinition(ExactNewsModel):
    source_id: str
    name: str
    feed_url: str
    tier: int = Field(ge=1, le=4)
    lang: str = "en"
    memberships: tuple[str, ...]
    enabled: bool = True
    refresh_interval_seconds: int = Field(default=120, ge=1)

    @field_validator(
        "source_id",
        "name",
        "feed_url",
        "lang",
        mode="before",
    )
    @classmethod
    def require_text(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("news_source_text_required")
        return normalized

    @field_validator("memberships", mode="before")
    @classmethod
    def normalize_memberships(cls, value: Any) -> tuple[str, ...]:
        if not isinstance(value, list | tuple):
            raise ValueError("news_source_memberships_required")
        memberships = tuple(sorted({str(item or "").strip().lower() for item in value if str(item or "").strip()}))
        if not memberships:
            raise ValueError("news_source_memberships_required")
        return memberships


class NewsFeedEntry(ExactNewsModel):
    guid: str | None = None
    link: str | None = None
    title: str | None = None
    description: str = ""
    published_at_ms: int | None = None
    language: str | None = None
    reporting_origin: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class NewsFeedFetch(ExactNewsModel):
    status_code: int
    fetch_path: Literal["direct", "relay"]
    direct_error_code: str | None = None
    entries: tuple[NewsFeedEntry, ...] = ()
    entries_seen: int = Field(default=0, ge=0)
    gate_counts: dict[str, int] = Field(default_factory=dict)
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False


class NewsClassification(ExactNewsModel):
    level: ThreatLevel
    category: EventCategory
    confidence: float = Field(ge=0, le=1)
    source: Literal["keyword", "keyword-historical-downgrade"]


class NewsBriefStory(ExactNewsModel):
    story_id: str
    title: str
    source: str
    url: str
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


class NewsFeedReader(Protocol):
    def fetch(
        self,
        *,
        source: NewsSourceDefinition,
        etag: str | None,
        last_modified: str | None,
    ) -> NewsFeedFetch: ...

    def close(self) -> None: ...


class NewsBriefPublisher(Protocol):
    def publish(self, stories: Sequence[NewsBriefStory]) -> NewsBriefDraft: ...

    def close(self) -> None: ...


def source_definition(value: NewsSourceDefinition | Mapping[str, Any] | Any) -> NewsSourceDefinition:
    if isinstance(value, NewsSourceDefinition):
        return value
    if isinstance(value, Mapping):
        return NewsSourceDefinition.model_validate({field: value[field] for field in NewsSourceDefinition.model_fields})
    return NewsSourceDefinition.model_validate(
        {field: getattr(value, field) for field in NewsSourceDefinition.model_fields}
    )


__all__ = [
    "BRIEF_PROMPT_VERSION",
    "BRIEF_SCHEMA_VERSION",
    "BRIEF_WORKFLOW_VERSION",
    "CLASSIFIER_VERSION",
    "IMPORTANCE_VERSION",
    "NEWS_LOCALE",
    "SOURCE_INVENTORY_VERSION",
    "STORY_IDENTITY_VERSION",
    "EventCategory",
    "NewsBriefDraft",
    "NewsBriefPublisher",
    "NewsBriefStory",
    "NewsClassification",
    "NewsFeedEntry",
    "NewsFeedFetch",
    "NewsFeedReader",
    "NewsSourceDefinition",
    "ThreatLevel",
    "source_definition",
]
