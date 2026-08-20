"""Price Review plane against real PostgreSQL (#88): resolution, quotes, due work, review aggregates.

These are the assertions that only a real database can make: idempotent keys, the due scan's terminality,
retention cascade, and the shape of the bounded review aggregates over the actual JSONB the pipeline writes.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.repositories import repositories_for_connection
from tracefold.news.instruments import Instrument
from tracefold.news.pricing import HORIZON_MS, REACTION_METRIC_VERSION, Quote

pytestmark = pytest.mark.integration

NOW = 1_787_000_000_000
HOUR = 3_600_000


@pytest.fixture(scope="module")
def conn():
    connection = connect_postgres_test(read_only=False)
    migrate(connection)
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def _clean(conn):
    for table in (
        "news_event_reactions",
        "news_quote_snapshots",
        "news_event_assets",
        "news_verdicts",
        "news_deliveries",
        "news_events",
        "news_items",
        "news_market_instruments",
        "news_symbol_aliases",
    ):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()


def _universe(conn, *instruments: Instrument) -> None:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.instruments.apply_snapshot(list(instruments), now_ms=NOW)


def _instrument(venue: str, venue_symbol: str, base: str, quote: str | None = "USDT") -> Instrument:
    return Instrument(
        venue=venue, venue_symbol=venue_symbol, base_symbol=base, instrument_class="crypto", quote_asset=quote
    )


def _event(
    conn,
    event_id: str,
    *,
    symbols: tuple[str, ...],
    opened_at_ms: int,
    decision: str = "push",
    direction: str = "bullish",
    delivered: bool = True,
    degraded: bool = False,
    magnitude: int = 2,
    event_type: str = "listing",
    ingest_mode: str = "live",
) -> None:
    conn.execute(
        """
        INSERT INTO news_items (
          item_id, source_id, source_item_key, title, published_at_ms, observed_at_ms,
          provider_metadata, first_ingest_mode, created_at_ms, updated_at_ms
        ) VALUES (%s, 'opennews', %s, 'headline', %s, %s, '{}'::jsonb, 'live', %s, %s)
        """,
        (f"i-{event_id}", f"i-{event_id}", opened_at_ms, opened_at_ms, opened_at_ms, opened_at_ms),
    )
    conn.execute(
        """
        INSERT INTO news_events (
          event_id, leader_item_id, family, comparison_fingerprint, comparison_title, leader_title,
          opened_at_ms, last_member_at_ms, expires_at_ms, admission, storyline_key, ingest_mode,
          created_at_ms, updated_at_ms
        ) VALUES (%s, %s, 'general', %s, 'c', 'leader headline', %s, %s, %s, 'candidate', %s, %s, %s, %s)
        """,
        (
            event_id,
            f"i-{event_id}",
            event_id,
            opened_at_ms,
            opened_at_ms,
            opened_at_ms + HOUR,
            f"asset:{symbols[0]}" if symbols else "theme:none",
            ingest_mode,
            opened_at_ms,
            opened_at_ms,
        ),
    )
    for symbol in symbols:
        conn.execute(
            "INSERT INTO news_event_assets (symbol, event_id, market_type, opened_at_ms) VALUES (%s, %s, NULL, %s)",
            (symbol, event_id, opened_at_ms),
        )
    verdict = {
        "direction": direction,
        "magnitude": magnitude,
        "event_type": event_type,
        "assets": [{"symbol": symbol, "role": "primary"} for symbol in symbols],
    }
    conn.execute(
        """
        INSERT INTO news_verdicts (
          event_id, stage, policy_version, rule_baseline_decision, final_decision, verdict, degraded,
          created_at_ms
        ) VALUES (%s, 'triage', 'v6', %s, %s, %s::jsonb, %s, %s)
        """,
        (event_id, decision, decision, _json(verdict), degraded, opened_at_ms),
    )
    if delivered:
        conn.execute(
            """
            INSERT INTO news_deliveries (event_id, kind, state, card, attempted_at_ms, settled_at_ms,
                                         created_at_ms)
            VALUES (%s, 'first', 'sent', '{}'::jsonb, %s, %s, %s)
            """,
            (event_id, opened_at_ms, opened_at_ms, opened_at_ms),
        )
    conn.commit()


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


# ---------------------------------------------------------------------------- resolution
def test_resolution_is_exact_symbol_first_and_never_reference_only(conn) -> None:
    _universe(
        conn,
        _instrument("hl.xyz", "xyz:SKHY", "SKHY", None),
        _instrument("hl.xyz", "xyz:SKHX", "SKHX", None),
        _instrument("binance.perp", "BTCUSDT", "BTC"),
        _instrument("binance.spot", "BTCUSDC", "BTC", "USDC"),
        Instrument(venue="us.listed", venue_symbol="UWMC", base_symbol="UWMC", instrument_class="equity"),
    )
    conn.execute(
        "INSERT INTO news_symbol_aliases (alias, base_symbol, source, updated_at_ms)"
        " VALUES ('SKHX', 'SKHY', 'venue', %s) ON CONFLICT (alias) DO UPDATE SET base_symbol = 'SKHY'",
        (NOW,),
    )
    conn.commit()
    repos = repositories_for_connection(conn)

    resolved = repos.price.resolve_instruments(["SKHX", "SKHY", "BTC", "UWMC", "NOPE"])

    # The storyline throttle collapses SKHX into SKHY; pricing keeps the contract the Event actually named.
    assert resolved["SKHX"].venue_symbol == "xyz:SKHX"
    assert resolved["SKHY"].venue_symbol == "xyz:SKHY"
    # Venue precedence: the perp outranks spot, and USDT outranks USDC inside a venue.
    assert resolved["BTC"].venue == "binance.perp"
    # A reference-only ticker names something, but nothing anyone can price here.
    assert "UWMC" not in resolved and "NOPE" not in resolved


def test_an_alias_still_resolves_a_tag_that_names_nothing_on_its_own(conn) -> None:
    _universe(conn, _instrument("binance.perp", "GOLDUSDT", "GOLD"))
    conn.execute(
        "INSERT INTO news_symbol_aliases (alias, base_symbol, source, updated_at_ms)"
        " VALUES ('XAU', 'GOLD', 'venue', %s)",
        (NOW,),
    )
    conn.commit()
    resolved = repositories_for_connection(conn).price.resolve_instruments(["XAU"])
    assert resolved["XAU"].venue_symbol == "GOLDUSDT"


# ---------------------------------------------------------------------------- quotes
def test_quote_snapshots_are_latest_only_and_one_row_per_source(conn) -> None:
    _universe(conn, _instrument("binance.perp", "BTCUSDT", "BTC"))
    repos = repositories_for_connection(conn)
    quote = Quote(
        venue="binance.perp",
        venue_symbol="BTCUSDT",
        base_symbol="BTC",
        price=Decimal("68000"),
        price_kind="last",
        instrument_class="crypto",
        quote_asset="USDT",
        change_pct=1.5,
        change_basis="rolling_24h",
        source_at_ms=NOW - 500,
    )
    with repos.transaction():
        for index in range(3):
            repos.price.replace_source_snapshot(
                source_key="binance.perp",
                quotes=[quote],
                target_count=1,
                source_at_ms=NOW - 500,
                received_at_ms=NOW + index,
                now_ms=NOW + index,
            )

    rows = conn.execute("SELECT source_key, received_at_ms FROM news_quote_snapshots").fetchall()
    assert len(rows) == 1 and int(rows[0]["received_at_ms"]) == NOW + 2  # last value wins, no history


def test_quote_results_name_their_own_state_and_never_fabricate_a_price(conn) -> None:
    _universe(
        conn,
        _instrument("binance.perp", "BTCUSDT", "BTC"),
        _instrument("hl.perp", "HYPE", "HYPE", None),
    )
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.price.replace_source_snapshot(
            source_key="binance.perp",
            quotes=[
                Quote(
                    venue="binance.perp",
                    venue_symbol="BTCUSDT",
                    base_symbol="BTC",
                    price=Decimal("68000"),
                    price_kind="last",
                    change_pct=1.5,
                    change_basis="rolling_24h",
                    source_at_ms=NOW,
                )
            ],
            target_count=1,
            source_at_ms=NOW,
            received_at_ms=NOW,
            now_ms=NOW,
        )

    fresh = {
        row["requested_symbol"]: row
        for row in repos.price.quotes_for_symbols(["BTC", "HYPE", "NOPE"], now_ms=NOW + 1_000)
    }
    assert fresh["BTC"]["state"] == "fresh" and fresh["BTC"]["price"] == "68000"
    assert fresh["BTC"]["change_basis"] == "rolling_24h"
    # Quoted by a source that has not answered yet is not the same as naming nothing.
    assert fresh["HYPE"]["state"] == "unavailable" and fresh["HYPE"]["price"] is None
    assert fresh["NOPE"]["state"] == "unlisted" and fresh["NOPE"]["venue"] is None

    stale = {row["requested_symbol"]: row for row in repos.price.quotes_for_symbols(["BTC"], now_ms=NOW + 60_000)}
    assert stale["BTC"]["state"] == "stale" and stale["BTC"]["price"] == "68000"  # stale keeps its number


def test_duplicate_request_symbols_cannot_multiply_repository_work(conn) -> None:
    _universe(conn, _instrument("binance.perp", "BTCUSDT", "BTC"))
    repos = repositories_for_connection(conn)
    results = repos.price.quotes_for_symbols(["BTC", "btc", "BTC"], now_ms=NOW)
    assert [row["requested_symbol"] for row in results] == ["BTC", "btc"]


# ---------------------------------------------------------------------------- due work
def test_the_due_scan_covers_held_events_and_stops_at_terminal_rows(conn) -> None:
    _universe(conn, _instrument("binance.perp", "BTCUSDT", "BTC"))
    _event(conn, "pushed", symbols=("BTC",), opened_at_ms=NOW - 2 * HOUR)
    _event(conn, "dropped", symbols=("BTC",), opened_at_ms=NOW - 2 * HOUR, decision="drop", delivered=False)
    _event(conn, "fresh", symbols=("BTC",), opened_at_ms=NOW - 60_000)  # 1H not due yet
    _event(conn, "recovered", symbols=("BTC",), opened_at_ms=NOW - 2 * HOUR, ingest_mode="recovery")
    repos = repositories_for_connection(conn)

    due = repos.price.due_reactions(now_ms=NOW, limit=100)

    # Acquisition is not restricted to delivered Events — a held Event is exactly what the miss review needs.
    assert {row["event_id"] for row in due} == {"pushed", "dropped"}

    with repos.transaction():
        repos.price.upsert_reaction(
            {
                "event_id": "pushed",
                "symbol": "BTC",
                "anchor_at_ms": NOW - 2 * HOUR,
                "venue": "binance.perp",
                "venue_symbol": "BTCUSDT",
                "p0": Decimal("100"),
                "p0_at_ms": NOW - 2 * HOUR,
                "p1": Decimal("101"),
                "p1_at_ms": NOW - HOUR,
                "return_1h_bps": 100,
                "state": "partial",
                "unavailable_reason": "no_candle_within_gap",
            },
            now_ms=NOW,
        )

    # A partial row that already named its reason has finished trying; re-asking every minute is a spin.
    assert {row["event_id"] for row in repos.price.due_reactions(now_ms=NOW, limit=100)} == {"dropped"}


def test_reaction_writes_are_idempotent_and_never_lose_a_persisted_price_point(conn) -> None:
    _universe(conn, _instrument("binance.perp", "BTCUSDT", "BTC"))
    _event(conn, "e1", symbols=("BTC",), opened_at_ms=NOW - 5 * HOUR)
    repos = repositories_for_connection(conn)
    base = {
        "event_id": "e1",
        "symbol": "BTC",
        "anchor_at_ms": NOW - 5 * HOUR,
        "venue": "binance.perp",
        "venue_symbol": "BTCUSDT",
        "p0": Decimal("100"),
        "p0_at_ms": NOW - 5 * HOUR,
        "p1": Decimal("101"),
        "p1_at_ms": NOW - 4 * HOUR,
        "return_1h_bps": 100,
        "state": "partial",
    }
    with repos.transaction():
        repos.price.upsert_reaction(base, now_ms=NOW)
        repos.price.upsert_reaction(base, now_ms=NOW)  # replay writes the same row
        repos.price.upsert_reaction(
            {**base, "p4": Decimal("110"), "p4_at_ms": NOW - HOUR, "return_4h_bps": 1000, "state": "complete"},
            now_ms=NOW,
        )

    rows = repos.price.event_reactions("e1")
    assert len(rows) == 1
    assert rows[0]["state"] == "complete"
    assert rows[0]["return_1h_bps"] == 100 and rows[0]["return_4h_bps"] == 1000
    assert rows[0]["p0"].startswith("100")  # the raw close is retained beside the return, for audit
    assert rows[0]["metric_version"] == REACTION_METRIC_VERSION


def test_reactions_cascade_with_the_event_under_existing_retention(conn) -> None:
    _universe(conn, _instrument("binance.perp", "BTCUSDT", "BTC"))
    _event(conn, "e1", symbols=("BTC",), opened_at_ms=NOW - 5 * HOUR)
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.price.upsert_reaction(
            {
                "event_id": "e1",
                "symbol": "BTC",
                "anchor_at_ms": NOW - 5 * HOUR,
                "state": "unavailable",
                "unavailable_reason": "instrument_unresolved",
            },
            now_ms=NOW,
        )
    conn.execute("DELETE FROM news_items WHERE item_id = 'i-e1'")
    conn.commit()
    assert conn.execute("SELECT count(*) AS n FROM news_event_reactions").fetchone()["n"] == 0


# ---------------------------------------------------------------------------- review
def _complete(conn, event_id: str, symbol: str, *, anchor: int, bps_1h: int, bps_4h: int) -> None:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.price.upsert_reaction(
            {
                "event_id": event_id,
                "symbol": symbol,
                "anchor_at_ms": anchor,
                "venue": "binance.perp",
                "venue_symbol": f"{symbol}USDT",
                "p0": Decimal("100"),
                "p0_at_ms": anchor,
                "p1": Decimal("101"),
                "p1_at_ms": anchor + HOUR,
                "p4": Decimal("104"),
                "p4_at_ms": anchor + 4 * HOUR,
                "return_1h_bps": bps_1h,
                "return_4h_bps": bps_4h,
                "is_primary": True,
                "state": "complete",
            },
            now_ms=NOW,
        )


def test_review_reports_coverage_direction_and_potential_misses(conn) -> None:
    _universe(conn, _instrument("binance.perp", "BTCUSDT", "BTC"), _instrument("binance.perp", "ETHUSDT", "ETH"))
    anchor = NOW - 6 * HOUR
    _event(conn, "hit", symbols=("BTC",), opened_at_ms=anchor, direction="bullish")
    _event(
        conn, "miss", symbols=("ETH",), opened_at_ms=anchor, direction="bearish", decision="throttled", delivered=False
    )
    _event(conn, "nocover", symbols=("BTC",), opened_at_ms=anchor, direction="bullish")
    _complete(conn, "hit", "BTC", anchor=anchor, bps_1h=150, bps_4h=300)
    _complete(conn, "miss", "ETH", anchor=anchor, bps_1h=900, bps_4h=1200)

    review = repositories_for_connection(conn).price.review(hours=168, now_ms=NOW)

    coverage = {row["horizon"]: row for row in review["coverage"]}
    assert coverage["1h"]["eligible_n"] == 3
    assert coverage["1h"]["priced_n"] == 2
    assert coverage["1h"]["coverage_pct"] == pytest.approx(66.7, abs=0.1)

    directions = {(row["direction"], row["horizon"]): row for row in review["directions"]}
    assert directions[("bullish", "1h")]["hits"] == 1
    assert directions[("bullish", "1h")]["priced_n"] == 1
    assert directions[("bearish", "1h")]["hits"] == 0  # a bearish call that rose is not a hit
    assert review["summary"]["hit_1h_n"] == 2
    assert review["summary"]["hit_1h_pct"] == pytest.approx(50.0)

    misses = review["potential_misses"]
    assert [row["event_id"] for row in misses] == ["miss"]  # only what never reached the reader
    assert misses[0]["final_decision"] == "throttled"
    assert misses[0]["return_1h_bps"] == 900
    assert misses[0]["assets"][0]["venue_symbol"] == "ETHUSDT"

    magnitudes = {row["magnitude"]: row for row in review["magnitudes"]}
    assert magnitudes[2]["eligible_n"] == 3
    assert magnitudes[2]["median_abs_1h_bps"] in {150, 900}
    types = {row["event_type"]: row for row in review["event_types"]}
    assert types["listing"]["eligible_n"] == 3 and types["listing"]["pushed_n"] == 2


def test_degraded_and_recovery_events_stay_out_of_the_scored_denominators(conn) -> None:
    _universe(conn, _instrument("binance.perp", "BTCUSDT", "BTC"))
    anchor = NOW - 6 * HOUR
    _event(conn, "degraded", symbols=("BTC",), opened_at_ms=anchor, degraded=True)
    _event(conn, "recovery", symbols=("BTC",), opened_at_ms=anchor, ingest_mode="recovery")
    _complete(conn, "degraded", "BTC", anchor=anchor, bps_1h=500, bps_4h=500)
    _complete(conn, "recovery", "BTC", anchor=anchor, bps_1h=500, bps_4h=500)

    review = repositories_for_connection(conn).price.review(hours=168, now_ms=NOW)

    assert review["summary"]["hit_1h_n"] == 0
    coverage = {row["horizon"]: row for row in review["coverage"]}
    assert coverage["1h"]["eligible_n"] == 1  # recovery never enters the eligible set at all
    assert coverage["1h"]["degraded_n"] == 1  # the degraded one stays visible in the diagnostics


def test_event_level_aggregate_contributes_one_sample_per_event(conn) -> None:
    _universe(conn, _instrument("binance.perp", "BTCUSDT", "BTC"), _instrument("binance.perp", "ETHUSDT", "ETH"))
    anchor = NOW - 6 * HOUR
    _event(conn, "multi", symbols=("BTC", "ETH"), opened_at_ms=anchor)
    _complete(conn, "multi", "BTC", anchor=anchor, bps_1h=100, bps_4h=100)
    _complete(conn, "multi", "ETH", anchor=anchor, bps_1h=300, bps_4h=300)
    repos = repositories_for_connection(conn)

    aggregates = repos.price.event_reaction_aggregates(["multi"], now_ms=NOW)

    assert aggregates["multi"]["asset_n"] == 2
    assert aggregates["multi"]["priced_n"] == 2
    assert aggregates["multi"]["return_1h_bps"] == 100  # discrete median, not a sum
    assert aggregates["multi"]["state"] == "complete"

    review = repos.price.review(hours=168, now_ms=NOW)
    coverage = {row["horizon"]: row for row in review["coverage"]}
    assert coverage["1h"]["eligible_n"] == 1  # mentioning two assets does not double-weight one judgment


def test_an_event_with_no_priceable_primary_has_no_aggregate_but_stays_visible(conn) -> None:
    _universe(conn, _instrument("binance.perp", "BTCUSDT", "BTC"))
    anchor = NOW - 6 * HOUR
    _event(conn, "e1", symbols=("BTC",), opened_at_ms=anchor)
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.price.upsert_reaction(
            {
                "event_id": "e1",
                "symbol": "BTC",
                "anchor_at_ms": anchor,
                "is_primary": True,
                "state": "unavailable",
                "unavailable_reason": "no_candle_within_gap",
            },
            now_ms=NOW,
        )

    aggregate = repos.price.event_reaction_aggregates(["e1"], now_ms=NOW)["e1"]
    assert aggregate["state"] == "unavailable"
    assert aggregate["return_1h_bps"] is None
    assert aggregate["unavailable_reason"] == "no_candle_within_gap"
    review = repos.price.review(hours=168, now_ms=NOW)
    reasons = {row["reason"] for row in review["coverage"][0]["unavailable"]}
    assert "no_candle_within_gap" in reasons


def test_price_status_reports_source_freshness_and_backlog(conn) -> None:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.price.replace_source_snapshot(
            source_key="hl.perp",
            quotes=[
                Quote(
                    venue="hl.perp",
                    venue_symbol="HYPE",
                    base_symbol="HYPE",
                    price=Decimal("40"),
                    price_kind="mid",
                )
            ],
            target_count=1,
            source_at_ms=None,
            received_at_ms=NOW,
            now_ms=NOW,
        )

    status = repos.price.price_status(now_ms=NOW + 1_000)
    assert status["sources"][0]["source_key"] == "hl.perp"
    assert status["sources"][0]["state"] == "fresh"
    assert status["quotes"] == 1
    assert status["metric_version"] == REACTION_METRIC_VERSION


def test_the_review_window_is_bounded_by_the_requested_hours(conn) -> None:
    _universe(conn, _instrument("binance.perp", "BTCUSDT", "BTC"))
    old = NOW - 200 * HOUR
    _event(conn, "old", symbols=("BTC",), opened_at_ms=old)
    _complete(conn, "old", "BTC", anchor=old, bps_1h=100, bps_4h=100)
    repos = repositories_for_connection(conn)

    assert repos.price.review(hours=168, now_ms=NOW)["coverage"][0]["eligible_n"] == 0
    assert repos.price.review(hours=720, now_ms=NOW)["coverage"][0]["eligible_n"] == 1
    assert repos.price.review(hours=168, now_ms=NOW)["meta"]["hours"] == 168


def test_horizon_constants_match_the_stored_metric(conn) -> None:
    del conn
    assert HORIZON_MS == {"1h": 3_600_000, "4h": 14_400_000}
