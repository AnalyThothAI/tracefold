from __future__ import annotations

from typing import Any

from tracefold.market.radar.constants import TOKEN_RADAR_RESOLVER_POLICY_VERSION
from tracefold.platform.validation import require_nonnegative_int


class TokenTargetRepository:
    def __init__(self, conn: Any):
        self.conn = conn

    def target_identity(self, *, target_type: str, target_id: str) -> dict[str, Any] | None:
        if target_type == "Asset":
            row = self.conn.execute(
                """
                SELECT
                  'Asset' AS target_type,
                  registry_assets.asset_id AS target_id,
                  COALESCE(asset_identity_current.canonical_symbol, price_feeds.base_symbol) AS symbol,
                  asset_identity_current.canonical_name AS name,
                  registry_assets.chain_id,
                  registry_assets.address,
                  registry_assets.status,
                  price_feeds.pricefeed_id,
                  price_feeds.provider,
                  price_feeds.native_market_id,
                  price_feeds.quote_symbol,
                  price_feeds.feed_type
                FROM registry_assets
                LEFT JOIN asset_identity_current
                  ON asset_identity_current.asset_id = registry_assets.asset_id
                LEFT JOIN LATERAL (
                  SELECT *
                  FROM price_feeds
                  WHERE price_feeds.subject_type = 'Asset'
                    AND price_feeds.subject_id = registry_assets.asset_id
                    AND price_feeds.status IN ('candidate', 'canonical')
                  ORDER BY
                    CASE WHEN price_feeds.status = 'canonical' THEN 0 ELSE 1 END,
                    price_feeds.updated_at_ms DESC,
                    price_feeds.pricefeed_id ASC
                  LIMIT 1
                ) price_feeds ON true
                WHERE registry_assets.asset_id = %s
                """,
                [target_id],
            ).fetchone()
            return _target_identity_payload(target_type=target_type, row=row)
        if target_type == "CexToken":
            row = self.conn.execute(
                """
                SELECT
                  'CexToken' AS target_type,
                  cex_tokens.cex_token_id AS target_id,
                  cex_tokens.base_symbol AS symbol,
                  NULL AS name,
                  NULL AS chain_id,
                  NULL AS address,
                  cex_tokens.status,
                  price_feeds.pricefeed_id,
                  price_feeds.provider,
                  price_feeds.native_market_id,
                  price_feeds.quote_symbol,
                  price_feeds.feed_type
                FROM cex_tokens
                LEFT JOIN LATERAL (
                  SELECT *
                  FROM price_feeds
                  WHERE price_feeds.subject_type = 'CexToken'
                    AND price_feeds.subject_id = cex_tokens.cex_token_id
                    AND price_feeds.provider = 'binance'
                    AND price_feeds.feed_type = 'cex_swap'
                    AND price_feeds.quote_symbol = 'USDT'
                    AND price_feeds.status = 'canonical'
                  ORDER BY
                    price_feeds.updated_at_ms DESC,
                    price_feeds.native_market_id ASC
                  LIMIT 1
                ) price_feeds ON true
                WHERE cex_tokens.cex_token_id = %s
                """,
                [target_id],
            ).fetchone()
            return _target_identity_payload(target_type=target_type, row=row)
        return None

    def latest_market_tick(self, *, target_type: str, target_id: str) -> dict[str, Any] | None:
        if target_type == "Asset":
            row = self.conn.execute(
                """
                SELECT
                  market_tick_current.*,
                  market_tick_current.tick_observed_at_ms AS observed_at_ms,
                  market_tick_current.updated_at_ms AS received_at_ms
                FROM registry_assets
                JOIN market_tick_current
                  ON market_tick_current.target_type = 'chain_token'
                 AND market_tick_current.target_id = registry_assets.chain_id || ':' || registry_assets.address
                WHERE registry_assets.asset_id = %s
                """,
                [target_id],
            ).fetchone()
            return dict(row) if row is not None else None
        if target_type == "CexToken":
            row = self.conn.execute(
                """
                SELECT
                  market_tick_current.*,
                  market_tick_current.tick_observed_at_ms AS observed_at_ms,
                  market_tick_current.updated_at_ms AS received_at_ms
                FROM cex_tokens
                JOIN LATERAL (
                  SELECT *
                  FROM price_feeds
                  WHERE price_feeds.subject_type = 'CexToken'
                    AND price_feeds.subject_id = cex_tokens.cex_token_id
                    AND price_feeds.provider = 'binance'
                    AND price_feeds.feed_type = 'cex_swap'
                    AND price_feeds.quote_symbol = 'USDT'
                    AND price_feeds.status = 'canonical'
                  ORDER BY
                    price_feeds.updated_at_ms DESC,
                    price_feeds.native_market_id ASC
                  LIMIT 1
                ) price_feeds ON true
                JOIN market_tick_current
                  ON market_tick_current.target_type = 'cex_symbol'
                 AND market_tick_current.target_id = price_feeds.provider || ':' || price_feeds.native_market_id
                WHERE cex_tokens.cex_token_id = %s
                """,
                [target_id],
            ).fetchone()
            return dict(row) if row is not None else None
        return None

    def timeline_rows(
        self,
        *,
        target_type: str,
        target_id: str,
        since_ms: int,
        limit: int,
        cursor: tuple[int, str] | None = None,
    ) -> list[dict[str, Any]]:
        row_limit = require_nonnegative_int(limit, error_code="token_target_repository_limit_required")
        clauses = [
            "tir.target_type = %s",
            "tir.target_id = %s",
            "tir.is_current = true",
            "tir.resolver_policy_version = %s",
            "events.received_at_ms >= %s",
        ]
        params: list[Any] = [target_type, target_id, TOKEN_RADAR_RESOLVER_POLICY_VERSION, int(since_ms)]
        if cursor is not None:
            cursor_ms, cursor_event_id = cursor
            clauses.append("(events.received_at_ms, events.event_id) < (%s, %s)")
            params.extend([int(cursor_ms), str(cursor_event_id)])
        params.append(row_limit)
        rows = self.conn.execute(
            f"""
            WITH target_events AS (
            SELECT
              events.event_id,
              events.tweet_id,
              events.canonical_url,
              events.author_handle,
              events.text,
              events.text_clean,
              events.reference_json,
              events.received_at_ms,
              tir.target_type,
              tir.target_id,
              tir.resolution_status,
              tir.resolution_status AS attribution_status,
              CASE
                WHEN tir.resolution_status = 'EXACT' THEN 1.0
                WHEN tir.resolution_status = 'UNIQUE_BY_CONTEXT' THEN 0.9
                WHEN tir.resolution_status = 'AMBIGUOUS' THEN 0.45
                ELSE 0.0
              END AS confidence,
              registry_assets.chain_id,
              registry_assets.token_standard,
              registry_assets.address,
              asset_identity_current.canonical_symbol AS asset_symbol,
              asset_identity_current.canonical_name AS asset_name,
              asset_identity_current.identity_confidence AS asset_identity_confidence,
              cex_tokens.base_symbol AS cex_base_symbol,
              CASE
                WHEN tir.target_type = 'CexToken' THEN preferred_price_feed.pricefeed_id
                ELSE tir.pricefeed_id
              END AS pricefeed_id,
              price_feeds.provider,
              price_feeds.native_market_id,
              price_feeds.base_symbol AS pricefeed_base_symbol,
              price_feeds.quote_symbol,
              price_feeds.feed_type,
              event_tick.tick_id AS market_tick_id,
              event_tick.source_provider AS market_tick_provider,
              event_tick.observed_at_ms AS market_tick_observed_at_ms,
              event_tick.price_usd,
              NULL::numeric AS price_quote,
              NULL::text AS price_quote_symbol,
              CASE WHEN event_tick.tick_id IS NOT NULL THEN event_market_capture.capture_method ELSE NULL END
                AS market_capture_method,
              CASE WHEN event_tick.tick_id IS NOT NULL THEN event_market_capture.tick_lag_ms ELSE NULL END
                AS market_tick_lag_ms,
              row_number() OVER (
                PARTITION BY events.event_id
                ORDER BY
                  CASE
                    WHEN tir.resolution_status = 'EXACT' THEN 0
                    WHEN tir.resolution_status = 'UNIQUE_BY_CONTEXT' THEN 1
                    WHEN tir.resolution_status = 'AMBIGUOUS' THEN 2
                    ELSE 3
                  END,
                  tir.confidence DESC,
                  tir.decision_time_ms DESC,
                  tir.resolution_id DESC
              ) AS event_target_rank
            FROM token_intent_resolutions tir
            JOIN events ON events.event_id = tir.event_id
            LEFT JOIN registry_assets
              ON tir.target_type = 'Asset'
             AND registry_assets.asset_id = tir.target_id
            LEFT JOIN asset_identity_current
              ON tir.target_type = 'Asset'
             AND asset_identity_current.asset_id = tir.target_id
            LEFT JOIN cex_tokens
              ON tir.target_type = 'CexToken'
             AND cex_tokens.cex_token_id = tir.target_id
            LEFT JOIN LATERAL (
              SELECT *
              FROM price_feeds
              WHERE tir.target_type = 'CexToken'
                AND price_feeds.subject_type = 'CexToken'
                AND price_feeds.subject_id = tir.target_id
                AND price_feeds.provider = 'binance'
                AND price_feeds.feed_type = 'cex_swap'
                AND price_feeds.quote_symbol = 'USDT'
                AND price_feeds.status = 'canonical'
              ORDER BY
                price_feeds.updated_at_ms DESC,
                price_feeds.native_market_id ASC
              LIMIT 1
            ) preferred_price_feed ON true
            LEFT JOIN price_feeds
              ON price_feeds.pricefeed_id = CASE
                WHEN tir.target_type = 'CexToken' THEN preferred_price_feed.pricefeed_id
                ELSE tir.pricefeed_id
              END
            LEFT JOIN enriched_events event_market_capture
              ON event_market_capture.event_id = events.event_id
             AND event_market_capture.intent_id = tir.intent_id
             AND event_market_capture.resolution_id = tir.resolution_id
            LEFT JOIN market_ticks event_tick
              ON event_tick.observed_at_ms = event_market_capture.tick_observed_at_ms
             AND event_tick.tick_id = event_market_capture.tick_id
             AND event_tick.target_type = CASE
                WHEN tir.target_type = 'CexToken' THEN 'cex_symbol'
                ELSE event_tick.target_type
              END
             AND event_tick.target_id = CASE
                WHEN tir.target_type = 'CexToken' THEN price_feeds.provider || ':' || price_feeds.native_market_id
                ELSE event_tick.target_id
              END
             AND event_tick.source_provider = CASE
                WHEN tir.target_type = 'CexToken' THEN 'binance_cex_rest'
                ELSE event_tick.source_provider
              END
            WHERE {" AND ".join(clauses)}
            )
            SELECT *
            FROM target_events
            WHERE event_target_rank = 1
            ORDER BY received_at_ms DESC, event_id DESC
            LIMIT %s
            """,
            params,
        ).fetchall()
        return [_public_row(dict(row)) for row in rows]

    def timeline_rows_for_event_ids(
        self,
        *,
        target_type: str,
        target_id: str,
        event_ids: list[str] | tuple[str, ...],
        limit: int,
    ) -> list[dict[str, Any]]:
        row_limit = require_nonnegative_int(limit, error_code="token_target_repository_limit_required")
        source_event_ids = [str(event_id).strip() for event_id in event_ids if str(event_id or "").strip()]
        if not source_event_ids or row_limit <= 0:
            return []
        clauses = [
            "tir.target_type = %s",
            "tir.target_id = %s",
            "tir.is_current = true",
            "tir.resolver_policy_version = %s",
        ]
        params: list[Any] = [
            source_event_ids,
            target_type,
            target_id,
            TOKEN_RADAR_RESOLVER_POLICY_VERSION,
        ]
        params.append(row_limit)
        rows = self.conn.execute(
            f"""
            WITH requested_events AS (
              SELECT event_ids.event_id::text AS event_id, event_ids.ordinality::bigint AS source_event_ordinal
              FROM unnest(%s::text[]) WITH ORDINALITY AS event_ids(event_id, ordinality)
              WHERE NULLIF(btrim(event_ids.event_id::text), '') IS NOT NULL
            ),
            target_events AS (
            SELECT
              requested_events.source_event_ordinal,
              events.event_id,
              events.tweet_id,
              events.canonical_url,
              events.author_handle,
              events.text,
              events.text_clean,
              events.reference_json,
              events.received_at_ms,
              tir.target_type,
              tir.target_id,
              tir.resolution_status,
              tir.resolution_status AS attribution_status,
              CASE
                WHEN tir.resolution_status = 'EXACT' THEN 1.0
                WHEN tir.resolution_status = 'UNIQUE_BY_CONTEXT' THEN 0.9
                WHEN tir.resolution_status = 'AMBIGUOUS' THEN 0.45
                ELSE 0.0
              END AS confidence,
              registry_assets.chain_id,
              registry_assets.token_standard,
              registry_assets.address,
              asset_identity_current.canonical_symbol AS asset_symbol,
              asset_identity_current.canonical_name AS asset_name,
              asset_identity_current.identity_confidence AS asset_identity_confidence,
              cex_tokens.base_symbol AS cex_base_symbol,
              CASE
                WHEN tir.target_type = 'CexToken' THEN preferred_price_feed.pricefeed_id
                ELSE tir.pricefeed_id
              END AS pricefeed_id,
              price_feeds.provider,
              price_feeds.native_market_id,
              price_feeds.base_symbol AS pricefeed_base_symbol,
              price_feeds.quote_symbol,
              price_feeds.feed_type,
              event_tick.tick_id AS market_tick_id,
              event_tick.source_provider AS market_tick_provider,
              event_tick.observed_at_ms AS market_tick_observed_at_ms,
              event_tick.price_usd,
              NULL::numeric AS price_quote,
              NULL::text AS price_quote_symbol,
              CASE WHEN event_tick.tick_id IS NOT NULL THEN event_market_capture.capture_method ELSE NULL END
                AS market_capture_method,
              CASE WHEN event_tick.tick_id IS NOT NULL THEN event_market_capture.tick_lag_ms ELSE NULL END
                AS market_tick_lag_ms,
              row_number() OVER (
                PARTITION BY events.event_id
                ORDER BY
                  CASE
                    WHEN tir.resolution_status = 'EXACT' THEN 0
                    WHEN tir.resolution_status = 'UNIQUE_BY_CONTEXT' THEN 1
                    WHEN tir.resolution_status = 'AMBIGUOUS' THEN 2
                    ELSE 3
                  END,
                  tir.confidence DESC,
                  tir.decision_time_ms DESC,
                  tir.resolution_id DESC
              ) AS event_target_rank
            FROM requested_events
            JOIN events ON events.event_id = requested_events.event_id
            JOIN token_intent_resolutions tir
              ON tir.event_id = events.event_id
            LEFT JOIN registry_assets
              ON tir.target_type = 'Asset'
             AND registry_assets.asset_id = tir.target_id
            LEFT JOIN asset_identity_current
              ON tir.target_type = 'Asset'
             AND asset_identity_current.asset_id = tir.target_id
            LEFT JOIN cex_tokens
              ON tir.target_type = 'CexToken'
             AND cex_tokens.cex_token_id = tir.target_id
            LEFT JOIN LATERAL (
              SELECT *
              FROM price_feeds
              WHERE tir.target_type = 'CexToken'
                AND price_feeds.subject_type = 'CexToken'
                AND price_feeds.subject_id = tir.target_id
                AND price_feeds.provider = 'binance'
                AND price_feeds.feed_type = 'cex_swap'
                AND price_feeds.quote_symbol = 'USDT'
                AND price_feeds.status = 'canonical'
              ORDER BY
                price_feeds.updated_at_ms DESC,
                price_feeds.native_market_id ASC
              LIMIT 1
            ) preferred_price_feed ON true
            LEFT JOIN price_feeds
              ON price_feeds.pricefeed_id = CASE
                WHEN tir.target_type = 'CexToken' THEN preferred_price_feed.pricefeed_id
                ELSE tir.pricefeed_id
              END
            LEFT JOIN enriched_events event_market_capture
              ON event_market_capture.event_id = events.event_id
             AND event_market_capture.intent_id = tir.intent_id
             AND event_market_capture.resolution_id = tir.resolution_id
            LEFT JOIN market_ticks event_tick
              ON event_tick.observed_at_ms = event_market_capture.tick_observed_at_ms
             AND event_tick.tick_id = event_market_capture.tick_id
             AND event_tick.target_type = CASE
                WHEN tir.target_type = 'CexToken' THEN 'cex_symbol'
                ELSE event_tick.target_type
              END
             AND event_tick.target_id = CASE
                WHEN tir.target_type = 'CexToken' THEN price_feeds.provider || ':' || price_feeds.native_market_id
                ELSE event_tick.target_id
              END
             AND event_tick.source_provider = CASE
                WHEN tir.target_type = 'CexToken' THEN 'binance_cex_rest'
                ELSE event_tick.source_provider
              END
            WHERE {" AND ".join(clauses)}
            )
            SELECT *
            FROM target_events
            WHERE event_target_rank = 1
            ORDER BY source_event_ordinal ASC, received_at_ms DESC, event_id DESC
            LIMIT %s
            """,
            params,
        ).fetchall()
        return [_public_row(dict(row)) for row in rows]


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "symbol": row.get("asset_symbol") or row.get("cex_base_symbol") or row.get("pricefeed_base_symbol"),
    }


def _target_identity_payload(*, target_type: str, row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    row_dict = dict(row)
    return {
        "target_type": target_type,
        "target_id": row_dict.get("target_id"),
        "symbol": row_dict.get("symbol"),
        "name": row_dict.get("name"),
        "chain_id": row_dict.get("chain_id"),
        "address": row_dict.get("address"),
        "status": row_dict.get("status") or "resolved",
        "source": "registry_assets" if target_type == "Asset" else "cex_tokens",
        "reason": "TARGET_ID",
        "pricefeed_id": row_dict.get("pricefeed_id"),
        "provider": row_dict.get("provider"),
        "native_market_id": row_dict.get("native_market_id"),
        "quote_symbol": row_dict.get("quote_symbol"),
        "feed_type": row_dict.get("feed_type"),
    }
