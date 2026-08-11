from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date
from typing import Any

from tracefold.macro.domain import (
    DatasetSpec,
    DocumentFact,
    FedOfficialRoleFact,
    ReleaseFact,
    SeriesFact,
)
from tracefold.macro.fed_roles import match_effective_role
from tracefold.platform.postgres.queue_terminal import terminalize_source_row


class MacroRepository:
    """PostgreSQL seam for Macro facts, acquisition state, and decision reads."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def ensure_target(self, spec: DatasetSpec, *, now_ms: int, max_attempts: int) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO macro_acquisition_targets(
              target_key, dataset_id, partition_key, clock_kind, cursor_json,
              status, next_due_at_ms, priority, attempt_count, max_attempts,
              created_at_ms, updated_at_ms
            )
            VALUES (%s, %s, 'latest', %s, '{}'::jsonb, 'pending', %s, %s, 0, %s, %s, %s)
            ON CONFLICT(target_key) DO UPDATE SET
              clock_kind = EXCLUDED.clock_kind,
              max_attempts = EXCLUDED.max_attempts,
              status = CASE
                WHEN macro_acquisition_targets.status = 'stale'
                  AND macro_acquisition_targets.clock_kind <> 'backfill'
                  AND EXCLUDED.clock_kind <> 'backfill'
                  THEN 'delayed'
                ELSE macro_acquisition_targets.status
              END,
              attempt_count = CASE
                WHEN macro_acquisition_targets.status = 'stale'
                  AND macro_acquisition_targets.clock_kind <> 'backfill'
                  AND EXCLUDED.clock_kind <> 'backfill'
                  THEN 0
                ELSE macro_acquisition_targets.attempt_count
              END,
              next_due_at_ms = CASE
                WHEN macro_acquisition_targets.status = 'stale'
                  AND macro_acquisition_targets.clock_kind <> 'backfill'
                  AND EXCLUDED.clock_kind <> 'backfill'
                  THEN LEAST(macro_acquisition_targets.next_due_at_ms, EXCLUDED.next_due_at_ms)
                ELSE macro_acquisition_targets.next_due_at_ms
              END,
              updated_at_ms = EXCLUDED.updated_at_ms
            WHERE macro_acquisition_targets.clock_kind IS DISTINCT FROM EXCLUDED.clock_kind
               OR macro_acquisition_targets.max_attempts IS DISTINCT FROM EXCLUDED.max_attempts
               OR (
                 macro_acquisition_targets.status = 'stale'
                 AND macro_acquisition_targets.clock_kind <> 'backfill'
                 AND EXCLUDED.clock_kind <> 'backfill'
               )
            """,
            (
                spec.target_key,
                spec.dataset_id,
                spec.clock_kind,
                int(now_ms),
                10 if spec.critical else 100,
                int(max_attempts),
                int(now_ms),
                int(now_ms),
            ),
        )
        return int(cursor.rowcount)

    def enqueue_backfill_target(
        self,
        spec: DatasetSpec,
        *,
        start_date: date,
        end_date: date,
        now_ms: int,
        max_attempts: int,
        history_class: str | None = None,
        priority: int = 50,
    ) -> dict[str, Any]:
        if start_date > end_date:
            raise ValueError("macro_backfill_invalid_range")
        partition_key = f"{start_date.isoformat()}..{end_date.isoformat()}"
        target_key = f"{spec.dataset_id}:backfill:{partition_key}"
        row = self.conn.execute(
            """
            INSERT INTO macro_acquisition_targets(
              target_key, dataset_id, partition_key, clock_kind, cursor_json,
              status, next_due_at_ms, priority, attempt_count, max_attempts,
              created_at_ms, updated_at_ms
            )
            VALUES (
              %s, %s, %s, 'backfill', %s::jsonb,
              'backfilling', %s, %s, 0, %s, %s, %s
            )
            ON CONFLICT(dataset_id, partition_key) DO UPDATE SET
              cursor_json = EXCLUDED.cursor_json,
              status = 'backfilling',
              next_due_at_ms = EXCLUDED.next_due_at_ms,
              priority = EXCLUDED.priority,
              leased_until_ms = NULL,
              lease_owner = NULL,
              attempt_count = 0,
              max_attempts = EXCLUDED.max_attempts,
              last_error_code = NULL,
              updated_at_ms = EXCLUDED.updated_at_ms
            RETURNING *
            """,
            (
                target_key,
                spec.dataset_id,
                partition_key,
                json.dumps(
                    {
                        "start_date": start_date.isoformat(),
                        "end_date": end_date.isoformat(),
                        "history_class": history_class,
                    },
                    sort_keys=True,
                ),
                int(now_ms),
                int(priority),
                int(max_attempts),
                int(now_ms),
                int(now_ms),
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("macro_backfill_target_not_written")
        return dict(row)

    def promote_covering_backfill_target(
        self,
        spec: DatasetSpec,
        *,
        start_date: date,
        end_date: date,
        history_class: str,
        priority: int,
        now_ms: int,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            WITH candidate AS (
              SELECT target_key
              FROM macro_acquisition_targets
              WHERE dataset_id = %s
                AND clock_kind = 'backfill'
                AND status = 'current'
                AND cursor_json ? 'start_date'
                AND cursor_json ? 'end_date'
                AND (cursor_json ->> 'start_date')::date <= %s
                AND (cursor_json ->> 'end_date')::date >= %s
              ORDER BY
                (cursor_json ->> 'start_date')::date DESC,
                (cursor_json ->> 'end_date')::date,
                target_key
              LIMIT 1
              FOR UPDATE
            )
            UPDATE macro_acquisition_targets AS target
            SET cursor_json = target.cursor_json || jsonb_build_object(
                  'history_class', %s::text
                ),
                priority = %s,
                updated_at_ms = %s
            FROM candidate
            WHERE target.target_key = candidate.target_key
            RETURNING target.*
            """,
            (
                spec.dataset_id,
                start_date,
                end_date,
                history_class,
                int(priority),
                int(now_ms),
            ),
        ).fetchone()
        if row is None:
            return None
        self.conn.execute(
            """
            DELETE FROM macro_acquisition_targets AS redundant
            USING macro_acquisition_targets AS covering
            WHERE covering.target_key = %s
              AND redundant.dataset_id = covering.dataset_id
              AND redundant.clock_kind = 'backfill'
              AND redundant.target_key <> covering.target_key
              AND redundant.status <> 'claimed'
              AND redundant.cursor_json ? 'start_date'
              AND redundant.cursor_json ? 'end_date'
              AND (redundant.cursor_json ->> 'start_date')::date
                    >= (covering.cursor_json ->> 'start_date')::date
              AND (redundant.cursor_json ->> 'end_date')::date
                    <= (covering.cursor_json ->> 'end_date')::date
            """,
            (row["target_key"],),
        )
        return dict(row)

    def claim_target(
        self,
        *,
        clock_kind: str,
        lease_owner: str,
        lease_ms: int,
        now_ms: int,
        target_keys: tuple[str, ...] = (),
    ) -> dict[str, Any] | None:
        target_scope = "AND target_key = ANY(%(target_keys)s)" if target_keys else ""
        row = self.conn.execute(
            f"""
            WITH expired AS (
              UPDATE macro_acquisition_targets
              SET status = CASE
                    WHEN clock_kind = 'backfill' AND attempt_count >= max_attempts
                      THEN 'stale'
                    ELSE 'delayed'
                  END,
                  leased_until_ms = NULL,
                  lease_owner = NULL,
                  next_due_at_ms = CASE
                    WHEN clock_kind = 'backfill' AND attempt_count >= max_attempts
                      THEN next_due_at_ms
                    ELSE %(now_ms)s
                  END,
                  last_error_code = 'acquisition_lease_expired',
                  updated_at_ms = %(now_ms)s
              WHERE clock_kind = %(clock_kind)s
                {target_scope}
                AND status = 'claimed'
                AND leased_until_ms <= %(now_ms)s
              RETURNING target_key
            ), candidate AS (
              SELECT target_key, status AS previous_status
              FROM macro_acquisition_targets
              WHERE clock_kind = %(clock_kind)s
                {target_scope}
                AND status IN (
                  'pending', 'current', 'delayed', 'backfilling'
                )
                AND next_due_at_ms <= %(now_ms)s
                AND status <> 'unavailable'
              ORDER BY priority, next_due_at_ms, target_key
              FOR UPDATE SKIP LOCKED
              LIMIT 1
            )
            UPDATE macro_acquisition_targets AS target
            SET status = 'claimed',
                leased_until_ms = %(claimed_until_ms)s,
                lease_owner = %(lease_owner)s,
                attempt_count = target.attempt_count + 1,
                updated_at_ms = %(now_ms)s
            FROM candidate
            WHERE target.target_key = candidate.target_key
            RETURNING target.*, candidate.previous_status
            """,
            {
                "now_ms": int(now_ms),
                "clock_kind": clock_kind,
                "claimed_until_ms": int(now_ms + lease_ms),
                "lease_owner": lease_owner,
                "target_keys": list(target_keys),
            },
        ).fetchone()
        return dict(row) if row is not None else None

    def release_target_claim(
        self,
        *,
        target_key: str,
        lease_owner: str,
        previous_status: str,
        claimed_attempt_count: int,
    ) -> bool:
        if previous_status not in {
            "pending",
            "current",
            "delayed",
            "backfilling",
        }:
            raise ValueError("macro_acquisition_previous_status_invalid")
        row = self.conn.execute(
            """
            UPDATE macro_acquisition_targets
               SET status = %s,
                   leased_until_ms = NULL,
                   lease_owner = NULL,
                   attempt_count = attempt_count - 1
             WHERE target_key = %s
               AND status = 'claimed'
               AND lease_owner = %s
               AND attempt_count = %s
               AND attempt_count > 0
            RETURNING target_key
            """,
            (
                previous_status,
                target_key,
                lease_owner,
                int(claimed_attempt_count),
            ),
        ).fetchone()
        return row is not None

    def insert_series_fact(self, fact: SeriesFact) -> int:
        payload = {
            "dataset_id": fact.dataset_id,
            "series_id": fact.series_id,
            "reference_date": str(fact.reference_date),
            "value_numeric": fact.value_numeric,
            "value_text": fact.value_text,
            "unit": fact.unit,
            "published_at_ms": fact.published_at_ms,
        }
        fact_hash = _payload_hash(payload)
        fact_id = (
            "macro_"
            + hashlib.sha256(
                (f"{fact.dataset_id}|{fact.series_id}|{fact.reference_date}|{fact_hash}").encode()
            ).hexdigest()
        )
        cursor = self.conn.execute(
            """
            INSERT INTO macro_series_facts(
              fact_id, dataset_id, series_id, reference_date, vintage_date,
              value_numeric, value_text, unit, published_at_ms, received_at_ms,
              source_url, fact_hash, raw_data_json
            )
            VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb
            )
            ON CONFLICT DO NOTHING
            """,
            (
                fact_id,
                fact.dataset_id,
                fact.series_id,
                fact.reference_date,
                fact.vintage_date,
                fact.value_numeric,
                fact.value_text,
                fact.unit,
                fact.published_at_ms,
                fact.received_at_ms,
                fact.source_url,
                fact_hash,
                json.dumps(fact.raw_data, sort_keys=True),
            ),
        )
        return int(cursor.rowcount)

    def insert_release_fact(self, fact: ReleaseFact) -> int:
        payload = {
            "dataset_id": fact.dataset_id,
            "release_id": fact.release_id,
            "series_id": fact.series_id,
            "reference_period": fact.reference_period,
            "scheduled_at_ms": fact.scheduled_at_ms,
            "actual_value": fact.actual_value,
            "prior_value": fact.prior_value,
            "revised_prior_value": fact.revised_prior_value,
            "estimate_value": fact.estimate_value,
            "unit": fact.unit,
            "importance_tier": fact.importance_tier,
        }
        fact_hash = _payload_hash(payload)
        release_fact_id = (
            "macrorel_"
            + hashlib.sha256(
                f"{fact.dataset_id}|{fact.release_id}|{fact.reference_period}|{fact_hash}".encode()
            ).hexdigest()
        )
        cursor = self.conn.execute(
            """
            INSERT INTO macro_release_facts(
              release_fact_id, dataset_id, release_id, series_id, reference_period,
              scheduled_at_ms, published_at_ms, received_at_ms, actual_value,
              prior_value, revised_prior_value, estimate_value, unit, importance_tier,
              source_url, fact_hash, raw_data_json
            )
            VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb
            )
            ON CONFLICT DO NOTHING
            """,
            (
                release_fact_id,
                fact.dataset_id,
                fact.release_id,
                fact.series_id,
                fact.reference_period,
                fact.scheduled_at_ms,
                fact.published_at_ms,
                fact.received_at_ms,
                fact.actual_value,
                fact.prior_value,
                fact.revised_prior_value,
                fact.estimate_value,
                fact.unit,
                fact.importance_tier,
                fact.source_url,
                fact_hash,
                json.dumps(fact.raw_data, sort_keys=True),
            ),
        )
        return int(cursor.rowcount)

    def insert_document(self, fact: DocumentFact) -> int:
        payload = {
            "document_id": fact.document_id,
            "dataset_id": fact.dataset_id,
            "document_type": fact.document_type,
            "title": fact.title,
            "effective_date": str(fact.effective_date),
            "published_at_ms": fact.published_at_ms,
            "content_text": fact.content_text,
        }
        fact_hash = _payload_hash(payload)
        cursor = self.conn.execute(
            """
            INSERT INTO macro_documents(
              document_id, dataset_id, document_type, title, effective_date,
              published_at_ms, received_at_ms, source_url, content_text,
              fact_hash, metadata_json
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT DO NOTHING
            """,
            (
                fact.document_id,
                fact.dataset_id,
                fact.document_type,
                fact.title,
                fact.effective_date,
                fact.published_at_ms,
                fact.received_at_ms,
                fact.source_url,
                fact.content_text,
                fact_hash,
                json.dumps(fact.metadata, sort_keys=True),
            ),
        )
        return int(cursor.rowcount)

    def insert_fed_official_role_fact(self, fact: FedOfficialRoleFact) -> int:
        payload = {
            "dataset_id": fact.dataset_id,
            "official_id": fact.official_id,
            "official_name": fact.official_name,
            "role_title": fact.role_title,
            "organization": fact.organization,
            "effective_start": fact.effective_start.isoformat(),
            "effective_end": fact.effective_end.isoformat() if fact.effective_end else None,
            "fomc_participant": fact.fomc_participant,
            "fomc_voter": fact.fomc_voter,
            "source_url": fact.source_url,
        }
        fact_hash = _payload_hash(payload)
        cursor = self.conn.execute(
            """
            INSERT INTO macro_fed_official_role_facts(
              role_fact_id, dataset_id, official_id, official_name, role_title,
              organization, effective_start, effective_end, fomc_participant,
              fomc_voter, source_url, received_at_ms, fact_hash, raw_data_json
            )
            VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb
            )
            ON CONFLICT DO NOTHING
            """,
            (
                fact.role_fact_id,
                fact.dataset_id,
                fact.official_id,
                fact.official_name,
                fact.role_title,
                fact.organization,
                fact.effective_start,
                fact.effective_end,
                fact.fomc_participant,
                fact.fomc_voter,
                fact.source_url,
                int(fact.received_at_ms),
                fact_hash,
                json.dumps(fact.raw_data, sort_keys=True),
            ),
        )
        return int(cursor.rowcount)

    def complete_target(
        self,
        *,
        target_key: str,
        lease_owner: str,
        cursor: dict[str, Any],
        next_due_at_ms: int,
        completed_at_ms: int,
        status: str = "current",
    ) -> bool:
        row = self.conn.execute(
            """
            UPDATE macro_acquisition_targets
            SET cursor_json = %s::jsonb,
                status = %s,
                next_due_at_ms = %s,
                leased_until_ms = NULL,
                lease_owner = NULL,
                attempt_count = 0,
                last_success_at_ms = %s,
                last_error_code = NULL,
                updated_at_ms = %s
            WHERE target_key = %s
              AND status = 'claimed'
              AND lease_owner = %s
            RETURNING target_key
            """,
            (
                json.dumps(cursor, sort_keys=True),
                status,
                int(next_due_at_ms),
                int(completed_at_ms),
                int(completed_at_ms),
                target_key,
                lease_owner,
            ),
        ).fetchone()
        return row is not None

    def fail_target(
        self,
        *,
        target: dict[str, Any],
        lease_owner: str,
        error_code: str,
        next_due_at_ms: int,
        completed_at_ms: int,
        unavailable: bool,
    ) -> str | None:
        if unavailable:
            status = "unavailable"
        elif str(target["clock_kind"]) == "backfill" and int(target["attempt_count"]) >= int(target["max_attempts"]):
            status = "stale"
        else:
            status = "delayed"
        row = self.conn.execute(
            """
            UPDATE macro_acquisition_targets
            SET status = %s,
                next_due_at_ms = %s,
                leased_until_ms = NULL,
                lease_owner = NULL,
                last_error_code = %s,
                updated_at_ms = %s
            WHERE target_key = %s
              AND status = 'claimed'
              AND lease_owner = %s
            RETURNING target_key
            """,
            (
                status,
                int(next_due_at_ms),
                error_code,
                int(completed_at_ms),
                target["target_key"],
                lease_owner,
            ),
        ).fetchone()
        return status if row is not None else None

    def target_states(self, *, dataset_ids: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        if dataset_ids:
            rows = self.conn.execute(
                """
                SELECT *
                FROM macro_acquisition_targets
                WHERE dataset_id = ANY(%s)
                ORDER BY dataset_id, partition_key
                """,
                (list(dataset_ids),),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM macro_acquisition_targets ORDER BY dataset_id, partition_key"
            ).fetchall()
        return [dict(row) for row in rows]

    def projection_source_state(self) -> dict[str, Any]:
        fact_tables = [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT 'macro_series_facts' AS source_name,
                       count(*)::bigint AS row_count,
                       max(received_at_ms)::bigint AS frontier_ms
                  FROM macro_series_facts
                UNION ALL
                SELECT 'macro_release_facts', count(*)::bigint, max(received_at_ms)::bigint
                  FROM macro_release_facts
                UNION ALL
                SELECT 'macro_documents', count(*)::bigint, max(received_at_ms)::bigint
                  FROM macro_documents
                UNION ALL
                SELECT 'macro_fed_official_role_facts', count(*)::bigint, max(received_at_ms)::bigint
                  FROM macro_fed_official_role_facts
                UNION ALL
                SELECT 'macro_document_analyses', count(*)::bigint, max(created_at_ms)::bigint
                  FROM macro_document_analyses
                ORDER BY source_name
                """
            ).fetchall()
        ]
        target_states = [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT
                  target_key, dataset_id, partition_key, clock_kind, status,
                  cursor_json, next_due_at_ms, priority, attempt_count,
                  max_attempts, last_success_at_ms,
                  last_error_code, updated_at_ms
                FROM macro_acquisition_targets
                ORDER BY target_key
                """
            ).fetchall()
        ]
        analysis_job_state = self.document_analysis_job_state()
        return {
            "fact_tables": fact_tables,
            "target_states": target_states,
            "analysis_job_state": analysis_job_state,
        }

    def maintenance_dataset_fact_states(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT
              dataset_id,
              count(*)::bigint AS row_count,
              max(received_at_ms)::bigint AS source_frontier_ms,
              max(fact_hash) AS max_fact_hash
            FROM (
              SELECT dataset_id, received_at_ms, fact_hash
              FROM macro_series_facts
              UNION ALL
              SELECT dataset_id, received_at_ms, fact_hash
              FROM macro_release_facts
              UNION ALL
              SELECT dataset_id, received_at_ms, fact_hash
              FROM macro_documents
              UNION ALL
              SELECT dataset_id, received_at_ms, fact_hash
              FROM macro_fed_official_role_facts
              UNION ALL
              SELECT
                'federal_reserve.document.analysis' AS dataset_id,
                created_at_ms AS received_at_ms,
                payload_hash AS fact_hash
              FROM macro_document_analyses
            ) facts
            GROUP BY dataset_id
            ORDER BY dataset_id
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def upsert_dataset_projection_state(
        self,
        *,
        dataset_id: str,
        material_fingerprint: str,
        acquisition_status: str,
        source_frontier_ms: int,
        updated_at_ms: int,
        material_changed: bool = True,
    ) -> bool:
        cursor = self.conn.execute(
            """
            INSERT INTO macro_dataset_projection_states(
              dataset_id, material_fingerprint, acquisition_status,
              source_frontier_ms, updated_at_ms
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT(dataset_id) DO UPDATE SET
              material_fingerprint = CASE
                WHEN %s THEN excluded.material_fingerprint
                ELSE macro_dataset_projection_states.material_fingerprint
              END,
              acquisition_status = excluded.acquisition_status,
              source_frontier_ms = excluded.source_frontier_ms,
              updated_at_ms = excluded.updated_at_ms
            WHERE (
              macro_dataset_projection_states.material_fingerprint,
              macro_dataset_projection_states.acquisition_status,
              macro_dataset_projection_states.source_frontier_ms
            ) IS DISTINCT FROM (
              CASE
                WHEN %s THEN excluded.material_fingerprint
                ELSE macro_dataset_projection_states.material_fingerprint
              END,
              excluded.acquisition_status,
              excluded.source_frontier_ms
            )
            """,
            (
                str(dataset_id),
                str(material_fingerprint),
                str(acquisition_status),
                int(source_frontier_ms),
                int(updated_at_ms),
                bool(material_changed),
                bool(material_changed),
            ),
        )
        return int(cursor.rowcount or 0) == 1

    def ensure_dataset_projection_state(
        self,
        *,
        dataset_id: str,
        updated_at_ms: int,
    ) -> bool:
        cursor = self.conn.execute(
            """
            INSERT INTO macro_dataset_projection_states(
              dataset_id, material_fingerprint, acquisition_status,
              source_frontier_ms, updated_at_ms
            )
            VALUES (%s, 'missing', 'uninitialized', 0, %s)
            ON CONFLICT(dataset_id) DO NOTHING
            """,
            (str(dataset_id), int(updated_at_ms)),
        )
        return int(cursor.rowcount or 0) == 1

    def dataset_projection_states(
        self,
        *,
        dataset_ids: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        if not dataset_ids:
            return []
        rows = self.conn.execute(
            """
            SELECT
              dataset_id, material_fingerprint, acquisition_status,
              source_frontier_ms, updated_at_ms
            FROM macro_dataset_projection_states
            WHERE dataset_id = ANY(%s)
            ORDER BY dataset_id
            """,
            (list(dataset_ids),),
        ).fetchall()
        return [dict(row) for row in rows]

    def series_history(
        self,
        *,
        history_limits: Mapping[str, int],
        received_before_ms: int | None = None,
        row_cap: int | None = None,
    ) -> list[dict[str, Any]]:
        requested = {str(dataset_id): int(limit) for dataset_id, limit in history_limits.items() if int(limit) > 0}
        if not requested:
            return []
        rows = self.conn.execute(
            """
            WITH requested AS (
              SELECT *
              FROM unnest(%s::text[], %s::integer[])
                AS requested(dataset_id, max_rows)
            ), latest_vintage AS (
              SELECT DISTINCT ON (dataset_id, series_id, reference_date)
                facts.fact_id,
                facts.dataset_id,
                facts.series_id,
                facts.reference_date,
                facts.vintage_date,
                facts.value_numeric,
                facts.value_text,
                facts.unit,
                facts.published_at_ms,
                facts.received_at_ms,
                facts.source_url,
                facts.fact_hash,
                requested.max_rows
              FROM macro_series_facts AS facts
              JOIN requested USING (dataset_id)
              WHERE facts.received_at_ms <= COALESCE(%s::bigint, facts.received_at_ms)
              ORDER BY
                dataset_id, series_id, reference_date,
                vintage_date DESC, received_at_ms DESC
            ), ranked AS (
              SELECT
                latest_vintage.*,
                row_number() OVER (
                  PARTITION BY dataset_id, series_id
                  ORDER BY reference_date DESC
                ) AS row_number
              FROM latest_vintage
            )
            SELECT
              fact_id, dataset_id, series_id, reference_date, vintage_date,
              value_numeric, value_text, unit, published_at_ms, received_at_ms,
              source_url, fact_hash, row_number
            FROM ranked
            WHERE row_number <= max_rows
            ORDER BY dataset_id, series_id, reference_date
            LIMIT %s
            """,
            (
                list(requested),
                list(requested.values()),
                received_before_ms,
                _row_limit(row_cap),
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    def series_projection_history(
        self,
        *,
        history_limits: Mapping[str, int],
        received_before_ms: int | None = None,
        row_cap: int | None = None,
    ) -> list[dict[str, Any]]:
        """Load bounded presentation rows plus exact capped-history statistics.

        Full-history percentile datasets retain their former 10,000-row semantic
        window without sending that window through the projection process.
        """

        requested = {str(dataset_id): int(limit) for dataset_id, limit in history_limits.items() if int(limit) > 0}
        if not requested:
            return []
        rows = self.conn.execute(
            """
            WITH requested AS (
              SELECT *
              FROM unnest(%s::text[], %s::integer[])
                AS requested(dataset_id, semantic_rows)
            ), series_keys AS (
              SELECT
                requested.dataset_id,
                requested.semantic_rows,
                series.series_id
              FROM requested
              CROSS JOIN LATERAL (
                SELECT DISTINCT facts.series_id
                FROM macro_series_facts AS facts
                WHERE facts.dataset_id = requested.dataset_id
                  AND facts.received_at_ms <= COALESCE(
                    %s::bigint,
                    facts.received_at_ms
                  )
                ORDER BY facts.series_id
              ) AS series
            ), semantic_window AS (
              SELECT latest.*
              FROM series_keys
              CROSS JOIN LATERAL (
                SELECT current_vintage.*
                FROM (
                  SELECT DISTINCT ON (facts.reference_date)
                    facts.fact_id,
                    facts.dataset_id,
                    facts.series_id,
                    facts.reference_date,
                    facts.vintage_date,
                    facts.value_numeric,
                    facts.value_text,
                    facts.unit,
                    facts.published_at_ms,
                    facts.received_at_ms,
                    facts.source_url,
                    facts.fact_hash,
                    series_keys.semantic_rows
                  FROM macro_series_facts AS facts
                  WHERE facts.dataset_id = series_keys.dataset_id
                    AND facts.series_id = series_keys.series_id
                    AND facts.received_at_ms <= COALESCE(
                      %s::bigint,
                      facts.received_at_ms
                    )
                  ORDER BY
                    facts.reference_date DESC,
                    facts.vintage_date DESC,
                    facts.received_at_ms DESC
                ) AS current_vintage
                LIMIT series_keys.semantic_rows
              ) AS latest
            ), latest_numeric AS (
              SELECT DISTINCT ON (dataset_id)
                dataset_id,
                value_numeric AS latest_numeric
              FROM semantic_window
              WHERE value_numeric IS NOT NULL
              ORDER BY
                dataset_id, reference_date DESC, vintage_date DESC, received_at_ms DESC
            ), semantic_stats AS (
              SELECT
                rows.dataset_id,
                count(*) FILTER (WHERE rows.value_numeric IS NOT NULL) AS semantic_sample_count,
                min(rows.reference_date) FILTER (
                  WHERE rows.value_numeric IS NOT NULL
                ) AS semantic_history_start,
                round(
                  (
                    count(*) FILTER (
                      WHERE rows.value_numeric IS NOT NULL
                        AND rows.value_numeric <= latest.latest_numeric
                    )::numeric
                    / NULLIF(
                        count(*) FILTER (WHERE rows.value_numeric IS NOT NULL),
                        0
                      )::numeric
                    * 100
                  ),
                  2
                ) AS semantic_percentile
              FROM semantic_window AS rows
              JOIN latest_numeric AS latest USING (dataset_id)
              GROUP BY rows.dataset_id
            ), projection_ranked AS (
              SELECT
                semantic_window.*,
                row_number() OVER (
                  PARTITION BY dataset_id, series_id
                  ORDER BY reference_date DESC
                ) AS projection_row_number
              FROM semantic_window
            )
            SELECT
              rows.fact_id, rows.dataset_id, rows.series_id,
              rows.reference_date, rows.vintage_date,
              rows.value_numeric, rows.unit,
              rows.published_at_ms, rows.received_at_ms,
              rows.source_url,
              stats.semantic_sample_count,
              stats.semantic_history_start,
              stats.semantic_percentile
            FROM projection_ranked AS rows
            JOIN semantic_stats AS stats USING (dataset_id)
            WHERE rows.projection_row_number <= LEAST(rows.semantic_rows, 500)
            ORDER BY rows.dataset_id, rows.series_id, rows.reference_date
            LIMIT %s
            """,
            (
                list(requested),
                list(requested.values()),
                received_before_ms,
                received_before_ms,
                _row_limit(row_cap),
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    def document_projection_history(
        self,
        *,
        dataset_ids: tuple[str, ...],
        timeline_limit: int = 80,
        communication_window_days: int = 90,
        received_before_ms: int | None = None,
        row_cap: int | None = None,
    ) -> list[dict[str, Any]]:
        """Load the metadata window consumed by the rates/Fed read model."""

        if not dataset_ids:
            return []
        rows = self.conn.execute(
            """
            WITH eligible AS (
              SELECT
                document_id, dataset_id, document_type, title,
                effective_date, published_at_ms, received_at_ms,
                source_url, fact_hash, metadata_json,
                row_number() OVER (
                  ORDER BY effective_date DESC, published_at_ms DESC, document_id DESC
                ) AS timeline_rank,
                max(effective_date) OVER () AS reference_date,
                min(effective_date) OVER (
                  PARTITION BY dataset_id
                ) AS semantic_history_start,
                count(*) OVER (
                  PARTITION BY dataset_id
                ) AS semantic_sample_count
              FROM macro_documents
              WHERE dataset_id = ANY(%s)
                AND received_at_ms <= COALESCE(%s::bigint, received_at_ms)
            )
            SELECT
              document_id, dataset_id, document_type, title,
              effective_date, published_at_ms, received_at_ms,
              source_url, fact_hash, metadata_json,
              semantic_history_start, semantic_sample_count
            FROM eligible
            WHERE timeline_rank <= %s
               OR effective_date >= reference_date - %s::integer
            ORDER BY dataset_id, effective_date, published_at_ms, document_id
            LIMIT %s
            """,
            (
                list(dataset_ids),
                received_before_ms,
                int(timeline_limit),
                int(communication_window_days),
                _row_limit(row_cap),
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    def fed_official_role_projection_history(
        self,
        *,
        effective_from: date | None,
        received_before_ms: int | None = None,
        row_cap: int | None = None,
    ) -> list[dict[str, Any]]:
        """Load the roster snapshots needed by the selected document window."""

        rows = self.conn.execute(
            """
            WITH eligible AS (
              SELECT *
              FROM macro_fed_official_role_facts
              WHERE received_at_ms <= COALESCE(%s::bigint, received_at_ms)
            ), boundary AS (
              SELECT COALESCE(
                max(effective_start) FILTER (
                  WHERE %s::date IS NOT NULL
                    AND effective_start <= %s::date
                ),
                max(effective_start)
              ) AS effective_start
              FROM eligible
            )
            SELECT
              role_fact_id, dataset_id, official_id, official_name,
              role_title, organization, effective_start, effective_end,
              fomc_participant, fomc_voter, source_url, received_at_ms
            FROM eligible
            WHERE effective_start >= (SELECT effective_start FROM boundary)
            ORDER BY effective_start, official_id, role_fact_id
            LIMIT %s
            """,
            (
                received_before_ms,
                effective_from,
                effective_from,
                _row_limit(row_cap),
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    def document_analysis_projection_history(
        self,
        *,
        document_ids: tuple[str, ...],
        received_before_ms: int | None = None,
        row_cap: int | None = None,
    ) -> list[dict[str, Any]]:
        """Load only the latest passed analysis for selected documents."""

        if not document_ids:
            return []
        rows = self.conn.execute(
            """
            SELECT DISTINCT ON (analyses.document_id)
              analyses.*,
              'federal_reserve.document.analysis' AS dataset_id,
              documents.received_at_ms AS received_at_ms,
              documents.document_type,
              documents.title,
              documents.effective_date,
              documents.published_at_ms,
              documents.source_url,
              documents.metadata_json
            FROM macro_document_analyses AS analyses
            JOIN macro_documents AS documents USING (document_id)
            WHERE analyses.document_id = ANY(%s)
              AND analyses.reviewer_disposition = 'pass'
              AND documents.received_at_ms <= COALESCE(
                %s::bigint,
                documents.received_at_ms
              )
              AND analyses.created_at_ms <= COALESCE(
                %s::bigint,
                analyses.created_at_ms
              )
            ORDER BY
              analyses.document_id,
              analyses.created_at_ms DESC,
              analyses.analysis_id DESC
            LIMIT %s
            """,
            (
                list(document_ids),
                received_before_ms,
                received_before_ms,
                _row_limit(row_cap),
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    def release_history(
        self,
        *,
        dataset_ids: tuple[str, ...],
        received_before_ms: int | None = None,
        row_cap: int | None = None,
    ) -> list[dict[str, Any]]:
        if not dataset_ids:
            return []
        rows = self.conn.execute(
            """
            SELECT *
            FROM macro_release_facts
            WHERE dataset_id = ANY(%s)
              AND received_at_ms <= COALESCE(%s::bigint, received_at_ms)
            ORDER BY
              dataset_id,
              reference_period,
              received_at_ms,
              fact_hash
            LIMIT %s
            """,
            (list(dataset_ids), received_before_ms, _row_limit(row_cap)),
        ).fetchall()
        return [dict(row) for row in rows]

    def document_history(
        self,
        *,
        dataset_ids: tuple[str, ...],
        received_before_ms: int | None = None,
        row_cap: int | None = None,
    ) -> list[dict[str, Any]]:
        if not dataset_ids:
            return []
        rows = self.conn.execute(
            """
            SELECT *
            FROM macro_documents
            WHERE dataset_id = ANY(%s)
              AND received_at_ms <= COALESCE(%s::bigint, received_at_ms)
            ORDER BY dataset_id, published_at_ms
            LIMIT %s
            """,
            (list(dataset_ids), received_before_ms, _row_limit(row_cap)),
        ).fetchall()
        return [dict(row) for row in rows]

    def fed_official_role_history(
        self,
        *,
        received_before_ms: int | None = None,
        row_cap: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT *
            FROM macro_fed_official_role_facts
            WHERE received_at_ms <= COALESCE(%s::bigint, received_at_ms)
            ORDER BY effective_start, official_id, role_fact_id
            LIMIT %s
            """,
            (received_before_ms, _row_limit(row_cap)),
        ).fetchall()
        return [dict(row) for row in rows]

    def document_analysis_history(
        self,
        *,
        received_before_ms: int | None = None,
        row_cap: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT
              analyses.*,
              'federal_reserve.document.analysis' AS dataset_id,
              documents.received_at_ms AS received_at_ms,
              documents.document_type,
              documents.title,
              documents.effective_date,
              documents.published_at_ms,
              documents.source_url,
              documents.metadata_json
            FROM macro_document_analyses AS analyses
            JOIN macro_documents AS documents USING (document_id)
            WHERE documents.received_at_ms <= COALESCE(
              %s::bigint,
              documents.received_at_ms
            )
              AND analyses.created_at_ms <= COALESCE(
                %s::bigint,
                analyses.created_at_ms
            )
            ORDER BY documents.effective_date, analyses.created_at_ms, analyses.analysis_id
            LIMIT %s
            """,
            (received_before_ms, received_before_ms, _row_limit(row_cap)),
        ).fetchall()
        return [dict(row) for row in rows]

    def document_analysis_job_state(
        self,
        *,
        received_before_ms: int | None = None,
    ) -> dict[str, int]:
        row = self.conn.execute(
            """
            SELECT
              count(*)::int AS total,
              count(*) FILTER (
                WHERE status IN ('pending', 'claimed', 'retryable')
              )::int AS open,
              count(*) FILTER (WHERE status = 'failed')::int AS failed,
              count(*) FILTER (WHERE status = 'completed')::int AS completed
            FROM macro_document_analysis_jobs AS jobs
            JOIN macro_documents AS documents USING (document_id)
            WHERE documents.received_at_ms <= COALESCE(
              %s::bigint,
              documents.received_at_ms
            )
              AND (
                jobs.status = 'completed'
                OR NOT EXISTS (
                  SELECT 1
                  FROM macro_document_analyses AS analyses
                  WHERE analyses.document_id = jobs.document_id
                    AND analyses.document_hash = jobs.document_hash
                )
              )
            """,
            (received_before_ms,),
        ).fetchone()
        return {
            "total": int(row["total"]),
            "open": int(row["open"]),
            "failed": int(row["failed"]),
            "completed": int(row["completed"]),
        }

    def document_analysis_projection_state(self) -> dict[str, Any]:
        """Derive an append-only analysis change token plus native job buckets."""

        # The table rejects UPDATE/DELETE, so row_count changes on every legal
        # material mutation even when a backdated ID leaves both maxima unchanged.
        analysis_summary = dict(
            self.conn.execute(
                """
                SELECT count(*)::bigint AS row_count,
                       max(analysis_id) AS max_analysis_id,
                       max(created_at_ms)::bigint AS source_frontier_ms
                  FROM macro_document_analyses
                """
            ).fetchone()
        )
        job_state = self.document_analysis_job_state()
        if job_state["failed"] > 0:
            acquisition_status = "failed"
        elif int(analysis_summary["row_count"]) > 0:
            acquisition_status = "current"
        elif job_state["open"] > 0:
            acquisition_status = "pending"
        else:
            acquisition_status = "uninitialized"
        return {
            "dataset_id": "federal_reserve.document.analysis",
            "material_fingerprint": _payload_hash(
                {
                    "schema_version": "macro_document_analysis_projection_input_v2",
                    "analysis_summary": analysis_summary,
                    "job_state": job_state,
                }
            ),
            "acquisition_status": acquisition_status,
            "source_frontier_ms": int(analysis_summary["source_frontier_ms"] or 0),
        }

    def refresh_document_analysis_projection_state(
        self,
        *,
        updated_at_ms: int,
    ) -> bool:
        """Persist the rebuildable derived-analysis Dataset input."""

        state = self.document_analysis_projection_state()
        return self.upsert_dataset_projection_state(
            dataset_id=str(state["dataset_id"]),
            material_fingerprint=str(state["material_fingerprint"]),
            acquisition_status=str(state["acquisition_status"]),
            source_frontier_ms=int(state["source_frontier_ms"]),
            updated_at_ms=int(updated_at_ms),
        )

    def ensure_document_analysis_jobs(
        self,
        *,
        model_name: str,
        prompt_version: str,
        max_attempts: int,
        now_ms: int,
        fomc_lookback_days: int,
        speech_lookback_days: int,
        limit: int = 2_000,
        document_ids: tuple[str, ...] | None = None,
        refresh_projection_state: bool = True,
    ) -> int:
        rows = self.conn.execute(
            """
            SELECT
              documents.document_id,
              documents.document_type,
              documents.effective_date,
              documents.fact_hash,
              documents.metadata_json,
              documents.received_at_ms
            FROM macro_documents AS documents
            WHERE documents.document_type IN (
              'statement', 'implementation', 'minutes', 'sep', 'speech'
            )
              AND documents.dataset_id IN (
                'federal_reserve.fomc.documents',
                'federal_reserve.board.speeches',
                'federal_reserve.reserve_bank.speeches'
              )
              AND (%s::text[] IS NULL OR documents.document_id = ANY(%s::text[]))
              AND (
                (
                  documents.dataset_id = 'federal_reserve.fomc.documents'
                  AND documents.effective_date >= (
                    to_timestamp(%s::double precision / 1000.0) AT TIME ZONE 'UTC'
                  )::date - %s::int
                )
                OR (
                  documents.dataset_id IN (
                    'federal_reserve.board.speeches',
                    'federal_reserve.reserve_bank.speeches'
                  )
                  AND documents.effective_date >= (
                    to_timestamp(%s::double precision / 1000.0) AT TIME ZONE 'UTC'
                  )::date - %s::int
                )
              )
              AND NOT EXISTS (
                SELECT 1
                FROM macro_document_analyses AS analyses
                WHERE analyses.document_id = documents.document_id
                  AND analyses.document_hash = COALESCE(
                    documents.metadata_json ->> 'content_hash',
                    documents.fact_hash
                  )
                  AND analyses.model_name = %s
                  AND analyses.prompt_version = %s
              )
              AND NOT EXISTS (
                SELECT 1
                FROM macro_document_analysis_jobs AS jobs
                WHERE jobs.document_id = documents.document_id
                  AND jobs.document_hash = COALESCE(
                    documents.metadata_json ->> 'content_hash',
                    documents.fact_hash
                  )
                  AND jobs.model_name = %s
                  AND jobs.prompt_version = %s
              )
            ORDER BY documents.published_at_ms DESC, documents.document_id
            LIMIT %s
            """,
            (
                list(document_ids) if document_ids is not None else None,
                list(document_ids) if document_ids is not None else None,
                int(now_ms),
                int(fomc_lookback_days),
                int(now_ms),
                int(speech_lookback_days),
                model_name,
                prompt_version,
                model_name,
                prompt_version,
                int(limit),
            ),
        ).fetchall()
        role_rows = self.fed_official_role_history()
        roster_coverage = self.conn.execute(
            """
            SELECT
              (cursor_json ->> 'start_date')::date AS start_date,
              (cursor_json ->> 'end_date')::date AS end_date
            FROM macro_acquisition_targets
            WHERE dataset_id = 'federal_reserve.fomc.documents'
              AND clock_kind = 'backfill'
              AND status = 'current'
              AND cursor_json ? 'start_date'
              AND cursor_json ? 'end_date'
            """
        ).fetchall()
        written = 0
        for row in rows:
            if str(row["document_type"]) == "speech":
                metadata = row.get("metadata_json") or {}
                speaker_name = str(metadata.get("speaker_name") or "") if isinstance(metadata, dict) else ""
                effective_date = row["effective_date"]
                matched_role = match_effective_role(
                    speaker_name,
                    effective_date=effective_date,
                    role_rows=role_rows,
                )
                coverage_complete = any(
                    coverage["start_date"] <= effective_date <= coverage["end_date"] for coverage in roster_coverage
                )
                if matched_role is None and not coverage_complete:
                    continue
            document_hash = str((row.get("metadata_json") or {}).get("content_hash") or row["fact_hash"])
            identity = f"{row['document_id']}|{document_hash}|{model_name}|{prompt_version}"
            analysis_job_id = "macroda_" + hashlib.sha256(identity.encode()).hexdigest()
            cursor = self.conn.execute(
                """
                INSERT INTO macro_document_analysis_jobs(
                  analysis_job_id, document_id, document_hash, model_name,
                  prompt_version, status, next_due_at_ms, attempt_count,
                  max_attempts, created_at_ms, updated_at_ms
                )
                VALUES (%s, %s, %s, %s, %s, 'pending', %s, 0, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    analysis_job_id,
                    row["document_id"],
                    document_hash,
                    model_name,
                    prompt_version,
                    int(row["received_at_ms"]) + 60 * 60 * 1_000,
                    int(max_attempts),
                    int(now_ms),
                    int(now_ms),
                ),
            )
            written += int(cursor.rowcount)
        if refresh_projection_state:
            self.refresh_document_analysis_projection_state(updated_at_ms=now_ms)
        return written

    def claim_document_analysis_job(
        self,
        *,
        model_name: str,
        prompt_version: str,
        lease_owner: str,
        lease_ms: int,
        now_ms: int,
        analysis_job_id: str | None = None,
    ) -> dict[str, Any] | None:
        target_scope = "AND analysis_job_id = %(analysis_job_id)s" if analysis_job_id else ""
        row = self.conn.execute(
            f"""
            WITH expired AS (
              UPDATE macro_document_analysis_jobs
              SET status = CASE
                    WHEN attempt_count >= max_attempts THEN 'failed'
                    ELSE 'retryable'
                  END,
                  next_due_at_ms = %(now_ms)s,
                  leased_until_ms = NULL,
                  lease_owner = NULL,
                  last_error_code = 'document_analysis_lease_expired',
                  updated_at_ms = %(now_ms)s
              WHERE status = 'claimed'
                AND leased_until_ms <= %(now_ms)s
              RETURNING analysis_job_id
            ), candidate AS (
              SELECT analysis_job_id
              FROM macro_document_analysis_jobs
              WHERE status IN ('pending', 'retryable')
                AND next_due_at_ms <= %(now_ms)s
                AND model_name = %(model_name)s
                AND prompt_version = %(prompt_version)s
                {target_scope}
              ORDER BY next_due_at_ms, analysis_job_id
              FOR UPDATE SKIP LOCKED
              LIMIT 1
            )
            UPDATE macro_document_analysis_jobs AS jobs
            SET status = 'claimed',
                leased_until_ms = %(leased_until_ms)s,
                lease_owner = %(lease_owner)s,
                attempt_count = jobs.attempt_count + 1,
                updated_at_ms = %(now_ms)s
            FROM candidate
            WHERE jobs.analysis_job_id = candidate.analysis_job_id
            RETURNING jobs.*
            """,
            {
                "now_ms": int(now_ms),
                "model_name": model_name,
                "prompt_version": prompt_version,
                "analysis_job_id": analysis_job_id,
                "leased_until_ms": int(now_ms + lease_ms),
                "lease_owner": lease_owner,
            },
        ).fetchone()
        return dict(row) if row is not None else None

    def peek_document_analysis_job(
        self,
        *,
        model_name: str,
        prompt_version: str,
        now_ms: int,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT analysis_job_id, next_due_at_ms
              FROM macro_document_analysis_jobs
             WHERE model_name = %s
               AND prompt_version = %s
               AND (
                 (status IN ('pending', 'retryable') AND next_due_at_ms <= %s)
                 OR (status = 'claimed' AND leased_until_ms <= %s)
               )
               AND attempt_count < max_attempts
             ORDER BY next_due_at_ms, analysis_job_id
             LIMIT 1
            """,
            (model_name, prompt_version, int(now_ms), int(now_ms)),
        ).fetchone()
        return dict(row) if row is not None else None

    def release_document_analysis_claim(
        self,
        *,
        analysis_job_id: str,
        lease_owner: str,
        claimed_attempt_count: int,
    ) -> bool:
        row = self.conn.execute(
            """
            UPDATE macro_document_analysis_jobs
               SET status = CASE
                     WHEN attempt_count = 1 THEN 'pending'
                     ELSE 'retryable'
                   END,
                   leased_until_ms = NULL,
                   lease_owner = NULL,
                   attempt_count = attempt_count - 1
             WHERE analysis_job_id = %s
               AND status = 'claimed'
               AND lease_owner = %s
               AND attempt_count = %s
               AND attempt_count > 0
            RETURNING *
            """,
            (analysis_job_id, lease_owner, int(claimed_attempt_count)),
        ).fetchone()
        return row is not None

    def document_analysis_job_document(self, analysis_job_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT documents.*, jobs.analysis_job_id, jobs.document_hash,
                   jobs.model_name, jobs.prompt_version
            FROM macro_document_analysis_jobs AS jobs
            JOIN macro_documents AS documents USING (document_id)
            WHERE jobs.analysis_job_id = %s
            """,
            (analysis_job_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def prior_document_analysis(
        self,
        *,
        effective_date: date,
        official_id: str | None,
        document_type: str,
    ) -> dict[str, Any] | None:
        if official_id:
            predicate = "analyses.official_id = %s"
            value = official_id
        else:
            predicate = "analyses.official_id IS NULL AND documents.document_type = %s"
            value = document_type
        row = self.conn.execute(
            f"""
            SELECT analyses.*, documents.effective_date, documents.title
            FROM macro_document_analyses AS analyses
            JOIN macro_documents AS documents USING (document_id)
            WHERE {predicate}
              AND documents.effective_date < %s
              AND analyses.policy_relevance = 'policy_signal'
              AND analyses.reviewer_disposition = 'pass'
            ORDER BY documents.effective_date DESC, analyses.created_at_ms DESC
            LIMIT 1
            """,
            (value, effective_date),
        ).fetchone()
        return dict(row) if row is not None else None

    def insert_document_analysis(
        self,
        *,
        analysis_id: str,
        document_id: str,
        document_hash: str,
        official_id: str | None,
        policy_relevance: str,
        stance: str,
        confidence: float | None,
        analysis: dict[str, Any],
        model_name: str,
        prompt_version: str,
        reviewer_disposition: str,
        created_at_ms: int,
        payload_hash: str,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO macro_document_analyses(
              analysis_id, document_id, document_hash, official_id,
              policy_relevance, stance, confidence, analysis_json,
              model_name, prompt_version, reviewer_disposition,
              created_at_ms, payload_hash
            )
            VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
              %s, %s, %s, %s, %s
            )
            ON CONFLICT DO NOTHING
            """,
            (
                analysis_id,
                document_id,
                document_hash,
                official_id,
                policy_relevance,
                stance,
                confidence,
                json.dumps(analysis, sort_keys=True),
                model_name,
                prompt_version,
                reviewer_disposition,
                int(created_at_ms),
                payload_hash,
            ),
        )
        return int(cursor.rowcount)

    def complete_document_analysis_job(
        self,
        *,
        analysis_job_id: str,
        lease_owner: str,
        completed_at_ms: int,
    ) -> bool:
        row = self.conn.execute(
            """
            UPDATE macro_document_analysis_jobs
            SET status = 'completed',
                leased_until_ms = NULL,
                lease_owner = NULL,
                last_error_code = NULL,
                updated_at_ms = %s
            WHERE analysis_job_id = %s
              AND status = 'claimed'
              AND lease_owner = %s
            RETURNING analysis_job_id
            """,
            (int(completed_at_ms), analysis_job_id, lease_owner),
        ).fetchone()
        return row is not None

    def fail_document_analysis_job(
        self,
        *,
        job: dict[str, Any],
        lease_owner: str,
        error_code: str,
        next_due_at_ms: int,
        completed_at_ms: int,
    ) -> bool:
        status = "failed" if int(job["attempt_count"]) >= int(job["max_attempts"]) else "retryable"
        row = self.conn.execute(
            """
            UPDATE macro_document_analysis_jobs
            SET status = %s,
                next_due_at_ms = %s,
                leased_until_ms = NULL,
                lease_owner = NULL,
                last_error_code = %s,
                updated_at_ms = %s
            WHERE analysis_job_id = %s
              AND status = 'claimed'
              AND lease_owner = %s
            RETURNING *
            """,
            (
                status,
                int(next_due_at_ms),
                error_code,
                int(completed_at_ms),
                job["analysis_job_id"],
                lease_owner,
            ),
        ).fetchone()
        if row is None:
            return False
        if status == "failed":
            source_row = {**dict(row), "native_target_key": str(row["analysis_job_id"])}
            terminalize_source_row(
                self.conn,
                owner_key="macro_document_analysis",
                source_table="macro_document_analysis_jobs",
                target_key=str(row["analysis_job_id"]),
                source_row=source_row,
                final_status="failed",
                final_reason=str(error_code),
                final_reason_bucket="model_attempts_exhausted",
                now_ms=int(completed_at_ms),
                attempt_count=int(row["attempt_count"]),
            )
        return True

    def upsert_module_current(
        self,
        *,
        module_id: str,
        current_health_state: str,
        history_depth_state: str,
        fact_cutoff_ms: int,
        payload: dict[str, Any],
        payload_hash: str,
        updated_at_ms: int,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO macro_module_current(
              module_id, current_health_state, history_depth_state,
              fact_cutoff_ms, payload_json, payload_hash, updated_at_ms
            )
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
            ON CONFLICT(module_id) DO UPDATE SET
              current_health_state = EXCLUDED.current_health_state,
              history_depth_state = EXCLUDED.history_depth_state,
              fact_cutoff_ms = EXCLUDED.fact_cutoff_ms,
              payload_json = EXCLUDED.payload_json,
              payload_hash = EXCLUDED.payload_hash,
              updated_at_ms = EXCLUDED.updated_at_ms
            WHERE macro_module_current.payload_hash IS DISTINCT FROM EXCLUDED.payload_hash
            """,
            (
                module_id,
                current_health_state,
                history_depth_state,
                int(fact_cutoff_ms),
                json.dumps(payload, sort_keys=True),
                payload_hash,
                int(updated_at_ms),
            ),
        )
        return int(cursor.rowcount)

    def module_current(self, module_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM macro_module_current WHERE module_id = %s",
            (module_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def all_modules_current(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM macro_module_current ORDER BY module_id").fetchall()
        return [dict(row) for row in rows]


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _row_limit(value: int | None) -> int:
    if value is None:
        return 2_147_483_647
    parsed = int(value)
    if parsed < 0:
        raise ValueError("macro_repository_row_cap_required")
    return parsed + 1


__all__ = ["MacroRepository"]
