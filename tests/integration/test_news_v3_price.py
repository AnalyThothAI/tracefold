"""Price Review plane against real PostgreSQL (#88): resolution, quotes, due work, review aggregates.

These are the assertions that only a real database can make: idempotent keys, the due scan's terminality,
retention cascade, and the shape of the bounded review aggregates over the actual JSONB the pipeline writes.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_event_updates import persist_analysis_document
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.market_review.instruments import Instrument
from tracefold.news.market_review.pricing import (
    QUOTE_FRESH_MAX_AGE_MS,
    Quote,
    QuoteRequest,
)
from tracefold.news.reader_card import quote_line, reader_quotes

pytestmark = pytest.mark.integration

NOW = 1_787_000_000_000
HOUR = 3_600_000


@pytest.fixture(scope="module")
def conn(postgres_module_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def _clean(conn):
    for table in (
        "news_quote_snapshots",
        "news_market_observations",
        "news_event_assets",
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
    ingest_mode: str = "live",
    admission: str = "candidate",
    event_kind: str = "news",
    ground_assets: bool = True,
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
          event_id, leader_item_id, dedupe_family, event_kind, comparison_fingerprint, comparison_title, leader_title,
          focus_fact_id, focus_fact_text, focus_fact_context, focus_fact_method, focus_span_start, focus_span_end,
          opened_at_ms, last_member_at_ms, expires_at_ms, admission, storyline_key, ingest_mode,
          created_at_ms, updated_at_ms
        ) VALUES (
          %s, %s, 'general', %s, %s, 'c', 'leader headline', %s,
          'leader headline', '', 'whole_item', 0, 15,
          %s, %s, %s, %s, %s, %s, %s, %s
        )
        """,
        (
            event_id,
            f"i-{event_id}",
            event_kind,
            event_id,
            f"fact:{event_id}",
            opened_at_ms,
            opened_at_ms,
            opened_at_ms + HOUR,
            admission,
            f"asset:{symbols[0]}" if symbols else "topic:rates",
            ingest_mode,
            opened_at_ms,
            opened_at_ms,
        ),
    )
    for symbol in symbols if ground_assets else ():
        conn.execute(
            "INSERT INTO news_event_assets (symbol, event_id, market_type, opened_at_ms) VALUES (%s, %s, NULL, %s)",
            (symbol, event_id, opened_at_ms),
        )
    revision = "a" * 64
    document = {
        "schema_version": "news_event_update_v2",
        "event_id": event_id,
        "content_revision": revision,
        "input_revision": 1,
        "previous_content_revision": None,
        "claims": [
            {
                "ref": f"cl:{event_id}",
                "fields": {
                    "assets": [
                        {"symbol": symbol, "market_type": "crypto_perp", "role": "primary"} for symbol in symbols
                    ]
                },
            }
        ],
    }
    persist_analysis_document(conn, document, adopted_at_ms=opened_at_ms)
    conn.commit()


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

    resolved = repos.price.resolve_instruments(
        [QuoteRequest("SKHX"), QuoteRequest("SKHY"), QuoteRequest("BTC"), QuoteRequest("UWMC"), QuoteRequest("NOPE")]
    )

    # Storyline identity collapses SKHX into SKHY; pricing keeps the contract the Event actually named.
    assert resolved[QuoteRequest("SKHX")].venue_symbol == "xyz:SKHX"
    assert resolved[QuoteRequest("SKHY")].venue_symbol == "xyz:SKHY"
    # Venue precedence: the perp outranks spot, and USDT outranks USDC inside a venue.
    assert resolved[QuoteRequest("BTC")].venue == "binance.perp"
    # A reference-only ticker names something, but nothing anyone can price here.
    assert QuoteRequest("UWMC") not in resolved and QuoteRequest("NOPE") not in resolved


def test_an_alias_still_resolves_a_tag_that_names_nothing_on_its_own(conn) -> None:
    _universe(conn, _instrument("binance.perp", "GOLDUSDT", "GOLD"))
    conn.execute(
        "INSERT INTO news_symbol_aliases (alias, base_symbol, source, updated_at_ms)"
        " VALUES ('XAU', 'GOLD', 'venue', %s)",
        (NOW,),
    )
    conn.commit()
    resolved = repositories_for_connection(conn).price.resolve_instruments([QuoteRequest("XAU")])
    assert resolved[QuoteRequest("XAU")].venue_symbol == "GOLDUSDT"


def test_delivery_resolution_exposes_ordered_venue_fallbacks_without_crossing_an_exact_alias(conn) -> None:
    _universe(
        conn,
        Instrument("binance.perp", "MSFTUSDT", "MSFT", "equity", "USDT"),
        Instrument("hl.xyz", "xyz:MSFT", "MSFT", "equity"),
        Instrument("okx.perp", "MSFT-USDT-SWAP", "MSFT", "equity", "USDT"),
        Instrument("hl.xyz", "xyz:SKHX", "SKHX", "equity"),
        Instrument("binance.perp", "SKHYUSDT", "SKHY", "equity", "USDT"),
    )
    conn.execute(
        "INSERT INTO news_symbol_aliases (alias, base_symbol, source, updated_at_ms)"
        " VALUES ('SKHX', 'SKHY', 'venue', %s)",
        (NOW,),
    )
    conn.commit()

    resolved = repositories_for_connection(conn).price.instruments_for_symbols(
        [QuoteRequest("MSFT"), QuoteRequest("SKHX")]
    )

    assert [(row.venue, row.venue_symbol) for row in resolved[QuoteRequest("MSFT")]] == [
        ("binance.perp", "MSFTUSDT"),
        ("hl.xyz", "xyz:MSFT"),
        ("okx.perp", "MSFT-USDT-SWAP"),
    ]
    assert [row.venue_symbol for row in resolved[QuoteRequest("SKHX")]] == ["xyz:SKHX"]


def test_quote_working_set_includes_recent_oi_ledger_symbols(conn) -> None:
    _universe(conn, _instrument("binance.perp", "DOGEUSDT", "DOGE"))
    # The ordinary grounded lane also names DOGE; the UNION must still return one symbol and obey the
    # caller's existing bound rather than multiplying provider work.
    _event(conn, "ev-news-doge", symbols=("DOGE",), opened_at_ms=NOW - 1)
    # The OI arm reaches the ledger through the Item that produced it (#553). There is no Event: a
    # market observation opens none, and the working set was reading one only because the foreign key
    # forced it to.
    from tests.support.market_oi import _write_oi

    repos = repositories_for_connection(conn)
    _write_oi(repos.news, "i-ev-oi", at_ms=NOW, change_bps=864)
    conn.execute(
        "UPDATE news_market_observations SET symbol='DOGE',raw_instrument='DOGE' WHERE observation_id='i-ev-oi'"
    )
    conn.commit()

    repos = repositories_for_connection(conn)
    symbols = repos.price.quote_target_symbols(since_ms=NOW - HOUR)

    assert symbols == ["DOGE"]
    assert repos.price.quote_target_symbols(since_ms=NOW - HOUR, limit=1) == ["DOGE"]


def test_quote_working_set_uses_admission_for_recovered_events(conn) -> None:
    _event(conn, "fresh", symbols=("BTC",), opened_at_ms=NOW, ingest_mode="recovery")
    _event(
        conn,
        "listing",
        symbols=("ETH",),
        opened_at_ms=NOW - 1,
        ingest_mode="recovery",
        admission="listing_deterministic",
    )
    _event(conn, "history", symbols=("DOGE",), opened_at_ms=NOW - 2, ingest_mode="recovery", admission="recovery")
    assert repositories_for_connection(conn).price.quote_target_symbols(since_ms=NOW - HOUR) == ["BTC", "ETH"]


def test_a_typed_question_never_resolves_to_a_same_name_contract_of_another_market(conn) -> None:
    """#651 §6.2, against the real catalogue: `V` is Visa and a crypto venue also lists a `V`.

    The untyped question returns whichever contract the venue ranking puts first — which is the coin,
    because a perp outranks everything — and nothing in the row says the Event was about a company. The
    typed question filters `news_market_instruments.instrument_class`, so the equity Event resolves to an
    equity contract or to nothing at all.
    """

    _universe(
        conn,
        _instrument("binance.perp", "VUSDT", "V"),
        Instrument("hl.xyz", "xyz:V", "V", "equity"),
        _instrument("binance.perp", "SEIUSDT", "SEI"),
        Instrument(venue="us.listed", venue_symbol="SEI", base_symbol="SEI", instrument_class="equity"),
    )
    repos = repositories_for_connection(conn)

    untyped = repos.price.resolve_instruments([QuoteRequest("V")])
    equity = repos.price.resolve_instruments([QuoteRequest("V", "equity")])
    crypto = repos.price.resolve_instruments([QuoteRequest("SEI", "crypto")])
    listed_only = repos.price.resolve_instruments([QuoteRequest("SEI", "equity")])

    assert untyped[QuoteRequest("V")].venue_symbol == "VUSDT"
    assert equity[QuoteRequest("V", "equity")].venue_symbol == "xyz:V"
    assert crypto[QuoteRequest("SEI", "crypto")].venue_symbol == "SEIUSDT"
    # The `us.listed` reference tier answers "this ticker exists", never "this is what it costs", so an
    # equity question it is the only answer to resolves to no priceable contract at all.
    assert listed_only == {}
    conn.commit()


def test_a_directory_only_equity_is_unavailable_rather_than_unlisted_or_a_coin(conn) -> None:
    """The third quote answer (#651 §6.2): the instrument is real and we poll no price source for it."""

    _universe(
        conn,
        _instrument("binance.perp", "SEIUSDT", "SEI"),
        Instrument(venue="us.listed", venue_symbol="SEI", base_symbol="SEI", instrument_class="equity"),
    )
    repos = repositories_for_connection(conn)

    rows = {
        (row["requested_symbol"], row["instrument_class"]): row
        for row in repos.price.quotes_for_symbols(
            [QuoteRequest("SEI", "equity"), QuoteRequest("SEI", "crypto"), QuoteRequest("NOPE", "equity")],
            now_ms=NOW,
        )
    }

    assert rows[("SEI", "equity")]["state"] == "unavailable"
    assert rows[("SEI", "equity")]["price"] is None and rows[("SEI", "equity")]["venue"] is None
    # The coin question still resolves to the coin; the two are separate rows of one batch.
    assert rows[("SEI", "crypto")]["state"] in {"unavailable", "unlisted"}
    assert rows[("SEI", "crypto")]["venue"] == "binance.perp"
    # A symbol the catalogue holds nowhere in the asked market is `unlisted`, which is a different answer.
    assert rows[("NOPE", None)]["state"] == "unlisted"
    conn.commit()


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


def test_forgetting_a_source_leaves_every_planned_one_alone(conn) -> None:
    """#88 follow-up: a source whose targets rotated out must not linger as a permanently stale row."""

    repos = repositories_for_connection(conn)
    quote = Quote(venue="hl.mkts", venue_symbol="mkts:X", base_symbol="X", price=Decimal("1"), price_kind="mid")
    with repos.transaction():
        for source in ("binance.perp", "hl.mkts"):
            repos.price.replace_source_snapshot(
                source_key=source,
                quotes=[quote],
                target_count=1,
                source_at_ms=NOW,
                received_at_ms=NOW,
                now_ms=NOW,
            )
        dropped = repos.price.forget_sources_except(["binance.perp"])

    assert dropped == 1
    assert set(repos.price.quote_snapshots()) == {"binance.perp"}
    with repos.transaction():
        assert repos.price.forget_sources_except([]) == 0  # an empty plan never wipes the table


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
                    reference_at_ms=NOW,
                )
            ],
            target_count=1,
            source_at_ms=NOW,
            received_at_ms=NOW,
            now_ms=NOW,
        )

    fresh = {
        row["requested_symbol"]: row
        for row in repos.price.quotes_for_symbols(
            [QuoteRequest("BTC"), QuoteRequest("HYPE"), QuoteRequest("NOPE")], now_ms=NOW + 1_000
        )
    }
    assert fresh["BTC"]["state"] == "fresh" and fresh["BTC"]["price"] == "68000"
    assert fresh["BTC"]["change_basis"] == "rolling_24h"
    assert fresh["BTC"]["received_age_ms"] == 1_000
    assert fresh["BTC"]["source_age_ms"] == 1_000
    assert fresh["BTC"]["effective_age_ms"] == 1_000
    assert fresh["BTC"]["freshness_basis"] == "source_and_received"
    assert fresh["BTC"]["reference_at_ms"] == NOW
    assert fresh["BTC"]["reference_age_ms"] == 1_000
    assert "age_ms" not in fresh["BTC"]
    # Quoted by a source that has not answered yet is not the same as naming nothing.
    assert fresh["HYPE"]["state"] == "unavailable" and fresh["HYPE"]["price"] is None
    assert fresh["NOPE"]["state"] == "unlisted" and fresh["NOPE"]["venue"] is None
    for absent in (fresh["HYPE"], fresh["NOPE"]):
        assert absent["received_age_ms"] is None
        assert absent["source_age_ms"] is None
        assert absent["effective_age_ms"] is None
        assert absent["freshness_basis"] is None
        assert absent["reference_at_ms"] is None
        assert absent["reference_age_ms"] is None

    aged = NOW + QUOTE_FRESH_MAX_AGE_MS + 1_000
    stale = {row["requested_symbol"]: row for row in repos.price.quotes_for_symbols([QuoteRequest("BTC")], now_ms=aged)}
    assert stale["BTC"]["state"] == "stale" and stale["BTC"]["price"] == "68000"  # stale keeps its number


def test_quote_freshness_preserves_future_timestamps_and_expires_only_the_reference_change(conn) -> None:
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
                    source_at_ms=NOW + 5_001,
                    reference_at_ms=NOW - 600_001,
                )
            ],
            target_count=1,
            source_at_ms=NOW + 5_001,
            received_at_ms=NOW,
            now_ms=NOW,
        )
        repos.price.replace_source_snapshot(
            source_key="hl.perp",
            quotes=[
                Quote(
                    venue="hl.perp",
                    venue_symbol="HYPE",
                    base_symbol="HYPE",
                    price=Decimal("40"),
                    price_kind="mid",
                    source_at_ms=None,
                )
            ],
            target_count=1,
            source_at_ms=None,
            received_at_ms=NOW - 45_000,
            now_ms=NOW,
        )

    rows = {
        row["requested_symbol"]: row
        for row in repos.price.quotes_for_symbols([QuoteRequest("BTC"), QuoteRequest("HYPE")], now_ms=NOW)
    }
    assert rows["BTC"]["state"] == "stale"
    assert rows["BTC"]["source_at_ms"] == NOW + 5_001
    assert rows["BTC"]["source_age_ms"] == 0
    assert rows["BTC"]["change_pct"] is None
    assert rows["BTC"]["reference_at_ms"] == NOW - 600_001
    assert rows["BTC"]["reference_age_ms"] == 600_001
    assert rows["HYPE"]["state"] == "fresh"
    assert rows["HYPE"]["freshness_basis"] == "received_only"
    assert rows["HYPE"]["source_age_ms"] is None


def test_quote_api_status_and_delivery_render_share_one_snapshot_freshness(conn) -> None:
    """#304: one durable snapshot has one state; readers do not reimplement receipt-only freshness."""

    _universe(conn, _instrument("binance.perp", "BTCUSDT", "BTC"))
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
                    reference_at_ms=NOW - 600_001,
                )
            ],
            target_count=1,
            source_at_ms=NOW,
            received_at_ms=NOW,
            now_ms=NOW,
        )

    current = repos.price.quotes_for_symbols([QuoteRequest("BTC")], now_ms=NOW)[0]
    current_status = repos.price.price_status(now_ms=NOW)["sources"][0]
    assert current["state"] == current_status["state"] == "fresh"
    assert current["effective_age_ms"] == current_status["effective_age_ms"] == 0
    assert current["change_pct"] is None
    assert quote_line(reader_quotes([current])).startswith("行情 BTC $68,000")
    assert "24h" not in quote_line(reader_quotes([current]))

    stale = repos.price.quotes_for_symbols([QuoteRequest("BTC")], now_ms=NOW + 45_001)[0]
    stale_status = repos.price.price_status(now_ms=NOW + 45_001)["sources"][0]
    assert stale["state"] == stale_status["state"] == "stale"
    assert stale["effective_age_ms"] == stale_status["effective_age_ms"] == 45_001
    assert quote_line(reader_quotes([stale])) == ""


def test_duplicate_request_symbols_cannot_multiply_repository_work(conn) -> None:
    _universe(conn, _instrument("binance.perp", "BTCUSDT", "BTC"))
    repos = repositories_for_connection(conn)
    results = repos.price.quotes_for_symbols(
        [QuoteRequest("BTC"), QuoteRequest("btc"), QuoteRequest("BTC")], now_ms=NOW
    )
    assert [row["requested_symbol"] for row in results] == ["BTC", "btc"]


# ---------------------------------------------------------------------------- due work


# ---------------------------------------------------------------------------- review


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
    assert "oldest_due_age_ms" not in status
    assert status["sources"][0]["source_key"] == "hl.perp"
    assert status["sources"][0]["state"] == "fresh"
    assert status["sources"][0]["freshness_basis"] == "received_only"
    assert status["sources"][0]["received_age_ms"] == 1_000
    assert status["sources"][0]["source_age_ms"] is None
    assert status["sources"][0]["effective_age_ms"] == 1_000
    assert "age_ms" not in status["sources"][0]
    assert status["quotes"] == 1
    assert "metric_version" not in status


def test_price_status_aggregates_the_oldest_and_worst_applicable_quote(conn) -> None:
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
                    source_at_ms=NOW - 1_000,
                ),
                Quote(
                    venue="binance.perp",
                    venue_symbol="ETHUSDT",
                    base_symbol="ETH",
                    price=Decimal("4000"),
                    price_kind="last",
                    source_at_ms=NOW - 45_001,
                ),
            ],
            target_count=2,
            source_at_ms=NOW - 1_000,
            received_at_ms=NOW,
            now_ms=NOW,
        )

    status = repos.price.price_status(now_ms=NOW)
    source = status["sources"][0]
    assert source["state"] == "stale"
    assert source["source_at_ms"] == NOW - 45_001
    assert source["source_age_ms"] == source["effective_age_ms"] == 45_001
    assert source["received_age_ms"] == 0
    assert source["freshness_basis"] == "source_and_received"
    assert status["fresh_sources"] == 0
