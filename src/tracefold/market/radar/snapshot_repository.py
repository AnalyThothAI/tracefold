from __future__ import annotations

from typing import Any, TypedDict

from psycopg.types.json import Jsonb

from tracefold.market.pricing.live_market import LIVE_MARKET_STALE_AFTER_MS
from tracefold.market.radar.constants import (
    TOKEN_RADAR_INPUT_BYTE_CAP,
    TOKEN_RADAR_INPUT_ROW_CAP,
    TOKEN_RADAR_SEMANTICS,
    TOKEN_RADAR_SNAPSHOT_SCHEMA_VERSION,
    TOKEN_RADAR_SOURCE_HORIZON_MS,
)
from tracefold.market.radar.reducer import (
    RadarEvidenceRevision,
    RadarSelectionKey,
    ReducedTokenRadar,
    TokenRadarInputOverflow,
    token_radar_input_row_size,
)
from tracefold.platform.postgres.postgres_client import require_transaction
from tracefold.platform.postgres.write_contract import mutation_count


class TokenRadarPublicationResult(TypedDict):
    status: str
    rows_written: int


class TokenRadarCurrentRepository:
    """One persistence seam for material-fact input and compact current state."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def load_material_inputs(self, *, now_ms: int) -> list[RadarEvidenceRevision]:
        revisions: list[RadarEvidenceRevision] = []
        input_bytes = 2
        with self.conn.cursor(name="token_radar_material_inputs") as cursor:
            cursor.execute(
                _TOKEN_RADAR_INPUT_SQL,
                (
                    max(0, int(now_ms) - TOKEN_RADAR_SOURCE_HORIZON_MS),
                    int(now_ms),
                    TOKEN_RADAR_INPUT_ROW_CAP + 1,
                ),
            )
            while batch := cursor.fetchmany(4_096):
                for raw in batch:
                    if len(revisions) >= TOKEN_RADAR_INPUT_ROW_CAP:
                        raise TokenRadarInputOverflow("token_radar_input_row_overflow")
                    revision = _material_revision(raw)
                    input_bytes += token_radar_input_row_size(revision) + int(bool(revisions))
                    if input_bytes > TOKEN_RADAR_INPUT_BYTE_CAP:
                        raise TokenRadarInputOverflow("token_radar_input_byte_overflow")
                    revisions.append(revision)
        return revisions

    def load_presentation_facts(
        self,
        selections: list[RadarSelectionKey],
        *,
        now_ms: int,
    ) -> list[dict[str, Any]]:
        parsed_now_ms = int(now_ms)
        if parsed_now_ms < 0:
            raise ValueError("token_radar_presentation_now_ms_invalid")
        requested = list(dict.fromkeys(selections))
        if not requested:
            return []
        if any(
            selection.target_type not in {"Asset", "CexToken"} or not selection.target_id for selection in requested
        ):
            raise ValueError("token_radar_presentation_selection_invalid")
        if len({(selection.target_type, selection.target_id) for selection in requested}) != len(requested):
            raise ValueError("token_radar_presentation_target_duplicate")
        rows = self.conn.execute(
            _TOKEN_RADAR_PRESENTATION_SQL,
            (
                [selection.target_type for selection in requested],
                [selection.target_id for selection in requested],
                [selection.trigger_event_id for selection in requested],
                [selection.trigger_intent_id for selection in requested],
                [selection.trigger_resolution_id for selection in requested],
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
        return _served_data(row)

    def publish(
        self,
        reduced: ReducedTokenRadar,
        *,
        updated_at_ms: int,
    ) -> TokenRadarPublicationResult:
        require_transaction(self.conn, operation="publish_token_radar_current")
        current = self.conn.execute(
            """
            SELECT snapshot_fingerprint
              FROM token_radar_current
             WHERE singleton_key = true
             FOR UPDATE
            """
        ).fetchone()
        if current is None:
            raise RuntimeError("token_radar_current_singleton_missing")
        if current.get("snapshot_fingerprint") == reduced.snapshot_fingerprint:
            return {"status": "unchanged", "rows_written": 0}
        cursor = self.conn.execute(
            """
            UPDATE token_radar_current
               SET snapshot_fingerprint = %s,
                   served_payload = %s,
                   updated_at_ms = %s
             WHERE singleton_key = true
            """,
            (
                reduced.snapshot_fingerprint,
                Jsonb(reduced.snapshot),
                int(updated_at_ms),
            ),
        )
        changed = mutation_count(cursor, error_code="token_radar_current_publish_count_invalid")
        return {
            "status": "published",
            "rows_written": changed,
        }


def _material_revision(row: Any) -> RadarEvidenceRevision:
    return RadarEvidenceRevision(
        event_id=row.get("event_id"),
        intent_id=row.get("intent_id"),
        resolution_id=row.get("resolution_id"),
        source_event_at_ms=row.get("source_event_at_ms"),
        received_at_ms=row.get("received_at_ms"),
        event_created_at_ms=row.get("event_created_at_ms"),
        action=row.get("action"),
        author_key=row.get("author_handle"),
        text_fingerprint=row.get("text_fingerprint"),
        resolution_status=row.get("resolution_status"),
        target_type=row.get("target_type"),
        target_id=row.get("target_id"),
        resolution_decision_at_ms=row.get("resolution_decision_at_ms"),
        resolution_created_at_ms=row.get("resolution_created_at_ms"),
    )


def _code_owned_sql_literals(values: tuple[str, ...]) -> str:
    return ",\n    ".join(f"'{value}'" for value in values)


_TOKEN_RADAR_INPUT_SQL = rf"""
SELECT
  candidate.event_id,
  selected.intent_id,
  selected.resolution_id,
  candidate.timestamp_ms AS source_event_at_ms,
  candidate.received_at_ms,
  candidate.created_at_ms AS event_created_at_ms,
  candidate.action,
  candidate.author_handle,
  candidate.text_fingerprint,
  selected.resolution_status,
  selected.target_type,
  selected.target_id,
  selected.decision_time_ms AS resolution_decision_at_ms,
  selected.created_at_ms AS resolution_created_at_ms
FROM (
  SELECT
    source.event_id,
    source.timestamp_ms,
    source.received_at_ms,
    source.created_at_ms,
    source.action,
    source.author_handle,
    source.token_radar_text_fingerprint AS text_fingerprint
    FROM events source
   WHERE source.timestamp_ms > %s
     AND source.timestamp_ms <= %s
     AND source.source_provider = '{TOKEN_RADAR_SEMANTICS.source_provider}'
     AND source.source_transport = '{TOKEN_RADAR_SEMANTICS.source_transport}'
     AND source.coverage = '{TOKEN_RADAR_SEMANTICS.source_coverage}'
     AND source.channel IN (
       {_code_owned_sql_literals(TOKEN_RADAR_SEMANTICS.source_channels)}
     )
     AND source.action IN ({_code_owned_sql_literals(TOKEN_RADAR_SEMANTICS.actions)})
   ORDER BY source.timestamp_ms ASC, source.event_id ASC
   OFFSET 0
) candidate
CROSS JOIN LATERAL (
  SELECT
    intent.intent_id,
    resolution.resolution_id,
    resolution.resolution_status,
    resolution.target_type,
    resolution.target_id,
    resolution.decision_time_ms,
    resolution.created_at_ms
  FROM token_intents intent
  JOIN token_intent_resolutions resolution
    ON resolution.intent_id = intent.intent_id
   AND resolution.event_id = candidate.event_id
  WHERE intent.event_id = candidate.event_id
  ORDER BY
    intent.intent_id ASC,
    resolution.decision_time_ms ASC,
    resolution.created_at_ms ASC,
    resolution.resolution_id ASC
  OFFSET 0
) selected
ORDER BY
  candidate.timestamp_ms ASC,
  candidate.event_id ASC,
  selected.intent_id ASC,
  selected.decision_time_ms ASC,
  selected.created_at_ms ASC,
  selected.resolution_id ASC
LIMIT %s
"""


_TOKEN_RADAR_PRESENTATION_SQL = r"""
WITH requested AS (
  SELECT *
    FROM unnest(
      %s::text[], %s::text[], %s::text[], %s::text[], %s::text[]
    ) WITH ORDINALITY AS requested(
      target_type,
      target_id,
      trigger_event_id,
      trigger_intent_id,
      trigger_resolution_id,
      ordinality
    )
),
market_keys AS (
  SELECT
    requested.*,
    COALESCE(
      CASE WHEN requested.target_type = 'Asset'
           THEN asset_identity_current.canonical_symbol END,
      CASE WHEN requested.target_type = 'CexToken'
           THEN cex_tokens.base_symbol END,
      trigger_intent.display_symbol
    ) AS symbol,
    CASE WHEN requested.target_type = 'Asset'
         THEN registry_assets.chain_id END AS chain,
    CASE WHEN requested.target_type = 'CexToken'
         THEN preferred_price_feed.provider END AS exchange,
    CASE WHEN requested.target_type = 'Asset'
         THEN registry_assets.address END AS address,
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
  LEFT JOIN asset_identity_current
    ON requested.target_type = 'Asset'
   AND asset_identity_current.asset_id = requested.target_id
  LEFT JOIN cex_tokens
    ON requested.target_type = 'CexToken'
   AND cex_tokens.cex_token_id = requested.target_id
  LEFT JOIN token_intents trigger_intent
    ON trigger_intent.event_id = requested.trigger_event_id
   AND trigger_intent.intent_id = requested.trigger_intent_id
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
)
SELECT
  market_keys.target_type,
  market_keys.target_id,
  market_keys.symbol,
  market_keys.chain,
  market_keys.exchange,
  market_keys.address,
  token_profile_current.name,
  token_profile_current.logo_url,
  signal_tick.price_usd AS signal_price_usd,
  market_tick_current.price_usd,
  market_tick_current.tick_observed_at_ms AS price_observed_at_ms,
  recent_market_caps.market_cap_usd,
  recent_market_caps.market_cap_observed_at_ms
FROM market_keys
LEFT JOIN token_profile_current
  ON token_profile_current.target_type = market_keys.target_type
 AND token_profile_current.target_id = market_keys.target_id
LEFT JOIN LATERAL (
  SELECT enriched.tick_observed_at_ms,
         enriched.tick_id,
         enriched.target_type,
         enriched.target_id
    FROM enriched_events enriched
   WHERE enriched.event_id = market_keys.trigger_event_id
     AND enriched.intent_id = market_keys.trigger_intent_id
     AND enriched.resolution_id = market_keys.trigger_resolution_id
   ORDER BY enriched.created_at_ms DESC
   LIMIT 1
) trigger_anchor ON true
LEFT JOIN market_ticks signal_tick
  ON signal_tick.observed_at_ms = trigger_anchor.tick_observed_at_ms
 AND signal_tick.tick_id = trigger_anchor.tick_id
 AND signal_tick.target_type = trigger_anchor.target_type
 AND signal_tick.target_id = trigger_anchor.target_id
LEFT JOIN market_tick_current
  ON market_tick_current.target_type = market_keys.market_target_type
 AND market_tick_current.target_id = market_keys.market_target_id
LEFT JOIN LATERAL (
  SELECT
    recent_cap_tick.market_cap_usd,
    recent_cap_tick.observed_at_ms AS market_cap_observed_at_ms
  FROM market_ticks AS recent_cap_tick
  WHERE recent_cap_tick.target_type = market_keys.market_target_type
    AND recent_cap_tick.target_id = market_keys.market_target_id
    AND recent_cap_tick.market_cap_usd > 0
    AND recent_cap_tick.observed_at_ms >= %s
    AND recent_cap_tick.observed_at_ms <= %s
  ORDER BY
    recent_cap_tick.observed_at_ms DESC,
    recent_cap_tick.received_at_ms DESC,
    recent_cap_tick.tick_id DESC
  LIMIT 1
) recent_market_caps ON true
ORDER BY market_keys.ordinality
"""


def _served_data(row: Any) -> dict[str, Any]:
    payload = row.get("served_payload")
    if not isinstance(payload, dict):
        raise RuntimeError("token_radar_current_payload_invalid")
    if payload.get("schema_version") != TOKEN_RADAR_SNAPSHOT_SCHEMA_VERSION:
        raise RuntimeError("token_radar_current_schema_invalid")
    return {
        "schema_version": TOKEN_RADAR_SNAPSHOT_SCHEMA_VERSION,
        "social_evidence_as_of_ms": int(payload["social_evidence_as_of_ms"]),
        "eligible_total": int(payload["eligible_total"]),
        "items": list(payload["items"]),
    }


__all__ = [
    "TokenRadarCurrentRepository",
    "TokenRadarPublicationResult",
]
