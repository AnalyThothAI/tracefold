from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from tracefold.platform.postgres.postgres_client import require_transaction
from tracefold.platform.postgres.queue_terminal import terminalize_source_row

_DETERMINISTIC_BACKOFF_MS = (1_000, 5_000, 30_000)
_DETERMINISTIC_MAX_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class FrontierSpec:
    domain: str
    table: str
    key_columns: tuple[str, ...]
    version_column: str
    stable_order: int


RADAR_FRONTIER = FrontierSpec(
    domain="radar",
    table="radar_projection_frontiers",
    key_columns=("target_type", "target_id", "window_key", "venue"),
    version_column="projection_version",
    stable_order=10,
)
PROFILE_FRONTIER = FrontierSpec(
    domain="profile",
    table="token_profile_projection_frontiers",
    key_columns=("target_type", "target_id"),
    version_column="projection_version",
    stable_order=20,
)
MACRO_FRONTIER = FrontierSpec(
    domain="macro",
    table="macro_module_frontiers",
    key_columns=("module_id",),
    version_column="projection_version",
    stable_order=30,
)
NEWS_FRONTIER = FrontierSpec(
    domain="news",
    table="news_projection_frontiers",
    key_columns=("bucket_id",),
    version_column="projection_version",
    stable_order=40,
)
MODEL_FRONTIER = FrontierSpec(
    domain="model",
    table="model_generation_frontiers",
    key_columns=("candidate_kind", "shard_key"),
    version_column="workflow_version",
    stable_order=10,
)

FRONTIER_SPECS = (
    RADAR_FRONTIER,
    PROFILE_FRONTIER,
    MACRO_FRONTIER,
    NEWS_FRONTIER,
    MODEL_FRONTIER,
)


class ProjectionFrontierRepository:
    """Typed PostgreSQL frontier state; callers own short transactions."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def next_due(self, spec: FrontierSpec, *, now_ms: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            f"""
            SELECT *
            FROM {spec.table}
            WHERE deadline_at_ms IS NOT NULL
              AND (
                status = 'dirty'
                OR (
                  status = 'retry_wait'
                  AND COALESCE(next_attempt_at_ms, deadline_at_ms) <= %(now_ms)s
                )
                OR (
                  status = 'running'
                  AND claimed_until_ms <= %(now_ms)s
                )
              )
            ORDER BY deadline_at_ms, {", ".join(spec.key_columns)}
            LIMIT 1
            """,
            {"now_ms": int(now_ms)},
        ).fetchone()
        return dict(row) if row is not None else None

    def mark_dirty(
        self,
        spec: FrontierSpec,
        *,
        key: dict[str, str],
        dirty_at_ms: int,
        deadline_at_ms: int,
        input_fingerprint: str,
        version: str,
        extra_insert: dict[str, object] | None = None,
    ) -> int:
        require_transaction(self.conn, operation=f"{spec.domain}_frontier_mark_dirty")
        values: dict[str, object] = {
            **_validated_key(spec, key),
            "status": "dirty",
            "first_dirty_at_ms": int(dirty_at_ms),
            "deadline_at_ms": int(deadline_at_ms),
            "next_attempt_at_ms": None,
            "attempt_count": 0,
            "input_fingerprint": _required_text(input_fingerprint, "input_fingerprint"),
            spec.version_column: _required_text(version, spec.version_column),
            "claimed_by": None,
            "claimed_until_ms": None,
            "last_error_code": None,
            "updated_at_ms": int(dirty_at_ms),
            **dict(extra_insert or {}),
        }
        columns = tuple(values)
        changed_input = (
            f"({spec.table}.input_fingerprint IS DISTINCT FROM EXCLUDED.input_fingerprint "
            f"OR {spec.table}.{spec.version_column} IS DISTINCT FROM EXCLUDED.{spec.version_column})"
        )
        cursor = self.conn.execute(
            f"""
            INSERT INTO {spec.table}({", ".join(columns)})
            VALUES ({", ".join(f"%({column})s" for column in columns)})
            ON CONFLICT({", ".join(spec.key_columns)}) DO UPDATE SET
              status = CASE
                WHEN {changed_input} THEN 'dirty'
                ELSE {spec.table}.status
              END,
              first_dirty_at_ms = CASE
                WHEN {changed_input}
                  AND {spec.table}.status IN ('clean', 'quarantined')
                THEN EXCLUDED.first_dirty_at_ms
                ELSE LEAST(
                  COALESCE({spec.table}.first_dirty_at_ms, EXCLUDED.first_dirty_at_ms),
                  EXCLUDED.first_dirty_at_ms
                )
              END,
              deadline_at_ms = CASE
                WHEN {changed_input}
                  AND {spec.table}.status IN ('clean', 'quarantined')
                THEN EXCLUDED.deadline_at_ms
                ELSE LEAST(
                  COALESCE({spec.table}.deadline_at_ms, EXCLUDED.deadline_at_ms),
                  EXCLUDED.deadline_at_ms
                )
              END,
              next_attempt_at_ms = CASE
                WHEN {changed_input} THEN NULL
                ELSE {spec.table}.next_attempt_at_ms
              END,
              attempt_count = CASE
                WHEN {changed_input} THEN 0
                ELSE {spec.table}.attempt_count
              END,
              transient_failure_count = CASE
                WHEN {changed_input} THEN 0
                ELSE {spec.table}.transient_failure_count
              END,
              input_fingerprint = EXCLUDED.input_fingerprint,
              {spec.version_column} = EXCLUDED.{spec.version_column},
              claimed_by = CASE
                WHEN {changed_input} THEN NULL
                ELSE {spec.table}.claimed_by
              END,
              claimed_until_ms = CASE
                WHEN {changed_input} THEN NULL
                ELSE {spec.table}.claimed_until_ms
              END,
              last_error_code = CASE
                WHEN {changed_input} THEN NULL
                ELSE {spec.table}.last_error_code
              END,
              updated_at_ms = EXCLUDED.updated_at_ms
            """,
            values,
        )
        return int(cursor.rowcount or 0)

    def claim(
        self,
        spec: FrontierSpec,
        *,
        key: dict[str, str],
        runtime_id: str,
        now_ms: int,
        lease_ms: int,
    ) -> dict[str, Any] | None:
        require_transaction(self.conn, operation=f"{spec.domain}_frontier_claim")
        params = {
            **_validated_key(spec, key),
            "runtime_id": UUID(str(runtime_id)),
            "now_ms": int(now_ms),
            "claimed_until_ms": int(now_ms) + int(lease_ms),
        }
        row = self.conn.execute(
            f"""
            UPDATE {spec.table}
            SET status = 'running',
                claimed_by = %(runtime_id)s,
                claimed_until_ms = %(claimed_until_ms)s,
                updated_at_ms = %(now_ms)s
            WHERE {_key_predicate(spec)}
              AND (
                status = 'dirty'
                OR (
                  status = 'retry_wait'
                  AND COALESCE(next_attempt_at_ms, deadline_at_ms) <= %(now_ms)s
                )
                OR (
                  status = 'running'
                  AND claimed_until_ms <= %(now_ms)s
                )
              )
            RETURNING *
            """,
            params,
        ).fetchone()
        return dict(row) if row is not None else None

    def complete(
        self,
        spec: FrontierSpec,
        *,
        key: dict[str, str],
        runtime_id: str,
        input_fingerprint: str,
        version: str,
        now_ms: int,
    ) -> bool:
        require_transaction(self.conn, operation=f"{spec.domain}_frontier_complete")
        cursor = self.conn.execute(
            f"""
            UPDATE {spec.table}
            SET status = 'clean',
                first_dirty_at_ms = NULL,
                deadline_at_ms = NULL,
                next_attempt_at_ms = NULL,
                attempt_count = 0,
                transient_failure_count = 0,
                claimed_by = NULL,
                claimed_until_ms = NULL,
                last_error_code = NULL,
                updated_at_ms = %(now_ms)s
            WHERE {_key_predicate(spec)}
              AND status = 'running'
              AND claimed_by = %(runtime_id)s
              AND input_fingerprint = %(input_fingerprint)s
              AND {spec.version_column} = %(version)s
            """,
            {
                **_validated_key(spec, key),
                "runtime_id": UUID(str(runtime_id)),
                "input_fingerprint": _required_text(input_fingerprint, "input_fingerprint"),
                "version": _required_text(version, spec.version_column),
                "now_ms": int(now_ms),
            },
        )
        return int(cursor.rowcount or 0) == 1

    def release_stale(
        self,
        spec: FrontierSpec,
        *,
        key: dict[str, str],
        runtime_id: str,
        now_ms: int,
    ) -> bool:
        return self._release(
            spec,
            key=key,
            runtime_id=runtime_id,
            status="dirty",
            next_attempt_at_ms=None,
            last_error_code="stale_snapshot",
            now_ms=now_ms,
            increment_attempt=False,
        )

    def fail_transient(
        self,
        spec: FrontierSpec,
        *,
        key: dict[str, str],
        runtime_id: str,
        error_code: str,
        now_ms: int,
    ) -> bool:
        require_transaction(self.conn, operation=f"{spec.domain}_frontier_fail_transient")
        cursor = self.conn.execute(
            f"""
            UPDATE {spec.table}
            SET status = 'retry_wait',
                next_attempt_at_ms = %(now_ms)s + CASE
                  WHEN transient_failure_count = 0 THEN 1000
                  WHEN transient_failure_count = 1 THEN 5000
                  WHEN transient_failure_count = 2 THEN 30000
                  ELSE 60000
                END,
                transient_failure_count = transient_failure_count + 1,
                claimed_by = NULL,
                claimed_until_ms = NULL,
                last_error_code = %(last_error_code)s,
                updated_at_ms = %(now_ms)s
            WHERE {_key_predicate(spec)}
              AND status = 'running'
              AND claimed_by = %(runtime_id)s
            """,
            {
                **_validated_key(spec, key),
                "runtime_id": UUID(str(runtime_id)),
                "last_error_code": _required_text(error_code, "last_error_code"),
                "now_ms": int(now_ms),
            },
        )
        return int(cursor.rowcount or 0) == 1

    def fail_deterministic(
        self,
        spec: FrontierSpec,
        *,
        key: dict[str, str],
        runtime_id: str,
        error_code: str,
        now_ms: int,
    ) -> dict[str, Any] | None:
        require_transaction(self.conn, operation=f"{spec.domain}_frontier_fail_deterministic")
        current = self.conn.execute(
            f"""
            SELECT *
            FROM {spec.table}
            WHERE {_key_predicate(spec)}
              AND status = 'running'
              AND claimed_by = %(runtime_id)s
            FOR UPDATE
            """,
            {
                **_validated_key(spec, key),
                "runtime_id": UUID(str(runtime_id)),
            },
        ).fetchone()
        if current is None:
            return None
        next_attempt_count = int(current["attempt_count"]) + 1
        quarantined = next_attempt_count >= _DETERMINISTIC_MAX_ATTEMPTS
        delay = _DETERMINISTIC_BACKOFF_MS[min(next_attempt_count - 1, len(_DETERMINISTIC_BACKOFF_MS) - 1)]
        next_attempt_at_ms = None if quarantined else int(now_ms) + delay
        status = "quarantined" if quarantined else "retry_wait"
        self.conn.execute(
            f"""
            UPDATE {spec.table}
            SET status = %(status)s,
                next_attempt_at_ms = %(next_attempt_at_ms)s,
                attempt_count = %(attempt_count)s,
                claimed_by = NULL,
                claimed_until_ms = NULL,
                last_error_code = %(error_code)s,
                updated_at_ms = %(now_ms)s
            WHERE {_key_predicate(spec)}
              AND status = 'running'
              AND claimed_by = %(runtime_id)s
            """,
            {
                **_validated_key(spec, key),
                "runtime_id": UUID(str(runtime_id)),
                "status": status,
                "next_attempt_at_ms": next_attempt_at_ms,
                "attempt_count": next_attempt_count,
                "error_code": _required_text(error_code, "error_code"),
                "now_ms": int(now_ms),
            },
        )
        result = {
            **dict(current),
            "status": status,
            "next_attempt_at_ms": next_attempt_at_ms,
            "attempt_count": next_attempt_count,
            "claimed_by": None,
            "claimed_until_ms": None,
            "last_error_code": error_code,
            "updated_at_ms": int(now_ms),
        }
        if quarantined:
            terminalize_source_row(
                self.conn,
                worker_name=f"{spec.domain}_projection",
                source_table=spec.table,
                target_key=_target_key(spec, key),
                source_row=result,
                final_status="quarantined",
                final_reason=f"deterministic_projection_failure:{error_code}",
                final_reason_bucket="timeout" if "timeout" in error_code else "other",
                now_ms=int(now_ms),
                attempt_count=next_attempt_count,
                payload_hash=str(current.get("input_fingerprint") or ""),
            )
        return result

    def _release(
        self,
        spec: FrontierSpec,
        *,
        key: dict[str, str],
        runtime_id: str,
        status: str,
        next_attempt_at_ms: int | None,
        last_error_code: str,
        now_ms: int,
        increment_attempt: bool,
    ) -> bool:
        require_transaction(self.conn, operation=f"{spec.domain}_frontier_release")
        cursor = self.conn.execute(
            f"""
            UPDATE {spec.table}
            SET status = %(status)s,
                next_attempt_at_ms = %(next_attempt_at_ms)s,
                attempt_count = attempt_count + %(attempt_increment)s,
                claimed_by = NULL,
                claimed_until_ms = NULL,
                last_error_code = %(last_error_code)s,
                updated_at_ms = %(now_ms)s
            WHERE {_key_predicate(spec)}
              AND status = 'running'
              AND claimed_by = %(runtime_id)s
            """,
            {
                **_validated_key(spec, key),
                "runtime_id": UUID(str(runtime_id)),
                "status": status,
                "next_attempt_at_ms": next_attempt_at_ms,
                "attempt_increment": int(increment_attempt),
                "last_error_code": _required_text(last_error_code, "last_error_code"),
                "now_ms": int(now_ms),
            },
        )
        return int(cursor.rowcount or 0) == 1


def _key_predicate(spec: FrontierSpec) -> str:
    return " AND ".join(f"{column} = %({column})s" for column in spec.key_columns)


def _validated_key(spec: FrontierSpec, key: dict[str, str]) -> dict[str, str]:
    if set(key) != set(spec.key_columns):
        raise ValueError(f"{spec.domain}_frontier_key_mismatch")
    return {column: _required_text(key[column], column) for column in spec.key_columns}


def _target_key(spec: FrontierSpec, key: dict[str, str]) -> str:
    return json.dumps(
        _validated_key(spec, key),
        sort_keys=True,
        separators=(",", ":"),
    )


def _required_text(value: object, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"projection_frontier_{field}_required")
    return normalized


__all__ = [
    "FRONTIER_SPECS",
    "MACRO_FRONTIER",
    "MODEL_FRONTIER",
    "NEWS_FRONTIER",
    "PROFILE_FRONTIER",
    "RADAR_FRONTIER",
    "FrontierSpec",
    "ProjectionFrontierRepository",
]
