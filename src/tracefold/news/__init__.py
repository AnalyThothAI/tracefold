"""WorldMonitor-compatible RSS -> NewsItem -> Story -> World Brief seam."""

from .classification import (
    SEVERITY_VALUES,
    classify_by_keyword,
    has_historical_marker,
)
from .health import attach_pipeline_runtime_health
from .identity import (
    STORY_SIMILARITY_THRESHOLD,
    candidate_tokens,
    cluster_texts,
    is_same_story,
    normalize_story_text,
    story_similarity,
    story_vector,
)
from .interface import NewsInterface
from .models import (
    BRIEF_PROMPT_VERSION,
    BRIEF_SCHEMA_VERSION,
    BRIEF_WORKFLOW_VERSION,
    CLASSIFIER_VERSION,
    IMPORTANCE_VERSION,
    NEWS_LOCALE,
    SOURCE_INVENTORY_VERSION,
    STORY_IDENTITY_VERSION,
    NewsBriefDraft,
    NewsBriefPublisher,
    NewsBriefStory,
    NewsClassification,
    NewsFeedEntry,
    NewsFeedFetch,
    NewsFeedReader,
    NewsSourceDefinition,
    source_definition,
)
from .ranking import importance_score, is_delayed_brief_excluded, select_top_stories
from .repository import NewsRepository
from .sources import WORLDMONITOR_COMMIT, default_sources
from .workers import NewsPipelineWorker, NewsWorldBriefWorker

__all__ = [
    "BRIEF_PROMPT_VERSION",
    "BRIEF_SCHEMA_VERSION",
    "BRIEF_WORKFLOW_VERSION",
    "CLASSIFIER_VERSION",
    "IMPORTANCE_VERSION",
    "NEWS_LOCALE",
    "SEVERITY_VALUES",
    "SOURCE_INVENTORY_VERSION",
    "STORY_IDENTITY_VERSION",
    "STORY_SIMILARITY_THRESHOLD",
    "WORLDMONITOR_COMMIT",
    "NewsBriefDraft",
    "NewsBriefPublisher",
    "NewsBriefStory",
    "NewsClassification",
    "NewsFeedEntry",
    "NewsFeedFetch",
    "NewsFeedReader",
    "NewsInterface",
    "NewsPipelineWorker",
    "NewsRepository",
    "NewsSourceDefinition",
    "NewsWorldBriefWorker",
    "attach_pipeline_runtime_health",
    "candidate_tokens",
    "classify_by_keyword",
    "cluster_texts",
    "default_sources",
    "has_historical_marker",
    "importance_score",
    "is_delayed_brief_excluded",
    "is_same_story",
    "normalize_story_text",
    "select_top_stories",
    "source_definition",
    "story_similarity",
    "story_vector",
]
