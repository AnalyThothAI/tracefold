"""The market read surface through the real app on real PostgreSQL (#553 PR-1).

Two questions the storage-level tests cannot answer. First, what the public Event API does with the
market Events that existed before the cut: the migration keeps every one of them, the public
`EventKind` no longer names their kinds, and a bookmarked or pushed link to one is an ordinary thing
for a reader to still have. Second, whether the collapse, the cursor and the detail survive the
envelope -- a group shape that validates in Python and not in Pydantic is a 500 nobody sees until a
reader opens the page.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.postgres_test_utils import connect_postgres_test, postgres_settings_storage
from tracefold.app.http.app import create_app
from tracefold.app.http.schemas.market import NewsMarketObservationData
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.market_contracts import REASON_UNPROCESSED
from tracefold.news.opennews import parse_opennews_message
from tracefold.news.pipeline.admission import admit_frame, admit_market_item, prepare_wallet_observation
from tracefold.news.storage.market import _OBSERVATION_KEYS, INTERNAL_OBSERVATION_KEYS
from tracefold.news.wallet_contracts import WalletEvent, WalletReference
from tracefold.platform.config.models import NewsSettings, Settings

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]

TOKEN = "market-api-token"
NOW = 1_900_000_000_000
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def app(tmp_path):
    settings = Settings(ws_token=TOKEN, news=NewsSettings(), storage=postgres_settings_storage())
    settings.set_config_dir(tmp_path / "app-home")
    return create_app(settings=settings)


@pytest.fixture()
def conn():
    connection = connect_postgres_test(read_only=False)
    yield connection
    connection.close()


def _frame(
    *,
    record_id: int,
    text: str,
    strategy_id: int,
    strategy_name: str,
    source_type: str,
    source: str = "binance",
    at_ms: int = NOW,
    extra: dict[str, Any] | None = None,
) -> Any:
    params: dict[str, Any] = {
        "id": record_id,
        "text": text,
        "source": source,
        "engineType": "market" if source_type in {"market", "wallet"} else "news",
        "ts": at_ms / 1000,
        "strategy": {"id": strategy_id, "name": strategy_name, "sourceType": source_type},
    }
    params.update(extra or {})
    frame = parse_opennews_message({"method": "strategy.triggered", "params": params})
    assert frame is not None
    return frame


def _admit(conn: Any, frame: Any, *, at_ms: int) -> str:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        result = admit_frame(
            repos,
            event=frame,
            ingest_mode="live",
            observed_at_ms=at_ms,
            trace_id=f"market-api-{frame.provider_record_id}",
            watchlist_symbols=frozenset(),
            now_ms=at_ms,
        )
    conn.commit()
    return result.item_id


def _seed_legacy_market_event(conn: Any, *, event_id: str, item_id: str, event_kind: str) -> None:
    """One Event of a kind the cut retired, exactly as the migration leaves it in place.

    Nothing in the code can create one any more, which is the point: the rows are immutable history
    and the reader's bookmark still resolves to this identity.
    """

    conn.execute(
        """
        INSERT INTO news_items (
          item_id, source_id, source_item_key, title, raw_first_line, description, reporting_origin,
          published_at_ms, observed_at_ms, provider_metadata, provenance, first_ingest_mode, trace_id,
          created_at_ms, updated_at_ms
        ) VALUES (
          %(item)s, 'opennews', %(item)s, 'TRUMP OI Rise 4.55%%', '', '', 'opennews', %(at)s, %(at)s,
          '{"strategies": [{"id": "1019", "name": "OI Event Monitor"}]}'::jsonb, '[]'::jsonb,
          'live', 'trace', %(at)s, %(at)s
        )
        """,
        {"item": item_id, "at": NOW},
    )
    conn.execute(
        """
        INSERT INTO news_events (
          event_id, leader_item_id, dedupe_family, comparison_fingerprint, comparison_title,
          leader_title, opened_at_ms, last_member_at_ms, expires_at_ms, admission, ingest_mode,
          trace_id, created_at_ms, updated_at_ms, focus_fact_id, focus_fact_text, focus_fact_context,
          focus_fact_method, focus_span_start, focus_span_end, event_kind, source_contract_reason
        ) VALUES (
          %(event)s, %(item)s, 'market_telemetry', %(event)s, 'legacy market card', 'legacy market card',
          %(at)s, %(at)s, %(at)s, 'telemetry_deterministic', 'live', 'trace', %(at)s, %(at)s,
          %(fact)s, 'legacy market card', '', 'whole_item', 0, 18, %(kind)s, %(reason)s
        )
        """,
        {
            "event": event_id,
            "item": item_id,
            "at": NOW,
            "fact": f"fact-{event_id}",
            "kind": event_kind,
            # The retired consistency CHECK still holds these rows: an `unsupported_market` Event
            # always carried a reason and the other two never did.
            "reason": "unsupported_market_contract" if event_kind == "unsupported_market" else None,
        },
    )
    conn.execute(
        """
        INSERT INTO news_event_members (event_id, item_id, joined_at_ms, match_kind, fact_id, fact_text)
        VALUES (%s, %s, %s, 'leader', %s, 'legacy market card')
        """,
        (event_id, item_id, NOW, f"fact-{event_id}"),
    )
    conn.commit()


@pytest.mark.parametrize("event_kind", ["oi", "liquidation", "unsupported_market"])
def test_a_retired_market_event_is_missing_from_the_event_api_rather_than_a_server_error(
    app, conn, event_kind: str
) -> None:
    """#553. The public `EventKind` no longer names these, so serving one cannot validate.

    A reader who kept a link to a pre-cut OI card must get "this is not an Event" -- the observation
    it was built from is readable at `/api/news/market`. Returning the row would fail the response
    envelope inside `_etagged` and surface as a 500, which reads as an outage rather than a move.
    """

    event_id = f"legacy-{event_kind}-event"
    _seed_legacy_market_event(conn, event_id=event_id, item_id=f"legacy-{event_kind}-item", event_kind=event_kind)

    with TestClient(app) as client:
        detail = client.get(f"/api/news/events/{event_id}", headers=AUTH)
        feed = client.get("/api/news/feed?limit=100", headers=AUTH)

    assert detail.status_code == 404
    assert detail.json() == {"ok": False, "error": "news_event_not_found"}
    assert feed.status_code == 200
    assert event_id not in {row["event_id"] for row in feed.json()["data"]["events"]}


def test_the_ordinary_news_feed_and_its_counts_never_see_a_retired_market_event(app, conn) -> None:
    """The `/news` denominator is editorial Events, and a retired market Event is not one."""

    _seed_legacy_market_event(conn, event_id="legacy-count-event", item_id="legacy-count-item", event_kind="oi")
    _admit(
        conn,
        _frame(
            record_id=7_710_001,
            text="A regulator approves a spot ETF for the second time this year",
            strategy_id=1018,
            strategy_name="News Score > 70",
            source_type="news",
            source="wire",
            extra={"score": 92},
        ),
        at_ms=NOW,
    )

    with TestClient(app) as client:
        feed = client.get("/api/news/feed?limit=100", headers=AUTH)

    body = feed.json()["data"]
    counts = body["counts"]
    assert feed.status_code == 200
    assert "legacy-count-event" not in {row["event_id"] for row in body["events"]}
    assert counts["total"] == len(body["events"]) == 1
    assert counts["total"] == counts["pushed"] + counts["held"] + counts["pending"]


def test_the_market_list_collapses_orders_and_pages_through_the_real_envelope(app, conn) -> None:
    """One request, one page: the collapse, the ordering and the cursor as a reader receives them."""

    oi_items = [
        _admit(
            conn,
            _frame(
                record_id=7_720_000 + index,
                text="TRUMP OI Rise 4.55%, OI Value 32.17M, Whale Long Profit 80.21%, Whale/OI Ratio 100.71%",
                strategy_id=1019,
                strategy_name="OI Event Monitor",
                source_type="market",
                at_ms=NOW + index,
            ),
            at_ms=NOW + index,
        )
        for index in range(2)
    ]
    liquidation_item = _admit(
        conn,
        _frame(
            record_id=7_720_100,
            text="SOL Large Short Liquidation 202.71K at $137.01",
            strategy_id=2083,
            strategy_name="Large-scale liquidation",
            source_type="market",
            source="okx",
            at_ms=NOW + 2,
        ),
        at_ms=NOW + 2,
    )
    wallet_item = _admit(
        conn,
        _frame(
            record_id=7_720_200,
            text="js-2 Close Short SOL $482,113.55 , Price $137.01 , PNL -$8,204.10",
            strategy_id=2026,
            strategy_name="聪明钱监控",
            source_type="wallet",
            source="",
            at_ms=NOW + 3,
            extra={"relatedAddress": "0x" + "5" * 40},
        ),
        at_ms=NOW + 3,
    )

    with TestClient(app) as client:
        window = f"from_ms={NOW - 1}&to_ms={NOW + 10}"
        first = client.get(f"/api/news/market?{window}&limit=2", headers=AUTH)
        page = first.json()["data"]
        second = client.get(f"/api/news/market?{window}&limit=2&cursor={page['next_cursor']}", headers=AUTH)
        narrowed = client.get(f"/api/news/market?{window}&kind=liquidation", headers=AUTH)

    assert first.status_code == 200
    assert page["filters"] == {
        "kind": None,
        "from_ms": NOW - 1,
        "to_ms": NOW + 10,
        "limit": 2,
        "asset": None,
        "provider": None,
        "venue": None,
        "measurement_definition": None,
        "sort": "latest",
    }
    # Newest first: the wallet print, then the liquidation. The two OI frames are one run and collapse
    # onto the second page as a single group carrying both.
    assert [group["latest"]["item_id"] for group in page["groups"]] == [wallet_item, liquidation_item]
    assert [group["market_kind"] for group in page["groups"]] == ["smart_money", "liquidation"]
    assert page["next_cursor"]

    rest = second.json()["data"]
    assert second.status_code == 200
    assert [group["latest"]["item_id"] for group in rest["groups"]] == [oi_items[1]]
    assert rest["groups"][0]["observation_count"] == 2
    assert rest["groups"][0]["first_event_at_ms"] == NOW
    assert rest["groups"][0]["last_event_at_ms"] == NOW + 1

    assert [group["market_kind"] for group in narrowed.json()["data"]["groups"]] == ["liquidation"]
    assert narrowed.json()["data"]["filters"]["kind"] == "liquidation"

    sources = {row["market_kind"]: row for row in page["sources"]}
    assert sources["oi"]["received"] == 2 and sources["oi"]["groups"] == 1
    assert sources["smart_money"]["parsed"] == 1
    assert sources["unknown_market"]["received"] == 0
    # No notification loop turn is taken in this test, so what a reader was told is nothing -- stated
    # as zeroes beside the intake rather than left out of the block (#553 PR-2 §6).
    assert (sources["oi"]["merged"], sources["oi"]["sent"], sources["oi"]["failed"]) == (0, 0, 0)
    assert [group["notification_status"] for group in page["groups"]] == ["unprocessed", "unprocessed"]


def test_the_market_detail_returns_the_stored_payload_the_typed_fact_and_the_timeline(app, conn) -> None:
    address = "0x" + "6" * 40
    item_id = _admit(
        conn,
        _frame(
            record_id=7_730_001,
            text="js-2 Open Long SOL $482,113.55 , Price $137.01",
            strategy_id=2026,
            strategy_name="聪明钱监控",
            source_type="wallet",
            source="",
            extra={
                "relatedAddress": address,
                "strategy": {
                    "id": 2026,
                    "name": "聪明钱监控",
                    "sourceType": "wallet",
                    "metrics": {"position_value": {"value": 482113.55, "unit": "USD"}},
                },
            },
        ),
        at_ms=NOW,
    )

    with TestClient(app) as client:
        detail = client.get(f"/api/news/market/{item_id}", headers=AUTH)
        missing = client.get(f"/api/news/market/{'0' * 64}", headers=AUTH)
        malformed = client.get("/api/news/market/not-an-item", headers=AUTH)

    body = detail.json()["data"]
    assert detail.status_code == 200
    assert body["observation"]["item_id"] == item_id
    assert (body["observation"]["action"], body["observation"]["position_side"]) == ("open", "long")
    assert body["observation"]["account_address"] == address
    assert body["observation"]["parse_status"] == "parsed"
    assert body["provider_params"]["relatedAddress"] == address
    assert body["provider_params"]["strategy"]["metrics"]["position_value"]["value"] == 482113.55
    assert [row["item_id"] for row in body["timeline"]] == [item_id]
    # Admitted live and no loop turn taken here, so it is on the notification to-do list and says so.
    assert (body["notification_status"], body["notification_reason"]) == ("unprocessed", REASON_UNPROCESSED)
    assert body["notification_delivery"] is None
    assert body["notification_covered_item_ids"] == []

    assert missing.status_code == 404
    assert missing.json() == {"ok": False, "error": "news_market_item_not_found"}
    assert malformed.status_code == 400
    assert malformed.json()["error"] == "news_market_item_invalid"


def test_the_market_surface_answers_when_the_pipeline_status_read_cannot(app, conn) -> None:
    """The list is the page. A status failure is a note in one strip, never a blank surface.

    Proven at the seam a browser cannot reach: `/api/news/status` is a separate request, so the market
    read stays answerable whatever it returns.
    """

    item_id = _admit(
        conn,
        _frame(
            record_id=7_740_001,
            text="Withdraw USDC",
            strategy_id=2026,
            strategy_name="聪明钱监控",
            source_type="wallet",
            source="",
        ),
        at_ms=NOW,
    )
    conn.execute("DROP TABLE IF EXISTS news_ingest_state_backup")
    conn.execute("ALTER TABLE news_ingest_state RENAME TO news_ingest_state_backup")
    conn.commit()
    try:
        with TestClient(app) as client:
            market = client.get(f"/api/news/market?from_ms={NOW - 1}&to_ms={NOW + 1}", headers=AUTH)
    finally:
        conn.execute("ALTER TABLE news_ingest_state_backup RENAME TO news_ingest_state")
        conn.commit()

    body = market.json()["data"]
    assert market.status_code == 200
    assert [group["latest"]["item_id"] for group in body["groups"]] == [item_id]
    # A raw card is retained and served with its reason; it is not a lesser row.
    assert body["groups"][0]["latest"]["parse_status"] == "raw"
    assert body["groups"][0]["latest"]["parse_error"] == "smart_money_template_unmatched"


def test_the_market_payload_matches_the_published_openapi_component(app, conn) -> None:
    """The envelope a reader receives is the one the generated client was built against."""

    _admit(
        conn,
        _frame(
            record_id=7_750_001,
            text="TRUMP OI Rise 4.55%, OI Value 32.17M, Whale Long Profit 80.21%, Whale/OI Ratio 100.71%",
            strategy_id=1019,
            strategy_name="OI Event Monitor",
            source_type="market",
        ),
        at_ms=NOW,
    )

    with TestClient(app) as client:
        response = client.get(f"/api/news/market?from_ms={NOW - 1}&to_ms={NOW + 1}", headers=AUTH)
        schema = client.get("/openapi.json").json()

    group = response.json()["data"]["groups"][0]
    component = schema["components"]["schemas"]["NewsMarketGroupData"]
    assert component["additionalProperties"] is False
    assert set(group) == set(component["properties"])
    assert json.loads(json.dumps(group)) == group
    assert int(time.time() * 1000) > 0


# --- the fifth kind, and the invariant that publishes it (#572 PR-2) -------------------------------

SMOKE_LIQUIDATION = "BTC Large Short Liquidation 4.55M at $118000"


def _admit_wallet_observation(conn: Any, *, at_ms: int, **overrides: Any) -> str:
    """One derived wallet observation, through the same admission the tape uses."""

    from tests.news.net_buy_fixtures import movement, roster, snapshot

    members = roster()
    for member in members:
        member["monitoring_from_ms"] = member["known_at_ms"] = at_ms - 3600000
    facts = [replace(movement(i, at=at_ms), token_symbol="FSD") for i in range(1, 6)]
    event = WalletEvent(
        item_id="",
        chain_id=4663,
        token=facts[0].token,
        token_symbol="FSD",
        trigger_tx_hash="0x" + f"{at_ms:064x}",
        event_at_ms=at_ms,
        received_at_ms=at_ms,
        detected_at_ms=at_ms,
        trigger_max_age_s=60,
        notification_eligible=True,
        notification_reason=None,
        initial_snapshot=snapshot(facts, members=members, cutoff_at_ms=at_ms, coverage_from_ms=at_ms - 3600000),
    )
    from tracefold.news.pipeline.admission import wallet_item_id

    event = replace(event, **overrides)
    prepared = prepare_wallet_observation(replace(event, item_id=wallet_item_id(event)))
    repos = repositories_for_connection(conn)
    with repos.transaction():
        admit_market_item(repos, prepared, ingest_mode="live", trace_id="market-api-wallet", now_ms=at_ms)
    conn.commit()
    return prepared.item_id


def test_the_smokes_own_observation_and_a_wallet_one_both_answer_two_hundred(app, conn) -> None:
    """The browser smoke's exact frame, plus the kind this PR adds, through the real HTTP app.

    This is the test that was missing when `/api/news/market` started answering 500. The storage layer
    was right, the fake-repository contract tests were right, and the surface a reader actually opens
    was broken -- because the read model serves one projection to two consumers and only one response
    path narrowed it to the published fields. Both kinds are here because the failure needed neither:
    an internal column on *any* observation is an extra key on *every* list response.
    """

    liquidation_item = _admit(
        conn,
        _frame(
            record_id=7_760_001,
            text=SMOKE_LIQUIDATION,
            strategy_id=2000,
            strategy_name="实时清算",
            source_type="market",
            at_ms=NOW,
        ),
        at_ms=NOW,
    )
    wallet_item = _admit_wallet_observation(conn, at_ms=NOW + 1)

    with TestClient(app) as client:
        listing = client.get(f"/api/news/market?from_ms={NOW - 1}&to_ms={NOW + 2}", headers=AUTH)
        liquidation_detail = client.get(f"/api/news/market/{liquidation_item}", headers=AUTH)
        wallet_detail = client.get(f"/api/news/market/{wallet_item}", headers=AUTH)

    assert listing.status_code == 200, listing.text
    body = listing.json()["data"]
    kinds = {group["market_kind"]: group for group in body["groups"]}
    assert set(kinds) == {"liquidation", "wallet"}
    assert kinds["liquidation"]["latest"]["title"] == SMOKE_LIQUIDATION
    assert kinds["wallet"]["latest"]["provider"] == "robinhood_chain"
    assert kinds["wallet"]["latest"]["wallet_snapshot"]["fast"]["qualified_n"] == 5
    assert kinds["wallet"]["latest"]["wallet_snapshot"]["fast"]["net_usd"] == "6000"
    # Every kind keeps a summary row, whether or not it reported anything.
    assert [source["market_kind"] for source in body["sources"]] == [
        "oi",
        "liquidation",
        "smart_money",
        "unknown_market",
        "wallet",
    ]

    assert liquidation_detail.status_code == 200, liquidation_detail.text
    assert wallet_detail.status_code == 200, wallet_detail.text
    detail = wallet_detail.json()["data"]
    assert detail["observation"]["market_kind"] == "wallet"
    assert detail["observation"]["symbol"] == "FSD"
    # The detail's own timeline is an observation list too, and it publishes the same shape.
    assert [row["item_id"] for row in detail["timeline"]] == [wallet_item]


def test_every_projected_observation_column_is_published_or_declared_internal() -> None:
    """The invariant whose absence turned one removed field into a 500 on the whole market surface.

    `ExactApiSchema` forbids an unknown key, so a column this read model projects has exactly two
    honest fates: it is a published field, or it is named in `INTERNAL_OBSERVATION_KEYS` because the
    notification loop reads it and a reader does not. There is no third one, and "nobody noticed"
    was what the third one looked like.
    """

    projected = (set(_OBSERVATION_KEYS) | {"notification_status", "notification_reason"}) - INTERNAL_OBSERVATION_KEYS

    assert projected == set(NewsMarketObservationData.model_fields)
    # Not vacuous: the internal set names a column the projection really carries.
    assert set(_OBSERVATION_KEYS) >= INTERNAL_OBSERVATION_KEYS
    assert INTERNAL_OBSERVATION_KEYS.isdisjoint(NewsMarketObservationData.model_fields)


def test_oi_sort_requires_a_comparable_scope_and_pages_without_losing_ties(app, conn) -> None:
    for index, (symbol, change) in enumerate((("BTC", "4"), ("ETH", "9"), ("SOL", "9"))):
        _admit(
            conn,
            _frame(
                record_id=8_100_000 + index,
                text=f"{symbol} OI Rise {change}%, OI Value 32.17M, Whale Long Profit 80.21%, Whale/OI Ratio 100.71%",
                strategy_id=1019,
                strategy_name="OI Event Monitor",
                source_type="market",
                at_ms=NOW + index,
            ),
            at_ms=NOW + index,
        )
    with TestClient(app) as client:
        params = {"kind": "oi", "from_ms": NOW - 1, "to_ms": NOW + 10}
        latest = client.get("/api/news/market", headers=AUTH, params=params).json()["data"]["groups"][0]["latest"]
        assert latest["measurement_window_ms"] == 300000
        assert latest["measurement_contract_status"] == "proven"
        refused = client.get("/api/news/market", headers=AUTH, params={**params, "sort": "oi_change"})
        assert refused.status_code == 400
        scope = {
            **params,
            "sort": "oi_change",
            "provider": latest["provider"],
            "venue": latest["source_venue"],
            "measurement_definition": latest["measurement_definition"],
            "limit": 1,
        }
        first = client.get("/api/news/market", headers=AUTH, params=scope)
        assert first.status_code == 200, first.text
        data = first.json()["data"]
        assert data["groups"][0]["latest"]["symbol"] == "SOL"
        second = client.get("/api/news/market", headers=AUTH, params={**scope, "cursor": data["next_cursor"]})
        assert second.status_code == 200, second.text
        assert second.json()["data"]["groups"][0]["latest"]["symbol"] == "ETH"
        filtered = client.get("/api/news/market", headers=AUTH, params={**params, "asset": "btc"}).json()["data"]
        assert [g["latest"]["symbol"] for g in filtered["groups"]] == ["BTC"]


def test_wallet_events_paging_totals_deep_link_and_old_contract_rejection(app, conn):
    # Two minutes back: inside every history range, and inside the status funnel's whole-minute window.
    stamp = int(time.time() * 1000) - 120_000
    ids = []
    for offset in range(3):
        ids.append(
            _admit_wallet_observation(
                conn,
                at_ms=stamp + offset,
                notification_eligible=offset != 1,
                notification_reason="wallet_notifications_disabled" if offset == 1 else None,
            )
        )
        conn.execute(
            "UPDATE news_market_wallet_events SET ended_at_ms=%s WHERE item_id=%s", (stamp + offset + 1800000, ids[-1])
        )
        conn.commit()
    with TestClient(app) as client:
        first = client.get("/api/news/wallets/events", headers=AUTH, params={"limit": 1}).json()["data"]
        assert first["totals"] == {"total": 3, "active": 0, "sent": 0}
        assert first["events"][0]["episode_id"] == ids[2]
        second = client.get(
            "/api/news/wallets/events", headers=AUTH, params={"limit": 1, "cursor": first["next_cursor"]}
        ).json()["data"]
        assert second["events"][0]["episode_id"] == ids[1]
        assert second["totals"] == first["totals"]
        assert second["events"][0]["notification_state"] == "not_alerted"
        assert second["events"][0]["notification_reason"] == "wallet_notifications_disabled"
        direct = client.get(f"/api/news/wallets/events/{ids[0]}", headers=AUTH)
        assert direct.status_code == 200, direct.text
        detail = direct.json()["data"]
        assert detail["event"]["initial_snapshot"]["fast"]["net_usd"] == "6000"
        # The sampler's t0 stage is the only writer of the baseline, and the detail projects what it
        # wrote -- price, moment and source together, with no horizon receipt yet (#649 §8).
        assert detail["event"]["reference_price"] is None and detail["outcomes"] == []
        repos = repositories_for_connection(conn)
        with repos.transaction():
            assert repos.news.chain_tape_record_reference(
                WalletReference(
                    item_id=ids[0],
                    price=Decimal("1.25"),
                    at_ms=stamp + 4000,
                    source="dexscreener_robinhood_chain_base_token",
                    trigger_at_ms=stamp,
                )
            )
        conn.commit()
        baseline = client.get(f"/api/news/wallets/events/{ids[0]}", headers=AUTH).json()["data"]["event"]
        assert baseline["reference_price"] == "1.25"
        assert baseline["reference_at_ms"] == stamp + 4000
        assert baseline["reference_source"] == "dexscreener_robinhood_chain_base_token"
        assert client.get("/api/news/wallets/cards", headers=AUTH).status_code == 404
        for params in (
            {"window": "24h"},
            {"kind": "buy"},
            {"wallet_address": "0x1"},
            {"token_address": "0x1"},
            {"history_range": "7d", "cursor": first["next_cursor"]},
        ):
            assert client.get("/api/news/wallets/events", headers=AUTH, params=params).status_code == 400
        assert client.get("/api/news/wallets/events", headers=AUTH, params={"history_range": "30m"}).status_code == 422
        assert client.get("/api/news/wallets/events").status_code == 401
        assert client.get("/api/news/wallets/events/missing", headers=AUTH).status_code == 404
        auxiliary = client.get("/api/news/wallets", headers=AUTH)
        assert auxiliary.status_code == 200
        status = auxiliary.json()["data"]
        assert set(status) == {
            "roster",
            "tape",
            "thresholds",
            "funnel",
            "collection_lagging",
            "notifications_enabled",
        }
        # An empty roster cannot reach either quorum, and the page must say so rather than "no chance
        # today": the counts, the two thresholds and the verdict are all the server's (#649 §7.3).
        assert status["thresholds"] == {"fast_n": 3, "slow_n": 5, "sufficient": False}
        assert status["roster"]["quality_count"] == status["roster"]["whale_count"] == 0
        assert status["roster"]["supported_quality_count"] == 0
        assert status["collection_lagging"] is True, "no collection cutoff at all is not a fresh one"
        # Three episodes, none of which reached a channel, and one stated leading reason.
        assert status["funnel"]["events"] == 3 and status["funnel"]["sent"] == 0
        assert status["funnel"]["unsent_reason"] == "wallet_notifications_disabled"
        assert status["funnel"]["unsent_reason_count"] == 1
        assert status["funnel"]["window_to_ms"] - status["funnel"]["window_from_ms"] == 86_400_000


def _roster_and_tape(conn: Any, *, quality: int, whale: int, at_ms: int, monitoring_from_ms: int) -> None:
    """The production shape of this defect: many observed addresses, almost no qualifying ones."""

    from tracefold.news.chain_tape.contracts import BLOCK_COMPLETE_TX_INDEX, RosterMember, TapeCursor

    members = [
        RosterMember(
            wallet="0x" + f"{index + 1:040x}",
            handle=f"handle{index + 1}",
            followers=0,
            realized_pnl=1000.0,
            closed_trades=20,
            win_rate=0.5,
            profit_factor=2.0,
            open_cost=10000.0,
            rank_quality=index + 1 if index < quality else None,
            rank_whale=index + 1 if index < whale else None,
        )
        for index in range(whale)
    ]
    repos = repositories_for_connection(conn)
    with repos.transaction():
        snapshot = repos.news.chain_tape_store_roster(members, now_ms=at_ms - 600_000)
        repos.news.chain_tape_save_state(
            cursor=TapeCursor(1000, BLOCK_COMPLETE_TX_INDEX),
            roster_version=snapshot.roster_version,
            outcome="success",
            error=None,
            now_ms=at_ms,
            succeeded=True,
        )
        repos.news.chain_tape_record_coverage(
            from_ms=at_ms - 3_600_000,
            through_ms=at_ms,
            through_block=1000,
            through_log=BLOCK_COMPLETE_TX_INDEX,
            gap_at_ms=None,
            wallets=snapshot.wallets,
        )
        conn.execute("UPDATE news_market_wallet_roster SET monitoring_from_ms = %s", (monitoring_from_ms,))
        # The refresh task's own record, which is what the page reads since #649 §5.1: this list was
        # published by a refresh that completed, so both stamps are that refresh's.
        repos.news.chain_tape_save_roster_refresh(now_ms=at_ms - 600_000, succeeded=True, error=None)
    conn.commit()


def test_wallet_status_counts_the_quality_pool_against_both_quorums_rather_than_the_whole_list(app, conn):
    """147 observed addresses and one qualifying one is 「the list cannot trigger」, not 「no opportunity」."""

    now = int(time.time() * 1000)
    _roster_and_tape(conn, quality=1, whale=147, at_ms=now, monitoring_from_ms=now - 3_600_000)
    with TestClient(app) as client:
        status = client.get("/api/news/wallets", headers=AUTH).json()["data"]
    assert status["roster"]["quality_count"] == 1
    assert status["roster"]["whale_count"] == 147
    assert status["roster"]["supported_quality_count"] == 1
    assert status["thresholds"] == {"fast_n": 3, "slow_n": 5, "sufficient": False}
    assert status["collection_lagging"] is False
    assert status["roster"]["last_success_at_ms"] == status["roster"]["taken_at_ms"]
    # The attempt stamp is the refresh task's, and a refresh that published set both to the same
    # moment. It is not derived from the collection turn (#649 §5.1).
    assert status["roster"]["last_attempt_at_ms"] == status["roster"]["last_success_at_ms"]
    assert status["roster"]["last_error"] is None
    assert status["roster"]["window"] == "30d"
    assert status["notifications_enabled"] is True
    assert len(status["roster"]["members"]) == 147


def test_wallet_status_separates_a_warming_up_pool_from_one_that_is_simply_too_small(app, conn):
    now = int(time.time() * 1000)
    _roster_and_tape(conn, quality=6, whale=147, at_ms=now, monitoring_from_ms=now - 60_000)
    with TestClient(app) as client:
        status = client.get("/api/news/wallets", headers=AUTH).json()["data"]
    # Six qualifying addresses, none of which has watched a whole 5m window yet.
    assert status["roster"]["quality_count"] == 6
    assert status["roster"]["supported_quality_count"] == 0
    assert status["thresholds"]["sufficient"] is False


def test_wallet_status_reports_a_roster_refresh_failure_without_moving_the_published_version(app, conn):
    """The refresh task's own record, not the collection turn's error (#649 §5.1).

    The collection turn is a success here and the refresh is the thing that failed, which is the whole
    point of separating them: a page that inferred the roster's health from the tape's `last_error`
    could not tell a throttled refresh from a chain RPC that would not answer.
    """

    now = int(time.time() * 1000)
    _roster_and_tape(conn, quality=6, whale=6, at_ms=now, monitoring_from_ms=now - 3_600_000)
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.news.chain_tape_save_roster_refresh(
            now_ms=now + 5_000, succeeded=False, error="robinhoodtrenches:roster_rate_limited"
        )
    conn.commit()
    with TestClient(app) as client:
        status = client.get("/api/news/wallets", headers=AUTH).json()["data"]
    assert status["roster"]["last_error"] == "robinhoodtrenches:roster_rate_limited"
    assert status["roster"]["last_attempt_at_ms"] == now + 5_000
    # The failure publishes nothing, so the last complete version and its real stamp are untouched.
    assert status["roster"]["last_success_at_ms"] == status["roster"]["taken_at_ms"] == now - 600_000
    assert status["tape"]["last_outcome"] == "success", "the chain half succeeded; only the refresh failed"
    assert status["thresholds"]["sufficient"] is True


def test_wallet_status_calls_a_stale_collection_cutoff_lagging(app, conn):
    now = int(time.time() * 1000)
    _roster_and_tape(conn, quality=6, whale=6, at_ms=now, monitoring_from_ms=now - 3_600_000)
    conn.execute("UPDATE news_market_wallet_tape_state SET scanned_at_ms = %s", (now - 120_000,))
    conn.commit()
    with TestClient(app) as client:
        status = client.get("/api/news/wallets", headers=AUTH).json()["data"]
    assert status["collection_lagging"] is True


def test_wallet_detail_timeline_exact_cutoff_and_keyset_include_small_sell_and_transfer(app, conn):
    from tests.news.net_buy_fixtures import movement

    stamp = int(time.time() * 1000) - 1000
    episode = _admit_wallet_observation(conn, at_ms=stamp)
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.news.chain_tape_record_fills(
            [
                movement(1, at=stamp, raw=1234567890123456789),
                movement(2, wallet=1, kind="sell", usd="1", raw=1, at=stamp),
                movement(3, wallet=1, kind="transfer_out", usd=None, raw=1, at=stamp),
                replace(movement(4, at=stamp), log_index=101),
            ]
        )
    conn.commit()
    with TestClient(app) as client:
        first = client.get(f"/api/news/wallets/events/{episode}", headers=AUTH, params={"limit": 2}).json()["data"]
        assert [row["kind"] for row in first["fills"]] == ["transfer_out", "sell"]
        second = client.get(
            f"/api/news/wallets/events/{episode}",
            headers=AUTH,
            params={"limit": 2, "fills_cursor": first["next_fills_cursor"]},
        ).json()["data"]
        assert [row["amount_raw"] for row in second["fills"]] == ["1234567890123456789"]
        assert second["next_fills_cursor"] is None
