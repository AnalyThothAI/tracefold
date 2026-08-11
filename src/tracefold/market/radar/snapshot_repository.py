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
            while batch := cursor.fetchmany(256):
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
            SELECT state_fingerprint, latest_attempt_status, latest_error_code,
                   state_changed_at_ms, served_payload
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
        evaluation_at_ms: int,
    ) -> TokenRadarPublicationResult:
        require_transaction(self.conn, operation="publish_token_radar_current")
        current = self.conn.execute(
            """
            SELECT ruleset_version, ruleset_fingerprint, state_fingerprint,
                   latest_attempt_status, latest_error_code, evaluation_at_ms,
                   state_changed_at_ms
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
        semantics_unchanged = (
            current.get("ruleset_version") == reduced.ruleset_version
            and current.get("ruleset_fingerprint") == reduced.ruleset_fingerprint
        )
        if state_unchanged and semantics_unchanged and str(current["latest_attempt_status"]) == "ready":
            return {"status": "unchanged", "rows_written": 0}
        current_public_state = _public_state_key(current)
        state_changed_at_ms = (
            int(current["state_changed_at_ms"]) if current_public_state == ("current", None) else int(evaluation_at_ms)
        )
        if state_unchanged and semantics_unchanged:
            cursor = self.conn.execute(
                """
                UPDATE token_radar_current
                   SET input_fingerprint = %s,
                       evaluation_at_ms = %s,
                       input_rows = %s,
                       input_bytes = %s,
                       latest_attempt_status = 'ready',
                       latest_error_code = NULL,
                       state_changed_at_ms = %s,
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
                    state_changed_at_ms,
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
                   state_changed_at_ms = %s,
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
                int(reduced.snapshot["social_evidence_as_of_ms"]),
                int(evaluation_at_ms),
                int(reduced.input_rows),
                int(reduced.input_bytes),
                Jsonb(reduced.snapshot),
                state_changed_at_ms,
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
        current = self.conn.execute(
            """
            SELECT state_fingerprint, latest_attempt_status, latest_error_code,
                   evaluation_at_ms, state_changed_at_ms
              FROM token_radar_current
             WHERE singleton_key = true
             FOR UPDATE
            """
        ).fetchone()
        if current is None:
            raise RuntimeError("token_radar_current_singleton_missing")
        if int(current["evaluation_at_ms"]) > int(evaluation_at_ms):
            return 0
        current_public_state = _public_state_key(current)
        failed_public_state = (
            ("stale", _public_failure_reason(code))
            if current.get("state_fingerprint") is not None
            else ("unavailable", None)
        )
        if str(current["latest_attempt_status"]) == "failed" and current_public_state == failed_public_state:
            return 0
        state_changed_at_ms = (
            int(evaluation_at_ms)
            if current_public_state != failed_public_state
            else int(current["state_changed_at_ms"])
        )
        cursor = self.conn.execute(
            """
            UPDATE token_radar_current
               SET evaluation_at_ms = %s,
                   latest_attempt_status = 'failed',
                   latest_error_code = %s,
                   failure_count = failure_count + 1,
                   state_changed_at_ms = %s,
                   updated_at_ms = %s
             WHERE singleton_key = true
               AND evaluation_at_ms <= %s
            """,
            (
                int(evaluation_at_ms),
                code,
                state_changed_at_ms,
                int(evaluation_at_ms),
                int(evaluation_at_ms),
            ),
        )
        return mutation_count(cursor, error_code="token_radar_current_failure_count_invalid")


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
        text=row.get("text"),
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
  event.event_id,
  intent.intent_id,
  resolution.resolution_id,
  event.timestamp_ms AS source_event_at_ms,
  event.received_at_ms,
  event.created_at_ms AS event_created_at_ms,
  event.action,
  event.author_handle,
  COALESCE(event.text_clean, event.search_text, event.text) AS text,
  resolution.resolution_status,
  resolution.target_type,
  resolution.target_id,
  resolution.decision_time_ms AS resolution_decision_at_ms,
  resolution.created_at_ms AS resolution_created_at_ms
FROM events event
JOIN token_intents intent
  ON intent.event_id = event.event_id
JOIN token_intent_resolutions resolution
  ON resolution.intent_id = intent.intent_id
 AND resolution.event_id = event.event_id
WHERE event.timestamp_ms > %s
  AND event.timestamp_ms <= %s
  AND event.source_provider = '{TOKEN_RADAR_SEMANTICS.source_provider}'
  AND event.source_transport = '{TOKEN_RADAR_SEMANTICS.source_transport}'
  AND event.coverage = '{TOKEN_RADAR_SEMANTICS.source_coverage}'
  AND event.channel IN (
    {_code_owned_sql_literals(TOKEN_RADAR_SEMANTICS.source_channels)}
  )
  AND event.action IN ({_code_owned_sql_literals(TOKEN_RADAR_SEMANTICS.actions)})
ORDER BY
  event.timestamp_ms ASC,
  event.event_id ASC,
  intent.intent_id ASC,
  resolution.decision_time_ms ASC,
  resolution.created_at_ms ASC,
  resolution.resolution_id ASC
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
LEFT JOIN recent_market_caps
  ON recent_market_caps.market_target_type = market_keys.market_target_type
 AND recent_market_caps.market_target_id = market_keys.market_target_id
ORDER BY market_keys.ordinality
"""


def _served_data(row: Any) -> dict[str, Any]:
    payload = row.get("served_payload")
    if not isinstance(payload, dict):
        raise RuntimeError("token_radar_current_payload_invalid")
    if payload.get("schema_version") != TOKEN_RADAR_SNAPSHOT_SCHEMA_VERSION:
        raise RuntimeError("token_radar_current_schema_invalid")

    state, stale_reason = _public_state_key(row)
    business: dict[str, Any]
    if state == "unavailable":
        business = {
            "schema_version": TOKEN_RADAR_SNAPSHOT_SCHEMA_VERSION,
            "social_evidence_as_of_ms": 0,
            "eligible_total": 0,
            "items": [],
        }
    else:
        business = payload

    return {
        "schema_version": TOKEN_RADAR_SNAPSHOT_SCHEMA_VERSION,
        "state": state,
        "stale_reason": stale_reason,
        "state_changed_at_ms": int(row.get("state_changed_at_ms") or 0),
        "social_evidence_as_of_ms": int(business["social_evidence_as_of_ms"]),
        "eligible_total": int(business["eligible_total"]),
        "items": list(business["items"]),
    }


def _public_failure_reason(error_code: Any) -> str:
    return "source_unavailable" if str(error_code or "") == "token_radar_source_unavailable" else "projection_failed"


def _public_state_key(row: Any) -> tuple[str, str | None]:
    if row.get("state_fingerprint") is None:
        return ("unavailable", None)
    status = str(row.get("latest_attempt_status") or "")
    if status == "ready":
        return ("current", None)
    if status == "failed":
        return ("stale", _public_failure_reason(row.get("latest_error_code")))
    raise RuntimeError("token_radar_current_status_invalid")


__all__ = [
    "TokenRadarCurrentRepository",
    "TokenRadarPublicationResult",
]
