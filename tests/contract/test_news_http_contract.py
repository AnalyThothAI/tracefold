from fastapi.routing import APIRoute

from tracefold.app.http import routes_news
from tracefold.app.http.schemas import (
    NewsBriefData,
    NewsBriefPublicationData,
    NewsBriefRunData,
    NewsBriefStatusData,
    NewsIngestStatusData,
    NewsRssStatusData,
    NewsSourceData,
    NewsStoryStatusData,
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


def test_news_health_contract_has_public_rss_without_retired_gap_or_projection_backlog() -> None:
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
    assert {"gap_unclosed", "gap_version"}.isdisjoint(source_fields)
