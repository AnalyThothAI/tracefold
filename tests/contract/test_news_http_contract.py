"""News V3 HTTP contract: three read-only routes, exact schemas, bounded 400/404 behaviour."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from tracefold.app.http import routes_news, schemas_news
from tracefold.app.http.app import create_app
from tracefold.platform.config.settings import Settings

TOKEN = "contract-token"


def _event(event_id: str = "ev-1") -> dict[str, Any]:
    return {
        "event_id": event_id,
        "family": "general",
        "leader_title": "Copper surges toward record on LME",
        "leader_url": "https://example.test/copper",
        "leader_description": "",
        "reporting_origin": "example.test",
        "opened_at_ms": 1_800_000_000_000,
        "last_member_at_ms": 1_800_000_000_000,
        "member_count": 1,
        "admission": "candidate",
        "priority": "normal",
        "provider_score_max": 75.0,
        "engine_type": "news",
        "asset_class": "macro",
        "grounded_assets": [],
        "watchlist_hits": [],
        "macro_lexicon": True,
        "storyline_key": "copper",
        "context_line": "",
        "published_at_ms": None,
        "ingest_mode": "live",
        "provenance": ["1018"],
    }


class _FakeNewsRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_feed(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list_feed", kwargs))
        if kwargs.get("cursor") == "broken":
            raise ValueError("news_feed_cursor_invalid")
        return {
            "events": [{**_event(), "title_zh": "铜价冲击纪录"}],
            "next_cursor": None,
            "filters": {
                "family": kwargs["family"],
                "admission": kwargs["admission"],
                "priority": kwargs["priority"],
                "decision": kwargs["decision"],
                "symbol": kwargs["symbol"],
                "q": kwargs["q"],
                "sort": kwargs["sort"],
                "limit": kwargs["limit"],
            },
        }

    def event_detail(self, event_id: str) -> dict[str, Any] | None:
        self.calls.append(("event_detail", {"event_id": event_id}))
        if event_id != "ev-1":
            return None
        return {
            "event": _event(),
            "members": [],
            "verdicts": [],
            "deliveries": [],
            "labels": [],
        }

    def status_snapshot(self, *, now_ms: int) -> dict[str, Any]:
        self.calls.append(("status_snapshot", {"now_ms": now_ms}))
        return {
            "ingest": {
                "connected": False,
                "last_frame_at_ms": None,
                "last_publish_at_ms": None,
                "last_error_code": None,
                "configured_strategy_ids": ["1018", "1019"],
                "provider_enabled_strategy_ids": None,
                "strategy_warnings": [],
                "open_incidents": [],
            },
            "pipeline": {"events_1h": 0, "events_24h": 0},
            "delivery": {"sent_24h": 0, "sent_1h": 0, "terminal_24h": 0, "last_error_code": None, "e2e_p95_ms": None},
            "control": {"paused": False, "mutes": []},
        }


class _FakeCursor:
    @staticmethod
    def fetchone() -> None:
        return None


class _FakeConnection:
    @staticmethod
    def execute(*_args: Any, **_kwargs: Any) -> _FakeCursor:
        return _FakeCursor()


class _FakeRepositories:
    def __init__(self, news: _FakeNewsRepository) -> None:
        self.news = news
        self.conn = _FakeConnection()


class _FakeRuntime:
    def __init__(self, settings: Settings, news: _FakeNewsRepository) -> None:
        self.settings = settings
        self._news = news

    @contextmanager
    def repositories(self):
        yield _FakeRepositories(self._news)


@pytest.fixture
def client() -> tuple[TestClient, _FakeNewsRepository]:
    settings = Settings(ws_token=TOKEN)
    app = create_app(settings=settings)
    news = _FakeNewsRepository()
    app.state.service = _FakeRuntime(settings, news)
    return TestClient(app), news


def test_news_exposes_exactly_three_read_only_routes() -> None:
    routes = {
        (method, route.path)
        for route in routes_news.router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }

    assert routes == {
        ("GET", "/news/feed"),
        ("GET", "/news/events/{event_id}"),
        ("GET", "/news/status"),
    }


def test_news_schemas_are_exact_and_carry_no_retired_story_brief_surface() -> None:
    assert set(schemas_news.NewsFeedData.model_fields) == {"events", "next_cursor", "filters"}
    assert set(schemas_news.NewsFeedFiltersData.model_fields) == {
        "family",
        "admission",
        "priority",
        "decision",
        "symbol",
        "q",
        "sort",
        "limit",
    }
    assert set(schemas_news.NewsFeedEventData.model_fields) - set(schemas_news.NewsEventData.model_fields) == {
        "title_zh",
        "triage",
        "delivery",
    }
    assert set(schemas_news.NewsEventDetailData.model_fields) == {
        "event",
        "members",
        "verdicts",
        "deliveries",
        "labels",
    }
    assert set(schemas_news.NewsStatusData.model_fields) == {
        "state",
        "workers_state",
        "ingest",
        "broker",
        "pipeline",
        "delivery",
        "control",
        "watchlist",
        "measured_at_ms",
    }
    assert set(schemas_news.NewsIngestStatusData.model_fields) == {
        "connected",
        "last_frame_at_ms",
        "last_publish_at_ms",
        "last_error_code",
        "configured_strategy_ids",
        "provider_enabled_strategy_ids",
        "strategy_warnings",
        "open_incidents",
        "token_configured",
    }
    assert set(schemas_news.NewsDeliveryStatusData.model_fields) == {
        "sent_24h",
        "sent_1h",
        "terminal_24h",
        "last_error_code",
        "e2e_p95_ms",
        "delivery_available",
        "hourly_cap",
    }
    for name in dir(schemas_news):
        assert not any(marker in name for marker in ("Story", "Brief", "Rss", "TitleTranslation", "Notification")), name


def test_feed_returns_validated_envelope_and_forwards_bounded_filters(client) -> None:
    http, news = client

    response = http.get(
        "/api/news/feed",
        params={"token": TOKEN, "family": "general", "priority": "high", "sort": "priority", "limit": 5},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["filters"] == {
        "family": "general",
        "admission": None,
        "priority": "high",
        "decision": None,
        "symbol": None,
        "q": None,
        "sort": "priority",
        "limit": 5,
    }
    assert body["data"]["events"][0]["event_id"] == "ev-1"
    assert body["data"]["events"][0]["title_zh"] == "铜价冲击纪录"
    assert response.headers.get("etag")
    assert news.calls[0][1]["cursor"] is None


@pytest.mark.parametrize(
    ("params", "error", "field"),
    [
        ({"admission": "bogus"}, "news_feed_admission_invalid", "admission"),
        ({"decision": "maybe"}, "news_feed_decision_invalid", "decision"),
        ({"cursor": "broken"}, "news_feed_cursor_invalid", "cursor"),
        ({"story_id": "x"}, "unsupported_query_param", "story_id"),
    ],
)
def test_feed_rejects_invalid_filters_with_bounded_400(client, params, error, field) -> None:
    http, _ = client

    response = http.get("/api/news/feed", params={"token": TOKEN, **params})

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": error, "field": field}


@pytest.mark.parametrize("params", [{"priority": "urgent"}, {"sort": "hot"}, {"limit": 0}, {"limit": 101}])
def test_feed_query_shape_violations_are_422(client, params) -> None:
    http, _ = client

    response = http.get("/api/news/feed", params={"token": TOKEN, **params})

    assert response.status_code == 422


def test_event_detail_returns_envelope_or_bounded_404(client) -> None:
    http, _ = client

    found = http.get("/api/news/events/ev-1", params={"token": TOKEN})
    assert found.status_code == 200
    assert found.json()["data"]["event"]["event_id"] == "ev-1"
    assert found.json()["data"]["members"] == []

    missing = http.get("/api/news/events/ev-404", params={"token": TOKEN})
    assert missing.status_code == 404
    assert missing.json() == {"ok": False, "error": "news_event_not_found"}

    too_long = http.get(f"/api/news/events/{'x' * 129}", params={"token": TOKEN})
    assert too_long.status_code == 400
    assert too_long.json() == {"ok": False, "error": "news_event_id_invalid", "field": "event_id"}


def test_status_reports_unavailable_without_broker_or_token(client) -> None:
    http, _ = client

    response = http.get("/api/news/status", params={"token": TOKEN})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["state"] == "unavailable"
    assert data["workers_state"] is None
    assert data["ingest"]["token_configured"] is False
    assert data["broker"] == {
        "configured": False,
        "connected": None,
        "queues": {},
        "error_code": None,
        "observed_at_ms": None,
    }
    assert data["delivery"]["delivery_available"] is False
    assert data["delivery"]["hourly_cap"] >= 1
    assert data["control"] == {"paused": False, "mutes": []}
    assert isinstance(data["watchlist"], list)


def test_news_routes_require_the_operator_token(client) -> None:
    http, _ = client

    for path in ("/api/news/feed", "/api/news/events/ev-1", "/api/news/status"):
        assert http.get(path).status_code == 401
        assert http.get(path, params={"token": "wrong"}).status_code == 401
