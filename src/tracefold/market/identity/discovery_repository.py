from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

from psycopg.types.json import Jsonb

from tracefold.platform.postgres.queue_terminal import terminalize_source_row
from tracefold.platform.postgres.write_contract import expect_mutation_count, mutation_count, returning_mutation_count
from tracefold.platform.validation import require_nonnegative_int, require_positive_int

DISCOVERY_PROVIDER = "okx_dex_search"
DISCOVERY_LOOKUP_QUEUE_TABLE = "token_discovery_dirty_lookup_keys"


class DiscoveryRepository:
    def __init__(self, conn: Any):
        self.conn = conn

    def enqueue_lookup_keys(
        self,
        lookup_keys: Iterable[str],
        *,
        reason: str,
        now_ms: int,
        due_at_ms: int | None = None,
        latest_seen_ms: int | None = None,
        intent_count: int = 1,
    ) -> int:
        records = _lookup_key_records(
            lookup_keys,
            reason=reason,
            now_ms=now_ms,
            due_at_ms=due_at_ms,
            latest_seen_ms=latest_seen_ms,
            intent_count=intent_count,
        )
        if not records:
            return 0

        cursor = self.conn.execute(
            """
            WITH incoming(
              provider, lookup_key, lookup_type, dirty_reason, payload_hash,
              due_at_ms, latest_seen_ms, intent_count, refresh_priority
            ) AS (
              SELECT *
              FROM unnest(
                %(providers)s::text[],
                %(lookup_keys)s::text[],
                %(lookup_types)s::text[],
                %(dirty_reasons)s::text[],
                %(payload_hashes)s::text[],
                %(due_at_ms_values)s::bigint[],
                %(latest_seen_ms_values)s::bigint[],
                %(intent_counts)s::bigint[],
                %(refresh_priorities)s::integer[]
              )
            )
            INSERT INTO token_discovery_dirty_lookup_keys(
              provider,
              lookup_key,
              lookup_type,
              dirty_reason,
              payload_hash,
              due_at_ms,
              latest_seen_ms,
              intent_count,
              refresh_priority,
              leased_until_ms,
              lease_owner,
              attempt_count,
              last_error,
              first_dirty_at_ms,
              updated_at_ms
            )
            SELECT
              incoming.provider,
              incoming.lookup_key,
              incoming.lookup_type,
              incoming.dirty_reason,
              incoming.payload_hash,
              incoming.due_at_ms,
              incoming.latest_seen_ms,
              incoming.intent_count,
              incoming.refresh_priority,
              NULL,
              NULL,
              0,
              NULL,
              %(now_ms)s,
              %(now_ms)s
            FROM incoming
            ON CONFLICT(provider, lookup_key) DO UPDATE SET
              lookup_type = EXCLUDED.lookup_type,
              dirty_reason = EXCLUDED.dirty_reason,
              payload_hash = EXCLUDED.payload_hash,
              due_at_ms = LEAST(token_discovery_dirty_lookup_keys.due_at_ms, EXCLUDED.due_at_ms),
              latest_seen_ms = GREATEST(
                token_discovery_dirty_lookup_keys.latest_seen_ms,
                EXCLUDED.latest_seen_ms
              ),
              intent_count = GREATEST(token_discovery_dirty_lookup_keys.intent_count, EXCLUDED.intent_count),
              refresh_priority = LEAST(
                token_discovery_dirty_lookup_keys.refresh_priority,
                EXCLUDED.refresh_priority
              ),
              leased_until_ms = NULL,
              lease_owner = NULL,
              attempt_count = CASE
                WHEN token_discovery_dirty_lookup_keys.payload_hash IS DISTINCT FROM EXCLUDED.payload_hash
                THEN 0
                ELSE token_discovery_dirty_lookup_keys.attempt_count
              END,
              last_error = NULL,
              reprocess_lookup_keys = CASE
                WHEN token_discovery_dirty_lookup_keys.payload_hash IS DISTINCT FROM EXCLUDED.payload_hash
                THEN NULL
                ELSE token_discovery_dirty_lookup_keys.reprocess_lookup_keys
              END,
              reprocess_after_intent_id = CASE
                WHEN token_discovery_dirty_lookup_keys.payload_hash IS DISTINCT FROM EXCLUDED.payload_hash
                THEN NULL
                ELSE token_discovery_dirty_lookup_keys.reprocess_after_intent_id
              END,
              reprocess_resolved = CASE
                WHEN token_discovery_dirty_lookup_keys.payload_hash IS DISTINCT FROM EXCLUDED.payload_hash
                THEN false
                ELSE token_discovery_dirty_lookup_keys.reprocess_resolved
              END,
              reprocess_queue_due_at_ms = CASE
                WHEN token_discovery_dirty_lookup_keys.payload_hash IS DISTINCT FROM EXCLUDED.payload_hash
                THEN NULL
                ELSE token_discovery_dirty_lookup_keys.reprocess_queue_due_at_ms
              END,
              first_dirty_at_ms = token_discovery_dirty_lookup_keys.first_dirty_at_ms,
              updated_at_ms = EXCLUDED.updated_at_ms
            WHERE token_discovery_dirty_lookup_keys.payload_hash IS DISTINCT FROM EXCLUDED.payload_hash
               OR token_discovery_dirty_lookup_keys.dirty_reason IS DISTINCT FROM EXCLUDED.dirty_reason
               OR token_discovery_dirty_lookup_keys.due_at_ms > EXCLUDED.due_at_ms
               OR token_discovery_dirty_lookup_keys.latest_seen_ms < EXCLUDED.latest_seen_ms
               OR token_discovery_dirty_lookup_keys.intent_count < EXCLUDED.intent_count
               OR token_discovery_dirty_lookup_keys.refresh_priority > EXCLUDED.refresh_priority
               OR token_discovery_dirty_lookup_keys.leased_until_ms IS NOT NULL
               OR token_discovery_dirty_lookup_keys.last_error IS NOT NULL
            """,
            _lookup_key_params(records, now_ms=now_ms),
        )
        return mutation_count(cursor, error_code="discovery_repository_rowcount_invalid")

    def claim_due_lookup_keys(
        self,
        *,
        now_ms: int,
        limit: int,
        lease_ms: int,
        running_timeout_ms: int,
        lease_owner: str,
        hot_since_ms: int | None = None,
        hot_not_found_retry_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        parsed_lease_ms = require_positive_int(lease_ms, error_code="discovery_lookup_claim_lease_ms_required")
        params = _due_params(
            now_ms=now_ms,
            limit=limit,
            running_timeout_ms=running_timeout_ms,
            hot_since_ms=hot_since_ms,
            hot_not_found_retry_ms=hot_not_found_retry_ms,
        )
        params.update(
            {
                "lease_owner": str(lease_owner),
                "leased_until_ms": int(now_ms) + parsed_lease_ms,
            }
        )

        cursor = self.conn.execute(
            """
            WITH due AS (
              SELECT queue.provider, queue.lookup_key
              FROM token_discovery_dirty_lookup_keys queue
              LEFT JOIN token_discovery_results AS results
                ON results.provider = queue.provider
               AND results.lookup_key = queue.lookup_key
              WHERE queue.provider = %(provider)s
                AND queue.due_at_ms <= %(now_ms)s
                AND (queue.leased_until_ms IS NULL OR queue.leased_until_ms <= %(now_ms)s)
                AND (
                  queue.reprocess_lookup_keys IS NOT NULL
                  OR results.lookup_key IS NULL
                  OR results.next_refresh_at_ms <= %(now_ms)s
                  OR (
                    results.status = 'running'
                    AND results.updated_at_ms < %(running_expired_ms)s
                  )
                  OR (
                    %(hot_since_ms)s::bigint IS NOT NULL
                    AND %(hot_not_found_retry_ms)s::bigint IS NOT NULL
                    AND queue.latest_seen_ms >= %(hot_since_ms)s::bigint
                    AND results.status = 'not_found'
                    AND results.last_lookup_at_ms <= %(now_ms)s::bigint - %(hot_not_found_retry_ms)s::bigint
                  )
                )
              ORDER BY
                CASE WHEN queue.reprocess_lookup_keys IS NOT NULL THEN 0 ELSE 1 END ASC,
                CASE
                  WHEN %(hot_since_ms)s::bigint IS NOT NULL AND queue.latest_seen_ms >= %(hot_since_ms)s::bigint
                    THEN 0
                  ELSE 1
                END ASC,
                queue.refresh_priority ASC,
                queue.latest_seen_ms DESC,
                CASE
                  WHEN results.lookup_key IS NULL THEN 0
                  WHEN results.status = 'error' THEN 1
                  ELSE 2
                END,
                queue.intent_count DESC,
                queue.lookup_key ASC
              LIMIT %(limit)s
              FOR UPDATE OF queue SKIP LOCKED
            )
            UPDATE token_discovery_dirty_lookup_keys queue
            SET leased_until_ms = %(leased_until_ms)s,
                lease_owner = %(lease_owner)s,
                attempt_count = queue.attempt_count + CASE
                  WHEN queue.reprocess_lookup_keys IS NULL OR queue.attempt_count = 0 THEN 1
                  ELSE 0
                END,
                last_error = NULL,
                reprocess_lookup_keys = CASE
                  WHEN queue.attempt_count = 0 THEN NULL
                  ELSE queue.reprocess_lookup_keys
                END,
                reprocess_after_intent_id = CASE
                  WHEN queue.attempt_count = 0 THEN NULL
                  ELSE queue.reprocess_after_intent_id
                END,
                reprocess_resolved = CASE
                  WHEN queue.attempt_count = 0 THEN false
                  ELSE queue.reprocess_resolved
                END,
                reprocess_queue_due_at_ms = CASE
                  WHEN queue.attempt_count = 0 THEN NULL
                  ELSE queue.reprocess_queue_due_at_ms
                END,
                updated_at_ms = %(now_ms)s
            FROM due
            LEFT JOIN token_discovery_results AS results
              ON results.provider = due.provider
             AND results.lookup_key = due.lookup_key
            WHERE queue.provider = due.provider
              AND queue.lookup_key = due.lookup_key
            RETURNING
              queue.*,
              results.status,
              results.result_hash,
              results.next_refresh_at_ms,
              COALESCE(results.error_count, 0) AS error_count
            """,
            params,
        )
        rows = cursor.fetchall()
        expect_mutation_count(cursor, expected=len(rows), error_code="discovery_repository_rowcount_invalid")
        return [dict(row) for row in rows]

    def mark_lookup_done(
        self,
        claims: Iterable[Mapping[str, Any]],
        *,
        now_ms: int,
    ) -> int:
        records = _claim_records(claims)
        if not records:
            return 0

        cursor = self.conn.execute(
            """
            WITH done AS (
              SELECT *
              FROM unnest(
                %(providers)s::text[],
                %(lookup_keys)s::text[],
                %(payload_hashes)s::text[],
                %(lease_owners)s::text[],
                %(attempt_counts)s::bigint[]
              ) AS done(provider, lookup_key, payload_hash, lease_owner, attempt_count)
            )
            DELETE FROM token_discovery_dirty_lookup_keys queue
            USING done
            WHERE queue.provider = done.provider
              AND queue.lookup_key = done.lookup_key
              AND queue.payload_hash = done.payload_hash
              AND queue.lease_owner = done.lease_owner
              AND queue.attempt_count = done.attempt_count
            """,
            _claim_params(records),
        )
        return mutation_count(cursor, error_code="discovery_repository_rowcount_invalid")

    def reschedule_lookup_claims(
        self,
        claims: Iterable[Mapping[str, Any]],
        *,
        due_at_ms: int,
        now_ms: int,
        last_error: str | None = None,
    ) -> int:
        records = _claim_records(claims)
        if not records:
            return 0
        params = _claim_params(records)
        params.update(
            {
                "due_at_ms": int(due_at_ms),
                "now_ms": int(now_ms),
                "last_error": str(last_error)[:2048] if last_error else None,
            }
        )

        cursor = self.conn.execute(
            """
            WITH rescheduled AS (
              SELECT *
              FROM unnest(
                %(providers)s::text[],
                %(lookup_keys)s::text[],
                %(payload_hashes)s::text[],
                %(lease_owners)s::text[],
                %(attempt_counts)s::bigint[]
              ) AS rescheduled(provider, lookup_key, payload_hash, lease_owner, attempt_count)
            )
            UPDATE token_discovery_dirty_lookup_keys queue
            SET due_at_ms = %(due_at_ms)s,
                leased_until_ms = NULL,
                lease_owner = NULL,
                last_error = %(last_error)s,
                reprocess_lookup_keys = NULL,
                reprocess_after_intent_id = NULL,
                reprocess_resolved = false,
                reprocess_queue_due_at_ms = NULL,
                updated_at_ms = %(now_ms)s
            FROM rescheduled
            WHERE queue.provider = rescheduled.provider
              AND queue.lookup_key = rescheduled.lookup_key
              AND queue.payload_hash = rescheduled.payload_hash
              AND queue.lease_owner = rescheduled.lease_owner
              AND queue.attempt_count = rescheduled.attempt_count
            """,
            params,
        )
        return mutation_count(cursor, error_code="discovery_repository_rowcount_invalid")

    def reschedule_lookup_claims_without_attempt(
        self,
        claims: Iterable[Mapping[str, Any]],
        *,
        due_at_ms: int,
        now_ms: int,
        last_error: str,
    ) -> int:
        """Release provider-wide failures without spending target attempts."""

        records = _claim_records(claims)
        if not records:
            return 0
        params = _claim_params(records)
        params.update(
            {
                "due_at_ms": int(due_at_ms),
                "now_ms": int(now_ms),
                "last_error": str(last_error)[:2048],
            }
        )
        cursor = self.conn.execute(
            """
            WITH rescheduled AS (
              SELECT *
              FROM unnest(
                %(providers)s::text[],
                %(lookup_keys)s::text[],
                %(payload_hashes)s::text[],
                %(lease_owners)s::text[],
                %(attempt_counts)s::bigint[]
              ) AS rescheduled(
                provider, lookup_key, payload_hash, lease_owner, attempt_count
              )
            )
            UPDATE token_discovery_dirty_lookup_keys queue
            SET due_at_ms = %(due_at_ms)s,
                leased_until_ms = NULL,
                lease_owner = NULL,
                attempt_count = queue.attempt_count - 1,
                last_error = %(last_error)s,
                updated_at_ms = %(now_ms)s
            FROM rescheduled
            WHERE queue.provider = rescheduled.provider
              AND queue.lookup_key = rescheduled.lookup_key
              AND queue.payload_hash = rescheduled.payload_hash
              AND queue.lease_owner = rescheduled.lease_owner
              AND queue.attempt_count = rescheduled.attempt_count
              AND queue.attempt_count > 0
            """,
            params,
        )
        return mutation_count(
            cursor,
            error_code="discovery_repository_rowcount_invalid",
        )

    def release_lookup_claims(self, claims: Iterable[Mapping[str, Any]]) -> int:
        """Release exact pre-work claims without changing due/error state."""

        records = _claim_records(claims)
        if not records:
            return 0
        cursor = self.conn.execute(
            """
            WITH released AS (
              SELECT *
              FROM unnest(
                %(providers)s::text[],
                %(lookup_keys)s::text[],
                %(payload_hashes)s::text[],
                %(lease_owners)s::text[],
                %(attempt_counts)s::bigint[]
              ) AS released(
                provider, lookup_key, payload_hash, lease_owner, attempt_count
              )
            )
            UPDATE token_discovery_dirty_lookup_keys queue
               SET leased_until_ms = NULL,
                   lease_owner = NULL,
                   attempt_count = queue.attempt_count - CASE
                     WHEN queue.reprocess_lookup_keys IS NULL THEN 1
                     ELSE 0
                   END
              FROM released
             WHERE queue.provider = released.provider
               AND queue.lookup_key = released.lookup_key
               AND queue.payload_hash = released.payload_hash
               AND queue.lease_owner = released.lease_owner
               AND queue.attempt_count = released.attempt_count
            """,
            _claim_params(records),
        )
        return mutation_count(cursor, error_code="discovery_repository_rowcount_invalid")

    def save_reprocess_continuation(
        self,
        claim: Mapping[str, Any],
        *,
        lookup_keys: list[str],
        after_intent_id: str | None,
        resolved: bool,
        queue_due_at_ms: int,
        now_ms: int,
    ) -> bool:
        records = _claim_records([claim])
        keys = sorted({str(key).strip() for key in lookup_keys if str(key).strip()})
        cursor_id = None if after_intent_id is None else str(after_intent_id).strip()
        if not keys or cursor_id == "":
            raise ValueError("resolution_reprocess_continuation_required")
        params = _claim_params(records)
        params.update(
            {
                "reprocess_lookup_keys": keys,
                "reprocess_after_intent_id": cursor_id,
                "reprocess_resolved": bool(resolved),
                "reprocess_queue_due_at_ms": int(queue_due_at_ms),
                "now_ms": int(now_ms),
            }
        )
        cursor = self.conn.execute(
            """
            WITH claimed AS (
              SELECT *
              FROM unnest(
                %(providers)s::text[],
                %(lookup_keys)s::text[],
                %(payload_hashes)s::text[],
                %(lease_owners)s::text[],
                %(attempt_counts)s::bigint[]
              ) AS claimed(provider, lookup_key, payload_hash, lease_owner, attempt_count)
            )
            UPDATE token_discovery_dirty_lookup_keys queue
               SET due_at_ms = %(now_ms)s,
                   leased_until_ms = NULL,
                   lease_owner = NULL,
                   last_error = NULL,
                   reprocess_lookup_keys = %(reprocess_lookup_keys)s::text[],
                   reprocess_after_intent_id = %(reprocess_after_intent_id)s,
                   reprocess_resolved = %(reprocess_resolved)s,
                   reprocess_queue_due_at_ms = %(reprocess_queue_due_at_ms)s,
                   updated_at_ms = %(now_ms)s
              FROM claimed
             WHERE queue.provider = claimed.provider
               AND queue.lookup_key = claimed.lookup_key
               AND queue.payload_hash = claimed.payload_hash
               AND queue.lease_owner = claimed.lease_owner
               AND queue.attempt_count = claimed.attempt_count
            """,
            params,
        )
        return mutation_count(cursor, error_code="discovery_repository_rowcount_invalid") == 1

    def terminalize_lookup_claims(
        self,
        claims: Iterable[Mapping[str, Any]],
        *,
        owner_key: str,
        final_status: str,
        final_reason: str,
        now_ms: int,
    ) -> dict[str, int]:
        claim_rows = [dict(claim) for claim in claims]
        records = _claim_records(claim_rows)
        if not records:
            return {"terminalized": 0, "deleted": 0}
        deleted_rows, deleted_count = self._delete_lookup_claims_returning(records)
        if deleted_count != len(records):
            if deleted_count == 0:
                return {"terminalized": 0, "deleted": 0}
            raise ValueError("terminalize_lookup_delete_mismatch")
        terminalized = 0
        for row in deleted_rows:
            provider = str(row.get("provider") or DISCOVERY_PROVIDER)
            lookup_key = str(row.get("lookup_key") or "").strip()
            if not lookup_key:
                continue
            terminalize_source_row(
                self.conn,
                owner_key=owner_key,
                source_table=DISCOVERY_LOOKUP_QUEUE_TABLE,
                target_key=f"{provider}:{lookup_key}",
                source_row=row,
                final_status=final_status,
                final_reason=final_reason,
                now_ms=now_ms,
                payload_hash=_terminal_source_payload_hash(row),
                first_seen_at_ms=_optional_int(row.get("first_dirty_at_ms")),
                last_attempted_at_ms=now_ms,
            )
            terminalized += 1
        return {"terminalized": terminalized, "deleted": deleted_count}

    def _delete_lookup_claims_returning(self, records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        cursor = self.conn.execute(
            """
            WITH done AS (
              SELECT *
              FROM unnest(
                %(providers)s::text[],
                %(lookup_keys)s::text[],
                %(payload_hashes)s::text[],
                %(lease_owners)s::text[],
                %(attempt_counts)s::bigint[]
              ) AS done(provider, lookup_key, payload_hash, lease_owner, attempt_count)
            )
            DELETE FROM token_discovery_dirty_lookup_keys queue
            USING done
            WHERE queue.provider = done.provider
              AND queue.lookup_key = done.lookup_key
              AND queue.payload_hash = done.payload_hash
              AND queue.lease_owner = done.lease_owner
              AND queue.attempt_count = done.attempt_count
            RETURNING queue.*
            """,
            _claim_params(records),
        )
        rows = cursor.fetchall()
        deleted_count = expect_mutation_count(
            cursor,
            expected=len(rows),
            error_code="discovery_repository_rowcount_invalid",
        )
        return [dict(row) for row in rows], deleted_count

    def start_lookup(
        self,
        *,
        provider: str,
        lookup_key: str,
        lookup_type: str,
        now_ms: int,
        running_timeout_ms: int,
    ) -> dict[str, Any]:
        parsed_running_timeout_ms = require_positive_int(
            running_timeout_ms,
            error_code="discovery_lookup_running_timeout_ms_required",
        )

        cursor = self.conn.execute(
            """
            INSERT INTO token_discovery_results(
              provider, lookup_key, lookup_type, status, candidate_count, candidate_ids_json,
              result_hash, last_lookup_at_ms, next_refresh_at_ms, created_at_ms, updated_at_ms
            )
            VALUES (%s, %s, %s, 'running', 0, '[]'::jsonb, NULL, %s, %s, %s, %s)
            ON CONFLICT(provider, lookup_key) DO UPDATE SET
              lookup_type = excluded.lookup_type,
              status = 'running',
              last_lookup_at_ms = excluded.last_lookup_at_ms,
              next_refresh_at_ms = excluded.next_refresh_at_ms,
              updated_at_ms = excluded.updated_at_ms
            RETURNING *
            """,
            (
                provider,
                lookup_key,
                lookup_type,
                int(now_ms),
                int(now_ms) + parsed_running_timeout_ms,
                int(now_ms),
                int(now_ms),
            ),
        )
        row = cursor.fetchone()
        return _required_returning_row(cursor, row)

    def finish_lookup(
        self,
        *,
        provider: str,
        lookup_key: str,
        lookup_type: str,
        status: str,
        candidate_ids: list[str],
        result_hash: str,
        next_refresh_at_ms: int,
        now_ms: int,
    ) -> bool:
        current = self.result(provider=provider, lookup_key=lookup_key)
        current_status = str((current or {}).get("status") or "")
        changed = (
            current is None
            or str(current.get("result_hash") or "") != result_hash
            or (current_status not in {"running", status})
        )
        cursor = self.conn.execute(
            """
            INSERT INTO token_discovery_results(
              provider, lookup_key, lookup_type, status, candidate_count, candidate_ids_json,
              result_hash, last_lookup_at_ms, next_refresh_at_ms, last_error, error_count,
              created_at_ms, updated_at_ms
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, 0, %s, %s)
            ON CONFLICT(provider, lookup_key) DO UPDATE SET
              lookup_type = excluded.lookup_type,
              status = excluded.status,
              candidate_count = excluded.candidate_count,
              candidate_ids_json = excluded.candidate_ids_json,
              result_hash = excluded.result_hash,
              last_lookup_at_ms = excluded.last_lookup_at_ms,
              next_refresh_at_ms = excluded.next_refresh_at_ms,
              last_error = NULL,
              error_count = 0,
              updated_at_ms = excluded.updated_at_ms
            """,
            (
                provider,
                lookup_key,
                lookup_type,
                status,
                len(candidate_ids),
                Jsonb(sorted(set(candidate_ids))),
                result_hash,
                int(now_ms),
                int(next_refresh_at_ms),
                int(now_ms),
                int(now_ms),
            ),
        )
        expect_mutation_count(cursor, expected=1, error_code="discovery_repository_rowcount_invalid")
        return changed

    def fail_lookup(
        self,
        *,
        provider: str,
        lookup_key: str,
        lookup_type: str,
        last_error: str,
        next_refresh_at_ms: int,
        now_ms: int,
    ) -> dict[str, Any]:
        cursor = self.conn.execute(
            """
            INSERT INTO token_discovery_results(
              provider, lookup_key, lookup_type, status, candidate_count, candidate_ids_json,
              result_hash, last_lookup_at_ms, next_refresh_at_ms, last_error, error_count,
              created_at_ms, updated_at_ms
            )
            VALUES (%s, %s, %s, 'error', 0, '[]'::jsonb, NULL, %s, %s, %s, 1, %s, %s)
            ON CONFLICT(provider, lookup_key) DO UPDATE SET
              lookup_type = excluded.lookup_type,
              status = 'error',
              last_lookup_at_ms = excluded.last_lookup_at_ms,
              next_refresh_at_ms = excluded.next_refresh_at_ms,
              last_error = excluded.last_error,
              error_count = token_discovery_results.error_count + 1,
              updated_at_ms = excluded.updated_at_ms
            RETURNING *
            """,
            (
                provider,
                lookup_key,
                lookup_type,
                int(now_ms),
                int(next_refresh_at_ms),
                last_error[:500],
                int(now_ms),
                int(now_ms),
            ),
        )
        row = cursor.fetchone()
        return _required_returning_row(cursor, row)

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM token_discovery_results
            GROUP BY status
            ORDER BY status
            """
        ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def result(self, *, provider: str, lookup_key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM token_discovery_results
            WHERE provider = %s AND lookup_key = %s
            """,
            (provider, lookup_key),
        ).fetchone()
        return dict(row) if row else None


def _lookup_key_records(
    lookup_keys: Iterable[str],
    *,
    reason: str,
    now_ms: int,
    due_at_ms: int | None,
    latest_seen_ms: int | None,
    intent_count: int,
) -> list[dict[str, Any]]:
    parsed_intent_count = require_positive_int(intent_count, error_code="discovery_lookup_intent_count_required")
    records: list[dict[str, Any]] = []
    for raw_key in sorted({str(key).strip() for key in lookup_keys if str(key).strip()}):
        lookup_type = _lookup_type(raw_key)
        if lookup_type is None:
            continue
        latest = int(latest_seen_ms if latest_seen_ms is not None else now_ms)
        record = {
            "provider": DISCOVERY_PROVIDER,
            "lookup_key": raw_key,
            "lookup_type": lookup_type,
            "dirty_reason": str(reason),
            "payload_hash": _payload_hash(raw_key, reason=str(reason), latest_seen_ms=latest),
            "due_at_ms": int(due_at_ms if due_at_ms is not None else now_ms),
            "latest_seen_ms": latest,
            "intent_count": parsed_intent_count,
            "refresh_priority": 0 if raw_key.startswith("symbol:") else 1,
        }
        records.append(record)
    return records


def _lookup_type(lookup_key: str) -> str | None:
    if lookup_key.startswith("symbol:"):
        return "dex_symbol_lookup"
    if lookup_key.startswith("address:"):
        return "address_lookup"
    return None


def _payload_hash(lookup_key: str, *, reason: str, latest_seen_ms: int) -> str:
    payload = f"{DISCOVERY_PROVIDER}:{lookup_key}:{reason}:{int(latest_seen_ms)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _lookup_key_params(records: list[dict[str, Any]], *, now_ms: int) -> dict[str, Any]:
    return {
        "providers": [record["provider"] for record in records],
        "lookup_keys": [record["lookup_key"] for record in records],
        "lookup_types": [record["lookup_type"] for record in records],
        "dirty_reasons": [record["dirty_reason"] for record in records],
        "payload_hashes": [record["payload_hash"] for record in records],
        "due_at_ms_values": [record["due_at_ms"] for record in records],
        "latest_seen_ms_values": [record["latest_seen_ms"] for record in records],
        "intent_counts": [record["intent_count"] for record in records],
        "refresh_priorities": [record["refresh_priority"] for record in records],
        "now_ms": int(now_ms),
    }


def _due_params(
    *,
    now_ms: int,
    limit: int,
    running_timeout_ms: int,
    hot_since_ms: int | None,
    hot_not_found_retry_ms: int | None,
) -> dict[str, Any]:
    parsed_limit = require_nonnegative_int(limit, error_code="discovery_lookup_claim_limit_required")
    parsed_running_timeout_ms = require_positive_int(
        running_timeout_ms,
        error_code="discovery_lookup_running_timeout_ms_required",
    )
    parsed_hot_since_ms = _optional_nonnegative_int(hot_since_ms, "discovery_lookup_hot_since_ms_required")
    parsed_hot_not_found_retry_ms = _optional_positive_int(
        hot_not_found_retry_ms,
        "discovery_lookup_hot_not_found_retry_ms_required",
    )
    return {
        "provider": DISCOVERY_PROVIDER,
        "now_ms": int(now_ms),
        "running_expired_ms": int(now_ms) - parsed_running_timeout_ms,
        "hot_since_ms": parsed_hot_since_ms,
        "hot_not_found_retry_ms": parsed_hot_not_found_retry_ms,
        "limit": parsed_limit,
    }


def _claim_records(claims: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for claim in claims:
        provider = _completion_provider(claim)
        lookup_key = _completion_lookup_key(claim)
        payload_hash = _completion_payload_hash(claim)
        lease_owner = _completion_lease_owner(claim)
        attempt_count = _completion_attempt_count(claim)
        if not payload_hash:
            raise ValueError("token discovery lookup claim completion requires payload_hash from claim_due")
        if not lease_owner:
            raise ValueError("token discovery lookup claim completion requires lease_owner from claim_due")
        records.append(
            {
                "provider": provider,
                "lookup_key": lookup_key,
                "payload_hash": payload_hash,
                "lease_owner": lease_owner,
                "attempt_count": attempt_count,
            }
        )
    return records


def _completion_attempt_count(claim: Mapping[str, Any]) -> int:
    try:
        value = claim["attempt_count"]
    except KeyError as exc:
        raise ValueError("token discovery lookup claim completion requires attempt_count from claim_due") from exc
    return require_positive_int(
        value,
        error_code="token discovery lookup claim completion requires attempt_count from claim_due",
    )


def _completion_provider(claim: Mapping[str, Any]) -> str:
    try:
        value = claim["provider"]
    except KeyError as exc:
        raise ValueError("token discovery lookup claim completion requires full lookup key from claim_due") from exc
    provider = str(value or "").strip()
    if not provider:
        raise ValueError("token discovery lookup claim completion requires full lookup key from claim_due")
    return provider


def _completion_lookup_key(claim: Mapping[str, Any]) -> str:
    try:
        value = claim["lookup_key"]
    except KeyError as exc:
        raise ValueError("token discovery lookup claim completion requires full lookup key from claim_due") from exc
    lookup_key = str(value or "").strip()
    if not lookup_key:
        raise ValueError("token discovery lookup claim completion requires full lookup key from claim_due")
    return lookup_key


def _completion_lease_owner(claim: Mapping[str, Any]) -> str:
    try:
        value = claim["lease_owner"]
    except KeyError as exc:
        raise ValueError("token discovery lookup claim completion requires lease_owner from claim_due") from exc
    if value is None:
        raise ValueError("token discovery lookup claim completion requires lease_owner from claim_due")
    lease_owner = str(value).strip()
    if not lease_owner:
        raise ValueError("token discovery lookup claim completion requires lease_owner from claim_due")
    return lease_owner


def _completion_payload_hash(claim: Mapping[str, Any]) -> str:
    try:
        value = claim["payload_hash"]
    except KeyError as exc:
        raise ValueError("token discovery lookup claim completion requires payload_hash from claim_due") from exc
    if value is None:
        raise ValueError("token discovery lookup claim completion requires payload_hash from claim_due")
    payload_hash = str(value).strip()
    if not payload_hash:
        raise ValueError("token discovery lookup claim completion requires payload_hash from claim_due")
    return payload_hash


def _terminal_source_payload_hash(row: Mapping[str, Any]) -> str:
    try:
        value = row["payload_hash"]
    except KeyError as exc:
        raise ValueError("token discovery lookup terminalization requires source payload_hash") from exc
    if value is None:
        raise ValueError("token discovery lookup terminalization requires source payload_hash")
    payload_hash = str(value).strip()
    if not payload_hash:
        raise ValueError("token discovery lookup terminalization requires source payload_hash")
    return payload_hash


def _claim_params(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "providers": [record["provider"] for record in records],
        "lookup_keys": [record["lookup_key"] for record in records],
        "payload_hashes": [record["payload_hash"] for record in records],
        "lease_owners": [record["lease_owner"] for record in records],
        "attempt_counts": [record["attempt_count"] for record in records],
    }


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _optional_positive_int(value: Any, error_code: str) -> int | None:
    if value is None:
        return None
    return require_positive_int(value, error_code=error_code)


def _optional_nonnegative_int(value: Any, error_code: str) -> int | None:
    if value is None:
        return None
    return require_nonnegative_int(value, error_code=error_code)


def _required_returning_row(cursor: Any, row: Any) -> dict[str, Any]:
    returning_mutation_count(cursor, row, error_code="discovery_repository_rowcount_invalid")
    if row is None:
        raise TypeError("discovery_repository_rowcount_invalid")
    return dict(row)
