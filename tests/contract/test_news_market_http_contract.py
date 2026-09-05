"""The market read surface (#553): two GETs over stored Items and their typed facts.

Everything here holds when the model is unconfigured, the sender is down and Trading is faulted. A
market observation exists because the provider reported it and this process stored it, so the only
question either route asks is of PostgreSQL. Nothing on this surface is gated on a card having been
sent, and nothing folds "what the parser proved" together with "what the sender did".
"""

from __future__ import annotations

import base64
import time
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tracefold.app.http.app import create_app
from tracefold.app.http.schemas import market as market_schemas
from tracefold.news import (
    MARKET_KINDS,
    MARKET_PAGE_MAX,
    MARKET_WINDOW_DEFAULT_MS,
    MARKET_WINDOW_MAX_MS,
    NOTIFICATION_REASON_NOT_CONNECTED,
    NOTIFICATION_STATUS_NOT_CONNECTED,
)
from tracefold.platform.config.models import Settings
from tracefold.platform.observability import TelemetryRegistry

TOKEN = "contract-token"
# A stored `news_items.item_id` is sha256(source_id, provider record id): 64 lowercase hex characters.
OI_ITEM_ID = "a" * 64
LIQUIDATION_ITEM_ID = "b" * 64
RAW_ITEM_ID = "c" * 64
UNKNOWN_ITEM_ID = "d" * 64


def _observation(item_id: str, *, market_kind: str, group_key: str, **overrides: Any) -> dict[str, Any]:
    """One observation with every public field present, so the response cannot hide an unset one."""

    observation: dict[str, Any] = dict.fromkeys(market_schemas.NewsMarketObservationData.model_fields)
    observation.update(
        item_id=item_id,
        market_kind=market_kind,
        parse_status="parsed",
        ingest_mode="live",
        historical=False,
        group_key=group_key,
        title="BTCUSDT 持仓异动",
        event_at_ms=1_800_000_000_000,
        received_at_ms=1_800_000_001_000,
        provider="opennews",
    )
    observation.update(overrides)
    return observation


OI_OBSERVATION = _observation(
    OI_ITEM_ID,
    market_kind="oi",
    group_key="oi|opennews|binance|BTCUSDT|oi_change_15m",
    source_strategy_id="1019",
    source_venue="binance",
    raw_instrument="BTCUSDT",
    symbol="BTC",
    measurement_definition="oi_change_15m",
    direction="bullish",
    oi_change_bps=420,
    oi_value_usd=9_100_000_000,
    received_at_ms=1_800_000_009_000,
)
LIQUIDATION_OBSERVATION = _observation(
    LIQUIDATION_ITEM_ID,
    market_kind="liquidation",
    group_key="liquidation|opennews|okx|BTC-USDT-SWAP|long",
    source_strategy_id="2083",
    source_venue="okx",
    raw_instrument="BTC-USDT-SWAP",
    symbol="BTC",
    liquidated_position_side="long",
    forced_order_side="sell",
    # Exact stored text, never a JSON number: the ledger holds a provider notional the console renders
    # and never computes with, and a float round-trip would quietly edit a recorded fact.
    notional_usd="1234567.89",
    price="61234.5",
    received_at_ms=1_800_000_005_000,
)
RAW_OBSERVATION = _observation(
    RAW_ITEM_ID,
    market_kind="unknown_market",
    group_key=f"raw|unknown_market|{RAW_ITEM_ID}",
    source_strategy_id="4242",
    parse_status="raw",
    parse_error="unknown_market_source",
    title="未知市场策略推送",
    received_at_ms=1_800_000_002_000,
)
# The same liquidation group one observation earlier, which is what the detail page's timeline expands.
LIQUIDATION_EARLIER = {
    **LIQUIDATION_OBSERVATION,
    "item_id": "e" * 64,
    "notional_usd": "990000.00",
    "received_at_ms": 1_800_000_004_000,
}


def _group(observation: dict[str, Any], *, observation_count: int = 1) -> dict[str, Any]:
    return {
        "group_key": observation["group_key"],
        "market_kind": observation["market_kind"],
        "observation_count": observation_count,
        "first_event_at_ms": observation["event_at_ms"] - 60_000,
        "last_event_at_ms": observation["event_at_ms"],
        "latest": observation,
        "notification_status": NOTIFICATION_STATUS_NOT_CONNECTED,
        "notification_reason": NOTIFICATION_REASON_NOT_CONNECTED,
    }


def _position(group: dict[str, Any]) -> tuple[int, str]:
    return int(group["latest"]["received_at_ms"]), str(group["latest"]["item_id"])


class _FakeNewsRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # Newest first, exactly as the storage read orders them.
        self.groups = [
            _group(OI_OBSERVATION, observation_count=3),
            _group(LIQUIDATION_OBSERVATION, observation_count=2),
            _group(RAW_OBSERVATION),
        ]
        self.details = {
            LIQUIDATION_ITEM_ID: {
                **LIQUIDATION_OBSERVATION,
                "provider_params": {"strategy_id": "2083", "instrument": "BTC-USDT-SWAP", "side": "long"},
                "description": "OKX BTC-USDT-SWAP 多头强平 1,234,567.89 USDT",
                "raw_first_line": "OKX 强平",
                "notification_status": NOTIFICATION_STATUS_NOT_CONNECTED,
                "notification_reason": NOTIFICATION_REASON_NOT_CONNECTED,
            }
        }

    def market_groups(
        self,
        *,
        kinds: tuple[str, ...],
        from_ms: int,
        to_ms: int,
        cursor_received_at_ms: int,
        cursor_item_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            (
                "market_groups",
                {
                    "kinds": kinds,
                    "from_ms": from_ms,
                    "to_ms": to_ms,
                    "cursor_received_at_ms": cursor_received_at_ms,
                    "cursor_item_id": cursor_item_id,
                    "limit": limit,
                },
            )
        )
        selected = [group for group in self.groups if not kinds or group["market_kind"] in kinds]
        after_cursor = [
            group for group in selected if _position(group) < (int(cursor_received_at_ms), str(cursor_item_id))
        ]
        return after_cursor[: int(limit)]

    def market_sources(self, *, from_ms: int, to_ms: int) -> list[dict[str, Any]]:
        self.calls.append(("market_sources", {"from_ms": from_ms, "to_ms": to_ms}))
        counted = {"oi": (3, 3, 0, 1), "liquidation": (2, 2, 0, 1), "unknown_market": (1, 0, 1, 1)}
        return [
            {
                "market_kind": kind,
                "received": counted.get(kind, (0, 0, 0, 0))[0],
                "parsed": counted.get(kind, (0, 0, 0, 0))[1],
                "raw": counted.get(kind, (0, 0, 0, 0))[2],
                "groups": counted.get(kind, (0, 0, 0, 0))[3],
                "last_received_at_ms": 1_800_000_009_000 if kind in counted else None,
            }
            for kind in MARKET_KINDS
        ]

    def market_item(self, *, item_id: str) -> dict[str, Any] | None:
        self.calls.append(("market_item", {"item_id": item_id}))
        detail = self.details.get(item_id)
        return dict(detail) if detail is not None else None

    def market_group_timeline(self, *, group_key: str) -> list[dict[str, Any]]:
        self.calls.append(("market_group_timeline", {"group_key": group_key}))
        if group_key != LIQUIDATION_OBSERVATION["group_key"]:
            return []
        return [LIQUIDATION_OBSERVATION, LIQUIDATION_EARLIER]


class _FakeRepositories:
    def __init__(self, news: _FakeNewsRepository) -> None:
        self.news = news


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


def test_the_market_list_reports_what_was_parsed_and_what_was_sent_as_two_separate_pairs(client) -> None:
    """A raw card that was delivered and a parsed card that was not are both ordinary states.

    One combined "outcome" column would have to misreport one of them, so `parse_status`/`parse_error`
    and `notification_status`/`notification_reason` travel as two independent pairs on every group.
    """

    http, _ = client

    response = http.get("/api/news/market", params={"token": TOKEN})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    data = body["data"]
    assert set(data) == {"groups", "next_cursor", "sources", "filters", "notifications_connected"}
    assert [group["market_kind"] for group in data["groups"]] == ["oi", "liquidation", "unknown_market"]
    assert set(data["groups"][0]) == set(market_schemas.NewsMarketGroupData.model_fields)
    assert set(data["groups"][0]["latest"]) == set(market_schemas.NewsMarketObservationData.model_fields)
    parsed, raw = data["groups"][0], data["groups"][2]
    assert (parsed["latest"]["parse_status"], parsed["latest"]["parse_error"]) == ("parsed", None)
    assert (raw["latest"]["parse_status"], raw["latest"]["parse_error"]) == ("raw", "unknown_market_source")
    # PR-1 stores and reads the facts; the notification loop is PR-2's. Both groups say so identically,
    # which is what proves the pair is not derived from the parse outcome beside it.
    for group in data["groups"]:
        assert group["notification_status"] == NOTIFICATION_STATUS_NOT_CONNECTED
        assert group["notification_reason"] == NOTIFICATION_REASON_NOT_CONNECTED
    assert data["notifications_connected"] is False
    assert [source["market_kind"] for source in data["sources"]] == list(MARKET_KINDS)
    assert response.headers.get("etag")


def test_a_group_that_was_never_sent_is_still_a_readable_observation(client) -> None:
    """Whether a card was sent is reported per group and is never a precondition for reading it.

    The provider reported the observation and this process stored it; whether a card went out is a
    separate fact about the sender. Gating the read on it would hide every market observation in the
    system for as long as the sender is unwired -- which, in PR-1, is always.
    """

    http, _ = client

    response = http.get("/api/news/market", params={"token": TOKEN})
    filtered = http.get(
        "/api/news/market",
        params={"token": TOKEN, "notification_status": NOTIFICATION_STATUS_NOT_CONNECTED},
    )

    assert response.status_code == 200
    assert len(response.json()["data"]["groups"]) == 3
    # There is no parameter to express it with, either: an unknown filter is refused rather than ignored.
    assert filtered.status_code == 400
    assert filtered.json() == {
        "ok": False,
        "error": "unsupported_query_param",
        "field": "notification_status",
    }


def test_the_default_market_window_is_the_last_72_hours_forwarded_as_absolute_bounds(client) -> None:
    """A reader reviewing what arrived on Tuesday asks a question a rolling offset cannot express.

    The default is a convenience the route resolves once, into the same absolute pair an explicit
    request sends. The storage read never sees "last N hours", so a page and its `next_cursor` cannot
    describe two different windows as the clock moves between them.
    """

    http, news = client
    before_ms = int(time.time() * 1000)

    response = http.get("/api/news/market", params={"token": TOKEN})

    assert response.status_code == 200
    forwarded = news.calls[0][1]
    assert before_ms <= forwarded["to_ms"] <= int(time.time() * 1000)
    assert forwarded["to_ms"] - forwarded["from_ms"] == MARKET_WINDOW_DEFAULT_MS
    assert MARKET_WINDOW_DEFAULT_MS == 72 * 60 * 60_000
    filters = response.json()["data"]["filters"]
    assert filters == {"kind": None, "from_ms": forwarded["from_ms"], "to_ms": forwarded["to_ms"], "limit": 50}
    # The per-kind intake summary describes the same window, never a second one of its own.
    assert news.calls[1] == ("market_sources", {"from_ms": forwarded["from_ms"], "to_ms": forwarded["to_ms"]})


def test_the_market_window_may_sit_anywhere_in_retention_but_one_request_spans_at_most_168_hours(client) -> None:
    """The span bound is what one page may scan, not how far back the data goes.

    A link into a week from last month must still open, or the bound would silently become a retention
    policy the operator never set.
    """

    http, news = client
    long_ago_to_ms = 1_700_000_000_000
    long_ago_from_ms = long_ago_to_ms - MARKET_WINDOW_MAX_MS

    old = http.get(
        "/api/news/market",
        params={"token": TOKEN, "from_ms": long_ago_from_ms, "to_ms": long_ago_to_ms},
    )

    assert old.status_code == 200
    assert news.calls[0][1]["from_ms"] == long_ago_from_ms
    assert news.calls[0][1]["to_ms"] == long_ago_to_ms
    assert old.json()["data"]["filters"]["from_ms"] == long_ago_from_ms

    too_wide = http.get(
        "/api/news/market",
        params={"token": TOKEN, "from_ms": long_ago_from_ms - 1, "to_ms": long_ago_to_ms},
    )
    assert too_wide.status_code == 400
    assert too_wide.json() == {"ok": False, "error": "news_market_window_too_wide", "field": "to_ms"}
    assert MARKET_WINDOW_MAX_MS == 168 * 60 * 60_000


@pytest.mark.parametrize(
    ("from_ms", "to_ms"),
    [(1_700_000_000_000, 1_700_000_000_000), (1_700_000_000_001, 1_700_000_000_000)],
)
def test_a_market_window_that_ends_before_it_starts_is_a_bounded_400(client, from_ms: int, to_ms: int) -> None:
    """An empty or inverted window is a caller error, not an empty page that looks like quiet markets."""

    http, news = client

    response = http.get("/api/news/market", params={"token": TOKEN, "from_ms": from_ms, "to_ms": to_ms})

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "news_market_window_invalid", "field": "from_ms"}
    assert news.calls == []


def test_the_market_page_is_capped_at_one_hundred_groups(client) -> None:
    """The page bound is the route's, so no caller can ask one request to scan the whole window."""

    http, news = client

    assert http.get("/api/news/market", params={"token": TOKEN, "limit": MARKET_PAGE_MAX}).status_code == 200
    # One row beyond the page is what tells the route whether a `next_cursor` exists at all.
    assert news.calls[0][1]["limit"] == MARKET_PAGE_MAX + 1
    assert MARKET_PAGE_MAX == 100
    for out_of_bounds in (0, MARKET_PAGE_MAX + 1):
        response = http.get("/api/news/market", params={"token": TOKEN, "limit": out_of_bounds})
        assert response.status_code == 422, out_of_bounds


def test_market_kind_accepts_the_four_market_families_and_names_anything_else_invalid(client) -> None:
    """`kind` is a closed vocabulary, and an Event kind is not in it.

    Falling through to "no filter" would serve every kind under a heading naming one, which is exactly
    the confusion #553 removed by keeping the market and Event vocabularies apart.
    """

    http, news = client

    every_kind = http.get("/api/news/market", params={"token": TOKEN, "kind": ",".join(MARKET_KINDS)})

    assert every_kind.status_code == 200
    assert news.calls[0][1]["kinds"] == MARKET_KINDS
    assert every_kind.json()["data"]["filters"]["kind"] == ",".join(MARKET_KINDS)

    one_kind = http.get("/api/news/market", params={"token": TOKEN, "kind": "liquidation"})
    assert one_kind.status_code == 200
    assert [group["market_kind"] for group in one_kind.json()["data"]["groups"]] == ["liquidation"]

    for rejected in ("news", "listing", "oi,news", "", "OI"):
        response = http.get("/api/news/market", params={"token": TOKEN, "kind": rejected})
        if not rejected:
            # An empty `kind` is "no kind filter", the same request as omitting it.
            assert response.status_code == 200, rejected
            continue
        assert response.status_code == 400, rejected
        assert response.json() == {"ok": False, "error": "news_market_kind_invalid", "field": "kind"}


def test_the_market_routes_refuse_an_unknown_parameter_and_require_the_operator_token(client) -> None:
    http, news = client

    unknown = http.get("/api/news/market", params={"token": TOKEN, "symbol": "BTC"})
    assert unknown.status_code == 400
    assert unknown.json() == {"ok": False, "error": "unsupported_query_param", "field": "symbol"}

    unknown_on_detail = http.get(f"/api/news/market/{LIQUIDATION_ITEM_ID}", params={"token": TOKEN, "kind": "oi"})
    assert unknown_on_detail.status_code == 400
    assert unknown_on_detail.json() == {"ok": False, "error": "unsupported_query_param", "field": "kind"}

    for path in ("/api/news/market", f"/api/news/market/{LIQUIDATION_ITEM_ID}"):
        assert http.get(path).status_code == 401, path
        assert http.get(path, params={"token": "wrong"}).status_code == 401, path
    assert news.calls == []


def test_a_market_cursor_is_an_opaque_position_that_round_trips_to_the_next_page(client) -> None:
    """The cursor is a position, not a filter: page two resumes below page one's last group.

    Encoding the sort key the list actually orders on is what makes that true. A cursor that re-derived
    a filter would let a group arriving mid-page appear on both pages, or fall between them.
    """

    http, news = client

    first = http.get("/api/news/market", params={"token": TOKEN, "limit": 1})

    assert first.status_code == 200
    first_page = first.json()["data"]
    assert [group["market_kind"] for group in first_page["groups"]] == ["oi"]
    assert first_page["next_cursor"]

    second = http.get("/api/news/market", params={"token": TOKEN, "limit": 1, "cursor": first_page["next_cursor"]})

    assert second.status_code == 200
    forwarded = news.calls[2][1]
    assert (forwarded["cursor_received_at_ms"], forwarded["cursor_item_id"]) == _position(first_page["groups"][0])
    assert [group["market_kind"] for group in second.json()["data"]["groups"]] == ["liquidation"]

    # An opaque cursor is still bounded input: a broken one is named, never decoded into a silent
    # position at the top of the list.
    broken = http.get("/api/news/market", params={"token": TOKEN, "cursor": "not-base64!!"})
    assert broken.status_code == 400
    assert broken.json() == {"ok": False, "error": "news_market_cursor_invalid", "field": "cursor"}
    not_a_position = base64.urlsafe_b64encode(b"soon|" + LIQUIDATION_ITEM_ID.encode()).decode().rstrip("=")
    assert http.get("/api/news/market", params={"token": TOKEN, "cursor": not_a_position}).status_code == 400


def test_the_market_detail_returns_the_provider_payload_the_typed_fact_and_the_whole_group_timeline(client) -> None:
    """One observation in full, read by Item identity and therefore not bound by the list's window.

    The stored provider payload travels because it is the evidence: a reader who doubts the parsed
    numbers must be able to see what the provider actually sent, without a second tool.
    """

    http, news = client

    response = http.get(f"/api/news/market/{LIQUIDATION_ITEM_ID}", params={"token": TOKEN})

    assert response.status_code == 200
    data = response.json()["data"]
    assert set(data) == {
        "observation",
        "provider_params",
        "description",
        "raw_first_line",
        "notification_status",
        "notification_reason",
        "timeline",
        "notifications_connected",
    }
    assert set(data["observation"]) == set(market_schemas.NewsMarketObservationData.model_fields)
    assert data["provider_params"] == {"strategy_id": "2083", "instrument": "BTC-USDT-SWAP", "side": "long"}
    # The typed liquidation fact, and its decimal figures as the exact stored text rather than as JSON
    # numbers a float round-trip would edit.
    assert data["observation"]["liquidated_position_side"] == "long"
    assert data["observation"]["forced_order_side"] == "sell"
    assert data["observation"]["notional_usd"] == "1234567.89"
    assert data["observation"]["price"] == "61234.5"
    assert data["notification_status"] == NOTIFICATION_STATUS_NOT_CONNECTED
    assert data["notifications_connected"] is False
    # The timeline is the group's own, newest first, and is read by the group key the Item carries.
    assert news.calls[1] == ("market_group_timeline", {"group_key": LIQUIDATION_OBSERVATION["group_key"]})
    assert [entry["item_id"] for entry in data["timeline"]] == [LIQUIDATION_ITEM_ID, "e" * 64]
    assert [entry["notional_usd"] for entry in data["timeline"]] == ["1234567.89", "990000.00"]


def test_a_market_item_id_that_is_not_an_item_identity_never_reaches_the_indexed_lookup(client) -> None:
    """`news_items.item_id` is a sha256 hex digest; a 2 KB path segment is a caller error, not a query."""

    http, news = client

    for bad in ("not-a-hash", "0" * 63, "g" * 64, "x" * 200):
        response = http.get(f"/api/news/market/{bad}", params={"token": TOKEN})
        assert response.status_code == 400, bad
        assert response.json() == {"ok": False, "error": "news_market_item_invalid", "field": "item_id"}
    assert news.calls == []

    # The identity itself is case-insensitive, and is normalized once before the lookup rather than
    # missing the index on a link someone pasted in upper case.
    upper = http.get(f"/api/news/market/{LIQUIDATION_ITEM_ID.upper()}", params={"token": TOKEN})
    assert upper.status_code == 200
    assert news.calls[0] == ("market_item", {"item_id": LIQUIDATION_ITEM_ID})


def test_an_unretained_market_item_is_a_bounded_404_rather_than_an_empty_observation(client) -> None:
    http, news = client

    response = http.get(f"/api/news/market/{UNKNOWN_ITEM_ID}", params={"token": TOKEN})

    assert response.status_code == 404
    assert response.json() == {"ok": False, "error": "news_market_item_not_found"}
    # No timeline read for an Item that has no group.
    assert [name for name, _ in news.calls] == ["market_item"]


def test_a_millisecond_past_the_ledgers_integer_range_is_a_named_400(client) -> None:
    """A `bigint` column cannot hold it, so the request is malformed and says so (#553).

    Without this the value reaches psycopg and the operator gets a 500 from the driver, which reads as
    "the market surface is broken" rather than "that window does not exist".
    """

    http, news = client
    beyond = 2**63

    window = http.get("/api/news/market", params={"token": TOKEN, "from_ms": beyond})
    upper = http.get("/api/news/market", params={"token": TOKEN, "to_ms": beyond})
    cursor = http.get(
        "/api/news/market",
        params={"token": TOKEN, "cursor": _encode_cursor_for_test(beyond, "a" * 64)},
    )

    assert (window.status_code, window.json()["error"], window.json()["field"]) == (
        400,
        "news_market_window_invalid",
        "from_ms",
    )
    assert (upper.status_code, upper.json()["error"], upper.json()["field"]) == (
        400,
        "news_market_window_invalid",
        "to_ms",
    )
    assert (cursor.status_code, cursor.json()["error"]) == (400, "news_market_cursor_invalid")
    assert news.calls == []


def _encode_cursor_for_test(received_at_ms: int, item_id: str) -> str:
    return base64.urlsafe_b64encode(f"{received_at_ms}|{item_id}".encode()).decode().rstrip("=")
