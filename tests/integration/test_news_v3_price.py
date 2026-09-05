"""Price Review plane against real PostgreSQL (#88): resolution, quotes, due work, review aggregates.

These are the assertions that only a real database can make: idempotent keys, the due scan's terminality,
retention cascade, and the shape of the bounded review aggregates over the actual JSONB the pipeline writes.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_judgment import scored_judgment
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.market_review.instruments import Instrument
from tracefold.news.market_review.pricing import (
    HORIZON_MS,
    QUOTE_FRESH_MAX_AGE_MS,
    REACTION_METRIC_VERSION,
    Quote,
)
from tracefold.news.models import TriageVerdict
from tracefold.news.program.runtime import PROGRAM_VERSION as SEMANTIC_PROGRAM_VERSION
from tracefold.news.reader_card import quote_line, reader_quotes
from tracefold.news.triage_rules import DecisionResult, DegradedJudgment

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
        "news_event_reactions",
        "news_quote_snapshots",
        "news_oi_signals",
        "news_event_assets",
        "news_verdicts",
        "news_deliveries",
        "news_events",
        "news_items",
        "news_market_instrument_listing_events",
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
    repos = repositories_for_connection(conn)
    evidence = repos.news.append_evidence_snapshot(event_id=event_id, now_ms=opened_at_ms)
    verdict = TriageVerdict(
        novelty="new_fact",
        assets=[{"symbol": symbol, "role": "primary"} for symbol in symbols],
        direction=direction,
        scope="single_name",
        magnitude=magnitude,
        confidence=1.0,
        headline_zh="价格复盘测试",
    )
    decision_result = DecisionResult(
        final=decision,
        override_rule="recorded_fixture",
        throttled_by=None,
        rule_baseline=decision,
    )
    if degraded:
        judgment = DegradedJudgment(
            verdict=verdict,
            decision=decision_result,
            error_code="news_semantic_program_unconfigured",
        )
        origin = "degraded"
        judgment_sha256 = judgment.judgment_sha256
        model_editorial = None
        model = None
        error_code = judgment.error_code
        trace_extra = {"judgment": judgment.judgment_atom}
    else:
        judgment = scored_judgment(verdict)
        origin = "model"
        judgment_sha256 = judgment.scored_judgment_sha256
        model_editorial = judgment.editorial.model_dump(mode="json")
        model = "test"
        error_code = None
        trace_extra = {"editorial_sha256": judgment.editorial.editorial_sha256}
    runtime_manifest_sha = "b" * 64
    program_sha256 = "a" * 64
    trace = {
        **trace_extra,
        "judgment_contract_version": judgment.judgment_contract_version,
        "judgment_origin": origin,
        "judgment_sha256": judgment_sha256,
        "verdict_sha256": canonical_sha(verdict.model_dump(mode="json")),
        "runtime_manifest_sha": runtime_manifest_sha,
        "evidence_version": int(evidence["evidence_version"]),
        "evidence_sha256": str(evidence["evidence_sha256"]),
        "focus_fact_id": str(evidence["focus_fact_id"]),
        "program_version": SEMANTIC_PROGRAM_VERSION,
        "program_sha256": program_sha256,
        "told": [],
        "told_count": 0,
    }
    repos.news.insert_verdict(
        event_id=event_id,
        stage="triage",
        policy_version="news_triage_policy_v13",
        judgment_contract_version=judgment.judgment_contract_version,
        judgment_origin=origin,
        rule_baseline_decision=decision_result.rule_baseline,
        final_decision=decision_result.final,
        override_rule=decision_result.override_rule,
        throttled_by=decision_result.throttled_by,
        verdict=verdict.model_dump(mode="json"),
        model_editorial=model_editorial,
        judgment_sha256=judgment_sha256,
        runtime_manifest_sha=runtime_manifest_sha,
        model=model,
        program_version=SEMANTIC_PROGRAM_VERSION,
        program_sha256=program_sha256,
        degraded=degraded,
        error_code=error_code,
        trace=trace,
        evidence_version=int(evidence["evidence_version"]),
        evidence_sha256=str(evidence["evidence_sha256"]),
        focus_fact_id=str(evidence["focus_fact_id"]),
        now_ms=opened_at_ms,
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

    # Storyline identity collapses SKHX into SKHY; pricing keeps the contract the Event actually named.
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

    resolved = repositories_for_connection(conn).price.instruments_for_symbols(["MSFT", "SKHX"])

    assert [(row.venue, row.venue_symbol) for row in resolved["MSFT"]] == [
        ("binance.perp", "MSFTUSDT"),
        ("hl.xyz", "xyz:MSFT"),
        ("okx.perp", "MSFT-USDT-SWAP"),
    ]
    assert [row.venue_symbol for row in resolved["SKHX"]] == ["xyz:SKHX"]


def test_quote_working_set_includes_recent_oi_ledger_symbols(conn) -> None:
    _universe(conn, _instrument("binance.perp", "DOGEUSDT", "DOGE"))
    # The ordinary grounded lane also names DOGE; the UNION must still return one symbol and obey the
    # caller's existing bound rather than multiplying provider work.
    _event(conn, "ev-news-doge", symbols=("DOGE",), opened_at_ms=NOW - 1, delivered=False)
    # The OI arm reaches the ledger through the Item that produced it (#553). There is no Event: a
    # market observation opens none, and the working set was reading one only because the foreign key
    # forced it to.
    conn.execute(
        """
        INSERT INTO news_items (
          item_id, source_id, source_item_key, title, raw_first_line, description, reporting_origin,
          published_at_ms, observed_at_ms, provider_metadata, provenance, first_ingest_mode, trace_id,
          created_at_ms, updated_at_ms, market_kind, market_source_strategy_id, market_parse_status,
          market_notify_state
        ) VALUES (
          'i-ev-oi', 'opennews', 'i-ev-oi', 'DOGE OI Rise', '', '', 'opennews', %s, %s,
          '{}'::jsonb, '[]'::jsonb, 'live', 'trace', %s, %s, 'oi', '1019', 'parsed',
          -- A live market Item is a to-do for the notification loop, and the CHECK that says so
          -- refuses a NULL marker outright (#553 PR-2): a writer that does not know the column is an
          -- old writer, and the loop would never see its observation.
          'pending'
        )
        """,
        (NOW, NOW, NOW, NOW),
    )
    conn.execute(
        """
        INSERT INTO news_oi_signals (
          event_id, metric_version, symbol, raw_instrument, direction, oi_change_bps, oi_value_usd,
          whale_long_profit_bps, whale_oi_ratio_bps, observed_at_ms, received_at_ms, created_at_ms,
          provider, measurement_definition, source_item_id, source_venue, available_at_ms, historical
        ) VALUES (
          'ev-oi', 'oi_signal_v1', 'DOGE', 'DOGE', 'rise', 864, 73010000, 8060, 21097, %s, %s, %s,
          'opennews', 'oi_signal_v1|unproven|unproven', 'i-ev-oi', 'binance', %s, false
        )
        """,
        (NOW, NOW, NOW, NOW),
    )
    conn.commit()

    repos = repositories_for_connection(conn)
    symbols = repos.price.quote_target_symbols(since_ms=NOW - HOUR)

    assert symbols == ["DOGE"]
    assert repos.price.quote_target_symbols(since_ms=NOW - HOUR, limit=1) == ["DOGE"]


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
        for row in repos.price.quotes_for_symbols(["BTC", "HYPE", "NOPE"], now_ms=NOW + 1_000)
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
    stale = {row["requested_symbol"]: row for row in repos.price.quotes_for_symbols(["BTC"], now_ms=aged)}
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

    rows = {row["requested_symbol"]: row for row in repos.price.quotes_for_symbols(["BTC", "HYPE"], now_ms=NOW)}
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

    current = repos.price.quotes_for_symbols(["BTC"], now_ms=NOW)[0]
    current_status = repos.price.price_status(now_ms=NOW)["sources"][0]
    assert current["state"] == current_status["state"] == "fresh"
    assert current["effective_age_ms"] == current_status["effective_age_ms"] == 0
    assert current["change_pct"] is None
    assert quote_line(reader_quotes([current])).startswith("行情 BTC $68,000")
    assert "24h" not in quote_line(reader_quotes([current]))

    stale = repos.price.quotes_for_symbols(["BTC"], now_ms=NOW + 45_001)[0]
    stale_status = repos.price.price_status(now_ms=NOW + 45_001)["sources"][0]
    assert stale["state"] == stale_status["state"] == "stale"
    assert stale["effective_age_ms"] == stale_status["effective_age_ms"] == 45_001
    assert quote_line(reader_quotes([stale])) == ""


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


def test_the_price_plane_plans_a_reaction_from_the_events_own_grounded_asset(conn) -> None:
    """`due_reactions` walks Event-assets, not verdicts, and the Gate's grounding is what writes them.

    #267 added a second writer so a deterministic market judge could attach the primary its Event's
    Gate could not ground. That judge, and the Events it judged, are gone (#553): a market observation
    opens no Event, so it reaches no reaction horizon and there is nothing left to write back. What
    remains is the one path that was always true for editorial Events.
    """

    _universe(conn, _instrument("binance.perp", "TRUMPUSDT", "TRUMP"))
    _event(conn, "grounded-frame", symbols=("TRUMP",), opened_at_ms=NOW - 2 * HOUR)
    repos = repositories_for_connection(conn)

    due = repos.price.due_reactions(now_ms=NOW, limit=100)

    assert [(row["event_id"], row["symbol"]) for row in due] == [("grounded-frame", "TRUMP")]
    # The anchor is the Event's own, so the 1 H and 4 H horizons are measured from the frame.
    assert due[0]["anchor_at_ms"] == NOW - 2 * HOUR
    assert due[0]["is_primary"] is True
    assert not hasattr(repos.news, "record_event_assets")


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
    assert rows[0]["is_primary"] is False


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


def test_review_reports_coverage_and_potential_misses(conn) -> None:
    _universe(conn, _instrument("binance.perp", "BTCUSDT", "BTC"), _instrument("binance.perp", "ETHUSDT", "ETH"))
    anchor = NOW - 6 * HOUR
    _event(conn, "hit", symbols=("BTC",), opened_at_ms=anchor, direction="bullish")
    _event(
        conn, "miss", symbols=("ETH",), opened_at_ms=anchor, direction="bearish", decision="throttled", delivered=False
    )
    _event(conn, "nocover", symbols=("BTC",), opened_at_ms=anchor, direction="bullish")
    _event(conn, "not-mature", symbols=("BTC",), opened_at_ms=NOW - 60_000, direction="bullish")
    _complete(conn, "hit", "BTC", anchor=anchor, bps_1h=150, bps_4h=300)
    _complete(conn, "miss", "ETH", anchor=anchor, bps_1h=900, bps_4h=1200)

    review = repositories_for_connection(conn).price.review(hours=168, now_ms=NOW)

    coverage = {row["horizon"]: row for row in review["coverage"]}
    assert coverage["1h"]["eligible_n"] == 3  # the one-minute-old Event is not yet in the denominator
    assert coverage["1h"]["priced_n"] == 2
    assert coverage["1h"]["coverage_pct"] == pytest.approx(66.7, abs=0.1)

    assert review["directions"] == []
    assert review["summary"]["hit_1h_n"] == 0
    assert review["summary"]["hit_1h_pct"] is None

    misses = review["potential_misses"]
    assert [row["event_id"] for row in misses] == ["miss"]  # only what never reached the reader
    assert misses[0]["final_decision"] == "throttled"
    assert misses[0]["return_1h_bps"] == 900
    assert misses[0]["assets"][0]["venue_symbol"] == "ETHUSDT"

    # #112 retires direction, magnitude, and taxonomy rankings: none is causal quality evidence.
    assert review["magnitudes"] == []
    assert review["event_families"] == []


def test_market_miss_queue_clusters_duplicate_events_into_one_fact(conn) -> None:
    _universe(conn, _instrument("binance.perp", "WMTUSDT", "WMT"))
    anchor = NOW - 6 * HOUR
    for index, event_id in enumerate(("wmt-a", "wmt-b")):
        _event(
            conn,
            event_id,
            symbols=("WMT",),
            opened_at_ms=anchor + index * 60_000,
            decision="drop",
            delivered=False,
        )
        conn.execute(
            "UPDATE news_events SET leader_title = %s WHERE event_id = %s",
            ("沃尔玛下调全年业绩指引，股价盘前下跌", event_id),
        )
        _complete(conn, event_id, "WMT", anchor=anchor + index * 60_000, bps_1h=-692, bps_4h=-500)
    conn.commit()

    misses = repositories_for_connection(conn).price.review(hours=168, now_ms=NOW)["potential_misses"]

    assert len(misses) == 1
    assert misses[0]["fact_cluster_n"] == 2
    assert misses[0]["related_event_ids"] == ["wmt-a", "wmt-b"]
    assert len(misses[0]["fact_cluster_key"]) == 64


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
    assert aggregates["multi"]["p0"] is None  # prices in different units cannot be aggregated
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
    assert aggregate["p0"] is None
    assert aggregate["return_1h_bps"] is None
    assert aggregate["unavailable_reason"] == "no_candle_within_gap"
    review = repos.price.review(hours=168, now_ms=NOW)
    reasons = {row["reason"] for row in review["coverage"][0]["unavailable"]}
    assert "no_candle_within_gap" in reasons


def test_backlog_lateness_is_measured_against_each_row_own_horizon(conn) -> None:
    """A row waiting for 4H is not three hours late just because 4H is three hours after 1H."""

    _universe(conn, _instrument("binance.perp", "BTCUSDT", "BTC"))
    repos = repositories_for_connection(conn)

    # On time: 1H matured a minute ago and nothing has measured it yet.
    _event(conn, "fresh", symbols=("BTC",), opened_at_ms=NOW - HOUR - 60_000)
    assert repos.price.oldest_due_age_ms(now_ms=NOW, history_max_age_ms=30 * 24 * HOUR) == pytest.approx(
        60_000, abs=1_000
    )

    # Also on time: a partial row whose 4H matured a minute ago. Under the old definition this reported
    # three hours and would have sat permanently above the 15-minute warning threshold.
    _event(conn, "partial", symbols=("BTC",), opened_at_ms=NOW - 4 * HOUR - 60_000)
    with repos.transaction():
        repos.price.upsert_reaction(
            {
                "event_id": "partial",
                "symbol": "BTC",
                "anchor_at_ms": NOW - 4 * HOUR - 60_000,
                "venue": "binance.perp",
                "venue_symbol": "BTCUSDT",
                "p0": Decimal("100"),
                "p0_at_ms": NOW - 4 * HOUR,
                "p1": Decimal("101"),
                "p1_at_ms": NOW - 3 * HOUR,
                "return_1h_bps": 100,
                "is_primary": True,
                "state": "partial",
            },
            now_ms=NOW,
        )

    assert repos.price.oldest_due_age_ms(now_ms=NOW, history_max_age_ms=30 * 24 * HOUR) == pytest.approx(
        60_000, abs=1_000
    )

    # Genuinely behind: an unmeasured row whose 1H matured two hours ago.
    _event(conn, "late", symbols=("BTC",), opened_at_ms=NOW - 3 * HOUR)
    assert repos.price.oldest_due_age_ms(now_ms=NOW, history_max_age_ms=30 * 24 * HOUR) == pytest.approx(
        2 * HOUR, abs=1_000
    )


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
    assert "oldest_due_age_ms" in status  # the backlog SLO, reported rather than merely computable
    assert status["sources"][0]["source_key"] == "hl.perp"
    assert status["sources"][0]["state"] == "fresh"
    assert status["sources"][0]["freshness_basis"] == "received_only"
    assert status["sources"][0]["received_age_ms"] == 1_000
    assert status["sources"][0]["source_age_ms"] is None
    assert status["sources"][0]["effective_age_ms"] == 1_000
    assert "age_ms" not in status["sources"][0]
    assert status["quotes"] == 1
    assert status["metric_version"] == REACTION_METRIC_VERSION


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
