"""News-owned point-in-time projection read by App composition for Trading.

The row contracts below are the published shape of that projection. They are `TypedDict`s
because the rows *are* the SELECT lists — naming the columns is the whole contract, and a runtime
model here would coerce values PostgreSQL already typed and turn a nullable LEFT JOIN column into a
different value. Nothing outside this module may add, rename or retype a key without editing them,
which is what makes the App-side mapper break at type-check time instead of at 03:00 in a runner.

They say nothing about *trade* eligibility: freshness, the venue rule and the liquidity floor live in
the Signal lane's own pure rules. This side owns the point-in-time boundaries and the deterministic
order, and nothing about the editorial pipeline that runs beside the OI ledger (#510).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TypedDict

# Bump when a key is added, removed or retyped below — and when what a key *means* changes without the
# key moving. The consumer's mapper is versioned against it, so neither a silently widened projection
# nor a silently re-scoped one can reach Trading unnoticed.
#
# Entries below v14 describe the verdict-derived OI read and are kept as the contract's history: the
# joins, pins and freeze checks several of them argue for no longer exist. v14 is the current shape.
#
# v2 (#211): the two candidate reads return the *newest* rows in the window rather than the oldest.
# Trading's scan horizon grew from `max_age x 3` to `max_age + max(lookback)` so a legal counterpart is
# actually visible, and with an ascending `LIMIT` a busy hour of that wider window would have been
# answered entirely with its oldest rows — spending the whole budget on context and returning none of
# the fresh triggers the scan exists to find.
#
# v6 (#264): the OI read stops being "the OI verdicts the reader pushed" and becomes "the OI facts this
# generation parsed and persisted". `final_decision` and the deterministic rule are still selected — a
# capital decision must be traceable to the judgment beside it — but they no longer decide *visibility*,
# and the two Trading thresholds the SELECT used to execute (`max_rank_in_window`, `min_oi_value_usd`)
# are gone from the signature. Measured over the seven days this table has existed, the reader's own
# `whale_oi_ratio > 80%` push rule admitted 2 of the 7 frames that meet the target strategy's
# conditions; the other 5 — TUT 15.48%/54.24% among them — were `drop` and never reached Trading at all.
#
# v10 (#458): that reader rule is gone entirely and every frame arrives as `stored` / `drop`, so this
# read is now the only judgment of an OI frame there is. Two consequences here. The pin moves to
# `news_oi_signal_v3`, which means frames judged by the old rule stop projecting — deliberately: their
# `program_sha256` attests thresholds that no longer exist. And the freeze check loses its
# `rank_in_window` leg with the column, keeping the six measurements and the three source-contract
# fields it still binds verdict-to-ledger.
#
# v7 (#265): the OI read publishes what the provider proves about *how* the frame was measured —
# `source_strategy_id`, `source_contract_version`, `measurement_window_ms` — beside the four numbers it
# measured. All three are nullable together, and `NULL` is the contract: it means the interval could not
# be proven for this frame and a consumer must refuse it rather than assume five minutes.
#
# v9 (#331): the editorial-verdict and liquidation reads are gone. Editorial News no longer triggers
# automatic capital and there is no online liquidation consumer, so publishing either was a projection
# nothing read — and an invitation for the Signal lane to grow a second trigger by accident. The OI
# read and the instrument reads are what remains.
# v10 (#369): OI carries its typed judgment identity directly.  The retired
# synthetic Editorial envelope is neither read nor reconstructed.
# v11 (#377): the OI ledger freezes its source Item, venue, availability clock and learning epoch at
# insertion. Evidence capture no longer reconstructs an old frame from a mutable Event leader or the
# currently active learning bundle.
# v12 (#377): evidence capture and fixed-window verification publish bounded bulk catalogue/source
# projections. App no longer runs one catalogue query per source or treats Trading's Gate ledger as
# the universe of News facts that should have reached that Gate.
# v13 (#433-C review): publish the immutable title token consumed by the OI parser so the App seam can
# distinguish Hyperliquid's `XYZ-` builder-DEX market from the main perpetual book.
#
# v14 (#510 PR-4): the OI reads are the numeric ledger and nothing else. `news_oi_signals` already holds
# the deterministic measurements, the source Item, the frame's venue, both clocks and the provider's
# measurement contract, so the verdict join, the learning-epoch join, the six `trace` equalities that
# re-proved the ledger against the verdict's own copy of it, the `split_part(title)` provider token and
# the four News version literals are gone. `news_items.first_ingest_mode` is the one column the Item
# still owes this projection. The frame reaches Trading without passing through the editorial pipeline,
# the active learning arm or the Program identity, which is what makes a News policy or Program move
# stop being a Trading contract change. The builder-DEX distinction now rides on `source_venue`, the
# provider's own field, rather than on a title token: `symbol` in this ledger is already canonical.
NEWS_TRADE_PROJECTION_VERSION = "news_trade_projection_v14"

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
    """One exactly-listed native crypto perpetual for one underlying."""

    venue: str
    venue_symbol: str
    base_symbol: str
    instrument_class: str
    quote_asset: str | None
    status: str
    last_seen_ms: int


class TradeEvidenceCollectionHealthRow(TypedDict):
    """Blind-safe collector state and source denominator for one future batch."""

    connected: bool
    last_frame_at_ms: int | None
    last_error_code: str | None
    expected_source_count: int


class TradeEvidenceCatalogProjectionRow(TypedDict):
    """One point-in-time listing row attached to its bounded evidence source."""

    event_id: str
    metric_version: str
    source_observed_at_ms: int
    venue: str
    venue_symbol: str
    base_symbol: str
    instrument_class: str
    quote_asset: str | None
    status: str
    last_seen_ms: int


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

    def trade_evidence_collection_health(
        self,
        *,
        start_observed_at_ms: int,
        end_observed_at_ms: int,
        available_at_or_before_ms: int,
        source_venues: tuple[str, ...],
    ) -> TradeEvidenceCollectionHealthRow:
        """News-owned raw collector and denominator facts for blind-safe Trading health."""

        row = self.conn.execute(
            """
            SELECT ingest.connected, ingest.last_frame_at_ms, ingest.last_error_code,
                   (SELECT count(*)
                      FROM news_oi_signals signal
                     WHERE signal.observed_at_ms >= %(start)s
                       AND signal.observed_at_ms < %(end)s
                       AND signal.available_at_ms <= %(available)s
                       AND signal.source_venue = ANY(%(venues)s)) AS expected_source_count
              FROM news_ingest_state ingest
             WHERE ingest.singleton_key = 'opennews'
            """,
            {
                "start": int(start_observed_at_ms),
                "end": int(end_observed_at_ms),
                "available": int(available_at_or_before_ms),
                "venues": list(source_venues),
            },
        ).fetchone()
        return TradeEvidenceCollectionHealthRow(
            connected=False if row is None else bool(row["connected"]),
            last_frame_at_ms=None if row is None else row["last_frame_at_ms"],
            last_error_code="collector_state_missing" if row is None else row["last_error_code"],
            expected_source_count=0 if row is None else int(row["expected_source_count"]),
        )

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

    def trade_evidence_catalog_rows(
        self,
        *,
        metric_version: str,
        start_observed_at_ms: int,
        end_observed_at_ms: int,
        known_at_or_before_ms: int,
        available_at_or_before_ms: int,
        source_limit: int,
        catalog_limit: int,
    ) -> list[TradeEvidenceCatalogProjectionRow]:
        """Bulk point-in-time catalogues for the exact bounded evidence-source query."""

        rows = self.conn.execute(
            """
            WITH sources AS (
              SELECT s.event_id, s.metric_version, s.observed_at_ms, s.symbol, s.source_venue
                FROM news_oi_signals s
               WHERE s.metric_version = %(metric)s
                 AND s.available_at_ms <= %(available)s
                 AND s.observed_at_ms >= %(start)s
                 AND s.observed_at_ms < %(end)s
                 AND s.created_at_ms <= %(known)s
               ORDER BY s.observed_at_ms, s.event_id
               LIMIT %(source_limit)s
            )
            SELECT source.event_id, source.metric_version,
                   source.observed_at_ms AS source_observed_at_ms,
                   historical.venue, historical.venue_symbol, historical.base_symbol,
                   historical.instrument_class, historical.quote_asset, historical.status,
                   historical.observed_at_ms AS last_seen_ms
              FROM sources source
              JOIN LATERAL (
                SELECT DISTINCT ON (event.venue, event.venue_symbol)
                       event.venue, event.venue_symbol, event.base_symbol,
                       event.instrument_class, event.quote_asset, event.status, event.observed_at_ms
                  FROM news_market_instrument_listing_events event
                 WHERE event.venue = CASE
                         WHEN lower(coalesce(source.source_venue, '')) IN
                           ('binance', 'binance.perp', 'binance.usdm') THEN 'binance.perp'
                         WHEN lower(coalesce(source.source_venue, '')) IN
                           ('hyperliquid', 'hl.perp', 'hyperliquid.perp') THEN 'hl.perp'
                         ELSE ''
                       END
                   AND event.base_symbol = upper(btrim(source.symbol))
                   AND event.observed_at_ms <= source.observed_at_ms
                 ORDER BY event.venue, event.venue_symbol, event.observed_at_ms DESC
              ) historical ON historical.status = 'trading' AND historical.instrument_class = 'crypto'
             ORDER BY source.observed_at_ms, source.event_id, historical.venue,
                      CASE historical.quote_asset WHEN 'USDT' THEN 0 WHEN 'USDC' THEN 1 ELSE 2 END,
                      length(historical.venue_symbol), historical.venue_symbol
             LIMIT %(catalog_limit)s
            """,
            {
                "metric": metric_version,
                "available": int(available_at_or_before_ms),
                "start": int(start_observed_at_ms),
                "end": int(end_observed_at_ms),
                "known": int(known_at_or_before_ms),
                "source_limit": int(source_limit),
                "catalog_limit": int(catalog_limit),
            },
        ).fetchall()
        return [
            TradeEvidenceCatalogProjectionRow(
                event_id=str(row["event_id"]),
                metric_version=str(row["metric_version"]),
                source_observed_at_ms=int(row["source_observed_at_ms"]),
                venue=str(row["venue"]),
                venue_symbol=str(row["venue_symbol"]),
                base_symbol=str(row["base_symbol"]),
                instrument_class=str(row["instrument_class"]),
                quote_asset=None if row["quote_asset"] is None else str(row["quote_asset"]),
                status=str(row["status"]),
                last_seen_ms=int(row["last_seen_ms"]),
            )
            for row in rows
        ]

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
                       quote_asset, status, last_seen_ms
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
                       quote_asset, status, observed_at_ms AS last_seen_ms
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
                last_seen_ms=row["last_seen_ms"],
            )
            for row in rows
        ]

    def trade_execution_instruments(self) -> list[TradeInstrumentProjectionRow]:
        """All active Binance USD-M rows for the cold capability-snapshot command.

        Crypto classification deliberately crosses the App seam instead of filtering here: every
        provider candidate must receive an included row or a named mechanical exclusion.
        """

        rows = self.conn.execute(
            """
            SELECT venue, venue_symbol, base_symbol, instrument_class, quote_asset, status, last_seen_ms
              FROM news_market_instruments
             WHERE venue = 'binance.perp'
               AND status = 'trading'
             ORDER BY venue_symbol, base_symbol
            """
        ).fetchall()
        return [
            TradeInstrumentProjectionRow(
                venue=row["venue"],
                venue_symbol=row["venue_symbol"],
                base_symbol=row["base_symbol"],
                instrument_class=row["instrument_class"],
                quote_asset=row["quote_asset"],
                status=row["status"],
                last_seen_ms=row["last_seen_ms"],
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
