"""Public News module interface."""

from .models import (
    EventCategory,
    NewsBriefPublisher,
    NewsBriefSource,
    NewsBriefStory,
    NewsBriefStoryLine,
    NewsBriefSynthesisResult,
    NewsFeedEntry,
    NewsFeedExpectedError,
    NewsFeedFetch,
    NewsFeedReader,
    NewsSourceDefinition,
    PublicInsightsCategory,
    PublicInsightsThreatLevel,
    ThreatLevel,
)
from .opennews import (
    OpenNewsEvent,
    OpenNewsExpectedError,
    OpenNewsHistoryError,
    OpenNewsStrategyHistory,
)
from .projection_worker import NewsStoryProjectionWorker
from .runtime import NewsAcquisition, NewsBriefCandidate
from .story_projection import NewsStoryFactSnapshot, NewsStoryProjection, build_story_projection

__all__ = [
    "EventCategory",
    "NewsAcquisition",
    "NewsBriefCandidate",
    "NewsBriefPublisher",
    "NewsBriefSource",
    "NewsBriefStory",
    "NewsBriefStoryLine",
    "NewsBriefSynthesisResult",
    "NewsFeedEntry",
    "NewsFeedExpectedError",
    "NewsFeedFetch",
    "NewsFeedReader",
    "NewsSourceDefinition",
    "NewsStoryFactSnapshot",
    "NewsStoryProjection",
    "NewsStoryProjectionWorker",
    "OpenNewsEvent",
    "OpenNewsExpectedError",
    "OpenNewsHistoryError",
    "OpenNewsStrategyHistory",
    "PublicInsightsCategory",
    "PublicInsightsThreatLevel",
    "ThreatLevel",
    "build_story_projection",
]
