"""Tracefold News v2 public interface.

The Story Interface is the only external read seam. RSS and model adapters are
injected into the two workers and remain private implementation details.
"""

from .identity import next_story_state_refresh, normalize_feed_entry, story_similarity
from .interface import StoryInterface
from .models import (
    ARTICLE_IDENTITY_VERSION,
    NEWS_ANALYSIS_PROMPT_VERSION,
    NEWS_ANALYSIS_SCHEMA_VERSION,
    NEWS_ANALYSIS_WORKFLOW_VERSION,
    STORY_IDENTITY_VERSION,
    STORY_IMPORTANCE_VERSION,
    STORY_LIFECYCLE_VERSION,
    NewsAnalysisContract,
    NewsAnalysisEvidence,
    NewsFeedEntry,
    NewsFeedFetch,
    NewsFeedReader,
    NewsSourceDefinition,
    NewsStoryAnalysisDraft,
    NewsStoryAnalysisResult,
    NewsStoryAnalyzer,
)
from .repository import NewsRepository
from .workers import NewsAnalysisWorker, NewsIngestWorker

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
    "NewsAnalysisWorker",
    "NewsFeedEntry",
    "NewsFeedFetch",
    "NewsFeedReader",
    "NewsIngestWorker",
    "NewsRepository",
    "NewsSourceDefinition",
    "NewsStoryAnalysisDraft",
    "NewsStoryAnalysisResult",
    "NewsStoryAnalyzer",
    "StoryInterface",
    "next_story_state_refresh",
    "normalize_feed_entry",
    "story_similarity",
]
