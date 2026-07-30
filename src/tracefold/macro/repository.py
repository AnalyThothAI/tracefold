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
              updated_at_ms = EXCLUDED.updated_at_ms
            WHERE macro_acquisition_targets.clock_kind IS DISTINCT FROM EXCLUDED.clock_kind
               OR macro_acquisition_targets.max_attempts IS DISTINCT FROM EXCLUDED.max_attempts
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
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            WITH expired AS (
              UPDATE macro_acquisition_targets
              SET status = CASE
                    WHEN attempt_count >= max_attempts THEN 'stale'
                    ELSE 'delayed'
                  END,
                  leased_until_ms = NULL,
                  lease_owner = NULL,
                  next_due_at_ms = CASE
                    WHEN attempt_count >= max_attempts THEN next_due_at_ms
                    ELSE %s
                  END,
                  last_error_code = 'acquisition_lease_expired',
                  updated_at_ms = %s
              WHERE clock_kind = %s
                AND status = 'claimed'
                AND leased_until_ms <= %s
              RETURNING target_key
            ), candidate AS (
              SELECT target_key
              FROM macro_acquisition_targets
              WHERE clock_kind = %s
                AND status IN (
                  'pending', 'current', 'delayed', 'backfilling'
                )
                AND next_due_at_ms <= %s
                AND status <> 'unavailable'
              ORDER BY priority, next_due_at_ms, target_key
              FOR UPDATE SKIP LOCKED
              LIMIT 1
            )
            UPDATE macro_acquisition_targets AS target
            SET status = 'claimed',
                leased_until_ms = %s,
                lease_owner = %s,
                attempt_count = target.attempt_count + 1,
                updated_at_ms = %s
            FROM candidate
            WHERE target.target_key = candidate.target_key
            RETURNING target.*
            """,
            (
                int(now_ms),
                int(now_ms),
                clock_kind,
                int(now_ms),
                clock_kind,
                int(now_ms),
                int(now_ms + lease_ms),
                lease_owner,
                int(now_ms),
            ),
        ).fetchone()
        return dict(row) if row is not None else None

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

    def record_receipt(
        self,
        *,
        target: dict[str, Any],
        receipt_id: str,
        started_at_ms: int,
        completed_at_ms: int,
        status: str,
        http_status: int | None,
        rows_seen: int,
        rows_inserted: int,
        response_hash: str | None,
        error_code: str | None,
        error_message: str | None,
        diagnostics: dict[str, Any],
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO macro_source_receipts(
              receipt_id, target_key, dataset_id, partition_key, started_at_ms,
              completed_at_ms, status, http_status, rows_seen, rows_inserted,
              response_hash, error_code, error_message, diagnostics_json
            )
            VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb
            )
            """,
            (
                receipt_id,
                target["target_key"],
                target["dataset_id"],
                target["partition_key"],
                int(started_at_ms),
                int(completed_at_ms),
                status,
                http_status,
                int(rows_seen),
                int(rows_inserted),
                response_hash,
                error_code,
                error_message,
                json.dumps(diagnostics, sort_keys=True),
            ),
        )

    def complete_target(
        self,
        *,
        target_key: str,
        lease_owner: str,
        receipt_id: str,
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
                last_receipt_id = %s,
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
                receipt_id,
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
        receipt_id: str,
        error_code: str,
        next_due_at_ms: int,
        completed_at_ms: int,
        unavailable: bool,
    ) -> bool:
        if unavailable:
            status = "unavailable"
        elif int(target["attempt_count"]) >= int(target["max_attempts"]):
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
                last_receipt_id = %s,
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
                receipt_id,
                error_code,
                int(completed_at_ms),
                target["target_key"],
                lease_owner,
            ),
        ).fetchone()
        return row is not None

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
                  max_attempts, last_receipt_id, last_success_at_ms,
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

    def projection_state(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM macro_projection_state
            WHERE singleton_key = 'current'
            """
        ).fetchone()
        return dict(row) if row is not None else None

    def upsert_projection_state(
        self,
        *,
        input_fingerprint: str,
        feature_count: int,
        module_count: int,
        projected_at_ms: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO macro_projection_state (
              singleton_key, input_fingerprint, feature_count,
              module_count, projected_at_ms
            )
            VALUES ('current', %s, %s, %s, %s)
            ON CONFLICT (singleton_key) DO UPDATE SET
              input_fingerprint = EXCLUDED.input_fingerprint,
              feature_count = EXCLUDED.feature_count,
              module_count = EXCLUDED.module_count,
              projected_at_ms = EXCLUDED.projected_at_ms
            """,
            (
                input_fingerprint,
                int(feature_count),
                int(module_count),
                int(projected_at_ms),
            ),
        )

    def receipt_states_at(
        self,
        *,
        cutoff_ms: int,
        dataset_ids: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT DISTINCT ON (receipts.dataset_id, receipts.partition_key)
              receipts.target_key,
              receipts.dataset_id,
              receipts.partition_key,
              CASE
                WHEN receipts.status IN ('ok', 'not_modified', 'empty') THEN 'current'
                WHEN receipts.status = 'invalid' THEN 'invalid'
                ELSE 'failed'
              END AS status,
              CASE
                WHEN receipts.partition_key = 'latest' THEN registry.clock_kind
                ELSE 'backfill'
              END AS clock_kind,
              receipts.completed_at_ms AS last_success_at_ms,
              receipts.completed_at_ms AS updated_at_ms,
              receipts.error_code AS last_error_code,
              '{}'::jsonb AS cursor_json
            FROM macro_source_receipts AS receipts
            JOIN macro_acquisition_targets AS registry
              ON registry.target_key = receipts.target_key
            WHERE receipts.completed_at_ms <= %s
              AND (
                cardinality(%s::text[]) = 0
                OR receipts.dataset_id = ANY(%s)
              )
            ORDER BY
              receipts.dataset_id,
              receipts.partition_key,
              receipts.completed_at_ms DESC,
              receipts.receipt_id DESC
            """,
            (
                int(cutoff_ms),
                list(dataset_ids),
                list(dataset_ids),
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    def recent_receipts(self, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT
              receipt_id,
              target_key,
              dataset_id,
              partition_key,
              completed_at_ms,
              status,
              http_status,
              rows_seen,
              rows_inserted,
              error_code,
              error_message,
              diagnostics_json
            FROM macro_source_receipts
            ORDER BY completed_at_ms DESC, receipt_id DESC
            LIMIT %s
            """,
            (int(limit),),
        ).fetchall()
        return [dict(row) for row in rows]

    def series_history(
        self,
        *,
        history_limits: Mapping[str, int],
        received_before_ms: int | None = None,
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
            """,
            (
                list(requested),
                list(requested.values()),
                received_before_ms,
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    def release_history(
        self,
        *,
        dataset_ids: tuple[str, ...],
        received_before_ms: int | None = None,
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
            """,
            (list(dataset_ids), received_before_ms),
        ).fetchall()
        return [dict(row) for row in rows]

    def document_history(
        self,
        *,
        dataset_ids: tuple[str, ...],
        received_before_ms: int | None = None,
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
            """,
            (list(dataset_ids), received_before_ms),
        ).fetchall()
        return [dict(row) for row in rows]

    def fed_official_role_history(
        self,
        *,
        received_before_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT *
            FROM macro_fed_official_role_facts
            WHERE received_at_ms <= COALESCE(%s::bigint, received_at_ms)
            ORDER BY effective_start, official_id, role_fact_id
            """,
            (received_before_ms,),
        ).fetchall()
        return [dict(row) for row in rows]

    def document_analysis_history(
        self,
        *,
        received_before_ms: int | None = None,
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
            """,
            (received_before_ms, received_before_ms),
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
    ) -> int:
        rows = self.conn.execute(
            """
            SELECT
              documents.document_id,
              documents.document_type,
              documents.effective_date,
              documents.fact_hash,
              documents.metadata_json
            FROM macro_documents AS documents
            WHERE documents.document_type IN (
              'statement', 'implementation', 'minutes', 'sep', 'speech'
            )
              AND documents.dataset_id IN (
                'federal_reserve.fomc.documents',
                'federal_reserve.board.speeches',
                'federal_reserve.reserve_bank.speeches'
              )
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
                    int(now_ms),
                    int(max_attempts),
                    int(now_ms),
                    int(now_ms),
                ),
            )
            written += int(cursor.rowcount)
        return written

    def claim_document_analysis_job(
        self,
        *,
        model_name: str,
        prompt_version: str,
        lease_owner: str,
        lease_ms: int,
        now_ms: int,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            WITH expired AS (
              UPDATE macro_document_analysis_jobs
              SET status = CASE
                    WHEN attempt_count >= max_attempts THEN 'failed'
                    ELSE 'retryable'
                  END,
                  next_due_at_ms = %s,
                  leased_until_ms = NULL,
                  lease_owner = NULL,
                  last_error_code = 'document_analysis_lease_expired',
                  updated_at_ms = %s
              WHERE status = 'claimed'
                AND leased_until_ms <= %s
              RETURNING analysis_job_id
            ), candidate AS (
              SELECT analysis_job_id
              FROM macro_document_analysis_jobs
              WHERE status IN ('pending', 'retryable')
                AND next_due_at_ms <= %s
                AND model_name = %s
                AND prompt_version = %s
              ORDER BY next_due_at_ms, analysis_job_id
              FOR UPDATE SKIP LOCKED
              LIMIT 1
            )
            UPDATE macro_document_analysis_jobs AS jobs
            SET status = 'claimed',
                leased_until_ms = %s,
                lease_owner = %s,
                attempt_count = jobs.attempt_count + 1,
                updated_at_ms = %s
            FROM candidate
            WHERE jobs.analysis_job_id = candidate.analysis_job_id
            RETURNING jobs.*
            """,
            (
                int(now_ms),
                int(now_ms),
                int(now_ms),
                int(now_ms),
                model_name,
                prompt_version,
                int(now_ms + lease_ms),
                lease_owner,
                int(now_ms),
            ),
        ).fetchone()
        return dict(row) if row is not None else None

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
            RETURNING analysis_job_id
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
        return row is not None

    def upsert_feature(
        self,
        *,
        feature_id: str,
        as_of_date: date,
        formula_version: str,
        value_numeric: float,
        unit: str,
        inputs: list[dict[str, Any]],
        payload_hash: str,
        computed_at_ms: int,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO macro_feature_series(
              feature_id, as_of_date, formula_version, value_numeric,
              unit, inputs_json, payload_hash, computed_at_ms
            )
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            ON CONFLICT(feature_id, as_of_date) DO UPDATE SET
              formula_version = EXCLUDED.formula_version,
              value_numeric = EXCLUDED.value_numeric,
              unit = EXCLUDED.unit,
              inputs_json = EXCLUDED.inputs_json,
              payload_hash = EXCLUDED.payload_hash,
              computed_at_ms = EXCLUDED.computed_at_ms
            WHERE macro_feature_series.payload_hash IS DISTINCT FROM EXCLUDED.payload_hash
            """,
            (
                feature_id,
                as_of_date,
                formula_version,
                value_numeric,
                unit,
                json.dumps(inputs, sort_keys=True),
                payload_hash,
                int(computed_at_ms),
            ),
        )
        return int(cursor.rowcount)

    def feature_history(self, *, feature_ids: tuple[str, ...]) -> list[dict[str, Any]]:
        if not feature_ids:
            return []
        rows = self.conn.execute(
            """
            SELECT *
            FROM macro_feature_series
            WHERE feature_id = ANY(%s)
            ORDER BY feature_id, as_of_date
            """,
            (list(feature_ids),),
        ).fetchall()
        return [dict(row) for row in rows]

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


__all__ = ["MacroRepository"]
