from __future__ import annotations

import json
from collections.abc import Callable
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
FRONTIER_SPECS = (
    RADAR_FRONTIER,
    PROFILE_FRONTIER,
    MACRO_FRONTIER,
)


class ProjectionFrontierRepository:
    """Typed PostgreSQL frontier state; callers own short transactions."""

    def __init__(
        self,
        conn: Any,
        *,
        transition_observer: Callable[[tuple[str, str]], None] | None = None,
    ) -> None:
        self.conn = conn
        self._transition_observer = transition_observer

    def next_due(self, spec: FrontierSpec, *, now_ms: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            f"""
            SELECT *
            FROM {spec.table}
            WHERE deadline_at_ms IS NOT NULL
              AND (
                (
                  status = 'dirty'
                  AND COALESCE(
                    next_attempt_at_ms,
                    first_dirty_at_ms,
                    deadline_at_ms
                  ) <= %(now_ms)s
                )
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
        eligible_at_ms: int | None = None,
        input_fingerprint: str,
        version: str,
        extra_insert: dict[str, object] | None = None,
    ) -> int:
        require_transaction(self.conn, operation=f"{spec.domain}_frontier_mark_dirty")
        if spec == RADAR_FRONTIER:
            return self._mark_radar_dirty(
                key=key,
                dirty_at_ms=dirty_at_ms,
                deadline_at_ms=deadline_at_ms,
                eligible_at_ms=eligible_at_ms,
                input_fingerprint=input_fingerprint,
                version=version,
            )
        values: dict[str, object] = {
            **_validated_key(spec, key),
            "status": "dirty",
            "first_dirty_at_ms": int(dirty_at_ms),
            "deadline_at_ms": int(deadline_at_ms),
            "next_attempt_at_ms": (int(eligible_at_ms) if eligible_at_ms is not None else None),
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
        row = self.conn.execute(
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
                WHEN {changed_input}
                  AND {spec.table}.status IN ('clean', 'quarantined')
                THEN EXCLUDED.next_attempt_at_ms
                WHEN {changed_input}
                  AND (
                    {spec.table}.next_attempt_at_ms IS NULL
                    OR EXCLUDED.next_attempt_at_ms IS NULL
                  )
                THEN NULL
                WHEN {changed_input}
                THEN LEAST(
                  {spec.table}.next_attempt_at_ms,
                  EXCLUDED.next_attempt_at_ms
                )
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
            RETURNING WITH (OLD AS previous, NEW AS current)
              CASE
                WHEN current.status = 'dirty' AND previous.status IS NULL THEN true
                WHEN current.status = 'dirty'
                  AND previous.status IN ('clean', 'quarantined', 'running')
                THEN true
                ELSE false
              END AS executable_arrival
            """,
            values,
        ).fetchone()
        if row is None:
            return 0
        if bool(row["executable_arrival"]):
            self._observe(spec.domain, "arrival")
        return 1

    def _mark_radar_dirty(
        self,
        *,
        key: dict[str, str],
        dirty_at_ms: int,
        deadline_at_ms: int,
        eligible_at_ms: int | None,
        input_fingerprint: str,
        version: str,
    ) -> int:
        values: dict[str, object] = {
            **_validated_key(RADAR_FRONTIER, key),
            "dirty_at_ms": int(dirty_at_ms),
            "deadline_at_ms": int(deadline_at_ms),
            "eligible_at_ms": (int(eligible_at_ms) if eligible_at_ms is not None else None),
            "input_fingerprint": _required_text(input_fingerprint, "input_fingerprint"),
            "projection_version": _required_text(version, "projection_version"),
        }
        row = self.conn.execute(
            """
            INSERT INTO radar_projection_frontiers(
              target_type, target_id, window_key, venue, status,
              first_dirty_at_ms, deadline_at_ms, next_attempt_at_ms,
              attempt_count, transient_failure_count, input_fingerprint,
              projection_version, claimed_by, claimed_until_ms,
              claimed_input_fingerprint, claimed_projection_version,
              last_error_code, updated_at_ms
            )
            VALUES (
              %(target_type)s, %(target_id)s, %(window_key)s, %(venue)s,
              'dirty', %(dirty_at_ms)s, %(deadline_at_ms)s,
              %(eligible_at_ms)s, 0, 0, %(input_fingerprint)s,
              %(projection_version)s, NULL, NULL, NULL, NULL, NULL,
              %(dirty_at_ms)s
            )
            ON CONFLICT(target_type, target_id, window_key, venue)
            DO UPDATE SET
              status = CASE
                WHEN radar_projection_frontiers.status = 'running'
                  THEN 'running'
                ELSE 'dirty'
              END,
              first_dirty_at_ms = LEAST(
                COALESCE(
                  radar_projection_frontiers.first_dirty_at_ms,
                  EXCLUDED.first_dirty_at_ms
                ),
                EXCLUDED.first_dirty_at_ms
              ),
              deadline_at_ms = LEAST(
                COALESCE(
                  radar_projection_frontiers.deadline_at_ms,
                  EXCLUDED.deadline_at_ms
                ),
                EXCLUDED.deadline_at_ms
              ),
              next_attempt_at_ms = CASE
                WHEN radar_projection_frontiers.status = 'running'
                  THEN radar_projection_frontiers.next_attempt_at_ms
                ELSE EXCLUDED.next_attempt_at_ms
              END,
              attempt_count = CASE
                WHEN radar_projection_frontiers.status = 'running'
                  THEN radar_projection_frontiers.attempt_count
                ELSE 0
              END,
              transient_failure_count = CASE
                WHEN radar_projection_frontiers.status = 'running'
                  THEN radar_projection_frontiers.transient_failure_count
                ELSE 0
              END,
              input_fingerprint = EXCLUDED.input_fingerprint,
              projection_version = EXCLUDED.projection_version,
              claimed_by = CASE
                WHEN radar_projection_frontiers.status = 'running'
                  THEN radar_projection_frontiers.claimed_by
                ELSE NULL
              END,
              claimed_until_ms = CASE
                WHEN radar_projection_frontiers.status = 'running'
                  THEN radar_projection_frontiers.claimed_until_ms
                ELSE NULL
              END,
              claimed_input_fingerprint = CASE
                WHEN radar_projection_frontiers.status = 'running'
                  THEN radar_projection_frontiers.claimed_input_fingerprint
                ELSE NULL
              END,
              claimed_projection_version = CASE
                WHEN radar_projection_frontiers.status = 'running'
                  THEN radar_projection_frontiers.claimed_projection_version
                ELSE NULL
              END,
              last_error_code = CASE
                WHEN radar_projection_frontiers.status = 'running'
                  THEN radar_projection_frontiers.last_error_code
                ELSE NULL
              END,
              updated_at_ms = EXCLUDED.updated_at_ms
            WHERE (
              radar_projection_frontiers.input_fingerprint,
              radar_projection_frontiers.projection_version
            ) IS DISTINCT FROM (
              EXCLUDED.input_fingerprint,
              EXCLUDED.projection_version
            )
            RETURNING WITH (OLD AS previous, NEW AS current)
              CASE
                WHEN previous.status IS NULL THEN true
                WHEN previous.status IN ('clean', 'quarantined') THEN true
                WHEN previous.status = 'running'
                  AND (
                    previous.input_fingerprint,
                    previous.projection_version
                  ) IS NOT DISTINCT FROM (
                    previous.claimed_input_fingerprint,
                    previous.claimed_projection_version
                  )
                THEN true
                ELSE false
              END AS executable_arrival
            """,
            values,
        ).fetchone()
        if row is None:
            return 0
        if bool(row["executable_arrival"]):
            self._observe(RADAR_FRONTIER.domain, "arrival")
        return 1

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
        claim_snapshot = (
            """,
                claimed_input_fingerprint = input_fingerprint,
                claimed_projection_version = projection_version"""
            if spec == RADAR_FRONTIER
            else ""
        )
        row = self.conn.execute(
            f"""
            UPDATE {spec.table}
            SET status = 'running',
                claimed_by = %(runtime_id)s,
                claimed_until_ms = %(claimed_until_ms)s,
                updated_at_ms = %(now_ms)s
                {claim_snapshot}
            WHERE {_key_predicate(spec)}
              AND (
                (
                  status = 'dirty'
                  AND COALESCE(
                    next_attempt_at_ms,
                    first_dirty_at_ms,
                    deadline_at_ms
                  ) <= %(now_ms)s
                )
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
        if spec == RADAR_FRONTIER:
            cursor = self.conn.execute(
                """
                UPDATE radar_projection_frontiers
                SET status = CASE
                      WHEN (
                        input_fingerprint,
                        projection_version
                      ) = (
                        claimed_input_fingerprint,
                        claimed_projection_version
                      )
                        THEN 'clean'
                      ELSE 'dirty'
                    END,
                    first_dirty_at_ms = CASE
                      WHEN (
                        input_fingerprint,
                        projection_version
                      ) = (
                        claimed_input_fingerprint,
                        claimed_projection_version
                      )
                        THEN NULL
                      ELSE first_dirty_at_ms
                    END,
                    deadline_at_ms = CASE
                      WHEN (
                        input_fingerprint,
                        projection_version
                      ) = (
                        claimed_input_fingerprint,
                        claimed_projection_version
                      )
                        THEN NULL
                      ELSE deadline_at_ms
                    END,
                    next_attempt_at_ms = NULL,
                    attempt_count = 0,
                    transient_failure_count = 0,
                    claimed_by = NULL,
                    claimed_until_ms = NULL,
                    claimed_input_fingerprint = NULL,
                    claimed_projection_version = NULL,
                    last_error_code = NULL,
                    updated_at_ms = %(now_ms)s
                WHERE target_type = %(target_type)s
                  AND target_id = %(target_id)s
                  AND window_key = %(window_key)s
                  AND venue = %(venue)s
                  AND status = 'running'
                  AND claimed_by = %(runtime_id)s
                  AND claimed_input_fingerprint = %(input_fingerprint)s
                  AND claimed_projection_version = %(version)s
                """,
                {
                    **_validated_key(spec, key),
                    "runtime_id": UUID(str(runtime_id)),
                    "input_fingerprint": _required_text(
                        input_fingerprint,
                        "input_fingerprint",
                    ),
                    "version": _required_text(version, spec.version_column),
                    "now_ms": int(now_ms),
                },
            )
            completed = int(cursor.rowcount or 0) == 1
            if completed:
                self._observe(spec.domain, "completion")
            return completed
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
        completed = int(cursor.rowcount or 0) == 1
        if completed:
            self._observe(spec.domain, "completion")
        return completed

    def _observe(self, domain: str, transition: str) -> None:
        if self._transition_observer is not None:
            self._transition_observer((str(domain), str(transition)))

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

    def release_prework(
        self,
        spec: FrontierSpec,
        *,
        key: dict[str, str],
        runtime_id: str,
        now_ms: int,
    ) -> bool:
        """Release only the exact claim without changing attempts, clocks, or errors."""

        require_transaction(self.conn, operation=f"{spec.domain}_frontier_release_prework")
        clear_claim_snapshot = _clear_claim_snapshot_sql(spec)
        cursor = self.conn.execute(
            f"""
            UPDATE {spec.table}
            SET status = 'dirty',
                claimed_by = NULL,
                claimed_until_ms = NULL,
                updated_at_ms = %(now_ms)s
                {clear_claim_snapshot}
            WHERE {_key_predicate(spec)}
              AND status = 'running'
              AND claimed_by = %(runtime_id)s
            """,
            {
                **_validated_key(spec, key),
                "runtime_id": UUID(str(runtime_id)),
                "now_ms": int(now_ms),
            },
        )
        return int(cursor.rowcount or 0) == 1

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
        clear_claim_snapshot = _clear_claim_snapshot_sql(spec)
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
                {clear_claim_snapshot}
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
        clear_claim_snapshot = _clear_claim_snapshot_sql(spec)
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
                {clear_claim_snapshot}
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
                owner_key=f"{spec.domain}_projection",
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

    def retry_quarantined(
        self,
        spec: FrontierSpec,
        *,
        key: dict[str, str],
        input_fingerprint: str,
        version: str,
        now_ms: int,
    ) -> dict[str, Any] | None:
        """Explicitly retry the same quarantined input without losing its deadline."""

        require_transaction(self.conn, operation=f"{spec.domain}_frontier_retry_quarantined")
        row = self.conn.execute(
            f"""
            UPDATE {spec.table}
            SET status = 'dirty',
                next_attempt_at_ms = NULL,
                attempt_count = 0,
                transient_failure_count = 0,
                claimed_by = NULL,
                claimed_until_ms = NULL,
                last_error_code = NULL,
                updated_at_ms = %(now_ms)s
            WHERE {_key_predicate(spec)}
              AND status = 'quarantined'
              AND input_fingerprint = %(input_fingerprint)s
              AND {spec.version_column} = %(version)s
            RETURNING *
            """,
            {
                **_validated_key(spec, key),
                "input_fingerprint": _required_text(
                    input_fingerprint,
                    "input_fingerprint",
                ),
                "version": _required_text(version, spec.version_column),
                "now_ms": int(now_ms),
            },
        ).fetchone()
        return dict(row) if row is not None else None

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
        clear_claim_snapshot = _clear_claim_snapshot_sql(spec)
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
                {clear_claim_snapshot}
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


def _clear_claim_snapshot_sql(spec: FrontierSpec) -> str:
    if spec != RADAR_FRONTIER:
        return ""
    return """,
                claimed_input_fingerprint = NULL,
                claimed_projection_version = NULL"""


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
    "PROFILE_FRONTIER",
    "RADAR_FRONTIER",
    "FrontierSpec",
    "ProjectionFrontierRepository",
]
