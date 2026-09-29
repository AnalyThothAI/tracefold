"""News V3 HTTP contract: read surfaces plus the narrow ReviewDesk mutation adapter."""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from typing import Any, get_args

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from tests.support.news_event_updates import first_update, notify_plan, raised_update
from tracefold.app.http.app import create_app
from tracefold.app.http.schemas import events as event_schemas
from tracefold.app.http.schemas import feed as feed_schemas
from tracefold.app.http.schemas import news_common as news_common_schemas
from tracefold.app.http.schemas import status as status_schemas
from tracefold.news import EVENT_KINDS, MARKET_KINDS
from tracefold.news.market_review.instruments import InstrumentSearchIdentity
from tracefold.news.market_review.pricing import REACTION_METRIC_VERSION
from tracefold.news.models import Admission
from tracefold.news.update_view import (
    event_update_view,
    intent_views,
    notification_view,
    semantic_view,
    sent_headline,
)
from tracefold.news.updates.identity import canonical_json
from tracefold.platform.config.models import Settings
from tracefold.platform.observability import TelemetryRegistry

TOKEN = "contract-token"


def _event(event_id: str = "ev-1") -> dict[str, Any]:
    return {
        "event_id": event_id,
        "event_kind": "news",
        "leader_title": "Copper surges toward record on LME",
        "leader_url": "https://example.test/copper",
        "leader_description": "",
        "reporting_origin": "example.test",
        "opened_at_ms": 1_800_000_000_000,
        "last_member_at_ms": 1_800_000_000_000,
        "member_count": 1,
        "admission": "candidate",
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


_OUTCOME = {"kind": "no_update", "text_zh": "仅有来源", "reason_zh": "没有当前语义工作记录", "group": "held"}


class _FakeNewsRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.events = [_event()]
        self.event_assets_by_id = {"ev-1": ["COPPER", "SPOT"]}
        self.detail_overrides: dict[str, dict[str, Any]] = {}

    def item_related_events(self, *, item_id: str, after_event_id: str | None, limit: int) -> dict[str, Any]:
        self.calls.append(
            ("item_related_events", {"item_id": item_id, "after_event_id": after_event_id, "limit": limit})
        )
        return {"item_id": item_id, "total_events": 0, "events": [], "next_cursor": None}

    def list_feed(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list_feed", kwargs))
        if kwargs.get("cursor") == "broken":
            raise ValueError("news_feed_cursor_invalid")
        search = kwargs.get("search")
        return {
            "events": [{**event, "outcome": _OUTCOME} for event in self.events],
            "next_cursor": None,
            "counts": None
            if kwargs.get("cursor")
            else {"total": len(self.events), "pushed": 0, "held": len(self.events), "pending": 0},
            "filters": {
                "source_authority": ",".join(kwargs.get("source_authority") or ()) or None,
                "subject_code": ",".join(kwargs.get("subject_code") or ()) or None,
                "event_kind": ",".join(kwargs.get("event_kind") or ()) or None,
                "admission": kwargs["admission"],
                "symbol": search.symbol if search else None,
                "q": search.q if search else None,
                "limit": kwargs["limit"],
                "outcome": kwargs.get("outcome"),
                "hours": kwargs.get("hours"),
            },
            "search": search.public_metadata() if search else None,
        }

    def event_detail(self, event_id: str) -> dict[str, Any] | None:
        self.calls.append(("event_detail", {"event_id": event_id}))
        event = next((event for event in self.events if event["event_id"] == event_id), None)
        if event is None:
            return None
        return {
            "event": dict(event),
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
            "deliveries": [],
            "feedback": {"feedback_n": 0, "latest": None},
            "evidence_snapshots": [],
            "reader_receipt": {
                "state": "not_received",
                "delivery_state": None,
                "error_code": None,
                "received_at_ms": None,
                "rendered_card": None,
            },
        } | self.detail_overrides.get(event_id, {})

    def event_asset_symbols(self, event_ids: Any) -> dict[str, list[str]]:
        requested = [str(event_id) for event_id in event_ids]
        self.calls.append(("event_asset_symbols", {"event_ids": requested}))
        return {
            event_id: list(self.event_assets_by_id[event_id])
            for event_id in requested
            if event_id in self.event_assets_by_id
        }

    def asset_usage_24h(self, *, now_ms: int) -> dict[str, list[str]]:
        self.calls.append(("asset_usage_24h", {"now_ms": now_ms}))
        return {"ev-1": ["COPPER", "SPOT"], "ev-2": ["SPOT"]}

    def semantic_status(self, *, now_ms: int) -> dict[str, Any]:
        self.calls.append(("semantic_status", {"now_ms": now_ms}))
        return {
            "semantic_observations_24h": 0,
            "semantic_adopted_24h": 0,
            "semantic_failed_24h": 0,
            "semantic_pending": 0,
            "semantic_failed_by_code_24h": {},
        }

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
            "delivery": {
                "sent_24h": 0,
                "sent_1h": 0,
                "terminal_24h": 0,
                "last_error_code": None,
                "e2e_p50_ms": None,
                "e2e_p95_ms": None,
            },
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
        }


class _FakeInstrumentsRepository:
    """#75 universe as the status route sees it before any snapshot has landed."""

    def asset_refs(self, symbols: Any) -> dict[str, dict[str, Any]]:
        # Keyed by the raw provider tag; `symbol` comes back normalized (the real one strips `XYZ-`).
        listed = {"BTR": "binance.perp", "COPPER": "hl.xyz"}
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

    def search_identity(self, symbol: str, *, allow_pair: bool = True) -> InstrumentSearchIdentity | None:
        token = str(symbol).upper()
        if token in {"BTC", "BTCUSDT", "BTC/USDT", "BTC-USDT", "BTC_USDT"}:
            assert allow_pair is True
            return InstrumentSearchIdentity(base_symbol="BTC", event_symbols=("BTC",))
        return None

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

    def contracts_for(self, base_symbol: str, *, limit: int = 24) -> list[dict[str, Any]]:
        del limit
        if str(base_symbol).upper() != "COPPER":
            return []
        return [
            {
                "venue": "hl.xyz",
                "venue_symbol": "XYZ-COPPER",
                "instrument_class": "commodity",
                "quote_asset": "USDC",
                "reference_only": False,
            },
            # A second contract on the same venue, which is the ordinary case: WIF is `WIFUSDT` and
            # `WIFUSDC` on `binance.perp`. `venues` answers "which venues", so it must not repeat one.
            {
                "venue": "hl.xyz",
                "venue_symbol": "XYZ-COPPER-B",
                "instrument_class": "commodity",
                "quote_asset": "USDT",
                "reference_only": False,
            },
            {
                "venue": "us.listed",
                "venue_symbol": "HG",
                "instrument_class": "commodity",
                "quote_asset": None,
                "reference_only": True,
            },
        ]

    def is_tradeable(self, base_symbol: str) -> bool:
        return str(base_symbol).upper() == "COPPER"

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

    def quotes_for_symbols(self, requests: Any, **kwargs: Any) -> list[dict[str, Any]]:
        symbols = [request.symbol for request in requests]
        self.calls.append(("quotes_for_symbols", {"symbols": symbols, **kwargs}))
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
                "received_age_ms": None,
                "source_age_ms": None,
                "effective_age_ms": None,
                "freshness_basis": None,
                "reference_at_ms": None,
                "reference_age_ms": None,
                "state": "unlisted",
                "state_zh": "无可交易合约",
            }
            for symbol in symbols
        ]

    def price_status(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("price_status", kwargs))
        return {
            "metric_version": REACTION_METRIC_VERSION,
            "oldest_due_age_ms": 0,
            "sources": [],
            "fresh_sources": 0,
            "quotes": 0,
            "reaction_partial_7d": 0,
            "reaction_complete_7d": 0,
            "reaction_unavailable_7d": 0,
        }


class _FakeRepositories:
    def __init__(self, news: _FakeNewsRepository, workers_runtime_row: dict[str, Any] | None = None) -> None:
        self.news = news
        self.instruments = _FakeInstrumentsRepository()
        self.price = _FakePriceRepository()
        self._workers_runtime_row = workers_runtime_row

    def workers_runtime_row(self) -> dict[str, Any] | None:
        return self._workers_runtime_row

    def compile_news_search(self, *, q: str | None, symbol: str | None):
        from tracefold.news.search import compile_news_search

        return compile_news_search(q=q, symbol=symbol, instruments=self.instruments)


class _FakeRuntime:
    def __init__(
        self,
        settings: Settings,
        news: _FakeNewsRepository,
        workers_runtime_row: dict[str, Any] | None = None,
    ) -> None:
        self.settings = settings
        self._news = news
        self.telemetry = TelemetryRegistry()
        self._workers_runtime_row = workers_runtime_row

    @contextmanager
    def repositories(self):
        yield _FakeRepositories(self._news, self._workers_runtime_row)


@pytest.fixture
def client() -> tuple[TestClient, _FakeNewsRepository]:
    settings = Settings(ws_token=TOKEN)
    app = create_app(settings=settings)
    news = _FakeNewsRepository()
    app.state.service = _FakeRuntime(settings, news)
    return TestClient(app), news


def test_news_exposes_read_routes_and_no_write_route_at_all() -> None:
    app = create_app(settings=Settings(ws_token=TOKEN))
    routes = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/news/")
        for method in route.methods
    }

    assert routes == {
        ("GET", "/api/news/feed"),
        ("GET", "/api/news/events/{event_id}"),
        ("GET", "/api/news/items/{item_id}/events"),
        ("GET", "/api/news/status"),
        # #88: current quotes and 命中复盘. Both are read-only and bounded; quotes stay off the feed so a
        # price tick cannot invalidate the feed's ETag every three seconds.
        ("GET", "/api/news/quotes"),
        # #207 PR-W1: what one base_symbol *is*, for the token page every asset chip now links to.
        ("GET", "/api/news/symbols/{base}"),
        # #553: a market observation is a stored fact, not an Event, and this is the only surface that
        # serves one. The feed cannot answer the same question a second time and disagree.
        ("GET", "/api/news/market"),
        ("GET", "/api/news/market/{item_id}"),
        # #572 PR-3: the chain wallet tape's own state. The market list answers what observations
        # arrived; these two answer what the tape is doing -- its roster, its ingest position, and the
        # price receipt taken after every card its rules opened.
        ("GET", "/api/news/wallets"),
        ("GET", "/api/news/wallets/events"),
        ("GET", "/api/news/wallets/events/{episode_id}"),
    }


def test_news_schemas_publish_current_event_update_and_feedback_only() -> None:
    assert set(feed_schemas.NewsFeedFiltersData.model_fields) == {
        "source_authority",
        "subject_code",
        "event_kind",
        "admission",
        "symbol",
        "q",
        "limit",
        "outcome",
        "hours",
    }
    assert set(feed_schemas.NewsFeedEventData.model_fields) - set(event_schemas.NewsEventData.model_fields) == {
        "outcome",
        "update",
        "delivery",
        "reaction",
    }
    assert set(event_schemas.NewsEventDetailData.model_fields) == {
        "event",
        "outcome",
        "event_update",
        "processing",
        "timeline",
        "members",
        "deliveries",
        "feedback",
        "evidence_snapshots",
        "reader_receipt",
        "normalization",
        "reaction",
        "reactions",
    }
    assert set(event_schemas.NewsEventFeedbackData.model_fields) == {"feedback_n", "latest"}
    assert set(status_schemas.NewsSourceContractStageCountsData.model_fields) == {"received", "parsed", "adopted"}
    assert {"received", "admitted", "adopted", "selected", "delivered"} <= set(
        status_schemas.NewsFunnelData.model_fields
    )
    assert not hasattr(news_common_schemas, "NewsLegacyVerdictData")
    assert not hasattr(event_schemas, "NewsVerdictData")
    assert not hasattr(event_schemas, "NewsAcceptedReviewData")


def test_feed_returns_validated_envelope_and_forwards_bounded_filters(client) -> None:
    http, news = client

    response = http.get(
        "/api/news/feed",
        params={"token": TOKEN, "subject_code": "medtop:16000000,medtop:04000000", "limit": 5},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["filters"] == {
        "source_authority": None,
        "subject_code": "medtop:04000000,medtop:16000000",
        "event_kind": None,
        "admission": None,
        "symbol": None,
        "q": None,
        "limit": 5,
        "outcome": None,
        "hours": None,
    }
    assert body["data"]["events"][0]["event_id"] == "ev-1"
    assert "priority" not in body["data"]["events"][0]
    assert body["data"]["events"][0]["outcome"]["kind"] == "no_update"
    assert "title_zh" not in body["data"]["events"][0]
    # Raw provider/Gate evidence stays, while the durable Event-asset ledger is resolved beside it — so the
    # browser can strike through a symbol that names nothing without owning a symbol table.
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
    # Pattern/bound violations are rejected by the FastAPI query validators (422).
    assert http.get("/api/news/feed", params={"token": TOKEN, "outcome": "bogus"}).status_code == 422
    assert http.get("/api/news/feed", params={"token": TOKEN, "hours": 999}).status_code == 422


def test_feed_rejects_mixed_asset_and_text_search_before_repository_work(client) -> None:
    http, news = client

    response = http.get(
        "/api/news/feed",
        params={"token": TOKEN, "q": "BTC", "symbol": "BTC"},
    )

    assert response.status_code == 400
    assert response.json() == {
        "ok": False,
        "error": "news_feed_search_conflict",
        "field": "q",
    }
    assert news.calls == []


def test_feed_compiles_search_metadata_and_records_only_first_page_requests(client) -> None:
    http, news = client

    first = http.get("/api/news/feed", params={"token": TOKEN, "q": "$btc"})
    paged = http.get("/api/news/feed", params={"token": TOKEN, "q": "$btc", "cursor": "abc"})
    text = http.get("/api/news/feed", params={"token": TOKEN, "q": "bitcoin ETF"})

    assert first.status_code == paged.status_code == 200
    assert first.json()["data"]["search"] == {
        "mode": "asset",
        "normalized_query": "BTC",
        "resolved_symbols": ["BTC"],
    }
    forwarded = news.calls[0][1]
    assert forwarded["search"].event_symbols == ("BTC",)
    assert "q" not in forwarded and "symbol" not in forwarded
    # Even with the same Events, server-owned search explanation is response identity and therefore changes
    # the strong ETag. A cache cannot replay an AssetSearch explanation for a TextSearch response.
    assert first.headers["etag"] != text.headers["etag"]
    metrics = http.get("/metrics").text
    assert 'tracefold_news_search_requests_total{mode="asset",result="nonzero"} 1.0' in metrics


def test_feed_records_zero_result_text_search_without_user_text_labels(client) -> None:
    http, news = client
    news.events = []

    response = http.get("/api/news/feed", params={"token": TOKEN, "q": "private-query"})

    assert response.status_code == 200
    assert response.json()["data"]["search"] == {
        "mode": "text",
        "normalized_query": "private-query",
        "resolved_symbols": [],
    }
    metrics = http.get("/metrics").text
    assert 'tracefold_news_search_requests_total{mode="text",result="zero"} 1.0' in metrics
    assert "private-query" not in metrics


def test_feed_forwards_all_current_filters_in_canonical_order(client) -> None:
    http, news = client

    response = http.get(
        "/api/news/feed",
        params={
            "token": TOKEN,
            "source_authority": "unknown,issuer_first_party",
            "subject_code": "medtop:16000000,medtop:04000000",
            "event_kind": "listing,news",
        },
    )

    assert response.status_code == 200
    forwarded = news.calls[0][1]
    assert {"event_family", "change_state", "assertion_status"}.isdisjoint(forwarded)
    assert forwarded["source_authority"] == ("issuer_first_party", "unknown")
    assert forwarded["subject_code"] == ("medtop:04000000", "medtop:16000000")
    assert forwarded["event_kind"] == ("news", "listing")
    filters = response.json()["data"]["filters"]
    assert filters["source_authority"] == "issuer_first_party,unknown"
    assert filters["subject_code"] == "medtop:04000000,medtop:16000000"
    assert filters["event_kind"] == "news,listing"


@pytest.mark.parametrize("admission", sorted(get_args(Admission)))
def test_feed_accepts_every_admission_the_gate_can_still_produce(client, admission: str) -> None:
    """The filter vocabulary is read from the Gate's own, so no admitted Event can become unreachable.

    #553 deleted the three market admissions with the lane that set them. Deriving the parameters from
    `Admission` rather than restating them is what keeps the console's tabs and the Gate from drifting
    apart again: a sixth admission that no filter can spell would serve an empty list forever.
    """

    http, news = client

    response = http.get("/api/news/feed", params={"token": TOKEN, "admission": admission})

    assert response.status_code == 200
    assert news.calls[0][1]["admission"] == admission


def test_the_feed_is_no_longer_a_second_copy_of_the_live_market(client) -> None:
    """#553: an OI or liquidation observation is served by `/api/news/market` and by nothing else.

    While those frames became Events, the same provider record was readable twice -- once as a market
    row and once as an editorial Event -- and the two copies aged apart, each carrying a state the
    other could contradict. The feed keeps exactly one read of its own, over a vocabulary in which the
    market kinds cannot be spelled at all.
    """

    http, news = client

    assert http.get("/api/news/feed", params={"token": TOKEN}).status_code == 200
    assert [name for name, _ in news.calls] == ["list_feed", "event_asset_symbols"]
    assert set(MARKET_KINDS).isdisjoint(EVENT_KINDS)
    for market_kind in MARKET_KINDS:
        rejected = http.get("/api/news/feed", params={"token": TOKEN, "event_kind": market_kind})
        assert rejected.status_code == 400, market_kind
        assert rejected.json() == {"ok": False, "error": "news_feed_event_kind_invalid", "field": "event_kind"}


def test_feed_reports_tab_counts_on_the_first_page_only(client) -> None:
    http, _ = client

    first = http.get("/api/news/feed", params={"token": TOKEN}).json()["data"]
    paged = http.get("/api/news/feed", params={"token": TOKEN, "cursor": "abc"}).json()["data"]

    assert first["counts"] == {"total": 1, "pushed": 0, "held": 1, "pending": 0}
    assert paged["counts"] is None


@pytest.mark.parametrize(
    ("params", "error", "field"),
    [
        ({"admission": "bogus"}, "news_feed_admission_invalid", "admission"),
        # #706: the retired taxonomy axes are refused rather than ignored, so a stale console link cannot
        # serve an unfiltered feed under a filtered heading.
        ({"event_family": "other"}, "unsupported_query_param", "event_family"),
        ({"change_state": "announced"}, "unsupported_query_param", "change_state"),
        ({"assertion_status": "confirmed"}, "unsupported_query_param", "assertion_status"),
        ({"source_authority": "blog"}, "news_feed_source_authority_invalid", "source_authority"),
        ({"subject_code": "topic:1"}, "news_feed_subject_code_invalid", "subject_code"),
        ({"final_decision": "push"}, "unsupported_query_param", "final_decision"),
        # #553: the three market admissions are gone with the lane that set them. A stale console link
        # must be named invalid rather than fall through to "no filter", which would serve the whole
        # feed under a tab whose count says otherwise.
        ({"admission": "telemetry_deterministic"}, "news_feed_admission_invalid", "admission"),
        ({"admission": "liquidation_deterministic"}, "news_feed_admission_invalid", "admission"),
        ({"admission": "unsupported_market_contract"}, "news_feed_admission_invalid", "admission"),
        # #553: `?oi=` was the OI lane's own tab filter, and the parameter is refused rather than
        # ignored — a silently dropped filter serves an unfiltered feed under a filtered heading.
        ({"oi": "stored"}, "unsupported_query_param", "oi"),
        ({"direction": "up"}, "unsupported_query_param", "direction"),
        ({"event_kind": "social"}, "news_feed_event_kind_invalid", "event_kind"),
        ({"family": "general"}, "unsupported_query_param", "family"),
        ({"decision": "push"}, "unsupported_query_param", "decision"),
        ({"channel": "news"}, "unsupported_query_param", "channel"),
        ({"cursor": "broken"}, "news_feed_cursor_invalid", "cursor"),
        ({"priority": "high"}, "unsupported_query_param", "priority"),
        ({"sort": "priority"}, "unsupported_query_param", "sort"),
        ({"story_id": "x"}, "unsupported_query_param", "story_id"),
    ],
)
def test_feed_rejects_invalid_filters_with_bounded_400(client, params, error, field) -> None:
    http, _ = client

    response = http.get("/api/news/feed", params={"token": TOKEN, **params})

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": error, "field": field}


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}])
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


def test_deterministic_event_assets_project_to_feed_and_detail(client) -> None:
    """The durable Event-asset ledger is public even when the provider grounded no coin tag (#287).

    `listing_deterministic` is the one deterministic admission left after #553, and it is exactly the
    case that needs this: an exchange notice names its ticker in prose the Gate does not tag.
    """

    http, news = client
    news.events = [
        {
            **_event("ev-listing"),
            "event_kind": "listing",
            "grounded_assets": [],
            "admission": "listing_deterministic",
        },
        _event("ev-news"),
    ]
    news.event_assets_by_id = {"ev-listing": ["BTR"], "ev-news": ["COPPER", "SPOT"]}

    feed = http.get("/api/news/feed", params={"token": TOKEN})
    detail = http.get("/api/news/events/ev-listing", params={"token": TOKEN})

    assert feed.status_code == detail.status_code == 200
    feed_event = next(event for event in feed.json()["data"]["events"] if event["event_id"] == "ev-listing")
    detail_event = detail.json()["data"]["event"]
    expected = [{"symbol": "BTR", "base_symbol": "BTR", "venue": "binance.perp", "listed": True}]
    assert feed_event["grounded_assets"] == detail_event["grounded_assets"] == []
    assert feed_event["assets"] == detail_event["assets"] == expected


def test_status_counts_grounding_from_both_owners_without_either_reaching_across(client) -> None:
    """News owns Event-asset identity, the universe owns what it names; the route folds them (#87/#267)."""

    http, news = client

    body = http.get("/api/news/status", params={"token": TOKEN}).json()

    # ev-1 has COPPER (listed) so it grounds; ev-2 has only SPOT so it does not.
    assert body["data"]["funnel_24h"]["grounded"] == 1
    assert body["data"]["pipeline"]["ungrounded_by_symbol_24h"] == {"SPOT": 2}
    assert {"stage": "ungrounded", "key": "SPOT", "label_zh": "SPOT", "count": 2} in body["data"]["reasons_24h"]
    assert any(call[0] == "asset_usage_24h" for call in news.calls)


def test_event_detail_returns_current_envelope_or_missing_state(client) -> None:
    http, _ = client

    found = http.get("/api/news/events/ev-1", params={"token": TOKEN})
    assert found.status_code == 200
    assert found.json()["data"]["event"]["event_id"] == "ev-1"
    assert "priority" not in found.json()["data"]["event"]
    assert found.json()["data"]["members"] == []

    missing = http.get("/api/news/events/ev-404", params={"token": TOKEN})
    assert missing.status_code == 404
    assert missing.json() == {"ok": False, "error": "news_event_not_found"}

    too_long = http.get(f"/api/news/events/{'x' * 129}", params={"token": TOKEN})
    assert too_long.status_code == 400
    assert too_long.json() == {"ok": False, "error": "news_event_id_invalid", "field": "event_id"}


def test_item_related_events_is_bounded_authenticated_and_on_demand(client) -> None:
    http, news = client
    response = http.get("/api/news/items/item%3Ashared/events", params={"token": TOKEN, "after": "ev-4", "limit": 5})
    assert response.status_code == 200
    assert response.json()["data"] == {"item_id": "item:shared", "total_events": 0, "events": [], "next_cursor": None}
    assert news.calls[-1] == ("item_related_events", {"item_id": "item:shared", "after_event_id": "ev-4", "limit": 5})
    assert http.get("/api/news/items/item%3Ashared/events", params={"limit": 5}).status_code == 401
    assert http.get("/api/news/items/item%3Ashared/events", params={"token": TOKEN, "limit": 51}).status_code == 422


def test_event_detail_serves_the_event_update_and_its_processing(client) -> None:
    """#706: the detail of a News Agent Event, projected by the production read view, through the schema.

    The head is a real `assemble_update` revision -- a 25% tariff raised to 50% -- whose first revision
    was supported by one source and refuted by another. The Console must see what changed against which
    earlier statement, each source's relation, the inference labelled as one, the gap, and what the
    notification planner and the deliverer actually did, with the exact body sent.
    """

    http, news = client
    head = first_update("ev-1")
    raised = raised_update(head)
    plan = notify_plan(raised, key=True)
    body = "【重点】钢铁进口关税上调至 50%"
    ledger = {
        "intent_id": plan.intent_id,
        "kind": "update",
        "state": "sent",
        "card": {"headline_zh": "钢铁进口关税上调至 50%"},
        "receipt": {"channel": "telegram", "message_id": 42},
        "error_code": None,
        "attempted_at_ms": raised.adopted_at_ms + 10,
        "settled_at_ms": raised.adopted_at_ms + 20,
        "created_at_ms": raised.adopted_at_ms + 10,
        "content_revision": raised.content_revision,
        "claim_refs": list(plan.selected_claim_refs),
        "body": body,
        "payload_sha256": "f" * 64,
        "plan_key": True,
    }
    intents = intent_views([], [ledger])
    document = json.loads(canonical_json(raised))
    prior = {(head.ref, claim.ref): {"statement": claim.statement, "event_id": "ev-1"} for claim in head.claims}
    update = event_update_view({"document": document}, previous_claims=prior, sent_headline=sent_headline(intents))
    assert update is not None
    statements = {claim["ref"]: claim["statement"] for claim in update["claims"]}
    news.detail_overrides["ev-1"] = {
        "event_update": update,
        "processing": {
            "semantic": semantic_view(
                {"wanted_revision": 2, "done_revision": 2, "attempts": 1, "updated_at_ms": raised.adopted_at_ms}
            ),
            "observations": [],
            "notification": notification_view(
                {
                    "state": "done",
                    "content_revision": raised.content_revision,
                    "plan": json.loads(canonical_json(plan)),
                    "attempts": 0,
                    "updated_at_ms": raised.adopted_at_ms + 5,
                },
                statements=statements,
            ),
            "intents": intents,
            "update_error_code": None,
        },
    }

    response = http.get("/api/news/events/ev-1", params={"token": TOKEN})

    assert response.status_code == 200
    data = response.json()["data"]
    served = data["event_update"]
    # The headline a reader actually received outranks any claim text.
    assert (served["headline"], served["headline_source"]) == ("钢铁进口关税上调至 50%", "sent_card")
    assert served["topics"] == [{"code": "medtop:20000384", "label_zh": "关税"}]
    assert [(change["kind"], change["previous_statement"]) for change in served["changes"]] == [
        ("parameter_change", head.claims[0].statement)
    ]
    first, second = served["claims"]
    assert (first["mode"], first["phase"], first["phase_zh"]) == ("decision", "announced", "已宣布")
    assert first["effective_at"] == "2026-10-01" and first["conditions"] == ["unless a deal is signed"]
    assert first["quantities"] == [{"name": "rate", "value": "25", "unit": "%", "period": None}]
    assert first["disputed"] is True and second["disputed"] is False
    assert served["disputed_claim_refs"] == [first["ref"]]
    relations = {
        (source["source"]["publisher_id"], row["relation"])
        for source in served["sources"]
        for row in source["relations"]
    }
    assert {("wire", "supports"), ("rival", "refutes")} <= relations
    assert served["implications"][0]["origin_zh"] == "来源所述因果（推断）"
    assert served["open_questions"][0]["question"] == "Has the order been signed?"
    processing = data["processing"]
    assert processing["semantic"]["state"] == "done"
    decisions = processing["notification"]["plan"]["claim_decisions"]
    assert {row["reason_zh"] for row in decisions} == {"新增信息重要，标为重点"}
    assert processing["notification"]["plan"]["origin"] == "reader_v2"
    assert processing["notification"]["plan"]["key"] is True
    assert processing["intents"][0]["state"] == "sent" and processing["intents"][0]["body"] == body


def test_source_only_event_has_no_synthetic_legacy_judgment(client) -> None:
    http, _ = client
    data = http.get("/api/news/events/ev-1", params={"token": TOKEN}).json()["data"]
    assert data.get("event_update") is None
    assert data.get("processing") is None
    assert data["feedback"] == {"feedback_n": 0, "latest": None}
    assert "legacy_verdict" not in data and "verdicts" not in data


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
        "last_publish_error_code": None,
        "last_publish_error_at_ms": None,
    }
    assert data["delivery"]["delivery_available"] is False
    assert "hourly_cap" not in data["delivery"]
    assert isinstance(data["watchlist"], list)


def test_status_reports_the_latest_broker_publish_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings.model_validate(
        {
            "ws_token": TOKEN,
            "news": {
                "broker": {"url": "amqp://guest:guest@127.0.0.1:5672/"},
            },
        }
    )
    news = _FakeNewsRepository()
    original = news.status_snapshot

    def status_snapshot(*, now_ms: int) -> dict[str, Any]:
        snapshot = original(now_ms=now_ms)
        snapshot["broker"] = {
            "connected": True,
            "queues": {},
            "error_code": None,
            "observed_at_ms": now_ms,
            "last_publish_error_code": "news_broker_publish_failed:TimeoutError",
            "last_publish_error_at_ms": now_ms - 5_000,
        }
        return snapshot

    news.status_snapshot = status_snapshot  # type: ignore[method-assign]
    app = create_app(settings=settings)
    app.state.service = _FakeRuntime(settings, news)

    response = TestClient(app).get("/api/news/status", params={"token": TOKEN})

    assert response.status_code == 200
    broker = response.json()["data"]["broker"]
    assert broker["last_publish_error_code"] == "news_broker_publish_failed:TimeoutError"
    assert broker["last_publish_error_at_ms"] == broker["observed_at_ms"] - 5_000


def test_status_does_not_call_a_declared_target_available_without_running_workers() -> None:
    settings = Settings.model_validate(
        {
            "ws_token": TOKEN,
            "news": {
                "enabled": True,
                "push": {
                    "enabled": True,
                    "telegram_bot_token_file": "telegram_bot_token",
                    "telegram_chat_id": -1001234567890,
                },
            },
        }
    )
    app = create_app(settings=settings)
    news = _FakeNewsRepository()
    app.state.service = _FakeRuntime(settings, news)

    response = TestClient(app).get("/api/news/status", params={"token": TOKEN})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["workers_state"] is None
    assert data["delivery"]["delivery_available"] is False


def _running_workers_row(now_ms: int, capabilities: dict[str, Any]) -> dict[str, Any]:
    return {
        "runtime_id": "00000000-0000-0000-0000-0000000000aa",
        "runtime_version": "2",
        "lifecycle_state": "running",
        "started_at_ms": now_ms - 1_000,
        "heartbeat_at_ms": now_ms,
        "fatal_code": None,
        "capabilities": capabilities,
    }


@pytest.mark.parametrize(
    ("delivery_capability", "available"),
    [
        ({"state": "running", "reason": None}, True),
        ({"state": "unavailable", "reason": "news_item_push_telegram_bot_token_unavailable"}, False),
        ({"state": "faulted", "reason": "news-deliverer:RuntimeError"}, False),
    ],
)
def test_status_reports_delivery_from_the_sender_workers_actually_built(
    delivery_capability: dict[str, Any],
    available: bool,
) -> None:
    """#553 PR-3 acceptance 2. Serve validates the declared target; Workers built (or did not) the sender.

    Only Workers reads the secure token file, so a target that is complete in `config.yaml` and
    unreadable on disk is visible here and nowhere else. Presenting it as delivery-ready would be
    presenting a field error as a delivery.
    """

    settings = Settings.model_validate(
        {
            "ws_token": TOKEN,
            "news": {
                "enabled": True,
                "push": {
                    "enabled": True,
                    "telegram_bot_token_file": "telegram_bot_token",
                    "telegram_chat_id": -1001234567890,
                },
            },
        }
    )
    app = create_app(settings=settings)
    app.state.service = _FakeRuntime(
        settings,
        _FakeNewsRepository(),
        _running_workers_row(int(time.time() * 1000), {"news_delivery": delivery_capability}),
    )

    response = TestClient(app).get("/api/news/status", params={"token": TOKEN})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["workers_state"] == "running"
    assert data["delivery"]["delivery_available"] is available


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
    app.state.service = _FakeRuntime(settings, _FakeNewsRepository())

    # This is a route contract over the injected fake runtime. Entering TestClient's lifespan would
    # bootstrap the production PostgreSQL runtime and make a hermetic test depend on the operator HOME.
    http = TestClient(app)
    try:
        response = http.get("/api/news/status", params={"token": TOKEN})
    finally:
        http.close()

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["pipeline"]["extraction_model"] == "shared-model"
    assert data["pipeline"]["card_model"] is None
    assert data["pipeline"]["card_fallback_model"] is None
    assert data["pipeline"]["card_fallback_dedicated"] is False
    assert data["pipeline"]["news_program_identity"] is None
    assert data["pipeline"]["judgment_backend"] is None
    assert data["health"]["model"] == {
        "level": "bad",
        "summary_zh": "卡片模型不可用",
        "detail_zh": "卡片模型配置无效；语义工作等待可用配置",
    }


def test_status_names_the_configured_routes_and_never_the_trading_jev(monkeypatch: pytest.MonkeyPatch) -> None:
    jev = {"api_key": "jev-key", "base_url": "https://openrouter.ai/api", "model": "jev-1.13"}
    for llm, backend, judgment_model in (
        ({"trading_semantics": jev}, "generated", "shared-model"),
        ({"news_judgment": jev}, "native", "jev-1.13"),
    ):
        settings = Settings.model_validate(
            {
                "ws_token": TOKEN,
                "llm": {
                    "api_key": "triage-key",
                    "base_url": "https://triage.test/v1",
                    "news_triage_model": "shared-model",
                    **llm,
                },
            }
        )
        app = create_app(settings=settings)
        app.state.service = _FakeRuntime(settings, _FakeNewsRepository())
        http = TestClient(app)
        try:
            response = http.get("/api/news/status", params={"token": TOKEN})
        finally:
            http.close()
        pipeline = response.json()["data"]["pipeline"]
        assert pipeline["judgment_backend"] == backend
        assert pipeline["judgment_model"] == judgment_model
        assert pipeline["news_judgment_configured"] is (backend == "native")
        assert pipeline["card_model"] == "shared-model" and pipeline["card_dedicated"] is False
        assert len(pipeline["news_program_identity"]) > 20
        assert "jev-key" not in response.text and "triage-key" not in response.text


def test_status_marks_the_product_degraded_when_model_outputs_are_unusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings.model_validate(
        {
            "ws_token": TOKEN,
            "llm": {
                "api_key": "triage-key",
                "base_url": "https://triage.test/v1",
                "news_triage_model": "triage-model",
            },
            "news": {
                "opennews_token": "opennews-token",
                "broker": {"url": "amqp://guest:guest@127.0.0.1:5672/"},
            },
        }
    )
    news = _FakeNewsRepository()
    original = news.status_snapshot

    def status_snapshot(*, now_ms: int) -> dict[str, Any]:
        snapshot = original(now_ms=now_ms)
        snapshot["ingest"] = {
            **snapshot["ingest"],
            "connected": True,
            "last_frame_at_ms": now_ms,
        }
        snapshot["broker"] = {"connected": True, "queues": {}, "error_code": None, "observed_at_ms": now_ms}
        return snapshot

    def semantic_status(*, now_ms: int) -> dict[str, Any]:
        # Every semantic turn of the day ended in a contract fault: nothing was understood.
        return {
            "semantic_observations_24h": 0,
            "semantic_adopted_24h": 0,
            "semantic_failed_24h": 20,
            "semantic_pending": 0,
            "semantic_failed_by_code_24h": {"news_generation_output_contract_invalid": 20},
        }

    news.status_snapshot = status_snapshot  # type: ignore[method-assign]
    news.semantic_status = semantic_status  # type: ignore[method-assign]
    app = create_app(settings=settings)
    app.state.service = _FakeRuntime(settings, news)
    monkeypatch.setattr(
        "tracefold.app.http.routes.status._news_workers_observation",
        lambda *_a, **_k: ("running", None),
    )

    response = TestClient(app).get("/api/news/status", params={"token": TOKEN})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["health"]["model"]["level"] == "bad"
    assert data["health"]["model"]["summary_zh"] == "24 小时语义失败率 100%（20/20）"
    assert data["pipeline"]["semantic_failed_by_code_24h"] == {"news_generation_output_contract_invalid": 20}
    assert data["state"] == "degraded"


def test_status_marks_the_product_degraded_when_any_health_lane_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings.model_validate(
        {
            "ws_token": TOKEN,
            "llm": {
                "api_key": "triage-key",
                "base_url": "https://triage.test/v1",
                "news_triage_model": "triage-model",
            },
            "news": {
                "opennews_token": "opennews-token",
                "broker": {"url": "amqp://guest:guest@127.0.0.1:5672/"},
            },
        }
    )
    news = _FakeNewsRepository()
    original = news.status_snapshot

    def status_snapshot(*, now_ms: int) -> dict[str, Any]:
        snapshot = original(now_ms=now_ms)
        snapshot["ingest"] = {
            **snapshot["ingest"],
            "connected": True,
            "last_frame_at_ms": now_ms,
        }
        snapshot["broker"] = {
            "connected": True,
            "queues": {"news.raw": {"messages": 50, "consumers": 1}},
            "error_code": None,
            "observed_at_ms": now_ms,
        }
        return snapshot

    news.status_snapshot = status_snapshot  # type: ignore[method-assign]
    app = create_app(settings=settings)
    app.state.service = _FakeRuntime(settings, news)
    monkeypatch.setattr(
        "tracefold.app.http.routes.status._news_workers_observation",
        lambda *_a, **_k: ("running", None),
    )

    response = TestClient(app).get("/api/news/status", params={"token": TOKEN})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["health"]["broker"]["level"] == "warn"
    assert data["health"]["overall"] == "warn"
    assert data["state"] == "degraded"


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
    assert set(payload["data"]["quotes"][0]) == set(event_schemas.NewsQuoteData.model_fields)


def test_the_symbol_card_names_every_contract_and_keeps_the_reference_tier_visible(client) -> None:
    api, _ = client
    response = api.get("/api/news/symbols/xyz-copper", params={"token": TOKEN})

    assert response.status_code == 200
    data = response.json()["data"]
    # The provider's `XYZ-` spelling and the reader's lowercase URL both resolve to the one base.
    assert data["base_symbol"] == "COPPER"
    assert data["known"] is True and data["tradeable"] is True
    assert data["venues"] == ["hl.xyz", "us.listed"]
    assert len(data["contracts"]) == 3, "every contract is listed even when two share a venue"
    # #91: `us.listed` proves the ticker exists, not that anyone can trade it — the page renders both, so
    # the flag has to survive rather than being filtered out of the list.
    assert [contract["reference_only"] for contract in data["contracts"]] == [False, False, True]
    assert data["normalization"] == {"base_symbol": "COPPER", "aliases": ["COPPER", "HG"], "sources": ["seed"]}


def test_a_symbol_no_venue_lists_is_an_answer_not_a_404(client) -> None:
    """Every asset chip is a link now, including the struck-through ones that resolved to nothing."""

    api, _ = client
    response = api.get("/api/news/symbols/NEAR", params={"token": TOKEN})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data == {
        "base_symbol": "NEAR",
        "known": False,
        "tradeable": False,
        "venues": [],
        "contracts": [],
        "normalization": None,
    }


def test_the_symbol_route_rejects_a_path_segment_that_is_not_a_base_symbol(client) -> None:
    api, _ = client
    for bad in ("../../etc", "A" * 25, "BTC USD"):
        response = api.get(f"/api/news/symbols/{bad}", params={"token": TOKEN})
        assert response.status_code in {400, 404}, bad
        if response.status_code == 400:
            assert response.json()["error"] == "news_symbol_invalid"


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
    assert set(feed_schemas.NewsFeedEventData.model_fields).isdisjoint({"quote", "quotes", "price"})


def test_event_detail_keeps_the_two_market_meanings_in_separate_fields(client) -> None:
    api, _ = client
    detail = api.get("/api/news/events/ev-1", params={"token": TOKEN}).json()["data"]

    assert "reaction" in detail and "reactions" in detail
    # Nothing in either contract is called simply `change`, which could mean either meaning.
    assert "change" not in event_schemas.NewsReactionSummaryData.model_fields
    assert "change_pct" in event_schemas.NewsQuoteData.model_fields
    assert "change_basis" in event_schemas.NewsQuoteData.model_fields


def test_status_reports_the_price_plane_beside_the_pipeline(client) -> None:
    api, _ = client
    data = api.get("/api/news/status", params={"token": TOKEN}).json()["data"]
    assert data["price"]["metric_version"] == REACTION_METRIC_VERSION
    assert data["price"]["sources"] == []
    # The backlog SLO has to be *served*, not merely declared: the envelope drops unset fields, so a schema
    # default with no repository value disappears from the response entirely.
    assert data["price"]["oldest_due_age_ms"] == 0
