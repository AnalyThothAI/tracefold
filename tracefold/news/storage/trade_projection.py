"""News-owned point-in-time projection read by App composition for Trading.
The row contracts below are the published shape of that projection. They are `TypedDict`s
because the rows *are* the SELECT lists — naming the columns is the whole contract, and a runtime
model here would coerce values PostgreSQL already typed and turn a nullable LEFT JOIN column into a
different value. Nothing outside this module may add, rename or retype a key without editing them,
which is what makes the App-side mapper break at type-check time instead of at 03:00 in a runner.
A version string sat above them for thirteen revisions, restating in prose what the `TypedDict`s
already state in types, and it was never compared with anything in production (#537 PR-4).

The outbox freezes News facts in their owning transaction. Analysis applies Trading
identity and policy after acknowledging the public projection.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, TypedDict, cast


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


class TradeEventRow(TypedDict):
    event_id: int
    kind: str
    source_fact_key: str
    source_revision: str
    payload_sha256: str
    payload: dict[str, object]
    source_recorded_at_ms: int
    conflict_sha256: str | None


class TradeProjectionStorage:
    conn: Any

    def enqueue_trade_event(
        self,
        *,
        kind: str,
        source_fact_key: str,
        source_revision: str,
        payload: Mapping[str, object],
        source_recorded_at_ms: int,
    ) -> bool:
        """Freeze a public fact in the producer's own transaction.

        Retries retain the first payload and first recorded time. A different
        payload on the same identity is visible as a conflict, never an overwrite.
        """

        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        cursor = self.conn.execute(
            """
            INSERT INTO news_trade_events
              (kind, source_fact_key, source_revision, payload_sha256, payload, source_recorded_at_ms)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (kind, source_fact_key, source_revision) DO UPDATE
              SET conflict_sha256 = CASE
                  WHEN news_trade_events.payload_sha256 <> EXCLUDED.payload_sha256
                  THEN EXCLUDED.payload_sha256 ELSE news_trade_events.conflict_sha256 END
            RETURNING payload_sha256
            """,
            (kind, source_fact_key, source_revision, digest, serialized, int(source_recorded_at_ms)),
        )
        row = cursor.fetchone()
        return row is not None and row["payload_sha256"] == digest

    def unacknowledged_trade_events(self, *, limit: int) -> list[TradeEventRow]:
        """Drain the unconfirmed set, never a sequence high-water mark."""

        if not 1 <= limit <= 512:
            raise ValueError("trade_event_batch_invalid")
        rows = self.conn.execute(
            """
            SELECT event_id, kind, source_fact_key, source_revision, payload_sha256,
                   payload, source_recorded_at_ms, conflict_sha256
              FROM news_trade_events
             WHERE acknowledged_at_ms IS NULL AND rejected_reason IS NULL
             ORDER BY event_id LIMIT %s
            """,
            (limit,),
        ).fetchall()
        return [cast(TradeEventRow, dict(row)) for row in rows]

    def acknowledge_trade_event(self, *, event_id: int, payload_sha256: str, now_ms: int) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE news_trade_events SET acknowledged_at_ms = %s
             WHERE event_id = %s AND payload_sha256 = %s
               AND acknowledged_at_ms IS NULL AND rejected_reason IS NULL
            """,
            (int(now_ms), int(event_id), payload_sha256),
        )
        return bool(cursor.rowcount)

    def reject_trade_event(self, *, event_id: int, payload_sha256: str, reason: str) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE news_trade_events SET rejected_reason = %s
             WHERE event_id = %s AND payload_sha256 = %s
               AND acknowledged_at_ms IS NULL AND rejected_reason IS NULL
            """,
            (reason, int(event_id), payload_sha256),
        )
        return bool(cursor.rowcount)

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
