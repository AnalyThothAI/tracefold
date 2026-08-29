"""News V3 HTTP contract: read surfaces plus the narrow ReviewDesk mutation adapter."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from tracefold.app.http.app import create_app
from tracefold.app.http.schemas import events as event_schemas
from tracefold.app.http.schemas import feed as feed_schemas
from tracefold.app.http.schemas import news_common as news_common_schemas
from tracefold.app.http.schemas import status as status_schemas
from tracefold.news.market_review.instruments import InstrumentSearchIdentity
from tracefold.platform.config.models import Settings
from tracefold.platform.observability import TelemetryRegistry

TOKEN = "contract-token"


def _event(event_id: str = "ev-1") -> dict[str, Any]:
    return {
        "event_id": event_id,
        "event_kind": "news",
        "source_contract_reason": None,
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


_OUTCOME = {"kind": "queued_publish", "text_zh": "待处理", "reason_zh": "已入库，等待送审", "group": "pending"}


class _FakeNewsRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.events = [_event()]
        self.event_assets_by_id = {"ev-1": ["COPPER", "SPOT"]}

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
            else {"total": len(self.events), "pushed": 0, "held": 0, "pending": len(self.events)},
            "filters": {
                "event_family": ",".join(kwargs.get("event_family") or ()) or None,
                "change_state": ",".join(kwargs.get("change_state") or ()) or None,
                "assertion_status": ",".join(kwargs.get("assertion_status") or ()) or None,
                "source_authority": ",".join(kwargs.get("source_authority") or ()) or None,
                "subject_code": ",".join(kwargs.get("subject_code") or ()) or None,
                "final_decision": ",".join(kwargs.get("final_decision") or ()) or None,
                "event_kind": ",".join(kwargs.get("event_kind") or ()) or None,
                "admission": kwargs["admission"],
                "symbol": search.symbol if search else None,
                "q": search.q if search else None,
                "limit": kwargs["limit"],
                "outcome": kwargs.get("outcome"),
                "hours": kwargs.get("hours"),
                "oi": kwargs.get("oi"),
                "direction": ",".join(kwargs.get("directions") or ()) or None,
            },
            "search": search.public_metadata() if search else None,
        }

    def event_detail(self, event_id: str) -> dict[str, Any] | None:
        self.calls.append(("event_detail", {"event_id": event_id}))
        if event_id == "archive":
            return {"archive_only": True}
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

    def oi_window_occupancy(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("oi_window_occupancy", kwargs))
        return [{"symbol": "WIF", "used": 2}, {"symbol": "DOGE", "used": 1}]

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
            "oi": {"by_rule_24h": {"opening_move_with_whale_concentration": 3, "whale_ratio_below_threshold": 7}},
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


class _FakeCursor:
    @staticmethod
    def fetchone() -> None:
        return None

    @staticmethod
    def fetchall() -> list[Any]:
        return []


class _FakeConnection:
    """`repos.conn` is a live read on this surface: `/api/news/status` reads the Workers runtime row."""

    @staticmethod
    def execute(*_args: Any, **_kwargs: Any) -> _FakeCursor:
        return _FakeCursor()


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

    def compile_news_search(self, *, q: str | None, symbol: str | None):
        from tracefold.news.search import compile_news_search

        return compile_news_search(q=q, symbol=symbol, instruments=self.instruments)


class _FakeRuntime:
    def __init__(self, settings: Settings, news: _FakeNewsRepository) -> None:
        self.settings = settings
        self._news = news
        self.telemetry = TelemetryRegistry()

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
        ("GET", "/api/news/status"),
        # #88: current quotes and 命中复盘. Both are read-only and bounded; quotes stay off the feed so a
        # price tick cannot invalidate the feed's ETag every three seconds.
        ("GET", "/api/news/quotes"),
        # #207 PR-W1: what one base_symbol *is*, for the token page every asset chip now links to.
        ("GET", "/api/news/symbols/{base}"),
    }


def test_news_schemas_are_exact_and_carry_no_retired_story_brief_surface() -> None:
    assert set(event_schemas.NewsQuoteData.model_fields) == {
        "requested_symbol",
        "symbol",
        "base_symbol",
        "venue",
        "venue_symbol",
        "instrument_class",
        "quote_asset",
        "price",
        "price_kind",
        "price_kind_zh",
        "change_pct",
        "change_basis",
        "change_basis_zh",
        "source_at_ms",
        "received_at_ms",
        "received_age_ms",
        "source_age_ms",
        "effective_age_ms",
        "freshness_basis",
        "reference_at_ms",
        "reference_age_ms",
        "state",
        "state_zh",
    }
    assert set(status_schemas.NewsQuoteVenueData.model_fields) == {
        "source_key",
        "target_count",
        "quote_count",
        "received_age_ms",
        "source_age_ms",
        "effective_age_ms",
        "freshness_basis",
        "state",
        "source_at_ms",
        "received_at_ms",
    }
    assert {
        "received_age_ms",
        "source_age_ms",
        "effective_age_ms",
        "freshness_basis",
        "reference_at_ms",
        "reference_age_ms",
    } <= {name for name, field in event_schemas.NewsQuoteData.model_fields.items() if field.is_required()}
    assert {"received_age_ms", "source_age_ms", "effective_age_ms", "freshness_basis"} <= {
        name for name, field in status_schemas.NewsQuoteVenueData.model_fields.items() if field.is_required()
    }
    assert set(feed_schemas.NewsFeedData.model_fields) == {"events", "next_cursor", "counts", "filters", "search"}
    assert set(feed_schemas.NewsFeedCountsData.model_fields) == {"total", "pushed", "held", "pending"}
    assert set(feed_schemas.NewsFeedSearchData.model_fields) == {
        "mode",
        "normalized_query",
        "resolved_symbols",
    }
    assert set(feed_schemas.NewsFeedFiltersData.model_fields) == {
        "event_family",
        "change_state",
        "assertion_status",
        "source_authority",
        "subject_code",
        "final_decision",
        "event_kind",
        "admission",
        "symbol",
        "q",
        "limit",
        "outcome",
        "hours",
        # #207: the deterministic OI lane's outcome, which `final_decision` cannot express.
        "oi",
        "direction",
    }
    assert set(feed_schemas.NewsFeedEventData.model_fields) - set(event_schemas.NewsEventData.model_fields) == {
        "outcome",
        "triage",
        "delivery",
        # #88: the fixed post-Event return. The *current* quote is deliberately not a feed field.
        "reaction",
        # #207: the deterministic OI judgment, read back from the trace it wrote; null on every other admission.
        "oi",
    }
    assert "family" not in event_schemas.NewsEventData.model_fields
    assert set(news_common_schemas.NewsTriageSummaryData.model_fields).isdisjoint(
        {"event_type", "event_type_zh", "actionable", "model_decision", "model_decision_zh", "title_zh"}
    )
    assert {"payload", "dimensions", "novelty"}.isdisjoint(event_schemas.NewsAcceptedReviewData.model_fields)
    assert set(event_schemas.NewsVerdictData.model_fields) == {
        "stage",
        "policy_version",
        "judgment_contract_version",
        "judgment_origin",
        "judgment_sha256",
        "verdict",
        "model_editorial",
        "rule_baseline_decision",
        "final_decision",
        "override_rule",
        "throttled_by",
        "model",
        "program_version",
        "program_sha256",
        "degraded",
        "error_code",
        "evidence_version",
        "evidence_sha256",
        "focus_fact_id",
        "published_at_ms",
        "created_at_ms",
    }
    assert set(status_schemas.NewsSourceContractStageCountsData.model_fields) == {
        "received",
        "parsed",
        "parse_failed",
        "unsupported",
        "verdict",
    }
    assert set(status_schemas.NewsSourceContracts24hData.model_fields) == {
        "news_v1",
        "listing_v1",
        "oi_v1",
        "liquidation_v1",
        "unsupported_market",
    }
    assert set(status_schemas.NewsDuplicatesWithheld24hData.model_fields) == {"all"}
    with pytest.raises(ValueError):
        status_schemas.NewsDuplicatesWithheld24hData.model_validate({"throttled": 1})
    assert {"source_classifier_version", "source_contracts_24h"} <= set(
        status_schemas.NewsPipelineStatusData.model_fields
    )
    assert {"funnel_parsed_24h", "novelty_defaulted_24h"}.isdisjoint(status_schemas.NewsPipelineStatusData.model_fields)
    assert set(status_schemas.NewsFunnelData.model_fields) == {
        "received",
        "admitted",
        "candidates",
        "triaged",
        "tagged",
        "grounded",
        "decided_push",
        "delivered",
        "received_1h",
        "delivered_1h",
    }
    assert set(event_schemas.NewsEventDetailData.model_fields) == {
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
    assert set(event_schemas.NewsDeliveryData.model_fields) == {
        "kind",
        "state",
        "error_code",
        "attempted_at_ms",
        "settled_at_ms",
        "card",
        "receipt",
        "pending_card",
        "edit_state",
        "edit_error_code",
        "edit_attempted_at_ms",
        "edit_settled_at_ms",
    }
    assert set(news_common_schemas.NewsAssetRefData.model_fields) == {"symbol", "base_symbol", "venue", "listed"}
    assert set(news_common_schemas.NewsSymbolNormalizationData.model_fields) == {"base_symbol", "aliases", "sources"}
    assert set(news_common_schemas.NewsTaxonomyData.model_fields) == {
        "taxonomy_version",
        "codebook_sha256",
        "subject_codes",
        "subject_labels_zh",
        "event_family",
        "event_family_zh",
        "change_state",
        "change_state_zh",
        "source_authority",
        "source_authority_zh",
        "assertion_status",
        "assertion_status_zh",
    }
    assert "taxonomy" in news_common_schemas.NewsTriageSummaryData.model_fields
    assert set(news_common_schemas.NewsOutcomeData.model_fields) == {"kind", "text_zh", "reason_zh", "group"}
    assert set(status_schemas.NewsStatusData.model_fields) == {
        "state",
        "workers_state",
        "health",
        "funnel_24h",
        "reasons_24h",
        "ingest",
        "broker",
        "pipeline",
        # #207: the deterministic OI lane, read by the 持仓异动 monitor. Its gate names live in the judge's
        # trace, so `pipeline.dropped_by_rule` — which groups `override_rule` — can never carry them.
        "oi",
        "delivery",
        "learning_retention",
        "watchlist",
        "instruments",
        # #88 §11: per-source quote freshness and Reaction backlog, beside the pipeline's own health.
        "price",
        "measured_at_ms",
    }
    assert set(status_schemas.NewsIngestStatusData.model_fields) == {
        "connected",
        "last_frame_at_ms",
        "last_publish_at_ms",
        "last_error_code",
        "open_incidents",
        "token_configured",
    }
    assert set(status_schemas.NewsDeliveryStatusData.model_fields) == {
        "sent_24h",
        "sent_1h",
        "terminal_24h",
        "last_error_code",
        "e2e_p95_ms",
        "e2e_p50_ms",
        "delivery_available",
    }
    assert set(status_schemas.NewsLearningRetentionStatusData.model_fields) == {
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
    for schema_module in (
        event_schemas,
        feed_schemas,
        news_common_schemas,
        status_schemas,
    ):
        for name in dir(schema_module):
            assert not any(
                marker in name for marker in ("Story", "Brief", "Rss", "TitleTranslation", "Notification")
            ), name


def test_current_verdict_schema_rejects_raw_and_cross_origin_payloads() -> None:
    verdict = {
        "novelty": "new_fact",
        "restates": -1,
        "assets": [{"symbol": "BTC", "market_type": "crypto", "role": "primary"}],
        "direction": "bullish",
        "scope": "single_name",
        "magnitude": 2,
        "confidence": 0.9,
        "audience": "crypto",
        "headline_zh": "BTC 获得新的市场准入",
        "why_zh": "准入状态发生变化",
    }
    model_editorial = {
        "taxonomy": {
            "taxonomy_version": "news_taxonomy_v1",
            "codebook_sha256": "6f978685c1ffeb6615bfb5dc05eecb9004ebb6f7de8732602e2823d09a12daac",
            "subject_codes": ["medtop:20000385"],
            "subject_labels_zh": ["市场与交易所"],
            "event_family": "market_access",
            "event_family_zh": "市场准入",
            "change_state": "effective",
            "change_state_zh": "已生效",
            "source_authority": "issuer_first_party",
            "source_authority_zh": "发行方一手来源",
            "assertion_status": "confirmed",
            "assertion_status_zh": "已确认",
        },
        "relevance": {
            "impact_breadth": "single_instrument",
            "tradability": "direct",
            "surprise": "unscheduled",
            "development_delta": "state_change",
            "channels": ["exchange_access"],
            "affected_markets": ["single_asset"],
            "reader_value": "realtime",
        },
    }
    payload = {
        "stage": "triage",
        "policy_version": "news_triage_policy_v11",
        "judgment_contract_version": "news_judgment_v2",
        "judgment_origin": "model",
        "judgment_sha256": "b" * 64,
        "verdict": verdict,
        "model_editorial": model_editorial,
        "model": "model-v1",
        "program_version": "news_semantic_program_v8",
        "program_sha256": "d" * 64,
        "rule_baseline_decision": "push",
        "final_decision": "push",
        "evidence_version": 1,
        "evidence_sha256": "e" * 64,
        "focus_fact_id": "fact",
        "created_at_ms": 1,
    }

    validated = event_schemas.NewsVerdictData.model_validate(payload)
    assert validated.judgment_origin == "model"
    assert validated.verdict.assets[0].market_type == "crypto"
    deterministic = {**payload, "judgment_origin": "oi", "model": None, "model_editorial": None}
    assert event_schemas.NewsVerdictData.model_validate(deterministic).judgment_origin == "oi"
    with pytest.raises(ValueError, match="news_verdict_model_identity_origin_mismatch"):
        event_schemas.NewsVerdictData.model_validate({**payload, "judgment_origin": "oi"})
    with pytest.raises(ValueError, match="news_verdict_model_identity_origin_mismatch"):
        event_schemas.NewsVerdictData.model_validate({**payload, "model_editorial": None})
    with pytest.raises(ValueError):
        event_schemas.NewsVerdictData.model_validate({**payload, "trace": {"raw": True}})
    with pytest.raises(ValueError, match="news_verdict_degraded_origin_mismatch"):
        event_schemas.NewsVerdictData.model_validate({**deterministic, "judgment_origin": "degraded"})
    with pytest.raises(ValueError):
        event_schemas.NewsEvidenceSnapshotData.model_validate(
            {
                "event_id": "ev",
                "evidence_version": 1,
                "focus_fact_id": "fact",
                "evidence_sha256": "c" * 64,
                "provenance": "legacy_reconstructed",
                "release_eligible": False,
                "created_at_ms": 1,
            }
        )


def test_feed_returns_validated_envelope_and_forwards_bounded_filters(client) -> None:
    http, news = client

    response = http.get(
        "/api/news/feed",
        params={"token": TOKEN, "event_family": "other,financial_results", "limit": 5},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["filters"] == {
        "event_family": "financial_results,other",
        "change_state": None,
        "assertion_status": None,
        "source_authority": None,
        "subject_code": None,
        "final_decision": None,
        "event_kind": None,
        "admission": None,
        "symbol": None,
        "q": None,
        "limit": 5,
        "outcome": None,
        "hours": None,
        "oi": None,
        "direction": None,
    }
    assert body["data"]["events"][0]["event_id"] == "ev-1"
    assert "priority" not in body["data"]["events"][0]
    assert body["data"]["events"][0]["outcome"]["kind"] == "queued_publish"
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
            "direction": "neutral,bullish",
            "event_family": "other,financial_results",
            "change_state": "unknown,announced",
            "assertion_status": "rumor,confirmed",
            "source_authority": "unknown,issuer_first_party",
            "subject_code": "medtop:16000000,medtop:04000000",
            "final_decision": "throttled,push",
            "event_kind": "unsupported_market,liquidation,oi,listing,news",
        },
    )

    assert response.status_code == 200
    forwarded = news.calls[0][1]
    assert forwarded["directions"] == ("bullish", "neutral")
    assert forwarded["event_family"] == ("financial_results", "other")
    assert forwarded["change_state"] == ("announced", "unknown")
    assert forwarded["assertion_status"] == ("confirmed", "rumor")
    assert forwarded["source_authority"] == ("issuer_first_party", "unknown")
    assert forwarded["subject_code"] == ("medtop:04000000", "medtop:16000000")
    assert forwarded["final_decision"] == ("push", "throttled")
    assert forwarded["event_kind"] == ("news", "listing", "oi", "liquidation", "unsupported_market")
    filters = response.json()["data"]["filters"]
    assert filters["direction"] == "bullish,neutral"
    assert filters["event_family"] == "financial_results,other"
    assert filters["change_state"] == "announced,unknown"
    assert filters["assertion_status"] == "confirmed,rumor"
    assert filters["source_authority"] == "issuer_first_party,unknown"
    assert filters["subject_code"] == "medtop:04000000,medtop:16000000"
    assert filters["final_decision"] == "push,throttled"
    assert filters["event_kind"] == "news,listing,oi,liquidation,unsupported_market"


@pytest.mark.parametrize("admission", ["liquidation_deterministic", "unsupported_market_contract"])
def test_feed_accepts_every_material_deterministic_admission(client, admission: str) -> None:
    http, news = client

    response = http.get("/api/news/feed", params={"token": TOKEN, "admission": admission})

    assert response.status_code == 200
    assert news.calls[0][1]["admission"] == admission


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
        ({"event_family": "general"}, "news_feed_event_family_invalid", "event_family"),
        ({"change_state": "new"}, "news_feed_change_state_invalid", "change_state"),
        ({"assertion_status": "maybe"}, "news_feed_assertion_status_invalid", "assertion_status"),
        ({"source_authority": "blog"}, "news_feed_source_authority_invalid", "source_authority"),
        ({"subject_code": "topic:1"}, "news_feed_subject_code_invalid", "subject_code"),
        ({"final_decision": "maybe"}, "news_feed_final_decision_invalid", "final_decision"),
        # #207: the OI outcome is a closed set. An unknown value must not fall through to "no filter" —
        # that would serve the whole lane under a tab whose count says otherwise.
        ({"oi": "whale_ratio_below_threshold"}, "news_feed_oi_invalid", "oi"),
        ({"direction": "up"}, "news_feed_direction_invalid", "direction"),
        ({"direction": "bullish,bullish"}, "news_feed_direction_invalid", "direction"),
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
    """The durable Event-asset ledger is public even when the provider grounded no coin tag (#287)."""

    http, news = client
    news.events = [
        {**_event("ev-oi"), "grounded_assets": [], "admission": "telemetry_deterministic"},
        _event("ev-news"),
    ]
    news.event_assets_by_id = {"ev-oi": ["BTR"], "ev-news": ["COPPER", "SPOT"]}

    feed = http.get("/api/news/feed", params={"token": TOKEN})
    detail = http.get("/api/news/events/ev-oi", params={"token": TOKEN})

    assert feed.status_code == detail.status_code == 200
    feed_event = next(event for event in feed.json()["data"]["events"] if event["event_id"] == "ev-oi")
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


def test_event_detail_returns_current_envelope_or_explicit_archive_and_missing_states(client) -> None:
    http, _ = client

    found = http.get("/api/news/events/ev-1", params={"token": TOKEN})
    assert found.status_code == 200
    assert found.json()["data"]["event"]["event_id"] == "ev-1"
    assert "priority" not in found.json()["data"]["event"]
    assert found.json()["data"]["members"] == []

    archive = http.get("/api/news/events/archive", params={"token": TOKEN})
    assert archive.status_code == 410
    assert archive.json() == {"ok": False, "error": "news_event_archive_only"}

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
    assert isinstance(data["watchlist"], list)


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
    assert data["pipeline"]["reader_card_model"] is None
    assert data["pipeline"]["reader_card_fallback_model"] is None
    assert data["pipeline"]["reader_card_fallback_dedicated"] is False
    assert data["health"]["model"] == {
        "level": "bad",
        "summary_zh": "Reader 模型不可用",
        "detail_zh": "ReaderCard 配置无效；所有事件按规则兜底",
    }


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
        snapshot["pipeline"] = {
            **snapshot["pipeline"],
            "model_triage_24h": 20,
            "triage_degraded_24h": 20,
            "triage_degraded_by_code_24h": {"news_program_event_semantics_invalid": 20},
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
    assert data["health"]["model"]["level"] == "bad"
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
    assert data["price"]["metric_version"] == "reaction_v1"
    assert data["price"]["sources"] == []
    # The backlog SLO has to be *served*, not merely declared: the envelope drops unset fields, so a schema
    # default with no repository value disappears from the response entirely.
    assert data["price"]["oldest_due_age_ms"] == 0


def test_status_reports_the_oi_lane_with_both_threshold_sets(client) -> None:
    """#207: what the 持仓异动 monitor reads, in one section of the status the whole console already polls."""

    api, news = client
    data = api.get("/api/news/status", params={"token": TOKEN}).json()["data"]

    # The gate names are the judge's own, from its trace. `pipeline.dropped_by_rule` groups `override_rule`,
    # which `decide()` sets to the admission for every OI verdict, so it can never carry them.
    assert data["oi"]["by_rule_24h"] == {
        "opening_move_with_whale_concentration": 3,
        "whale_ratio_below_threshold": 7,
    }
    assert "whale_ratio_below_threshold" not in data["pipeline"].get("dropped_by_rule", {})

    # The News gates, echoed from `news.oi` exactly as they are running.
    assert data["oi"]["policy"] == {
        "window_ms": 4 * 3_600_000,
        "max_rank_in_window": 2,
        "whale_oi_ratio_above_bps": 8_000,
        "oi_change_at_least_bps": 0,
    }
    # No `trade_floors` (#331). News republished the capital lane's thresholds here, which invited a
    # console to compare a Case frozen last week against a floor edited yesterday. The lane's rules
    # belong to `/api/trading/*`, and the ones that decided a Case travel with that Case.
    assert "trade_floors" not in data["oi"]

    # The live window, read under the running thresholds and folded against the rank ceiling here.
    assert data["oi"]["window_occupancy"] == [
        {"symbol": "WIF", "used": 2, "max_rank_in_window": 2, "full": True},
        {"symbol": "DOGE", "used": 1, "max_rank_in_window": 2, "full": False},
    ]
    occupancy = next(call for name, call in news.calls if name == "oi_window_occupancy")
    assert occupancy["window_ms"] == 4 * 3_600_000
    assert occupancy["whale_oi_ratio_above_bps"] == 8_000
    assert occupancy["oi_change_at_least_bps"] == 0
