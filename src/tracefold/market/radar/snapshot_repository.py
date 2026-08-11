from __future__ import annotations

from typing import Any, TypedDict

from psycopg.types.json import Jsonb

from tracefold.market.pricing.live_market import LIVE_MARKET_STALE_AFTER_MS
from tracefold.market.radar.constants import (
    TOKEN_RADAR_INPUT_ROW_CAP,
    TOKEN_RADAR_SNAPSHOT_SCHEMA_VERSION,
)
from tracefold.market.radar.reducer import ReducedTokenRadar
from tracefold.platform.postgres.postgres_client import require_transaction
from tracefold.platform.postgres.write_contract import mutation_count


class TokenRadarPublicationResult(TypedDict):
    status: str
    rows_written: int


class TokenRadarCurrentRepository:
    """One persistence seam for material-fact input and compact current state."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def load_material_inputs(self, *, now_ms: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.conn.execute(
                _TOKEN_RADAR_INPUT_SQL,
                (
                    max(0, int(now_ms) - 2 * 60 * 60 * 1000),
                    int(now_ms),
                    TOKEN_RADAR_INPUT_ROW_CAP + 1,
                ),
            ).fetchall()
        ]

    def load_presentation_facts(
        self,
        targets: list[tuple[str, str]],
        *,
        now_ms: int,
    ) -> list[dict[str, Any]]:
        parsed_now_ms = int(now_ms)
        if parsed_now_ms < 0:
            raise ValueError("token_radar_presentation_now_ms_invalid")
        requested = list(
            dict.fromkeys(
                (str(target_type), str(target_id))
                for target_type, target_id in targets
                if str(target_type) in {"Asset", "CexToken"} and str(target_id)
            )
        )
        if not requested:
            return []
        rows = self.conn.execute(
            _TOKEN_RADAR_PRESENTATION_SQL,
            (
                [target_type for target_type, _target_id in requested],
                [target_id for _target_type, target_id in requested],
                max(0, parsed_now_ms - LIVE_MARKET_STALE_AFTER_MS),
                parsed_now_ms,
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    def served_snapshot(self) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT served_payload
              FROM token_radar_current
             WHERE singleton_key = true
            """
        ).fetchone()
        if row is None:
            raise RuntimeError("token_radar_current_singleton_missing")
        payload = row.get("served_payload")
        if not isinstance(payload, dict):
            raise RuntimeError("token_radar_current_payload_invalid")
        return dict(payload)

    def publish(
        self,
        reduced: ReducedTokenRadar,
        *,
        evaluation_at_ms: int,
    ) -> TokenRadarPublicationResult:
        require_transaction(self.conn, operation="publish_token_radar_current")
        current = self.conn.execute(
            """
            SELECT state_fingerprint, latest_attempt_status, evaluation_at_ms
              FROM token_radar_current
             WHERE singleton_key = true
             FOR UPDATE
            """
        ).fetchone()
        if current is None:
            raise RuntimeError("token_radar_current_singleton_missing")
        if int(current["evaluation_at_ms"]) > int(evaluation_at_ms):
            return {"status": "stale_skipped", "rows_written": 0}
        state_unchanged = current.get("state_fingerprint") == reduced.state_fingerprint
        if state_unchanged and str(current["latest_attempt_status"]) == "ready":
            return {"status": "unchanged", "rows_written": 0}
        if state_unchanged:
            cursor = self.conn.execute(
                """
                UPDATE token_radar_current
                   SET input_fingerprint = %s,
                       evaluation_at_ms = %s,
                       input_rows = %s,
                       input_bytes = %s,
                       latest_attempt_status = 'ready',
                       latest_error_code = NULL,
                       updated_at_ms = %s
                 WHERE singleton_key = true
                   AND evaluation_at_ms <= %s
                   AND latest_attempt_status = 'failed'
                """,
                (
                    reduced.input_fingerprint,
                    int(evaluation_at_ms),
                    int(reduced.input_rows),
                    int(reduced.input_bytes),
                    int(evaluation_at_ms),
                    int(evaluation_at_ms),
                ),
            )
            changed = mutation_count(cursor, error_code="token_radar_current_recovery_count_invalid")
            return {
                "status": "recovered" if changed else "stale_skipped",
                "rows_written": changed,
            }
        cursor = self.conn.execute(
            """
            UPDATE token_radar_current
               SET schema_version = %s,
                   ruleset_version = %s,
                   ruleset_fingerprint = %s,
                   input_fingerprint = %s,
                   state_fingerprint = %s,
                   evidence_as_of_ms = %s,
                   evaluation_at_ms = %s,
                   input_rows = %s,
                   input_bytes = %s,
                   latest_attempt_status = 'ready',
                   latest_error_code = NULL,
                   served_payload = %s,
                   updated_at_ms = %s
             WHERE singleton_key = true
               AND evaluation_at_ms <= %s
            """,
            (
                TOKEN_RADAR_SNAPSHOT_SCHEMA_VERSION,
                reduced.ruleset_version,
                reduced.ruleset_fingerprint,
                reduced.input_fingerprint,
                reduced.state_fingerprint,
                int(reduced.snapshot["evidence_as_of_ms"]),
                int(evaluation_at_ms),
                int(reduced.input_rows),
                int(reduced.input_bytes),
                Jsonb(reduced.snapshot),
                int(evaluation_at_ms),
                int(evaluation_at_ms),
            ),
        )
        changed = mutation_count(cursor, error_code="token_radar_current_publish_count_invalid")
        return {
            "status": "published" if changed else "stale_skipped",
            "rows_written": changed,
        }

    def record_failure(self, *, error_code: str, evaluation_at_ms: int) -> int:
        require_transaction(self.conn, operation="fail_token_radar_current")
        code = str(error_code).strip()
        if not code:
            raise ValueError("token_radar_failure_code_required")
        cursor = self.conn.execute(
            """
            UPDATE token_radar_current
               SET evaluation_at_ms = %s,
                   latest_attempt_status = 'failed',
                   latest_error_code = %s,
                   failure_count = failure_count + 1,
                   updated_at_ms = %s
             WHERE singleton_key = true
               AND evaluation_at_ms <= %s
            """,
            (
                int(evaluation_at_ms),
                code,
                int(evaluation_at_ms),
                int(evaluation_at_ms),
            ),
        )
        return mutation_count(cursor, error_code="token_radar_current_failure_count_invalid")


_TOKEN_RADAR_INPUT_SQL = r"""
WITH resolved AS (
  SELECT DISTINCT ON (
    resolution.target_type,
    resolution.target_id,
    event.event_id
  )
    resolution.target_type,
    resolution.target_id,
    resolution.resolution_status,
    resolution.resolution_id,
    intent.intent_id,
    intent.display_symbol,
    event.event_id,
    event.received_at_ms,
    event.author_handle,
    COALESCE(event.text_clean, event.search_text, event.text) AS text
  FROM events event
  JOIN token_intents intent
    ON intent.event_id = event.event_id
  JOIN token_intent_resolutions resolution
    ON resolution.intent_id = intent.intent_id
   AND resolution.event_id = event.event_id
  WHERE event.received_at_ms > %s
    AND event.received_at_ms <= %s
    AND resolution.is_current = true
    AND resolution.resolution_status IN ('EXACT', 'UNIQUE_BY_CONTEXT')
    AND resolution.target_type IN ('Asset', 'CexToken')
    AND resolution.target_id IS NOT NULL
  ORDER BY
    resolution.target_type,
    resolution.target_id,
    event.event_id,
    resolution.decision_time_ms DESC,
    resolution.resolution_id ASC,
    intent.intent_id ASC
),
hydrated AS (
  SELECT
    resolved.target_type,
    resolved.target_id,
    COALESCE(
      CASE WHEN resolved.target_type = 'Asset'
           THEN asset_identity_current.canonical_symbol END,
      CASE WHEN resolved.target_type = 'CexToken'
           THEN cex_tokens.base_symbol END,
      resolved.display_symbol
    ) AS symbol,
    CASE WHEN resolved.target_type = 'Asset'
         THEN registry_assets.chain_id END AS chain,
    CASE WHEN resolved.target_type = 'CexToken'
         THEN preferred_price_feed.provider END AS exchange,
    CASE WHEN resolved.target_type = 'Asset'
         THEN registry_assets.address END AS address,
    resolved.resolution_status,
    resolved.event_id,
    resolved.received_at_ms,
    resolved.author_handle,
    resolved.text,
    signal_tick.price_usd AS signal_price_usd
  FROM resolved
  LEFT JOIN registry_assets
    ON resolved.target_type = 'Asset'
   AND registry_assets.asset_id = resolved.target_id
  LEFT JOIN asset_identity_current
    ON resolved.target_type = 'Asset'
   AND asset_identity_current.asset_id = resolved.target_id
  LEFT JOIN cex_tokens
    ON resolved.target_type = 'CexToken'
   AND cex_tokens.cex_token_id = resolved.target_id
  LEFT JOIN LATERAL (
    SELECT price_feed.*
      FROM price_feeds price_feed
     WHERE resolved.target_type = 'CexToken'
       AND price_feed.subject_type = 'CexToken'
       AND price_feed.subject_id = resolved.target_id
       AND price_feed.provider = 'binance'
       AND price_feed.feed_type = 'cex_swap'
       AND price_feed.quote_symbol = 'USDT'
       AND price_feed.status = 'canonical'
     ORDER BY
       price_feed.updated_at_ms DESC,
       price_feed.native_market_id ASC,
       price_feed.pricefeed_id ASC
     LIMIT 1
  ) preferred_price_feed ON true
  LEFT JOIN LATERAL (
    SELECT enriched.*
      FROM enriched_events enriched
     WHERE enriched.event_id = resolved.event_id
       AND enriched.intent_id = resolved.intent_id
       AND enriched.resolution_id = resolved.resolution_id
     ORDER BY enriched.created_at_ms DESC
     LIMIT 1
  ) event_anchor ON true
  LEFT JOIN market_ticks signal_tick
    ON signal_tick.observed_at_ms = event_anchor.tick_observed_at_ms
   AND signal_tick.tick_id = event_anchor.tick_id
   AND signal_tick.target_type = event_anchor.target_type
   AND signal_tick.target_id = event_anchor.target_id
)
SELECT *
  FROM hydrated
 ORDER BY received_at_ms ASC, event_id ASC, target_type ASC, target_id ASC
 LIMIT %s
"""


_TOKEN_RADAR_PRESENTATION_SQL = r"""
WITH requested AS (
  SELECT *
    FROM unnest(%s::text[], %s::text[]) WITH ORDINALITY
      AS requested(target_type, target_id, ordinality)
),
market_keys AS (
  SELECT
    requested.target_type,
    requested.target_id,
    requested.ordinality,
    CASE
      WHEN requested.target_type = 'Asset' AND registry_assets.asset_id IS NOT NULL
        THEN 'chain_token'
      WHEN requested.target_type = 'CexToken' AND preferred_price_feed.pricefeed_id IS NOT NULL
        THEN 'cex_symbol'
    END AS market_target_type,
    CASE
      WHEN requested.target_type = 'Asset'
        THEN registry_assets.chain_id || ':' || registry_assets.address
      WHEN requested.target_type = 'CexToken'
        THEN preferred_price_feed.provider || ':' || preferred_price_feed.native_market_id
    END AS market_target_id
  FROM requested
  LEFT JOIN registry_assets
    ON requested.target_type = 'Asset'
   AND registry_assets.asset_id = requested.target_id
  LEFT JOIN LATERAL (
    SELECT price_feed.*
      FROM price_feeds price_feed
     WHERE requested.target_type = 'CexToken'
       AND price_feed.subject_type = 'CexToken'
       AND price_feed.subject_id = requested.target_id
       AND price_feed.provider = 'binance'
       AND price_feed.feed_type = 'cex_swap'
       AND price_feed.quote_symbol = 'USDT'
       AND price_feed.status = 'canonical'
     ORDER BY
       price_feed.updated_at_ms DESC,
       price_feed.native_market_id ASC,
       price_feed.pricefeed_id ASC
     LIMIT 1
  ) preferred_price_feed ON true
),
recent_market_caps AS (
  SELECT DISTINCT ON (
    market_keys.market_target_type,
    market_keys.market_target_id
  )
    market_keys.market_target_type,
    market_keys.market_target_id,
    market_ticks.market_cap_usd,
    market_ticks.observed_at_ms AS market_cap_observed_at_ms
  FROM market_keys
  JOIN market_ticks
    ON market_ticks.target_type = market_keys.market_target_type
   AND market_ticks.target_id = market_keys.market_target_id
  WHERE market_keys.market_target_type IS NOT NULL
    AND market_keys.market_target_id IS NOT NULL
    AND market_ticks.market_cap_usd > 0
    AND market_ticks.observed_at_ms >= %s
    AND market_ticks.observed_at_ms <= %s
  ORDER BY
    market_keys.market_target_type,
    market_keys.market_target_id,
    market_ticks.observed_at_ms DESC,
    market_ticks.received_at_ms DESC,
    market_ticks.tick_id DESC
)
SELECT
  market_keys.target_type,
  market_keys.target_id,
  token_profile_current.name,
  token_profile_current.logo_url,
  market_tick_current.price_usd,
  market_tick_current.tick_observed_at_ms AS price_observed_at_ms,
  recent_market_caps.market_cap_usd,
  recent_market_caps.market_cap_observed_at_ms
FROM market_keys
LEFT JOIN token_profile_current
  ON token_profile_current.target_type = market_keys.target_type
 AND token_profile_current.target_id = market_keys.target_id
LEFT JOIN market_tick_current
  ON market_tick_current.target_type = market_keys.market_target_type
 AND market_tick_current.target_id = market_keys.market_target_id
LEFT JOIN recent_market_caps
  ON recent_market_caps.market_target_type = market_keys.market_target_type
 AND recent_market_caps.market_target_id = market_keys.market_target_id
ORDER BY market_keys.ordinality
"""


__all__ = [
    "TokenRadarCurrentRepository",
    "TokenRadarPublicationResult",
]
