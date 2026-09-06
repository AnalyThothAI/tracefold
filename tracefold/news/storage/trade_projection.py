"""News-owned point-in-time projection read by App composition for Trading.

The row contracts below are the published shape of that projection. They are `TypedDict`s
because the rows *are* the SELECT lists — naming the columns is the whole contract, and a runtime
model here would coerce values PostgreSQL already typed and turn a nullable LEFT JOIN column into a
different value. Nothing outside this module may add, rename or retype a key without editing them,
which is what makes the App-side mapper break at type-check time instead of at 03:00 in a runner.
A version string sat above them for thirteen revisions, restating in prose what the `TypedDict`s
already state in types, and it was never compared with anything in production (#537 PR-4).

They say nothing about *trade* eligibility: freshness, the venue rule and the liquidity floor live in
the Signal lane's own pure rules. This side owns the point-in-time boundaries and the deterministic
order, and nothing about the editorial pipeline that runs beside the OI ledger (#510).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TypedDict

# A fact this ledger reconstructed rather than received is excluded from every read below (#553).
# `historical` rows carry their original provider and host stamps but became durable at the rebuild
# moment, so treating one as a trigger would author a Case from a measurement no scan could have seen
# at the time -- and replaying a frozen window would then disagree with what the live lane did. They
# stay fully readable through the market surface, which is where a reader asks about them.
#
# One read's ceiling per lane. The consumer's widest configured horizon is `max_age + max(lookback)` —
# 65 minutes at the shipped configuration — and the measured live rate through these exact predicates
# is about eleven rows an hour on the News lane and nothing like a full lane on the OI one, so this is
# roughly twenty times the volume it has to carry. It is still a ceiling, not a promise: a lane that
# comes back with exactly this many rows was truncated, and the funnel's `oi_rows` / `news_rows`
# counters are where that shows.
TRADE_PROJECTION_ROW_LIMIT = 256


class OiTradeProjectionRow(TypedDict):
    """One row of the deterministic OI ledger, with the ingest mode of the Item it was parsed from.

    Sixteen keys and no judgment: `news_oi_signals` is the fact, and whether the fact may reach capital
    is the Signal lane's own question. `metric_version` is half the row's primary key, so the pair
    `(event_id, metric_version)` is the durable source identity a consumer files its answer under.
    """

    event_id: str
    metric_version: str
    source_item_id: str
    symbol: str
    direction: str
    oi_change_bps: int
    oi_value_usd: int
    whale_long_profit_bps: int
    whale_oi_ratio_bps: int
    observed_at_ms: int
    # When the ledger row became durable, which is the earliest instant any consumer could have read it.
    available_at_ms: int
    ingest_mode: str
    # What the provider proves about the measurement, not about the market (#265). Nullable together:
    # a `NULL` window means unproven, and it is the answer a consumer must act on rather than default.
    # `whale_long_profit_bps` is the provider's own `Whale Long Profit N%` and nothing more — not an
    # account count, not a total unrealised PnL, and not "every smart-money account is in profit".
    source_strategy_id: str | None
    source_contract_version: str | None
    measurement_window_ms: int | None
    # The provider's own venue text, as the ledger froze it. Nullable: a frame whose provider metadata
    # carried no source is still a fact, and the consumer refuses it by name rather than guessing.
    venue: str | None


class TradeInstrumentProjectionRow(TypedDict):
    """One exactly-listed native crypto perpetual for one underlying.

    ``observed_at_ms`` is when the catalogue last wrote this row, which is not "the last refresh that saw this
    contract": since #570 A11 an unchanged catalogue writes no row, so a refresh that changes nothing moves
    nothing here. For every row written from `20260905_0367` onwards that is the observation time of the listing
    event written beside it, the same fact the replay branch below reads from the event ledger. Rows that predate
    the revision keep the stamp their last full refresh left and are not backfilled — a real observation of the
    contract, never later than its identity's, and replaced by a true one the next time the contract changes."""

    venue: str
    venue_symbol: str
    base_symbol: str
    instrument_class: str
    quote_asset: str | None
    status: str
    observed_at_ms: int


class TradeFixedWindowOiSourceRow(TypedDict):
    """Complete News-owned OI source identity for one fixed acceptance window."""

    event_id: str
    metric_version: str
    source_venue: str | None
    observed_at_ms: int
    available_at_ms: int
    created_at_ms: int


class TradeProjectionStorage:
    conn: Any

    def trade_candidate_oi_rows(
        self,
        *,
        metric_version: str,
        after_created_at_ms: int,
        until_created_at_ms: int,
        limit: int = TRADE_PROJECTION_ROW_LIMIT,
    ) -> list[OiTradeProjectionRow]:
        """Every OI fact this metric version persisted in the window, and the ingest mode behind it.

        The read is one ledger and one Item column (#510). It used to reach the same numbers through the
        editorial pipeline: the triage verdict for the frame, six `jsonb` equalities re-proving that the
        verdict's copy of the measurements still matched the ledger's, the currently active learning
        arm's epoch, four News version literals, and the leader Item's title split on whitespace for the
        provider token. Every one of those was upstream of the numbers rather than part of them, so a
        News policy bump — `news_triage_policy_v11` to `v12` in #504 — silently stopped Trading's
        projection and forced an edit to Trading's own contract. `news_oi_signals` is where the parser
        writes the fact; this reads the fact.

        What this read proves is only that the fact exists and where it came from. It decides nothing
        about eligibility: `ingest_mode`, the liquidity floor, freshness and the venue rule are all the
        Signal lane's, with a named durable answer each, which is what keeps `oi_rows = 0` answerable.

        The window is on `created_at_ms` — when the ledger row became durable — because that, not the
        provider's observation clock, is when a scan could first have seen it. Newest first at the limit
        (#211): every row a consumer can act on *now* is at the recent end of a window wide enough to
        hold an hour of superseded context, so truncating the far end costs context and truncating the
        near end would cost the triggers.
        """

        rows = self.conn.execute(
            """
            SELECT s.event_id,
                   s.metric_version,
                   s.source_item_id,
                   s.symbol,
                   s.direction,
                   s.oi_change_bps,
                   s.oi_value_usd,
                   s.whale_long_profit_bps,
                   s.whale_oi_ratio_bps,
                   s.observed_at_ms,
                   s.available_at_ms,
                   i.first_ingest_mode AS ingest_mode,
                   s.source_strategy_id,
                   s.source_contract_version,
                   s.measurement_window_ms,
                   s.source_venue AS venue
              FROM news_oi_signals s
              JOIN news_items i ON i.item_id = s.source_item_id
             WHERE s.metric_version = %s
               AND NOT s.historical
               AND s.created_at_ms > %s
               AND s.created_at_ms <= %s
             ORDER BY s.created_at_ms DESC, s.event_id DESC
             LIMIT %s
            """,
            (
                metric_version,
                int(after_created_at_ms),
                int(until_created_at_ms),
                int(limit),
            ),
        ).fetchall()
        return [_oi_projection_row(row) for row in rows]

    def trade_evidence_oi_rows(
        self,
        *,
        metric_version: str,
        start_observed_at_ms: int,
        end_observed_at_ms: int,
        known_at_or_before_ms: int,
        available_at_or_before_ms: int,
        limit: int = TRADE_PROJECTION_ROW_LIMIT,
    ) -> list[OiTradeProjectionRow]:
        """Freeze sources observed in the batch window and durable by its capture clock (#377).

        The same ledger the live read takes (#510). The two used to differ only in their window and in
        nothing else that mattered, yet both carried the verdict join, so a News identity move broke
        replay and the live lane in one step.
        """

        rows = self.conn.execute(
            """
            SELECT s.event_id,
                   s.metric_version,
                   s.source_item_id,
                   s.symbol,
                   s.direction,
                   s.oi_change_bps,
                   s.oi_value_usd,
                   s.whale_long_profit_bps,
                   s.whale_oi_ratio_bps,
                   s.observed_at_ms,
                   s.available_at_ms,
                   i.first_ingest_mode AS ingest_mode,
                   s.source_strategy_id,
                   s.source_contract_version,
                   s.measurement_window_ms,
                   s.source_venue AS venue
              FROM news_oi_signals s
              JOIN news_items i ON i.item_id = s.source_item_id
             WHERE s.metric_version = %s
               AND NOT s.historical
               AND s.available_at_ms <= %s
               AND s.observed_at_ms >= %s
               AND s.observed_at_ms < %s
               AND s.created_at_ms <= %s
             ORDER BY s.observed_at_ms, s.event_id
             LIMIT %s
            """,
            (
                metric_version,
                int(available_at_or_before_ms),
                int(start_observed_at_ms),
                int(end_observed_at_ms),
                int(known_at_or_before_ms),
                int(limit),
            ),
        ).fetchall()
        return [_oi_projection_row(row) for row in rows]

    def trade_fixed_window_oi_sources(
        self,
        *,
        metric_version: str,
        start_observed_at_ms: int,
        end_observed_at_ms: int,
        drain_cutoff_ms: int,
        limit: int,
    ) -> list[TradeFixedWindowOiSourceRow]:
        """Complete, bounded News source universe known by the preregistered drain cutoff."""

        rows = self.conn.execute(
            """
            SELECT s.event_id, s.metric_version, s.source_venue,
                   s.observed_at_ms, s.available_at_ms, s.created_at_ms
              FROM news_oi_signals s
             WHERE s.metric_version = %s
               AND NOT s.historical
               AND s.observed_at_ms >= %s AND s.observed_at_ms < %s
               AND s.available_at_ms <= %s AND s.created_at_ms <= %s
             ORDER BY s.observed_at_ms, s.event_id
             LIMIT %s
            """,
            (
                metric_version,
                int(start_observed_at_ms),
                int(end_observed_at_ms),
                int(drain_cutoff_ms),
                int(drain_cutoff_ms),
                int(limit),
            ),
        ).fetchall()
        return [
            TradeFixedWindowOiSourceRow(
                event_id=str(row["event_id"]),
                metric_version=str(row["metric_version"]),
                source_venue=None if row["source_venue"] is None else str(row["source_venue"]),
                observed_at_ms=int(row["observed_at_ms"]),
                available_at_ms=int(row["available_at_ms"]),
                created_at_ms=int(row["created_at_ms"]),
            )
            for row in rows
        ]

    def trade_candidate_instrument(
        self,
        *,
        base_symbol: str,
        venues: Sequence[str],
        observed_at_ms: int | None = None,
    ) -> list[TradeInstrumentProjectionRow]:
        """Exactly-listed native crypto perpetuals for one underlying, in the caller's venue order.

        `instrument_class = 'crypto'` is not decoration: Binance labels its 169 TradFi perps `EQUITY`
        and friends, so a `WMT` Event whose Gate class says crypto still resolves to nothing here.
        HIP-3 builder venues (`hl.xyz`) are excluded by naming the two native perp venues explicitly.

        Live callers omit ``observed_at_ms`` and see only the current catalogue. Replay callers read the
        last immutable listing event at or before the source cutoff, so neither a later listing/relisting
        nor a present-day delisting can alter the instrument identity that source fact observed.
        """

        if not venues:
            return []
        normalized_base = str(base_symbol or "").strip().upper()
        if observed_at_ms is None:
            rows = self.conn.execute(
                """
                SELECT venue, venue_symbol, base_symbol, instrument_class,
                       quote_asset, status, observed_at_ms
                  FROM news_market_instruments
                 WHERE base_symbol = %s
                   AND venue = ANY(%s)
                   AND status = 'trading'
                   AND instrument_class = 'crypto'
                 ORDER BY venue,
                          CASE quote_asset WHEN 'USDT' THEN 0 WHEN 'USDC' THEN 1 ELSE 2 END,
                          length(venue_symbol),
                          venue_symbol
                """,
                (normalized_base, list(venues)),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                WITH candidate_symbols AS (
                  SELECT DISTINCT venue, venue_symbol
                    FROM news_market_instrument_listing_events
                   WHERE venue = ANY(%s)
                     AND base_symbol = %s
                     AND observed_at_ms <= %s
                ), historical AS (
                  SELECT DISTINCT ON (event.venue, event.venue_symbol)
                         event.venue, event.venue_symbol, event.observed_at_ms,
                         event.base_symbol, event.instrument_class, event.quote_asset, event.status
                    FROM news_market_instrument_listing_events AS event
                    JOIN candidate_symbols AS candidate
                      ON candidate.venue = event.venue
                     AND candidate.venue_symbol = event.venue_symbol
                   WHERE event.observed_at_ms <= %s
                   ORDER BY event.venue, event.venue_symbol, event.observed_at_ms DESC
                )
                SELECT venue, venue_symbol, base_symbol, instrument_class,
                       quote_asset, status, observed_at_ms
                  FROM historical
                 WHERE base_symbol = %s
                   AND status = 'trading'
                   AND instrument_class = 'crypto'
                 ORDER BY venue,
                          CASE quote_asset WHEN 'USDT' THEN 0 WHEN 'USDC' THEN 1 ELSE 2 END,
                          length(venue_symbol),
                          venue_symbol
                """,
                (
                    list(venues),
                    normalized_base,
                    int(observed_at_ms),
                    int(observed_at_ms),
                    normalized_base,
                ),
            ).fetchall()
        return [
            TradeInstrumentProjectionRow(
                venue=row["venue"],
                venue_symbol=row["venue_symbol"],
                base_symbol=row["base_symbol"],
                instrument_class=row["instrument_class"],
                quote_asset=row["quote_asset"],
                status=row["status"],
                observed_at_ms=row["observed_at_ms"],
            )
            for row in rows
        ]


def _oi_projection_row(row: Any) -> OiTradeProjectionRow:
    """Name every selected column. No coercion: psycopg already returns the column's own type."""

    return OiTradeProjectionRow(
        event_id=row["event_id"],
        metric_version=row["metric_version"],
        source_item_id=row["source_item_id"],
        symbol=row["symbol"],
        direction=row["direction"],
        oi_change_bps=row["oi_change_bps"],
        oi_value_usd=row["oi_value_usd"],
        whale_long_profit_bps=row["whale_long_profit_bps"],
        whale_oi_ratio_bps=row["whale_oi_ratio_bps"],
        observed_at_ms=row["observed_at_ms"],
        available_at_ms=row["available_at_ms"],
        ingest_mode=row["ingest_mode"],
        source_strategy_id=row["source_strategy_id"],
        source_contract_version=row["source_contract_version"],
        measurement_window_ms=row["measurement_window_ms"],
        venue=row["venue"],
    )
