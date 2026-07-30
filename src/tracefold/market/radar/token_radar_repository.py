from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from typing import Any, TypedDict

from psycopg.types.json import Jsonb

from tracefold.market.radar.constants import (
    TOKEN_FACTOR_SNAPSHOT_VERSION,
    TOKEN_RADAR_DECISIONS,
)
from tracefold.market.radar.factor_snapshot_contract import require_token_factor_snapshot
from tracefold.market.radar.output_envelope import split_bounded_rows
from tracefold.market.radar.payload_hash import (
    canonical_token_radar_payload,
    stable_token_radar_payload_hash,
)
from tracefold.platform.postgres.postgres_client import require_transaction
from tracefold.platform.postgres.write_contract import mutation_count
from tracefold.platform.validation import require_nonnegative_int, require_positive_int

RADAR_ROW_COLUMNS = (
    "row_id",
    "projection_version",
    "window",
    "venue",
    "computed_at_ms",
    "source_max_received_at_ms",
    "generation_id",
    "published_at_ms",
    "source_frontier_ms",
    "lane",
    "target_type_key",
    "identity_id",
    "rank",
    "rank_score",
    "intent_id",
    "event_id",
    "target_type",
    "target_id",
    "pricefeed_id",
    "intent_json",
    "resolution_json",
    "factor_snapshot_json",
    "factor_version",
    "decision",
    "quality_status",
    "degraded_reasons_json",
    "data_health_json",
    "source_event_ids_json",
    "payload_hash",
    "listed_at_ms",
    "created_at_ms",
)
TARGET_FEATURE_PAYLOAD_LANES = frozenset({"attention", "resolved"})
RADAR_ROW_INSERT_COLUMNS_SQL = """
  row_id, projection_version, "window", venue, computed_at_ms, source_max_received_at_ms,
  generation_id, published_at_ms, source_frontier_ms,
  lane, target_type_key, identity_id, rank, rank_score, intent_id, event_id, target_type, target_id,
  pricefeed_id, intent_json, resolution_json, factor_snapshot_json,
  factor_version, decision, quality_status, degraded_reasons_json,
  data_health_json, source_event_ids_json, payload_hash,
  listed_at_ms, created_at_ms
"""
_PUBLICATION_BATCH_BYTE_CAP = 1 * 1024 * 1024


class PublicationResult(TypedDict):
    status: str
    generation_id: str
    rows_written: int


class TokenRadarRepository:
    def __init__(self, conn: Any):
        self.conn = conn

    def publish_current_generation(
        self,
        *,
        projection_version: str,
        window: str,
        venue: str,
        generation_id: str,
        published_at_ms: int,
        source_frontier_ms: int,
        rows: list[dict[str, Any]],
        source_rows: int | None = None,
        started_at_ms: int | None = None,
        finished_at_ms: int | None = None,
        on_current_changes: Callable[..., None] | None = None,
    ) -> PublicationResult:
        require_transaction(self.conn, operation="publish_token_radar_current_generation")
        self.conn.execute(
            """
            SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))
            """,
            (projection_version, f"{window}:{venue}"),
        )
        latest = self.conn.execute(
            """
            SELECT current_generation_id, current_published_at_ms
            FROM token_radar_publication_state
            WHERE projection_version = %s
              AND "window" = %s
              AND venue = %s
            """,
            (projection_version, window, venue),
        ).fetchone()
        latest_current_generation_id = (
            str(latest["current_generation_id"]) if latest and latest.get("current_generation_id") is not None else None
        )
        latest_published_at_ms = (
            int(latest["current_published_at_ms"])
            if latest and latest.get("current_published_at_ms") is not None
            else None
        )
        if latest_published_at_ms is not None and latest_published_at_ms > int(published_at_ms):
            return {"status": "stale_skipped", "generation_id": str(generation_id), "rows_written": 0}

        for row in rows:
            _validate_factor_contract(row)
        existing_current = self._current_rows_for_projection_set(
            projection_version=projection_version,
            window=window,
            venue=venue,
        )
        listed_at_by_key = self.first_seen_by_identity(
            projection_version=projection_version,
            window=window,
            venue=venue,
            rows=rows,
        )
        rows_to_insert = [
            _runtime_row_payload(
                row,
                projection_version=projection_version,
                window=window,
                venue=venue,
                generation_id=generation_id,
                published_at_ms=int(published_at_ms),
                source_frontier_ms=int(source_frontier_ms),
                listed_at_ms=listed_at_by_key.get(_identity_key(row), int(published_at_ms)),
            )
            for row in rows
        ]
        existing_generation_id = latest_current_generation_id or _first_generation_id(existing_current)
        existing_signature = stable_generation_id(
            projection_version=projection_version,
            window=window,
            venue=venue,
            rows=existing_current,
        )
        incoming_signature = stable_generation_id(
            projection_version=projection_version,
            window=window,
            venue=venue,
            rows=rows_to_insert,
        )
        if existing_generation_id is not None and existing_signature == incoming_signature:
            self._upsert_ready_publication_state(
                projection_version=projection_version,
                window=window,
                venue=venue,
                generation_id=existing_generation_id,
                published_at_ms=published_at_ms,
                source_frontier_ms=source_frontier_ms,
                row_count=len(rows_to_insert),
                source_rows=source_rows,
                started_at_ms=started_at_ms,
                finished_at_ms=finished_at_ms,
            )
            return {"status": "unchanged", "generation_id": existing_generation_id, "rows_written": 0}

        existing_by_key = {_current_key(row): row for row in existing_current}
        current_keys = {_current_key(row) for row in rows_to_insert}
        exited_rows = [row for key, row in existing_by_key.items() if key not in current_keys]
        rows_written = self._delete_current_rows_batch(
            projection_version=projection_version,
            window=window,
            venue=venue,
            rows=exited_rows,
        )
        rows_written += self._upsert_current_rows_batch(rows_to_insert)
        self.upsert_first_seen_batch(
            projection_version=projection_version,
            window=window,
            venue=venue,
            rows=rows_to_insert,
            computed_at_ms=int(published_at_ms),
        )
        self._upsert_ready_publication_state(
            projection_version=projection_version,
            window=window,
            venue=venue,
            generation_id=generation_id,
            published_at_ms=published_at_ms,
            source_frontier_ms=source_frontier_ms,
            row_count=len(rows_to_insert),
            source_rows=source_rows,
            started_at_ms=started_at_ms,
            finished_at_ms=finished_at_ms,
        )
        if on_current_changes is not None:
            on_current_changes(
                window=window,
                venue=venue,
                rows=rows_to_insert,
                exited_rows=exited_rows,
                previous_by_key=existing_by_key,
                computed_at_ms=int(published_at_ms),
            )
        return {"status": "published", "generation_id": str(generation_id), "rows_written": rows_written}

    def _delete_current_rows_batch(
        self,
        *,
        projection_version: str,
        window: str,
        venue: str,
        rows: list[dict[str, Any]],
    ) -> int:
        if not rows:
            return 0
        keys = [_current_key(row) for row in rows]
        cursor = self.conn.execute(
            """
            WITH keys(lane, target_type_key, identity_id) AS (
              SELECT *
              FROM unnest(%s::text[], %s::text[], %s::text[])
            )
            DELETE FROM token_radar_current_rows current
            USING keys
            WHERE current.projection_version = %s
              AND current."window" = %s
              AND current.venue = %s
              AND current.lane = keys.lane
              AND current.target_type_key = keys.target_type_key
              AND current.identity_id = keys.identity_id
            """,
            (
                [lane for lane, _target_type, _identity_id in keys],
                [target_type for _lane, target_type, _identity_id in keys],
                [identity_id for _lane, _target_type, identity_id in keys],
                projection_version,
                window,
                venue,
            ),
        )
        return mutation_count(cursor, error_code="token_radar_repository_rowcount_invalid")

    def _upsert_current_rows_batch(
        self,
        rows: list[dict[str, Any]],
    ) -> int:
        if not rows:
            return 0
        rows_written = 0
        for batch in split_bounded_rows(
            rows,
            context={"output_kind": "token_radar_current_rows"},
            byte_cap=_PUBLICATION_BATCH_BYTE_CAP,
        ):
            payloads = [_json_payload(row) for row in batch]
            row_placeholders = f"({', '.join(['%s'] * len(RADAR_ROW_COLUMNS))})"
            values_sql = ", ".join([row_placeholders] * len(payloads))
            params = [payload[column] for payload in payloads for column in RADAR_ROW_COLUMNS]
            cursor = self.conn.execute(
                f"""
                INSERT INTO token_radar_current_rows({RADAR_ROW_INSERT_COLUMNS_SQL})
                VALUES {values_sql}
                ON CONFLICT(projection_version, "window", venue, lane, target_type_key, identity_id)
                DO UPDATE SET
                  row_id = excluded.row_id,
                  computed_at_ms = excluded.computed_at_ms,
                  source_max_received_at_ms = excluded.source_max_received_at_ms,
                  generation_id = excluded.generation_id,
                  published_at_ms = excluded.published_at_ms,
                  source_frontier_ms = excluded.source_frontier_ms,
                  rank = excluded.rank,
                  rank_score = excluded.rank_score,
                  intent_id = excluded.intent_id,
                  event_id = excluded.event_id,
                  target_type = excluded.target_type,
                  target_id = excluded.target_id,
                  pricefeed_id = excluded.pricefeed_id,
                  intent_json = excluded.intent_json,
                  resolution_json = excluded.resolution_json,
                  factor_snapshot_json = excluded.factor_snapshot_json,
                  factor_version = excluded.factor_version,
                  decision = excluded.decision,
                  quality_status = excluded.quality_status,
                  degraded_reasons_json = excluded.degraded_reasons_json,
                  data_health_json = excluded.data_health_json,
                  source_event_ids_json = excluded.source_event_ids_json,
                  payload_hash = excluded.payload_hash,
                  listed_at_ms = excluded.listed_at_ms
                WHERE token_radar_current_rows.payload_hash IS DISTINCT FROM excluded.payload_hash
                   OR token_radar_current_rows.quality_status IS DISTINCT FROM excluded.quality_status
                   OR token_radar_current_rows.rank IS DISTINCT FROM excluded.rank
                   OR token_radar_current_rows.decision IS DISTINCT FROM excluded.decision
                """,
                params,
            )
            rows_written += mutation_count(
                cursor,
                error_code="token_radar_repository_rowcount_invalid",
            )
        return rows_written

    def _upsert_ready_publication_state(
        self,
        *,
        projection_version: str,
        window: str,
        venue: str,
        generation_id: str,
        published_at_ms: int,
        source_frontier_ms: int,
        row_count: int,
        source_rows: int | None,
        started_at_ms: int | None,
        finished_at_ms: int | None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO token_radar_publication_state(
              projection_version, "window", venue, current_generation_id, current_published_at_ms,
              current_source_frontier_ms, current_row_count, current_source_rows,
              latest_attempt_generation_id, latest_attempt_status, latest_attempt_started_at_ms,
              latest_attempt_finished_at_ms, latest_attempt_error, updated_at_ms
            )
            VALUES (
              %(projection_version)s, %(window)s, %(venue)s, %(current_generation_id)s,
              %(current_published_at_ms)s, %(current_source_frontier_ms)s, %(current_row_count)s,
              %(current_source_rows)s, %(latest_attempt_generation_id)s, %(latest_attempt_status)s,
              %(latest_attempt_started_at_ms)s, %(latest_attempt_finished_at_ms)s,
              %(latest_attempt_error)s, %(updated_at_ms)s
            )
            ON CONFLICT(projection_version, "window", venue) DO UPDATE SET
              current_generation_id = excluded.current_generation_id,
              current_published_at_ms = excluded.current_published_at_ms,
              current_source_frontier_ms = excluded.current_source_frontier_ms,
              current_row_count = excluded.current_row_count,
              current_source_rows = excluded.current_source_rows,
              latest_attempt_generation_id = excluded.latest_attempt_generation_id,
              latest_attempt_status = excluded.latest_attempt_status,
              latest_attempt_started_at_ms = excluded.latest_attempt_started_at_ms,
              latest_attempt_finished_at_ms = excluded.latest_attempt_finished_at_ms,
              latest_attempt_error = excluded.latest_attempt_error,
              updated_at_ms = excluded.updated_at_ms
            WHERE token_radar_publication_state.current_published_at_ms IS NULL
               OR token_radar_publication_state.current_published_at_ms <= excluded.current_published_at_ms
            """,
            {
                "projection_version": projection_version,
                "window": window,
                "venue": venue,
                "current_generation_id": str(generation_id),
                "current_published_at_ms": int(published_at_ms),
                "current_source_frontier_ms": int(source_frontier_ms),
                "current_row_count": max(0, int(row_count)),
                "current_source_rows": max(0, int(source_rows if source_rows is not None else row_count)),
                "latest_attempt_generation_id": str(generation_id),
                "latest_attempt_status": "ready",
                "latest_attempt_started_at_ms": int(started_at_ms) if started_at_ms is not None else None,
                "latest_attempt_finished_at_ms": (
                    int(finished_at_ms) if finished_at_ms is not None else int(published_at_ms)
                ),
                "latest_attempt_error": None,
                "updated_at_ms": _now_ms(),
            },
        )

    def _current_rows_for_projection_set(
        self,
        *,
        projection_version: str,
        window: str,
        venue: str,
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT *
            FROM token_radar_current_rows
            WHERE projection_version = %s
              AND "window" = %s
              AND venue = %s
            """,
            (projection_version, window, venue),
        ).fetchall()
        return [dict(row) for row in rows]

    def latest_current_rows(
        self,
        *,
        window: str,
        venue: str,
        limit: int,
        projection_version: str,
    ) -> list[dict[str, Any]]:
        row_limit = require_nonnegative_int(
            limit,
            error_code="token_radar_latest_current_rows_limit_required",
        )
        rows = self.conn.execute(
            """
            WITH ranked AS (
              SELECT *
              FROM (
                SELECT
                  current_rows.*,
                  row_number() OVER (PARTITION BY lane ORDER BY rank ASC) AS lane_rank
                FROM token_radar_current_rows current_rows
                JOIN token_radar_publication_state state
                 ON state.projection_version = current_rows.projection_version
                 AND state."window" = current_rows."window"
                 AND state.venue = current_rows.venue
                WHERE current_rows.projection_version = %s
                  AND current_rows."window" = %s
                  AND current_rows.venue = %s
                  AND state.current_generation_id IS NOT NULL
              ) latest_ranked
              WHERE lane_rank <= %s
            )
            SELECT ranked.*
            FROM ranked
            ORDER BY lane DESC, rank ASC
            LIMIT %s
            """,
            (
                projection_version,
                window,
                venue,
                row_limit,
                row_limit * 2,
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    def current_row_for_target(
        self,
        *,
        projection_version: str,
        window: str,
        venue: str,
        target_type: str,
        target_id: str,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT current_rows.*
            FROM token_radar_current_rows current_rows
            JOIN token_radar_publication_state state
             ON state.projection_version = current_rows.projection_version
             AND state."window" = current_rows."window"
             AND state.venue = current_rows.venue
            WHERE current_rows.projection_version = %s
              AND current_rows."window" = %s
              AND current_rows.venue = %s
              AND current_rows.target_type = %s
              AND current_rows.target_id = %s
              AND state.current_generation_id IS NOT NULL
            ORDER BY current_rows.lane DESC, current_rows.rank ASC
            LIMIT 1
            """,
            (projection_version, window, venue, target_type, target_id),
        ).fetchone()
        return dict(row) if row is not None else None

    def upsert_target_feature(
        self,
        *,
        projection_version: str,
        window: str,
        row: dict[str, Any],
        computed_at_ms: int,
    ) -> int:
        require_transaction(self.conn, operation="upsert_token_radar_target_feature")
        _validate_factor_contract(row)
        payload = _target_feature_payload(
            row,
            projection_version=projection_version,
            window=window,
            computed_at_ms=int(computed_at_ms),
        )
        cursor = self.conn.execute(
            """
            INSERT INTO token_radar_target_features(
              projection_version, "window", lane, target_type_key, identity_id,
              target_type, target_id, pricefeed_id, latest_event_received_at_ms,
              latest_market_observed_at_ms, attention_score, market_score, credibility_score,
              rank_score, factor_snapshot_json, intent_json, resolution_json,
              source_event_ids_json, source_intent_ids_json,
              source_resolution_ids_json, payload_hash, last_scored_at_ms, created_at_ms, updated_at_ms,
              social_heat_raw_score, social_heat_weight, social_propagation_raw_score,
              social_propagation_weight, timing_risk_raw_score, timing_risk_weight,
              cohort_high_confidence_mentions,
              cohort_kol_mentions, cohort_followup_authors, cohort_first_seen_global_24h,
              cohort_symbol, social_heat_mentions_1h,
              social_propagation_mentions, social_heat_latest_seen_ms, raw_composite_score,
              recommended_decision, gates_max_decision
            )
            VALUES (
              %(projection_version)s, %(window)s, %(lane)s, %(target_type_key)s, %(identity_id)s,
              %(target_type)s, %(target_id)s, %(pricefeed_id)s, %(latest_event_received_at_ms)s,
              %(latest_market_observed_at_ms)s, %(attention_score)s, %(market_score)s, %(credibility_score)s,
              %(rank_score)s, %(factor_snapshot_json)s, %(intent_json)s, %(resolution_json)s,
              %(source_event_ids_json)s, %(source_intent_ids_json)s,
              %(source_resolution_ids_json)s, %(payload_hash)s, %(last_scored_at_ms)s, %(created_at_ms)s,
              %(updated_at_ms)s, %(social_heat_raw_score)s, %(social_heat_weight)s,
              %(social_propagation_raw_score)s, %(social_propagation_weight)s,
              %(timing_risk_raw_score)s, %(timing_risk_weight)s,
              %(cohort_high_confidence_mentions)s, %(cohort_kol_mentions)s,
              %(cohort_followup_authors)s, %(cohort_first_seen_global_24h)s,
              %(cohort_symbol)s, %(social_heat_mentions_1h)s,
              %(social_propagation_mentions)s, %(social_heat_latest_seen_ms)s,
              %(raw_composite_score)s, %(recommended_decision)s, %(gates_max_decision)s
            )
            ON CONFLICT(projection_version, "window", lane, target_type_key, identity_id)
            DO UPDATE SET
              target_type = excluded.target_type,
              target_id = excluded.target_id,
              pricefeed_id = excluded.pricefeed_id,
              latest_event_received_at_ms = excluded.latest_event_received_at_ms,
              latest_market_observed_at_ms = excluded.latest_market_observed_at_ms,
              attention_score = excluded.attention_score,
              market_score = excluded.market_score,
              credibility_score = excluded.credibility_score,
              rank_score = excluded.rank_score,
              factor_snapshot_json = excluded.factor_snapshot_json,
              intent_json = excluded.intent_json,
              resolution_json = excluded.resolution_json,
              source_event_ids_json = excluded.source_event_ids_json,
              source_intent_ids_json = excluded.source_intent_ids_json,
              source_resolution_ids_json = excluded.source_resolution_ids_json,
              payload_hash = excluded.payload_hash,
              last_scored_at_ms = excluded.last_scored_at_ms,
              updated_at_ms = excluded.updated_at_ms,
              social_heat_raw_score = excluded.social_heat_raw_score,
              social_heat_weight = excluded.social_heat_weight,
              social_propagation_raw_score = excluded.social_propagation_raw_score,
              social_propagation_weight = excluded.social_propagation_weight,
              timing_risk_raw_score = excluded.timing_risk_raw_score,
              timing_risk_weight = excluded.timing_risk_weight,
              cohort_high_confidence_mentions = excluded.cohort_high_confidence_mentions,
              cohort_kol_mentions = excluded.cohort_kol_mentions,
              cohort_followup_authors = excluded.cohort_followup_authors,
              cohort_first_seen_global_24h = excluded.cohort_first_seen_global_24h,
              cohort_symbol = excluded.cohort_symbol,
              social_heat_mentions_1h = excluded.social_heat_mentions_1h,
              social_propagation_mentions = excluded.social_propagation_mentions,
              social_heat_latest_seen_ms = excluded.social_heat_latest_seen_ms,
              raw_composite_score = excluded.raw_composite_score,
              recommended_decision = excluded.recommended_decision,
              gates_max_decision = excluded.gates_max_decision
            WHERE token_radar_target_features.payload_hash IS DISTINCT FROM excluded.payload_hash
            """,
            payload,
        )
        return mutation_count(cursor, error_code="token_radar_repository_rowcount_invalid")

    def delete_target_feature(
        self,
        *,
        projection_version: str,
        window: str,
        lane: str,
        target_type_key: str,
        identity_id: str,
    ) -> int:
        require_transaction(self.conn, operation="delete_token_radar_target_feature")
        cursor = self.conn.execute(
            """
            DELETE FROM token_radar_target_features
            WHERE projection_version = %s
              AND "window" = %s
              AND lane = %s
              AND target_type_key = %s
              AND identity_id = %s
            """,
            (projection_version, window, lane, target_type_key, identity_id),
        )
        return mutation_count(cursor, error_code="token_radar_repository_rowcount_invalid")

    def prune_target_features(
        self,
        *,
        projection_version: str,
        window: str,
        latest_event_before_ms: int,
        limit: int,
    ) -> int:
        require_transaction(self.conn, operation="prune_token_radar_target_features")
        row_limit = require_positive_int(
            limit,
            error_code="token_radar_prune_target_features_limit_required",
        )
        cursor = self.conn.execute(
            """
            DELETE FROM token_radar_target_features
            WHERE ctid IN (
              SELECT ctid
              FROM token_radar_target_features
              WHERE projection_version = %s
                AND "window" = %s
                AND latest_event_received_at_ms < %s
              ORDER BY latest_event_received_at_ms ASC
              LIMIT %s
            )
            """,
            (projection_version, window, int(latest_event_before_ms), row_limit),
        )
        return mutation_count(cursor, error_code="token_radar_repository_rowcount_invalid")

    def list_compact_rank_inputs_for_rank_set(
        self,
        *,
        projection_version: str,
        window: str,
        min_latest_event_received_at_ms: int,
        row_cap: int | None = None,
    ) -> list[dict[str, Any]]:
        parsed_cap = (
            require_positive_int(
                row_cap,
                error_code="token_radar_compact_rank_input_row_cap_required",
            )
            if row_cap is not None
            else None
        )
        limit_sql = "LIMIT %s" if parsed_cap is not None else ""
        params: tuple[Any, ...] = (
            projection_version,
            window,
            int(min_latest_event_received_at_ms),
        )
        if parsed_cap is not None:
            params = (*params, parsed_cap + 1)
        rows = self.conn.execute(
            f"""
            SELECT
              projection_version,
              "window",
              lane,
              target_type_key,
              identity_id,
              target_type,
              target_id,
              pricefeed_id,
              latest_event_received_at_ms,
              latest_market_observed_at_ms,
              social_heat_raw_score,
              social_heat_weight,
              social_propagation_raw_score,
              social_propagation_weight,
              timing_risk_raw_score,
              timing_risk_weight,
              cohort_high_confidence_mentions,
              cohort_kol_mentions,
              cohort_followup_authors,
              cohort_first_seen_global_24h,
              cohort_symbol,
              social_heat_mentions_1h,
              social_propagation_mentions,
              social_heat_latest_seen_ms,
              raw_composite_score,
              recommended_decision,
              gates_max_decision,
              factor_snapshot_json #>> '{{subject,chain}}' AS subject_chain
            FROM token_radar_target_features
            WHERE projection_version = %s
              AND "window" = %s
              AND latest_event_received_at_ms >= %s
            ORDER BY lane DESC, rank_score DESC, latest_event_received_at_ms DESC, identity_id ASC
            {limit_sql}
            """,
            params,
        ).fetchall()
        if parsed_cap is not None and len(rows) > parsed_cap:
            raise RuntimeError("token_radar_compact_rank_input_shard_oversized")
        return [dict(row) for row in rows]

    def hydrate_rank_inputs_for_rank_set(
        self,
        *,
        projection_version: str,
        window: str,
        identities: Sequence[tuple[str, str, str]],
    ) -> list[dict[str, Any]]:
        requested = list(dict.fromkeys(identities))
        if not requested:
            return []
        lanes = [lane for lane, _target_type_key, _identity_id in requested]
        target_type_keys = [target_type_key for _lane, target_type_key, _identity_id in requested]
        identity_ids = [identity_id for _lane, _target_type_key, identity_id in requested]
        rows = self.conn.execute(
            """
            WITH requested(lane, target_type_key, identity_id) AS (
              SELECT *
              FROM unnest(%s::text[], %s::text[], %s::text[])
            )
            SELECT
              feature.projection_version,
              feature."window",
              feature.lane,
              feature.target_type_key,
              feature.identity_id,
              feature.target_type,
              feature.target_id,
              feature.pricefeed_id,
              feature.latest_event_received_at_ms,
              feature.factor_snapshot_json,
              feature.intent_json,
              feature.resolution_json,
              feature.source_event_ids_json,
              feature.source_intent_ids_json,
              feature.source_resolution_ids_json,
              feature.payload_hash,
              feature.last_scored_at_ms
            FROM token_radar_target_features feature
            JOIN requested
              ON requested.lane = feature.lane
             AND requested.target_type_key = feature.target_type_key
             AND requested.identity_id = feature.identity_id
            WHERE feature.projection_version = %s
              AND feature."window" = %s
            """,
            (
                lanes,
                target_type_keys,
                identity_ids,
                projection_version,
                window,
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    def first_seen_by_identity(
        self,
        *,
        projection_version: str,
        window: str,
        venue: str,
        rows: list[dict[str, Any]],
    ) -> dict[tuple[str, str], int]:
        identities = _nonempty_identities(rows)
        if not identities:
            return {}
        target_type_keys = [target_type for target_type, _ in identities]
        identity_ids = [identity_id for _, identity_id in identities]
        compact_rows = self.conn.execute(
            """
            WITH requested(target_type_key, identity_id) AS (
              SELECT *
              FROM unnest(%s::text[], %s::text[])
            )
            SELECT
              requested.target_type_key,
              requested.identity_id,
              first_seen.first_seen_ms
            FROM token_radar_target_first_seen first_seen
            JOIN requested
              ON requested.target_type_key = first_seen.target_type_key
             AND requested.identity_id = first_seen.identity_id
            WHERE first_seen.projection_version = %s
              AND first_seen."window" = %s
              AND first_seen.venue = %s
            """,
            (target_type_keys, identity_ids, projection_version, window, venue),
        ).fetchall()
        return {
            (str(row["target_type_key"]), str(row["identity_id"])): int(row["first_seen_ms"])
            for row in compact_rows
            if row.get("first_seen_ms") is not None
        }

    def upsert_first_seen_batch(
        self,
        *,
        projection_version: str,
        window: str,
        venue: str,
        rows: list[dict[str, Any]],
        computed_at_ms: int,
    ) -> int:
        require_transaction(self.conn, operation="upsert_token_radar_first_seen")
        now_ms = _now_ms()
        records: list[tuple[Any, ...]] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            target_type_key, identity_id = _identity_key(row)
            if not identity_id or (target_type_key, identity_id) in seen:
                continue
            seen.add((target_type_key, identity_id))
            first_seen_ms = int(row.get("listed_at_ms") or computed_at_ms)
            last_seen_ms = int(computed_at_ms)
            row_id = row.get("row_id")
            records.append(
                (
                    projection_version,
                    window,
                    venue,
                    target_type_key,
                    identity_id,
                    first_seen_ms,
                    last_seen_ms,
                    row_id,
                    row_id,
                    now_ms,
                    now_ms,
                )
            )
        if not records:
            return 0
        values_sql = ",".join(["(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"] * len(records))
        params = [value for record in records for value in record]
        cursor = self.conn.execute(
            f"""
            INSERT INTO token_radar_target_first_seen(
              projection_version, "window", venue, target_type_key, identity_id,
              first_seen_ms, last_seen_ms, first_row_id, latest_row_id, created_at_ms, updated_at_ms
            )
            VALUES {values_sql}
            ON CONFLICT(projection_version, "window", venue, target_type_key, identity_id)
            DO UPDATE SET
              first_seen_ms = LEAST(token_radar_target_first_seen.first_seen_ms, excluded.first_seen_ms),
              last_seen_ms = GREATEST(token_radar_target_first_seen.last_seen_ms, excluded.last_seen_ms),
              first_row_id = CASE
                WHEN excluded.first_seen_ms <= token_radar_target_first_seen.first_seen_ms
                  THEN excluded.first_row_id
                ELSE token_radar_target_first_seen.first_row_id
              END,
              latest_row_id = CASE
                WHEN excluded.last_seen_ms >= token_radar_target_first_seen.last_seen_ms
                  THEN excluded.latest_row_id
                ELSE token_radar_target_first_seen.latest_row_id
              END,
              updated_at_ms = excluded.updated_at_ms
            """,
            params,
        )
        return mutation_count(cursor, error_code="token_radar_repository_rowcount_invalid")

    def mark_publication_failed(
        self,
        *,
        projection_version: str,
        window: str,
        venue: str,
        generation_id: str,
        started_at_ms: int | None = None,
        finished_at_ms: int | None = None,
        error: str | None = None,
    ) -> None:
        require_transaction(self.conn, operation="mark_token_radar_publication_failed")
        now_ms = _now_ms()
        self.conn.execute(
            """
            INSERT INTO token_radar_publication_state(
              projection_version, "window", venue, latest_attempt_generation_id, latest_attempt_status,
              latest_attempt_started_at_ms, latest_attempt_finished_at_ms, latest_attempt_error,
              updated_at_ms
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(projection_version, "window", venue) DO UPDATE SET
              latest_attempt_generation_id = excluded.latest_attempt_generation_id,
              latest_attempt_status = excluded.latest_attempt_status,
              latest_attempt_started_at_ms = excluded.latest_attempt_started_at_ms,
              latest_attempt_finished_at_ms = excluded.latest_attempt_finished_at_ms,
              latest_attempt_error = excluded.latest_attempt_error,
              updated_at_ms = excluded.updated_at_ms
            """,
            (
                projection_version,
                window,
                venue,
                str(generation_id),
                "failed",
                int(started_at_ms) if started_at_ms is not None else None,
                int(finished_at_ms) if finished_at_ms is not None else None,
                error,
                now_ms,
            ),
        )

    def latest_publication_state(
        self,
        *,
        projection_version: str,
        windows: tuple[str, ...],
        venues: tuple[str, ...],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        requested = [(window, venue) for window in windows for venue in venues]
        if not requested:
            return {}
        values_sql = ",".join(["(%s, %s)"] * len(requested))
        params: list[Any] = []
        for window, venue in requested:
            params.extend([window, venue])
        rows = self.conn.execute(
            f"""
            WITH requested("window", venue) AS (VALUES {values_sql})
            SELECT state.*
            FROM requested
            JOIN token_radar_publication_state state
              ON state."window" = requested."window"
             AND state.venue = requested.venue
            WHERE state.projection_version = %s
            """,
            [*params, projection_version],
        ).fetchall()
        return {
            (str(row["window"]), str(row["venue"])): {
                "current_generation_id": row.get("current_generation_id"),
                "current_published_at_ms": (
                    int(row["current_published_at_ms"]) if row.get("current_published_at_ms") is not None else None
                ),
                "current_source_frontier_ms": (
                    int(row["current_source_frontier_ms"])
                    if row.get("current_source_frontier_ms") is not None
                    else None
                ),
                "current_row_count": int(row.get("current_row_count") or 0),
                "current_source_rows": int(row.get("current_source_rows") or 0),
                "latest_attempt_generation_id": row.get("latest_attempt_generation_id"),
                "latest_attempt_status": str(row["latest_attempt_status"]),
                "latest_attempt_started_at_ms": (
                    int(row["latest_attempt_started_at_ms"])
                    if row.get("latest_attempt_started_at_ms") is not None
                    else None
                ),
                "latest_attempt_finished_at_ms": (
                    int(row["latest_attempt_finished_at_ms"])
                    if row.get("latest_attempt_finished_at_ms") is not None
                    else None
                ),
                "latest_attempt_error": row.get("latest_attempt_error"),
                "updated_at_ms": int(row["updated_at_ms"]) if row.get("updated_at_ms") is not None else None,
            }
            for row in rows
        }


def _runtime_row_payload(
    row: dict[str, Any],
    *,
    projection_version: str,
    window: str,
    venue: str,
    generation_id: str,
    published_at_ms: int,
    source_frontier_ms: int,
    listed_at_ms: int,
) -> dict[str, Any]:
    out = dict(row)
    target_type_key, identity_id = _identity_key(out)
    out.update(
        {
            "projection_version": projection_version,
            "window": window,
            "venue": venue,
            "computed_at_ms": int(published_at_ms),
            "generation_id": str(generation_id),
            "published_at_ms": int(published_at_ms),
            "source_frontier_ms": int(source_frontier_ms),
            "target_type_key": target_type_key,
            "identity_id": identity_id,
            "listed_at_ms": int(listed_at_ms),
        }
    )
    out["payload_hash"] = _payload_hash(out)
    return out


def _target_feature_payload(
    row: dict[str, Any],
    *,
    projection_version: str,
    window: str,
    computed_at_ms: int,
) -> dict[str, Any]:
    target_type_key, identity_id = _identity_key(row)
    factor_snapshot = _required_target_feature_payload_mapping(row, "factor_snapshot_json")
    intent = _required_target_feature_payload_mapping(row, "intent_json")
    resolution = _required_target_feature_payload_mapping(row, "resolution_json")
    intent_id = _required_target_feature_payload_text(row, "intent_id")
    event_id = _required_target_feature_payload_text(row, "event_id")
    if _required_target_feature_payload_text(intent, "intent_id") != intent_id:
        raise ValueError("token_radar_target_feature_payload_invalid:intent_json")
    if _required_target_feature_payload_text(intent, "event_id") != event_id:
        raise ValueError("token_radar_target_feature_payload_invalid:intent_json")
    _required_target_feature_payload_text(resolution, "status")
    for field in ("reason_codes", "candidate_ids", "lookup_keys"):
        _required_target_feature_payload_list(resolution, field)
    lane = _required_target_feature_payload_lane(row)
    latest_event_received_at_ms = _required_target_feature_payload_int(row, "source_max_received_at_ms")
    source_event_ids = _required_target_feature_payload_string_list(row, "source_event_ids_json", allow_empty=False)
    if event_id not in source_event_ids:
        raise ValueError("token_radar_target_feature_payload_invalid:source_event_ids_json")
    created_at_ms = _required_target_feature_payload_int(row, "created_at_ms")
    composite = _required_target_feature_payload_mapping(factor_snapshot, "composite")
    gates = _required_target_feature_payload_mapping(factor_snapshot, "gates")
    rank_score = _required_target_feature_payload_number(composite, "rank_score")
    recommended_decision = _required_target_feature_payload_decision(composite, "recommended_decision")
    gates_max_decision = _required_target_feature_payload_decision(gates, "max_decision")
    families = factor_snapshot.get("families") if isinstance(factor_snapshot, dict) else {}
    social_heat = families.get("social_heat") if isinstance(families, dict) else {}
    social_propagation = families.get("social_propagation") if isinstance(families, dict) else {}
    timing_risk = families.get("timing_risk") if isinstance(families, dict) else {}
    social_heat_facts = social_heat.get("facts") if isinstance(social_heat, dict) else {}
    social_propagation_facts = social_propagation.get("facts") if isinstance(social_propagation, dict) else {}
    subject = factor_snapshot.get("subject") if isinstance(factor_snapshot, dict) else {}
    latest_market_observed_at_ms = _latest_market_observed_at_ms(factor_snapshot)
    payload = {
        "projection_version": projection_version,
        "window": window,
        "lane": lane,
        "target_type_key": target_type_key,
        "identity_id": identity_id,
        "target_type": row.get("target_type"),
        "target_id": row.get("target_id"),
        "pricefeed_id": row.get("pricefeed_id"),
        "latest_event_received_at_ms": latest_event_received_at_ms,
        "latest_market_observed_at_ms": latest_market_observed_at_ms,
        "attention_score": _family_score(social_heat),
        "market_score": _family_score(families.get("timing_risk") if isinstance(families, dict) else {}),
        "credibility_score": _family_score(social_propagation),
        "rank_score": rank_score,
        "factor_snapshot_json": factor_snapshot,
        "intent_json": intent,
        "resolution_json": resolution,
        "source_event_ids_json": source_event_ids,
        "source_intent_ids_json": [intent_id],
        "source_resolution_ids_json": _resolution_ids(row),
        "last_scored_at_ms": int(computed_at_ms),
        "created_at_ms": created_at_ms,
        "updated_at_ms": int(computed_at_ms),
        "social_heat_raw_score": _family_raw_score(social_heat),
        "social_heat_weight": _family_weight(social_heat),
        "social_propagation_raw_score": _family_raw_score(social_propagation),
        "social_propagation_weight": _family_weight(social_propagation),
        "timing_risk_raw_score": _family_raw_score(timing_risk),
        "timing_risk_weight": _family_weight(timing_risk),
        "cohort_high_confidence_mentions": int(row.get("_cohort_high_conf_count") or 0),
        "cohort_kol_mentions": int(row.get("_cohort_kol_count") or 0),
        "cohort_followup_authors": int(row.get("_cohort_followup_count") or 0),
        "cohort_first_seen_global_24h": row.get("_cohort_first_seen_global_24h") is True
        or row.get("first_seen_global_24h") is True,
        "cohort_symbol": str(
            (subject.get("symbol") if isinstance(subject, dict) else None)
            or (row.get("intent_json") or {}).get("display_symbol")
            or ""
        ).upper(),
        "social_heat_mentions_1h": _int_value(
            social_heat_facts.get("mentions_1h") if isinstance(social_heat_facts, dict) else None
        ),
        "social_propagation_mentions": _int_value(
            social_propagation_facts.get("mentions") if isinstance(social_propagation_facts, dict) else None
        ),
        "social_heat_latest_seen_ms": _optional_int_value(
            social_heat_facts.get("latest_seen_ms") if isinstance(social_heat_facts, dict) else None
        ),
        "raw_composite_score": rank_score,
        "recommended_decision": recommended_decision,
        "gates_max_decision": gates_max_decision,
    }
    payload["payload_hash"] = _target_feature_hash(payload)
    for key in (
        "factor_snapshot_json",
        "intent_json",
        "resolution_json",
        "source_event_ids_json",
        "source_intent_ids_json",
        "source_resolution_ids_json",
    ):
        payload[key] = Jsonb(_json_ready(payload[key]))
    return payload


def token_radar_target_feature_payload(
    row: dict[str, Any],
    *,
    projection_version: str,
    window: str,
    computed_at_ms: int,
) -> dict[str, Any]:
    """Return the pure, serialization-safe target-feature publication payload."""

    return {
        key: _json_ready(value)
        for key, value in _target_feature_payload(
            row,
            projection_version=projection_version,
            window=window,
            computed_at_ms=computed_at_ms,
        ).items()
    }


def _required_target_feature_payload_mapping(row: Mapping[str, Any], column: str) -> dict[str, Any]:
    try:
        value = row[column]
    except KeyError as exc:
        raise ValueError(f"token_radar_target_feature_payload_required:{column}") from exc
    if value is None:
        raise ValueError(f"token_radar_target_feature_payload_required:{column}")
    if not isinstance(value, dict) or not value:
        raise ValueError(f"token_radar_target_feature_payload_invalid:{column}")
    return value


def _required_target_feature_payload_lane(row: Mapping[str, Any]) -> str:
    lane = _required_target_feature_payload_text(row, "lane")
    if lane not in TARGET_FEATURE_PAYLOAD_LANES:
        raise ValueError("token_radar_target_feature_payload_invalid:lane")
    return lane


def _required_target_feature_payload_decision(row: Mapping[str, Any], column: str) -> str:
    decision = _required_target_feature_payload_text(row, column)
    if decision not in TOKEN_RADAR_DECISIONS:
        raise ValueError(f"token_radar_target_feature_payload_invalid:{column}")
    return decision


def _required_target_feature_payload_text(row: Mapping[str, Any], column: str) -> str:
    try:
        value = row[column]
    except KeyError as exc:
        raise ValueError(f"token_radar_target_feature_payload_required:{column}") from exc
    if value is None:
        raise ValueError(f"token_radar_target_feature_payload_required:{column}")
    text = str(value).strip()
    if not text:
        raise ValueError(f"token_radar_target_feature_payload_invalid:{column}")
    return text


def _required_target_feature_payload_int(row: Mapping[str, Any], column: str) -> int:
    try:
        value = row[column]
    except KeyError as exc:
        raise ValueError(f"token_radar_target_feature_payload_required:{column}") from exc
    if value is None:
        raise ValueError(f"token_radar_target_feature_payload_required:{column}")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"token_radar_target_feature_payload_invalid:{column}")
    parsed = int(value)
    if parsed < 0:
        raise ValueError(f"token_radar_target_feature_payload_invalid:{column}")
    return parsed


def _required_target_feature_payload_list(row: Mapping[str, Any], column: str) -> list[Any]:
    try:
        value = row[column]
    except KeyError as exc:
        raise ValueError(f"token_radar_target_feature_payload_required:{column}") from exc
    if value is None:
        raise ValueError(f"token_radar_target_feature_payload_required:{column}")
    if not isinstance(value, list):
        raise ValueError(f"token_radar_target_feature_payload_invalid:{column}")
    return list(value)


def _required_target_feature_payload_string_list(
    row: Mapping[str, Any],
    column: str,
    *,
    allow_empty: bool,
) -> list[str]:
    values = _required_target_feature_payload_list(row, column)
    if (not allow_empty and not values) or any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError(f"token_radar_target_feature_payload_invalid:{column}")
    return [value.strip() for value in values]


def _required_target_feature_payload_number(row: Mapping[str, Any], column: str) -> float:
    try:
        value = row[column]
    except KeyError as exc:
        raise ValueError(f"token_radar_target_feature_payload_required:{column}") from exc
    if value is None:
        raise ValueError(f"token_radar_target_feature_payload_required:{column}")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"token_radar_target_feature_payload_invalid:{column}")
    return float(value)


def _json_payload(row: dict[str, Any]) -> dict[str, Any]:
    _validate_factor_contract(row)
    _required_current_text(row, "intent_id")
    _required_current_text(row, "event_id")
    out = {column: row.get(column) for column in RADAR_ROW_COLUMNS}
    intent = _required_current_mapping(row, "intent_json")
    resolution = _required_current_mapping(row, "resolution_json")
    _required_current_resolution_text(resolution, "status")
    for field in ("reason_codes", "candidate_ids", "lookup_keys"):
        _required_current_resolution_list(resolution, field)
    data_health = _required_current_mapping(row, "data_health_json")
    source_event_ids = _required_current_source_ids(row)
    degraded_reasons = _required_current_list(row, "degraded_reasons_json")
    out["intent_json"] = Jsonb(_json_ready(intent))
    out["resolution_json"] = Jsonb(_json_ready(resolution))
    out["factor_snapshot_json"] = Jsonb(_json_ready(row["factor_snapshot_json"]))
    out["data_health_json"] = Jsonb(_json_ready(data_health))
    out["source_event_ids_json"] = Jsonb(_json_ready(source_event_ids))
    out["degraded_reasons_json"] = Jsonb(_json_ready(degraded_reasons))
    return out


def _required_current_mapping(row: Mapping[str, Any], field: str) -> dict[str, Any]:
    if field not in row or row[field] is None:
        raise ValueError(f"token_radar_current_row_required:{field}")
    value = row[field]
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"token_radar_current_row_invalid:{field}")
    return dict(value)


def _required_current_list(row: Mapping[str, Any], field: str) -> list[Any]:
    if field not in row or row[field] is None:
        raise ValueError(f"token_radar_current_row_required:{field}")
    value = row[field]
    if not isinstance(value, list):
        raise ValueError(f"token_radar_current_row_invalid:{field}")
    return list(value)


def _required_current_source_ids(row: Mapping[str, Any]) -> list[str]:
    values = _required_current_list(row, "source_event_ids_json")
    if not values or any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("token_radar_current_row_invalid:source_event_ids_json")
    return [value.strip() for value in values]


def _required_current_resolution_text(resolution: Mapping[str, Any], field: str) -> str:
    if field not in resolution or resolution[field] is None:
        raise ValueError(f"token_radar_current_resolution_required:{field}")
    value = resolution[field]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"token_radar_current_resolution_invalid:{field}")
    return value.strip()


def _required_current_resolution_list(resolution: Mapping[str, Any], field: str) -> list[Any]:
    if field not in resolution or resolution[field] is None:
        raise ValueError(f"token_radar_current_resolution_required:{field}")
    value = resolution[field]
    if not isinstance(value, list):
        raise ValueError(f"token_radar_current_resolution_invalid:{field}")
    return list(value)


def _current_key(row: dict[str, Any]) -> tuple[str, str, str]:
    target_type_key, identity_id = _current_row_identity_key(row)
    return (
        _required_current_text(row, "lane"),
        target_type_key,
        identity_id,
    )


def _identity_key(row: dict[str, Any]) -> tuple[str, str]:
    return _current_row_identity_key(row)


def _nonempty_identities(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return list(dict.fromkeys(_identity_key(row) for row in rows))


def stable_generation_id(
    *,
    projection_version: str,
    window: str,
    venue: str,
    rows: list[dict[str, Any]],
) -> str:
    stable_rows = [
        {
            "lane": _required_current_text(row, "lane"),
            "rank": int(row.get("rank") or 0),
            "target_type_key": _current_row_identity_key(row)[0],
            "identity_id": _current_row_identity_key(row)[1],
            "decision": row.get("decision"),
            "rank_score": _stable_rank_score(row),
            "quality_status": row.get("quality_status"),
            "degraded_reasons_json": _stable_degraded_reasons(row),
            "source_max_received_at_ms": row.get("source_max_received_at_ms"),
            "payload_hash": row.get("payload_hash"),
        }
        for row in rows
    ]
    stable_rows.sort(key=lambda item: (item["lane"], item["rank"], item["target_type_key"], item["identity_id"]))
    payload = canonical_token_radar_payload(
        {
            "projection_version": projection_version,
            "window": window,
            "venue": venue,
            "rows": stable_rows,
        }
    )
    return stable_token_radar_payload_hash(payload)


def _current_row_identity_key(row: Mapping[str, Any]) -> tuple[str, str]:
    return (
        _required_current_text(row, "target_type_key"),
        _required_current_text(row, "identity_id"),
    )


def _required_current_text(row: Mapping[str, Any], column: str) -> str:
    try:
        value = row[column]
    except KeyError as exc:
        raise ValueError("token_radar_current_identity_required") from exc
    if value is None:
        raise ValueError("token_radar_current_identity_required")
    text = str(value).strip()
    if not text:
        raise ValueError("token_radar_current_identity_required")
    return text


def _first_generation_id(rows: list[dict[str, Any]]) -> str | None:
    for row in rows:
        generation_id = row.get("generation_id")
        if generation_id is not None:
            return str(generation_id)
    return None


def _stable_rank_score(row: dict[str, Any]) -> Any:
    if row.get("rank_score") is not None:
        return _float_or_none(row.get("rank_score"))
    factor_snapshot = row.get("factor_snapshot_json")
    if not isinstance(factor_snapshot, dict):
        return None
    composite = factor_snapshot.get("composite")
    if not isinstance(composite, dict):
        return None
    return _float_or_none(composite.get("rank_score"))


def _stable_degraded_reasons(row: dict[str, Any]) -> list[str]:
    value = _json_ready(row.get("degraded_reasons_json"))
    if not isinstance(value, list):
        return []
    return sorted(str(item) for item in value if str(item))


def _payload_hash(row: dict[str, Any]) -> str:
    stable_payload = canonical_token_radar_payload(
        {
            column: _json_ready(row.get(column))
            for column in RADAR_ROW_COLUMNS
            if column
            not in {
                "row_id",
                "computed_at_ms",
                "generation_id",
                "published_at_ms",
                "source_frontier_ms",
                "payload_hash",
                "listed_at_ms",
                "created_at_ms",
            }
        }
    )
    return stable_token_radar_payload_hash(stable_payload)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _family_raw_score(family: Any) -> float | None:
    if not isinstance(family, dict):
        return None
    raw_score = _float_or_none(family.get("raw_score"))
    if raw_score is not None:
        return raw_score
    return _float_or_none(family.get("score"))


def _family_weight(family: Any) -> float:
    if not isinstance(family, dict):
        return 0.0
    return _float_or_none(family.get("weight")) or 0.0


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _int_value(value: Any) -> int:
    return _optional_int_value(value) or 0


def _optional_int_value(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _family_score(family: Any) -> float:
    if not isinstance(family, dict):
        return 0.0
    try:
        return float(family.get("score") or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _latest_market_observed_at_ms(factor_snapshot: Any) -> int | None:
    if not isinstance(factor_snapshot, dict):
        return None
    market = factor_snapshot.get("market")
    if not isinstance(market, dict):
        return None
    latest = market.get("decision_latest")
    if not isinstance(latest, dict):
        return None
    value = latest.get("observed_at_ms")
    return int(value) if value is not None else None


def _resolution_ids(row: dict[str, Any]) -> list[str]:
    resolution_id = row.get("resolution_id")
    if resolution_id:
        return [str(resolution_id)]
    resolution_json = row.get("resolution_json")
    if isinstance(resolution_json, dict):
        return [str(item) for item in resolution_json.get("resolution_ids") or [] if str(item)]
    return []


def _target_feature_hash(row: dict[str, Any]) -> str:
    stable_payload = canonical_token_radar_payload(
        {
            key: _json_ready(value)
            for key, value in row.items()
            if key not in {"payload_hash", "last_scored_at_ms", "created_at_ms", "updated_at_ms"}
        }
    )
    return stable_token_radar_payload_hash(stable_payload)


def _validate_factor_contract(row: dict[str, Any]) -> None:
    if "factor_snapshot_json" not in row:
        raise ValueError("factor_snapshot_json is required for token radar row hard-cut contract")
    factor_snapshot = row.get("factor_snapshot_json")
    if not isinstance(factor_snapshot, dict) or not factor_snapshot:
        raise ValueError("factor_snapshot_json must be non-empty for token radar row hard-cut contract")
    factor_version = str(row.get("factor_version") or "").strip()
    if not factor_version:
        raise ValueError("factor_version is required for token radar row hard-cut contract")
    schema_version = str(factor_snapshot.get("schema_version") or "").strip()
    if not schema_version:
        raise ValueError("factor_snapshot_json.schema_version is required for token radar row hard-cut contract")
    if schema_version != factor_version:
        raise ValueError("factor_snapshot_json.schema_version must match factor_version")
    if schema_version != TOKEN_FACTOR_SNAPSHOT_VERSION:
        raise ValueError(f"factor_snapshot_json.schema_version must be {TOKEN_FACTOR_SNAPSHOT_VERSION}")
    require_token_factor_snapshot(factor_snapshot, field_name="factor_snapshot_json")


def _json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    return value
