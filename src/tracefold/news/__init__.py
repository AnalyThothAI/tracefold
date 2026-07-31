"""WorldMonitor-compatible RSS -> NewsItem -> Story -> World Brief seam."""

from .classification import (
    SEVERITY_VALUES,
    classify_by_keyword,
    has_historical_marker,
)
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
    NewsBriefExpectedError,
    NewsBriefPublisher,
    NewsBriefStory,
    NewsClassification,
    NewsFeedEntry,
    NewsFeedExpectedError,
    NewsFeedFetch,
    NewsFeedReader,
    NewsSourceDefinition,
    source_definition,
)
from .projection import rebuild_all_news_for_maintenance
from .projection_worker import NewsProjectionCandidate
from .ranking import importance_score, is_delayed_brief_excluded, select_top_stories
from .repository import NewsRepository
from .runtime import NewsAcquisition, NewsBriefCandidate
from .sources import WORLDMONITOR_COMMIT, default_sources

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
    "NewsAcquisition",
    "NewsBriefCandidate",
    "NewsBriefDraft",
    "NewsBriefExpectedError",
    "NewsBriefPublisher",
    "NewsBriefStory",
    "NewsClassification",
    "NewsFeedEntry",
    "NewsFeedExpectedError",
    "NewsFeedFetch",
    "NewsFeedReader",
    "NewsInterface",
    "NewsProjectionCandidate",
    "NewsRepository",
    "NewsSourceDefinition",
    "candidate_tokens",
    "classify_by_keyword",
    "cluster_texts",
    "default_sources",
    "has_historical_marker",
    "importance_score",
    "is_delayed_brief_excluded",
    "is_same_story",
    "normalize_story_text",
    "rebuild_all_news_for_maintenance",
    "select_top_stories",
    "source_definition",
    "story_similarity",
    "story_vector",
]
