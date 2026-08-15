from fastapi.routing import APIRoute

from tracefold.app.http import routes_news
from tracefold.app.http.schemas import (
    NewsBriefData,
    NewsBriefPublicationData,
    NewsBriefRunData,
    NewsBriefStatusData,
    NewsIngestStatusData,
    NewsOpenNewsStatusData,
    NewsPushDelivery24hData,
    NewsRssStatusData,
    NewsSourceData,
    NewsStoryData,
    NewsStoryStatusData,
    NewsTitlePresentation24hData,
    NewsTitlePresentationStatusData,
)


def test_news_exposes_exactly_five_read_only_routes() -> None:
    routes = {
        (method, route.path)
        for route in routes_news.router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }

    assert routes == {
        ("GET", "/news/feed"),
        ("GET", "/news/stories/{story_id}"),
        ("GET", "/news/brief"),
        ("GET", "/news/sources"),
        ("GET", "/news/status"),
    }


def test_news_story_contract_has_no_push_notification_state() -> None:
    from tracefold.app.http import schemas

    assert not hasattr(schemas, "NewsNotificationData")
    assert "notification" not in NewsStoryData.model_fields
    assert "push_delivery_state" not in NewsStoryData.model_fields
    assert {"title", "original_title"} <= set(NewsStoryData.model_fields)


def test_news_brief_http_contract_is_one_slot_and_one_sealed_payload() -> None:
    assert set(NewsBriefData.model_fields) == {
        "state",
        "slot_at_ms",
        "next_due_at_ms",
        "publication",
        "latest_run",
    }
    assert set(NewsBriefRunData.model_fields) == {
        "slot_at_ms",
        "status",
        "model_outcome",
        "pointer_action",
        "attempt_count",
        "failure_count",
        "next_due_at_ms",
        "lease_expires_at_ms",
        "last_error_code",
        "updated_at_ms",
        "last_attempt_at_ms",
        "completed_at_ms",
    }
    assert set(NewsBriefStatusData.model_fields) == {
        "status",
        "reasons",
        "public_state",
        "slot_at_ms",
        "next_due_at_ms",
        "publication_id",
        "latest_run",
    }
    publication_fields = set(NewsBriefPublicationData.model_fields)
    assert {"publication_id", "slot_at_ms", "top_stories", "published_at_ms"} <= publication_fields
    assert {
        "target_fingerprint",
        "selected_story_ids",
        "created_at_ms",
    }.isdisjoint(publication_fields)


def test_news_health_contract_separates_current_wss_from_strategy_history() -> None:
    assert set(NewsIngestStatusData.model_fields) == {
        "status",
        "reasons",
        "rss",
        "opennews",
    }
    assert set(NewsRssStatusData.model_fields) == {
        "enabled",
        "source_count",
        "successful_source_count",
        "failed_source_count",
        "claimed_source_count",
        "next_due_at_ms",
        "latest_success_at_ms",
    }
    assert {
        "unmaterialized_item_count",
        "oldest_unmaterialized_at_ms",
    }.isdisjoint(NewsStoryStatusData.model_fields)
    source_fields = set(NewsSourceData.model_fields)
    assert {
        "feed_url",
        "refresh_interval_seconds",
        "next_fetch_at_ms",
        "last_outcome",
        "last_rejection_counts",
        "last_items_seen",
        "last_items_accepted",
    } <= source_fields
    assert {
        "last_connected_at_ms",
        "last_disconnected_at_ms",
        "last_accepted_strategy_trigger_at_ms",
        "strategy_history_status",
        "last_history_check_at_ms",
        "observed_strategy_count",
        "unresolved_incident_count",
        "incidents",
    } <= source_fields
    assert {"last_live_at_ms", "last_recovery_at_ms"}.isdisjoint(source_fields)
    assert set(NewsOpenNewsStatusData.model_fields) == {
        "source_id",
        "name",
        "live_connected",
        "configured_strategy_count",
        "observed_strategy_count",
        "last_connected_at_ms",
        "last_disconnected_at_ms",
        "last_accepted_strategy_trigger_at_ms",
        "strategy_history_status",
        "last_history_check_at_ms",
        "last_outcome",
        "last_error",
        "last_success_at_ms",
        "consecutive_failures",
        "last_rejection_counts",
        "last_items_seen",
        "last_items_accepted",
        "unresolved_incident_count",
        "incidents",
    }


def test_news_title_presentation_and_push_slo_contracts_are_separate() -> None:
    assert set(NewsTitlePresentation24hData.model_fields) == {
        "total",
        "attempted",
        "translated",
        "not_needed",
        "fallback",
        "provider_counts",
        "latency_p95_ms",
        "fallback_counts",
        "sample_complete",
    }
    assert set(NewsTitlePresentationStatusData.model_fields) == {
        "status",
        "reasons",
        "deepl_configured",
        "deepl_key_count",
        "deepseek_configured",
        "policy_version",
        "deepl_deadline_ms",
        "deepseek_deadline_ms",
        "pending_count",
        "resolving_count",
        "oldest_pending_at_ms",
        "oldest_push_blocking_at_ms",
        "oldest_resolving_at_ms",
        "resolution_24h",
        "measured_at_ms",
    }
    assert set(NewsPushDelivery24hData.model_fields) == {
        "completed",
        "sent",
        "terminal",
        "latency_p95_ms",
        "slo_met",
        "sample_complete",
    }
