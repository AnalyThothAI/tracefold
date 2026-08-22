"""News V3 HTTP contract: read surfaces plus the narrow ReviewDesk mutation adapter."""

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
        # One tag that names a listed contract, the provider's prefixed form of the *same* contract, and one
        # that names an English word — the three cases the console has to tell apart (#87).
        "grounded_assets": ["COPPER", "XYZ-COPPER", "SPOT"],
        "watchlist_hits": [],
        "macro_lexicon": True,
        "storyline_key": "copper",
        "context_line": "",
        "published_at_ms": None,
        "ingest_mode": "live",
        "provenance": ["1018"],
    }


_OUTCOME = {"kind": "queued_publish", "text_zh": "待处理", "reason_zh": "已入库，等待送审", "group": "pending"}


class _FakeNewsRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_feed(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list_feed", kwargs))
        if kwargs.get("cursor") == "broken":
            raise ValueError("news_feed_cursor_invalid")
        return {
            "events": [{**_event(), "title_zh": "铜价冲击纪录", "outcome": _OUTCOME}],
            "next_cursor": None,
            "counts": None if kwargs.get("cursor") else {"total": 1, "pushed": 0, "held": 0, "pending": 1},
            "filters": {
                "family": kwargs["family"],
                "admission": kwargs["admission"],
                "priority": kwargs["priority"],
                "decision": kwargs["decision"],
                "symbol": kwargs["symbol"],
                "q": kwargs["q"],
                "sort": kwargs["sort"],
                "limit": kwargs["limit"],
                "outcome": kwargs.get("outcome"),
                "hours": kwargs.get("hours"),
            },
        }

    def event_detail(self, event_id: str) -> dict[str, Any] | None:
        self.calls.append(("event_detail", {"event_id": event_id}))
        if event_id != "ev-1":
            return None
        return {
            "event": _event(),
            "outcome": _OUTCOME,
            "timeline": [
                {
                    "stage": "received",
                    "title_zh": "收到",
                    "at_ms": 1_800_000_000_000,
                    "summary_zh": "来源 example.test",
                    "facts": {},
                }
            ],
            "members": [],
            "verdicts": [],
            "deliveries": [],
            "review": {"judgment_n": 0, "accepted": None, "uncertain": False},
            "evidence_snapshots": [],
            "reader_receipt": {
                "state": "not_received",
                "delivery_state": None,
                "error_code": None,
                "received_at_ms": None,
                "rendered_card": None,
            },
        }

    def asset_usage_24h(self, *, now_ms: int) -> dict[str, list[str]]:
        self.calls.append(("asset_usage_24h", {"now_ms": now_ms}))
        return {"ev-1": ["COPPER", "SPOT"], "ev-2": ["SPOT"]}

    def status_snapshot(self, *, now_ms: int) -> dict[str, Any]:
        self.calls.append(("status_snapshot", {"now_ms": now_ms}))
        return {
            "ingest": {
                "connected": False,
                "last_frame_at_ms": None,
                "last_publish_at_ms": None,
                "last_error_code": None,
                "open_incidents": [],
            },
            "pipeline": {"events_1h": 0, "events_24h": 0},
            "delivery": {"sent_24h": 0, "sent_1h": 0, "terminal_24h": 0, "last_error_code": None, "e2e_p95_ms": None},
            "learning_retention": {
                "last_run_at_ms": None,
                "eligible_recordings": 0,
                "eligible_cases": 0,
                "eligible_artifacts": 0,
                "deleted_recordings": 0,
                "deleted_cases": 0,
                "deleted_artifacts": 0,
                "oldest_recording_age_ms": None,
                "oldest_case_age_ms": None,
                "oldest_artifact_age_ms": None,
                "last_error_code": None,
                "updated_at_ms": None,
            },
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


class _FakeInstrumentsRepository:
    """#75 universe as the status route sees it before any snapshot has landed."""

    def asset_refs(self, symbols: Any) -> dict[str, dict[str, Any]]:
        # Keyed by the raw provider tag; `symbol` comes back normalized (the real one strips `XYZ-`).
        listed = {"COPPER": "hl.xyz"}
        out: dict[str, dict[str, Any]] = {}
        for raw in symbols:
            norm = str(raw).upper().removeprefix("XYZ-")
            out[str(raw)] = {
                "symbol": norm,
                "base_symbol": norm,
                "venue": listed.get(norm),
                "listed": norm in listed,
            }
        return out

    def aliases_by_base(self, base_symbols: Any, *, sources: Any = None) -> dict[str, dict[str, Any]]:
        # #87 review: the console asks for operator aliases only. Venue-derived rows are mechanical
        # (`XYZ-{base}` exists for every builder-DEX base) and would fire the block on routine Events.
        groups = {"COPPER": ["COPPER", "HG"]}
        if sources is not None and "seed" not in tuple(sources):
            return {
                str(base): {"base_symbol": str(base), "aliases": [str(base)], "sources": []} for base in base_symbols
            }
        return {
            str(base): {
                "base_symbol": str(base),
                "aliases": groups.get(str(base), [str(base)]),
                "sources": ["seed"],
            }
            for base in base_symbols
        }

    def universe_summary(self) -> dict[str, object]:
        return {
            "trading": 0,
            "delisted": 0,
            "base_symbols": 0,
            "venues": 0,
            "last_snapshot_ms": None,
            "by_venue": {},
            "by_class": {},
            "dangling_aliases": 0,
            "reference_symbols": 0,
        }


class _FakePriceRepository:
    """#88 price plane before any quote or Reaction has landed: everything says so, nothing invents a zero."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def event_reaction_aggregates(self, event_ids: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("event_reaction_aggregates", {"event_ids": list(event_ids), **kwargs}))
        return {}

    def event_reactions(self, event_id: str) -> list[dict[str, Any]]:
        self.calls.append(("event_reactions", {"event_id": event_id}))
        return []

    def quotes_for_symbols(self, symbols: Any, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("quotes_for_symbols", {"symbols": list(symbols), **kwargs}))
        return [
            {
                "requested_symbol": symbol,
                "symbol": str(symbol).upper(),
                "base_symbol": str(symbol).upper(),
                "venue": None,
                "venue_symbol": None,
                "instrument_class": None,
                "quote_asset": None,
                "price": None,
                "price_kind": None,
                "price_kind_zh": "",
                "change_pct": None,
                "change_basis": None,
                "change_basis_zh": "",
                "source_at_ms": None,
                "received_at_ms": None,
                "age_ms": None,
                "state": "unlisted",
                "state_zh": "无可交易合约",
            }
            for symbol in symbols
        ]

    def review(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("review", kwargs))
        return {
            "meta": {
                "hours": int(kwargs.get("hours") or 168),
                "window_start_ms": 0,
                "window_end_ms": 1,
                "discovery_window_start_ms": 0,
                "metric_version": "reaction_v1",
                "measured_at_ms": 1,
            },
            "coverage": [],
            "directions": [],
            "magnitudes": [],
            "event_types": [],
            "potential_misses": [],
            "summary": {"hit_1h_pct": None, "hit_1h_n": 0, "coverage_1h_pct": None},
        }

    def price_status(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("price_status", kwargs))
        return {
            "metric_version": "reaction_v1",
            "oldest_due_age_ms": 0,
            "sources": [],
            "fresh_sources": 0,
            "quotes": 0,
            "reaction_partial_7d": 0,
            "reaction_complete_7d": 0,
            "reaction_unavailable_7d": 0,
        }


class _FakeRepositories:
    def __init__(self, news: _FakeNewsRepository) -> None:
        self.news = news
        self.instruments = _FakeInstrumentsRepository()
        self.price = _FakePriceRepository()
        self.conn = _FakeConnection()


class _FakeRuntime:
    def __init__(self, settings: Settings, news: _FakeNewsRepository) -> None:
        self.settings = settings
        self._news = news

    @contextmanager
    def repositories(self):
        yield _FakeRepositories(self._news)

    @contextmanager
    def review_transaction(self):
        yield _FakeConnection()


class _FakeReviewDesk:
    def __init__(self, _conn: Any) -> None:
        pass

    def open(self, query: Any, *, principal: Any) -> dict[str, Any]:
        del principal
        if query.view == "market":
            return {
                "view": "market",
                "title_zh": "事后市场观察",
                "disclaimer_zh": "价格变化不是因果或真值。",
                "reaction": _FakePriceRepository().review(hours=query.hours),
            }
        return {
            "view": query.view,
            "mode": query.mode,
            "status": "insufficient_evidence",
            "reader_contract_version": "reader_contract_v2",
            "rubric_version": "news_review_v2",
            "tasks": [],
            "next_cursor": None,
            "counts": {},
        }


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, _FakeNewsRepository]:
    settings = Settings(ws_token=TOKEN)
    app = create_app(settings=settings)
    news = _FakeNewsRepository()
    monkeypatch.setattr(routes_news, "ReviewDesk", _FakeReviewDesk)
    app.state.service = _FakeRuntime(settings, news)
    return TestClient(app), news


def test_news_exposes_read_routes_and_only_the_two_review_mutations() -> None:
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
        # #88: current quotes and 命中复盘. Both are read-only and bounded; quotes stay off the feed so a
        # price tick cannot invalidate the feed's ETag every three seconds.
        ("GET", "/news/quotes"),
        ("GET", "/news/review"),
        ("GET", "/news/review/tasks/{task_id}/evidence"),
        ("POST", "/news/review/tasks/{task_id}/responses"),
        ("POST", "/news/review/external-misses"),
    }


def test_news_schemas_are_exact_and_carry_no_retired_story_brief_surface() -> None:
    assert set(schemas_news.NewsFeedData.model_fields) == {"events", "next_cursor", "counts", "filters"}
    assert set(schemas_news.NewsFeedCountsData.model_fields) == {"total", "pushed", "held", "pending"}
    assert set(schemas_news.NewsFeedFiltersData.model_fields) == {
        "family",
        "admission",
        "priority",
        "decision",
        "symbol",
        "q",
        "sort",
        "limit",
        "outcome",
        "hours",
    }
    assert set(schemas_news.NewsFeedEventData.model_fields) - set(schemas_news.NewsEventData.model_fields) == {
        "title_zh",
        "outcome",
        "triage",
        "delivery",
        # #88: the fixed post-Event return. The *current* quote is deliberately not a feed field.
        "reaction",
    }
    assert set(schemas_news.NewsEventDetailData.model_fields) == {
        "event",
        "outcome",
        "triage",
        "timeline",
        "members",
        "verdicts",
        "deliveries",
        "review",
        "evidence_snapshots",
        "reader_receipt",
        "normalization",
        # #88: the event-level aggregate plus every per-asset Reaction, with the closes they came from.
        "reaction",
        "reactions",
    }
    assert set(schemas_news.NewsAssetRefData.model_fields) == {"symbol", "base_symbol", "venue", "listed"}
    assert set(schemas_news.NewsSymbolNormalizationData.model_fields) == {"base_symbol", "aliases", "sources"}
    assert set(schemas_news.NewsOutcomeData.model_fields) == {"kind", "text_zh", "reason_zh", "group"}
    assert set(schemas_news.NewsStatusData.model_fields) == {
        "state",
        "workers_state",
        "health",
        "funnel_24h",
        "reasons_24h",
        "ingest",
        "broker",
        "pipeline",
        "delivery",
        "learning_retention",
        "control",
        "watchlist",
        "instruments",
        # #88 §11: per-source quote freshness and Reaction backlog, beside the pipeline's own health.
        "price",
        "measured_at_ms",
    }
    assert set(schemas_news.NewsIngestStatusData.model_fields) == {
        "connected",
        "last_frame_at_ms",
        "last_publish_at_ms",
        "last_error_code",
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
    }
    assert set(schemas_news.NewsLearningRetentionStatusData.model_fields) == {
        "last_run_at_ms",
        "eligible_recordings",
        "eligible_cases",
        "eligible_artifacts",
        "deleted_recordings",
        "deleted_cases",
        "deleted_artifacts",
        "oldest_recording_age_ms",
        "oldest_case_age_ms",
        "oldest_artifact_age_ms",
        "last_error_code",
        "updated_at_ms",
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
        "outcome": None,
        "hours": None,
    }
    assert body["data"]["events"][0]["event_id"] == "ev-1"
    assert body["data"]["events"][0]["outcome"]["kind"] == "queued_publish"
    assert body["data"]["events"][0]["title_zh"] == "铜价冲击纪录"
    # #87: the raw provider tags stay, and beside them the same tags resolved against the instrument
    # universe — so the browser can strike through a tag that names nothing without owning a symbol table.
    assert body["data"]["events"][0]["grounded_assets"] == ["COPPER", "XYZ-COPPER", "SPOT"]
    # One entry per instrument named, not per tag: `COPPER` and `XYZ-COPPER` are the same contract, and once
    # resolved they are byte-identical (#87 review).
    assert body["data"]["events"][0]["assets"] == [
        {"symbol": "COPPER", "base_symbol": "COPPER", "venue": "hl.xyz", "listed": True},
        {"symbol": "SPOT", "base_symbol": "SPOT", "venue": None, "listed": False},
    ]
    assert response.headers.get("etag")
    assert news.calls[0][1]["cursor"] is None


def test_feed_forwards_outcome_group_and_hours_window(client) -> None:
    http, news = client

    response = http.get("/api/news/feed", params={"token": TOKEN, "outcome": "held", "hours": 6})

    assert response.status_code == 200
    forwarded = news.calls[0][1]
    assert forwarded["outcome"] == "held"
    assert forwarded["hours"] == 6
    # Pattern/bound violations are rejected by the FastAPI query validators (422), like the existing `priority` filter.
    assert http.get("/api/news/feed", params={"token": TOKEN, "outcome": "bogus"}).status_code == 422
    assert http.get("/api/news/feed", params={"token": TOKEN, "hours": 999}).status_code == 422


def test_feed_reports_tab_counts_on_the_first_page_only(client) -> None:
    http, _ = client

    first = http.get("/api/news/feed", params={"token": TOKEN}).json()["data"]
    paged = http.get("/api/news/feed", params={"token": TOKEN, "cursor": "abc"}).json()["data"]

    assert first["counts"] == {"total": 1, "pushed": 0, "held": 0, "pending": 1}
    assert paged["counts"] is None


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


def test_feed_resolves_every_tag_even_when_the_universe_knows_none_of_them(client) -> None:
    """A tag the universe cannot place still gets an entry, never a hole the browser has to guess about."""

    http, _ = client

    body = http.get("/api/news/feed", params={"token": TOKEN}).json()

    assert [asset["symbol"] for asset in body["data"]["events"][0]["assets"]] == ["COPPER", "SPOT"]
    assert [asset["listed"] for asset in body["data"]["events"][0]["assets"]] == [True, False]


def test_status_counts_grounding_from_both_owners_without_either_reaching_across(client) -> None:
    """#87: News owns which tags an Event carried, the universe owns what they name; the route folds them."""

    http, news = client

    body = http.get("/api/news/status", params={"token": TOKEN}).json()

    # ev-1 has COPPER (listed) so it grounds; ev-2 has only SPOT so it does not.
    assert body["data"]["funnel_24h"]["grounded"] == 1
    assert body["data"]["pipeline"]["ungrounded_by_symbol_24h"] == {"SPOT": 2}
    assert {"stage": "ungrounded", "key": "SPOT", "label_zh": "SPOT", "count": 2} in body["data"]["reasons_24h"]
    assert any(call[0] == "asset_usage_24h" for call in news.calls)


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
    assert "hourly_cap" not in data["delivery"]
    assert data["control"] == {"paused": False, "mutes": []}
    assert isinstance(data["watchlist"], list)


def test_status_marks_an_invalid_dedicated_reader_endpoint_bad(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings.model_validate(
        {
            "ws_token": TOKEN,
            "llm": {
                "api_key": "triage-key",
                "base_url": "https://triage.test/v1",
                "news_triage_model": "shared-model",
                "news_reader_card": {
                    "api_key": "reader-key",
                    "base_url": "ftp://reader.test/v1",
                    "model": "shared-model",
                },
            },
        }
    )
    app = create_app(settings=settings)
    monkeypatch.setattr(routes_news, "ReviewDesk", _FakeReviewDesk)
    app.state.service = _FakeRuntime(settings, _FakeNewsRepository())

    with TestClient(app) as http:
        response = http.get("/api/news/status", params={"token": TOKEN})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["pipeline"]["reader_card_model"] is None
    assert data["health"]["model"] == {
        "level": "bad",
        "summary_zh": "Reader 模型不可用",
        "detail_zh": "ReaderCard 配置无效；所有事件按规则兜底",
    }


def test_news_routes_require_the_operator_token(client) -> None:
    http, _ = client

    for path in ("/api/news/feed", "/api/news/events/ev-1", "/api/news/status"):
        assert http.get(path).status_code == 401
        assert http.get(path, params={"token": "wrong"}).status_code == 401


# ---------------------------------------------------------------------------- #88 price surfaces
def test_quotes_returns_one_result_per_requested_symbol(client) -> None:
    api, _ = client
    response = api.get("/api/news/quotes", params={"symbols": "BTC,ETH,BTC", "token": TOKEN})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    # Deduplicated by the server as well as the hook: a repeated symbol cannot multiply work.
    assert [quote["requested_symbol"] for quote in payload["data"]["quotes"]] == ["BTC", "ETH"]
    assert {quote["state"] for quote in payload["data"]["quotes"]} == {"unlisted"}
    assert payload["data"]["quotes"][0]["price"] is None  # never a fabricated zero


def test_quotes_rejects_an_oversized_or_malformed_symbol_batch(client) -> None:
    api, _ = client
    too_many = ",".join(f"S{index}" for index in range(101))
    response = api.get("/api/news/quotes", params={"symbols": too_many, "token": TOKEN})
    assert response.status_code == 400
    assert response.json()["error"] == "news_quotes_symbols_too_many"

    long_symbol = api.get("/api/news/quotes", params={"symbols": "X" * 33, "token": TOKEN})
    assert long_symbol.status_code == 400
    assert long_symbol.json()["error"] == "news_quotes_symbol_invalid"

    unknown = api.get("/api/news/quotes", params={"symbols": "BTC", "unknown": "1", "token": TOKEN})
    assert unknown.status_code == 400
    assert unknown.json()["error"] == "unsupported_query_param"


def test_review_defaults_to_learning_queue_and_market_is_explicit(client) -> None:
    api, _ = client
    response = api.get("/api/news/review", params={"hours": 168, "token": TOKEN})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["view"] == "queue"
    assert data["status"] == "insufficient_evidence"
    market = api.get("/api/news/review", params={"view": "market", "hours": 168, "token": TOKEN}).json()["data"]
    assert market["view"] == "market"
    assert market["reaction"]["meta"]["metric_version"] == "reaction_v1"

    assert api.get("/api/news/review", params={"hours": 721, "token": TOKEN}).status_code == 422
    assert api.get("/api/news/review", params={"hours": 0, "token": TOKEN}).status_code == 422


def test_the_feed_attaches_reactions_in_one_bounded_batch(client) -> None:
    api, news = client
    response = api.get("/api/news/feed", params={"token": TOKEN})

    assert response.status_code == 200
    events = response.json()["data"]["events"]
    assert "reaction" in events[0]
    del news


def test_current_quotes_never_travel_in_the_feed_body(client) -> None:
    """#88 §5: a price that changed must not invalidate the feed's ETag or re-run its count query."""

    api, _ = client
    body = api.get("/api/news/feed", params={"token": TOKEN}).json()["data"]
    serialized = repr(body)
    assert "price_kind" not in serialized and "change_basis" not in serialized
    assert set(schemas_news.NewsFeedEventData.model_fields).isdisjoint({"quote", "quotes", "price"})


def test_event_detail_keeps_the_two_market_meanings_in_separate_fields(client) -> None:
    api, _ = client
    detail = api.get("/api/news/events/ev-1", params={"token": TOKEN}).json()["data"]

    assert "reaction" in detail and "reactions" in detail
    # Nothing in either contract is called simply `change`, which could mean either meaning.
    assert "change" not in schemas_news.NewsReactionSummaryData.model_fields
    assert "change_pct" in schemas_news.NewsQuoteData.model_fields
    assert "change_basis" in schemas_news.NewsQuoteData.model_fields


def test_status_reports_the_price_plane_beside_the_pipeline(client) -> None:
    api, _ = client
    data = api.get("/api/news/status", params={"token": TOKEN}).json()["data"]
    assert data["price"]["metric_version"] == "reaction_v1"
    assert data["price"]["sources"] == []
    # The backlog SLO has to be *served*, not merely declared: the envelope drops unset fields, so a schema
    # default with no repository value disappears from the response entirely.
    assert data["price"]["oldest_due_age_ms"] == 0
