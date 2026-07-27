from __future__ import annotations

from typing import Any

from tracefold.market.radar.constants import TOKEN_RADAR_RESOLVER_POLICY_VERSION

STOCKS_RADAR_SOURCE_EVENT_LIMIT = 25


class StocksRadarQuery:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def stock_rows(self, *, since_ms: int, now_ms: int, limit: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            WITH recent_intents AS MATERIALIZED (
              SELECT
                ti.intent_id,
                e.event_id,
                e.author_handle,
                e.received_at_ms
              FROM events e
              JOIN token_intents ti ON ti.event_id = e.event_id
              WHERE e.received_at_ms >= %s
                AND e.received_at_ms <= %s
            ),
            stock_mentions AS MATERIALIZED (
              SELECT
                tir.target_id,
                ues.symbol,
                ues.security_name,
                ues.exchange,
                ues.instrument_type,
                recent_intents.event_id,
                recent_intents.author_handle,
                recent_intents.received_at_ms
              FROM recent_intents
              JOIN token_intent_resolutions tir
                ON tir.intent_id = recent_intents.intent_id
               AND tir.is_current = true
               AND tir.resolver_policy_version = %s
               AND tir.target_type = 'MarketInstrument'
               AND tir.resolution_status = 'NON_CRYPTO'
               AND tir.reason_codes_json @> '["CONFIRMED_US_EQUITY"]'::jsonb
              JOIN us_equity_symbols ues
                ON ues.market_instrument_id = tir.target_id
               AND ues.status = 'active'
            ),
            ranked_mentions AS MATERIALIZED (
              SELECT
                stock_mentions.*,
                row_number() OVER (
                  PARTITION BY target_id
                  ORDER BY received_at_ms DESC, event_id DESC
                ) AS event_rank
              FROM stock_mentions
            ),
            ranked AS MATERIALIZED (
              SELECT
                target_id,
                symbol,
                security_name,
                exchange,
                instrument_type,
                COUNT(*)::int AS mentions,
                COUNT(DISTINCT NULLIF(LOWER(author_handle), ''))::int AS unique_authors,
                MAX(received_at_ms)::bigint AS latest_seen_ms,
                MAX(CASE WHEN event_rank = 1 THEN event_id END) AS latest_event_id,
                MAX(CASE WHEN event_rank = 1 THEN author_handle END) AS latest_author_handle,
                COALESCE(
                  ARRAY_AGG(event_id ORDER BY event_rank) FILTER (WHERE event_rank <= %s),
                  ARRAY[]::text[]
                ) AS source_event_ids
              FROM ranked_mentions
              GROUP BY target_id, symbol, security_name, exchange, instrument_type
              ORDER BY mentions DESC, latest_seen_ms DESC, symbol ASC
              LIMIT %s
            )
            SELECT
              ranked.target_id,
              ranked.symbol,
              ranked.security_name,
              ranked.exchange,
              ranked.instrument_type,
              ranked.mentions,
              ranked.unique_authors,
              ranked.latest_seen_ms,
              ranked.latest_event_id,
              ranked.latest_author_handle,
              COALESCE(e.text_clean, e.text) AS latest_text,
              ranked.source_event_ids
            FROM ranked
            LEFT JOIN events e ON e.event_id = ranked.latest_event_id
            ORDER BY mentions DESC, latest_seen_ms DESC, symbol ASC
            """,
            (
                int(since_ms),
                int(now_ms),
                TOKEN_RADAR_RESOLVER_POLICY_VERSION,
                STOCKS_RADAR_SOURCE_EVENT_LIMIT,
                int(limit),
            ),
        ).fetchall()
        return [dict(row) for row in rows]
