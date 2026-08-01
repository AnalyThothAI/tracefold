from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal

from tests.factories import make_event
from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.provider_types import AssetMarketProviders
from tracefold.app.repositories import repositories_for_connection
from tracefold.app.workers import _PooledIngestStore
from tracefold.market import (
    DexTokenQuote,
    MarketTick,
    MarketTickPersistenceService,
    market_tick_id,
)
from tracefold.market.pricing.event_anchor_backfill_worker import EventAnchorBackfill


def test_ingest_chain_event_writes_pending_backfill_without_inline_provider_call(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    migrate(conn)
    event = make_event(
        "event-inline-market",
        text="https://gmgn.ai/eth/token/0x6982508145454ce325ddbe47a25d4ec3d2311933 is moving",
        received_at_ms=1_700_000_000_000,
    )
    provider = _DexQuoteProvider(
        [
            DexTokenQuote(
                chain_id="eip155:1",
                address="0x6982508145454ce325ddbe47a25d4ec3d2311933",
                observed_at_ms=event.received_at_ms + 250,
                price_usd=0.0123,
                liquidity_usd=123_000.0,
                market_cap_usd=1_000_000.0,
                volume_24h_usd=456_000.0,
                raw={"source": "fake"},
            )
        ]
    )
    store = _PooledIngestStore(
        _SingleConnectionDB(conn),
        providers=AssetMarketProviders(dex_quote_market=provider),
        event_anchor_active_window_ms=300_000,
    )
    try:
        result = store.ingest_event(event)
        event_row = conn.execute("SELECT * FROM events WHERE event_id = %s", (event.event_id,)).fetchone()
        enriched_rows = conn.execute("SELECT * FROM enriched_events WHERE event_id = %s", (event.event_id,)).fetchall()
        market_rows = conn.execute("SELECT * FROM market_ticks").fetchall()
        identity_rows = conn.execute(
            """
            SELECT *
            FROM asset_identity_evidence
            WHERE source_event_id = %s
              AND evidence_kind = 'tweet_contract_mention'
            """,
            (event.event_id,),
        ).fetchall()
    finally:
        conn.close()

    assert result.inserted is True
    assert event_row is not None
    assert provider.calls == []
    assert len(market_rows) == 0
    assert len(enriched_rows) == 1
    assert enriched_rows[0]["tick_id"] is None
    assert enriched_rows[0]["capture_method"] == "unavailable"
    assert enriched_rows[0]["capture_reason"] == "pending_backfill"
    assert enriched_rows[0]["target_type"] == "chain_token"
    assert enriched_rows[0]["target_id"] == "eip155:1:0x6982508145454ce325ddbe47a25d4ec3d2311933"
    assert len(identity_rows) == 1


def test_ingest_chain_event_with_provider_no_quote_still_writes_pending_backfill(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    migrate(conn)
    event = make_event(
        "event-unavailable-market",
        text="https://gmgn.ai/eth/token/0x6982508145454ce325ddbe47a25d4ec3d2311933 has no quote",
        received_at_ms=1_700_000_010_000,
    )
    provider = _DexQuoteProvider(
        [
            DexTokenQuote(
                chain_id="eip155:1",
                address="0x6982508145454ce325ddbe47a25d4ec3d2311933",
                observed_at_ms=event.received_at_ms,
                price_usd=None,
                raw={"source": "fake-null"},
            )
        ]
    )
    store = _PooledIngestStore(
        _SingleConnectionDB(conn),
        providers=AssetMarketProviders(dex_quote_market=provider),
        event_anchor_active_window_ms=300_000,
    )
    try:
        result = store.ingest_event(event)
        enriched_rows = conn.execute("SELECT * FROM enriched_events WHERE event_id = %s", (event.event_id,)).fetchall()
        market_rows = conn.execute("SELECT * FROM market_ticks").fetchall()
    finally:
        conn.close()

    assert result.inserted is True
    assert provider.calls == []
    assert len(market_rows) == 0
    assert len(enriched_rows) == 1
    assert enriched_rows[0]["capture_method"] == "unavailable"
    assert enriched_rows[0]["capture_reason"] == "pending_backfill"
    assert enriched_rows[0]["tick_id"] is None


def test_exhausted_event_anchor_uses_canonical_terminal_reason(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    migrate(conn)
    event = make_event(
        "event-exhausted-backfill",
        text="https://gmgn.ai/eth/token/0x6982508145454ce325ddbe47a25d4ec3d2311933 exhausted",
        received_at_ms=1_700_000_015_000,
    )
    store = _PooledIngestStore(
        _SingleConnectionDB(conn),
        providers=AssetMarketProviders(dex_quote_market=_DexQuoteProvider([])),
        event_anchor_active_window_ms=300_000,
    )
    try:
        store.ingest_event(event)
        conn.execute(
            """
            UPDATE event_anchor_backfill_jobs
               SET status = 'running', lease_owner = 'expired-worker',
                   leased_until_ms = %s, attempt_count = 3
             WHERE event_id = %s
            """,
            (event.received_at_ms + 500, event.event_id),
        )
        conn.commit()
        worker = EventAnchorBackfill(
            db=_SingleConnectionDB(conn),
            capture_service=object(),
            finite_operations=object(),
            runtime_id="test",
        )

        result = worker._expire_stale_jobs(now_ms=event.received_at_ms + 1_000)
        job = conn.execute(
            """
            SELECT status, last_reason FROM event_anchor_backfill_jobs
             WHERE event_id = %s
            """,
            (event.event_id,),
        ).fetchone()
        enriched = conn.execute(
            """
            SELECT capture_method, capture_reason FROM enriched_events
             WHERE event_id = %s
            """,
            (event.event_id,),
        ).fetchone()
    finally:
        conn.close()

    assert result == {"expired": 0, "failed": 1, "rescheduled": 0}
    assert job == {"status": "failed", "last_reason": "backfill_expired"}
    assert enriched == {"capture_method": "unavailable", "capture_reason": "backfill_expired"}


def test_ingest_chain_event_with_existing_tick_writes_composite_tick_capture(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    migrate(conn)
    event = make_event(
        "event-existing-market",
        text="https://gmgn.ai/eth/token/0x6982508145454ce325ddbe47a25d4ec3d2311933 has a fresh quote",
        received_at_ms=1_700_000_020_000,
    )
    target_id = "eip155:1:0x6982508145454ce325ddbe47a25d4ec3d2311933"
    observed_at_ms = event.received_at_ms - 250
    tick = MarketTick(
        tick_id=market_tick_id(
            target_type="chain_token",
            target_id=target_id,
            source_provider="gmgn_dex_quote",
            observed_at_ms=observed_at_ms,
        ),
        target_type="chain_token",
        target_id=target_id,
        chain="eip155:1",
        token_address="0x6982508145454ce325ddbe47a25d4ec3d2311933",
        exchange=None,
        instrument=None,
        pricefeed_id=None,
        source_tier="tier3_inline",
        source_provider="gmgn_dex_quote",
        observed_at_ms=observed_at_ms,
        received_at_ms=observed_at_ms,
        price_usd=Decimal("0.0123"),
        liquidity_usd=Decimal("123000"),
        volume_24h_usd=Decimal("456000"),
        open_interest_usd=None,
        market_cap_usd=Decimal("1000000"),
        holders=None,
        created_at_ms=observed_at_ms,
        raw_payload_json={"source": "test"},
    )
    repos = repositories_for_connection(
        conn,
    )
    with repos.transaction():
        MarketTickPersistenceService(repos).persist_ticks([tick], now_ms=observed_at_ms)
    store = _PooledIngestStore(
        _SingleConnectionDB(conn),
        providers=AssetMarketProviders(dex_quote_market=_DexQuoteProvider([])),
        event_anchor_active_window_ms=300_000,
    )
    try:
        result = store.ingest_event(event)
        enriched_rows = conn.execute("SELECT * FROM enriched_events WHERE event_id = %s", (event.event_id,)).fetchall()
        market_rows = conn.execute(
            "SELECT * FROM market_ticks WHERE target_type = %s AND target_id = %s",
            ("chain_token", target_id),
        ).fetchall()
    finally:
        conn.close()

    assert result.inserted is True
    assert len(enriched_rows) == 1
    assert enriched_rows[0]["tick_observed_at_ms"] == observed_at_ms
    assert enriched_rows[0]["tick_id"] == tick.tick_id
    assert enriched_rows[0]["capture_method"] == "tier3_inline"
    assert enriched_rows[0]["tick_lag_ms"] == 250
    assert [row["tick_id"] for row in market_rows] == [tick.tick_id]


class _DexQuoteProvider:
    def __init__(self, quotes: list[DexTokenQuote]) -> None:
        self.calls = []
        self._quotes = quotes

    def token_quotes(self, tokens):
        self.calls.append(tokens)
        return self._quotes


class _SingleConnectionDB:
    def __init__(self, conn) -> None:
        self.conn = conn

    @contextmanager
    def worker_session(self, *_args, **_kwargs) -> Iterator:
        yield repositories_for_connection(
            self.conn,
        )
