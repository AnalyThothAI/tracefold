from __future__ import annotations

import inspect
from pathlib import Path

from tracefold import news
from tracefold.news import NewsAcquisition, NewsSourceDefinition

ROOT = Path(__file__).resolve().parents[2]

PUBLIC_NEWS_INTERFACE = {
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
    "NewsPushDelivery",
    "NewsPushDeliveryError",
    "NewsPushEligibility",
    "NewsPushIneligibleReason",
    "NewsPushReceipt",
    "NewsSourceDefinition",
    "NewsStoryProjection",
    "OpenNewsEvent",
    "OpenNewsExpectedError",
    "PreparedNewsPush",
    "PublicInsightsCategory",
    "PublicInsightsThreatLevel",
    "ThreatLevel",
    "evaluate_news_push_eligibility",
}


def test_news_root_is_three_deep_capabilities_plus_public_models() -> None:
    assert set(news.__all__) == PUBLIC_NEWS_INTERFACE

    leaked_implementation = {
        "NewsInterface",
        "NewsProjectionService",
        "NewsProjectionSnapshot",
        "NewsRepository",
        "NewsStoryPush",
        "brief_system_prompt",
        "classify_by_keyword",
        "cluster_texts",
        "compute_news_story_projection",
        "importance_score",
        "opennews_source",
        "parse_brief_synthesis",
        "parse_opennews_message",
        "parse_opennews_rest_response",
        "rebuild_all_news_for_maintenance",
        "selection_fingerprint",
    }
    assert leaked_implementation.isdisjoint(news.__dict__)


def test_public_news_acquisition_owns_rss_and_opennews_inputs() -> None:
    assert (ROOT / "src/tracefold/integrations/news_feeds").is_dir()
    assert set(NewsSourceDefinition.model_fields) == {
        "source_id",
        "name",
        "tier",
        "lang",
        "source_kind",
        "enabled",
        "feed_url",
        "memberships",
        "refresh_interval_seconds",
    }

    parameters = inspect.signature(NewsAcquisition).parameters
    assert {
        "rss_sources",
        "rss_feed_reader",
        "rss_feed_parser",
        "opennews_source",
        "opennews_strategy_ids",
        "opennews_ws_client",
    } <= set(parameters)
    assert "opennews_rest_client" not in parameters


def test_opennews_production_has_no_subscription_or_rest_recovery_path() -> None:
    integration_root = ROOT / "src/tracefold/integrations/opennews"
    production_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            integration_root / "__init__.py",
            integration_root / "client.py",
            ROOT / "src/tracefold/app/workers.py",
            ROOT / "src/tracefold/news/runtime.py",
        )
    )

    assert "news.subscribe" not in production_text
    assert "OpenNewsRestClient" not in production_text
    assert "opennews_rest_client" not in production_text


def test_news_root_has_no_compatibility_getattr() -> None:
    assert "__getattr__" not in news.__dict__


def test_news_has_no_shallow_interface_or_duplicate_story_rebuild() -> None:
    news_root = ROOT / "src/tracefold/news"

    assert not (news_root / "interface.py").exists()
    assert "def rebuild_stories(" not in (news_root / "repository.py").read_text(encoding="utf-8")


def test_news_brief_is_not_registered_as_a_generic_queue() -> None:
    queue_health = (ROOT / "src/tracefold/app/queue_health.py").read_text(encoding="utf-8")
    queue_ops = (ROOT / "src/tracefold/app/cli/commands/queue_ops.py").read_text(encoding="utf-8")

    assert "news_brief" not in queue_health
    assert "news_brief" not in queue_ops


def test_push_delivery_accepts_only_the_current_frozen_schema() -> None:
    adapter = (ROOT / "src/tracefold/integrations/news_push.py").read_text(encoding="utf-8")

    assert 'payload.get("schema_version") != _DELIVERY_SCHEMA_VERSION' in adapter
    assert "schema_version is None" not in adapter
