"""OpenNews ingest incidents, broker state, and bounded retention operations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .sql_values import _dumps

RECOVERY_BACKLOG_LIMIT = 20
_PENDING_RECOVERY_INCIDENTS_SQL = """
    SELECT incident_id, cause_class, opened_at_ms, closed_at_ms, recovery_from_at_ms,
           recovery_to_at_ms, last_error_code, updated_at_ms
      FROM news_opennews_incidents
     WHERE recovery_status = 'pending' AND closed_at_ms IS NOT NULL
     ORDER BY incident_id
     LIMIT %s
"""


def pending_recovery_incidents_statement(*, limit: int) -> tuple[str, tuple[int]]:
    """Return the exact bounded statement shared by Recovery, status, and query audit."""

    return _PENDING_RECOVERY_INCIDENTS_SQL, (int(limit),)


class OperationsStorage:
    conn: Any

    def update_ingest_state(
        self,
        *,
        now_ms: int,
        connected: bool | None = None,
        last_frame_at_ms: int | None = None,
        last_publish_at_ms: int | None = None,
        last_error_code: str | None = None,
        clear_error: bool = False,
    ) -> None:
        self.conn.execute(
            """
            UPDATE news_ingest_state
               SET connected = COALESCE(%s, connected),
                   last_frame_at_ms = COALESCE(%s, last_frame_at_ms),
                   last_publish_at_ms = COALESCE(%s, last_publish_at_ms),
                   last_error_code = CASE WHEN %s THEN NULL ELSE COALESCE(%s, last_error_code) END,
                   updated_at_ms = GREATEST(updated_at_ms, %s)
             WHERE singleton_key = 'opennews'
            """,
            (
                connected,
                last_frame_at_ms,
                last_publish_at_ms,
                bool(clear_error),
                last_error_code,
                int(now_ms),
            ),
        )

    def update_broker_snapshot(self, *, snapshot: Mapping[str, Any], now_ms: int) -> None:
        self.conn.execute(
            """
            UPDATE news_ingest_state SET broker_snapshot = %s::jsonb, updated_at_ms = GREATEST(updated_at_ms, %s)
             WHERE singleton_key = 'opennews'
            """,
            (_dumps({**dict(snapshot), "observed_at_ms": int(now_ms)}), int(now_ms)),
        )

    def open_incident(
        self, *, cause_class: str, now_ms: int, planned: bool = False, close_code: int | None = None
    ) -> int:
        row = self.conn.execute(
            """
            SELECT incident_id FROM news_opennews_incidents
             WHERE closed_at_ms IS NULL AND cause_class = %s
             ORDER BY incident_id DESC LIMIT 1
            """,
            (cause_class,),
        ).fetchone()
        if row is not None:
            return int(row["incident_id"])
        row = self.conn.execute(
            """
            INSERT INTO news_opennews_incidents (
              cause_class, opened_at_ms, planned, close_code, recovery_status, created_at_ms, updated_at_ms
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING incident_id
            """,
            (
                cause_class,
                int(now_ms),
                bool(planned),
                close_code,
                "not_applicable" if cause_class == "triage_circuit_open" else "pending",
                int(now_ms),
                int(now_ms),
            ),
        ).fetchone()
        return int(row["incident_id"])

    def close_open_incidents(self, *, cause_classes: Sequence[str] | None, now_ms: int) -> int:
        cause_filter = "" if cause_classes is None else " AND cause_class = ANY(%s)"
        params: tuple[Any, ...] = (int(now_ms), int(now_ms), int(now_ms))
        if cause_classes is not None:
            params = (*params, list(cause_classes))
        cursor = self.conn.execute(
            f"""
            UPDATE news_opennews_incidents
               SET closed_at_ms = %s, recovery_to_at_ms = COALESCE(recovery_to_at_ms, %s),
                   recovery_status = CASE
                     WHEN cause_class IN ('broker_backpressure', 'broker_unavailable') THEN 'pending'
                     ELSE recovery_status
                   END,
                   updated_at_ms = %s
             WHERE closed_at_ms IS NULL{cause_filter}
            """,
            params,
        )
        return int(cursor.rowcount or 0)

    def pending_recovery_incidents(self, *, limit: int = 20) -> list[dict[str, Any]]:
        sql, params = pending_recovery_incidents_statement(limit=limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def recovery_backlog(self) -> dict[str, Any]:
        rows = self.pending_recovery_incidents(limit=RECOVERY_BACKLOG_LIMIT)
        pending_count = len(rows)
        oldest_opened_at_ms = min((int(row["opened_at_ms"]) for row in rows), default=None)
        latest_error = max(
            (row for row in rows if row["last_error_code"] is not None),
            key=lambda row: (int(row["updated_at_ms"]), int(row["incident_id"])),
            default=None,
        )
        last_error_code = latest_error["last_error_code"] if latest_error is not None else None
        return {
            "pending_count": pending_count,
            "oldest_opened_at_ms": oldest_opened_at_ms,
            "last_error_code": last_error_code,
            "reason": (
                "recovery_transient"
                if pending_count and last_error_code is not None
                else ("recovery_pending" if pending_count else None)
            ),
        }

    def open_incident_summary(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT cause_class, count(*)::int AS count, min(opened_at_ms) AS oldest_opened_at_ms
              FROM news_opennews_incidents
             WHERE closed_at_ms IS NULL
             GROUP BY cause_class
             ORDER BY cause_class
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def record_recovery_error(self, *, incident_id: int, error_code: str, now_ms: int) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE news_opennews_incidents
               SET last_error_code = %s, updated_at_ms = %s
             WHERE incident_id = %s AND recovery_status = 'pending'
            """,
            (str(error_code)[:200], int(now_ms), int(incident_id)),
        )
        return bool(cursor.rowcount)

    def complete_recovery(
        self,
        *,
        incident_id: int,
        status: str,
        recovered_count: int,
        error_code: str | None,
        recovery_from_at_ms: int | None,
        recovery_to_at_ms: int | None,
        now_ms: int,
    ) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE news_opennews_incidents
               SET recovery_status = %s, recovered_count = recovered_count + %s, last_error_code = %s,
                   recovery_from_at_ms = COALESCE(%s, recovery_from_at_ms),
                   recovery_to_at_ms = COALESCE(%s, recovery_to_at_ms), updated_at_ms = %s
             WHERE incident_id = %s AND recovery_status = 'pending'
            """,
            (
                status,
                int(recovered_count),
                error_code,
                recovery_from_at_ms,
                recovery_to_at_ms,
                int(now_ms),
                int(incident_id),
            ),
        )
        return bool(cursor.rowcount)

    def open_incidents(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT incident_id, cause_class, opened_at_ms, planned FROM news_opennews_incidents"
            " WHERE closed_at_ms IS NULL ORDER BY incident_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def expire_bands(self, *, now_ms: int) -> int:
        cursor = self.conn.execute("DELETE FROM news_event_bands WHERE expires_at_ms < %s", (int(now_ms),))
        return int(cursor.rowcount or 0)

    def purge_learning_retention(self, *, batch_size: int = 500) -> dict[str, Any]:
        """Run the database-owned bounded learning-evidence retention policy."""

        row = self.conn.execute(
            "SELECT purge_news_learning_retention(%s) AS result",
            (int(batch_size),),
        ).fetchone()
        return dict(row["result"] or {})

    def record_learning_retention_error(self, *, error_code: str, now_ms: int) -> None:
        self.conn.execute(
            """
            UPDATE news_learning_retention_state
               SET last_error_code = %s, updated_at_ms = %s
             WHERE singleton
            """,
            (str(error_code)[:200], int(now_ms)),
        )

    def purge_before(self, *, cutoff_ms: int, judged_cutoff_ms: int | None = None) -> int:
        """Delete raw Items older than ``cutoff_ms``, keeping any Item that is evidence for a judged or reviewed
        Event newer than ``judged_cutoff_ms`` (#81).

        Deleting `news_items` cascades to `news_events` (leader FK) and from there to verdicts, deliveries,
        members, assets, bands, snapshots and reviews, so one retention number decides the lifetime of the
        whole learning plane. An Item is evidence when *any* Event it belongs to — as leader or as a later
        member, which is what a rebuild of the Triage input needs — carries a verdict or review. Passing no
        ``judged_cutoff_ms`` keeps the old one-tier behaviour for callers that do not care.
        """

        if judged_cutoff_ms is None:
            cursor = self.conn.execute("DELETE FROM news_items WHERE observed_at_ms < %s", (int(cutoff_ms),))
            return int(cursor.rowcount or 0)
        cursor = self.conn.execute(
            """
            DELETE FROM news_items i
             WHERE i.observed_at_ms < %s
               AND NOT EXISTS (
                     SELECT 1
                       FROM news_event_members m
                       JOIN news_events e ON e.event_id = m.event_id
                      WHERE m.item_id = i.item_id
                        AND e.opened_at_ms >= %s
                        AND (EXISTS (SELECT 1 FROM news_verdicts v WHERE v.event_id = e.event_id)
                          OR EXISTS (SELECT 1 FROM news_reviews r WHERE r.event_id = e.event_id)
                          OR EXISTS (SELECT 1 FROM news_learning_cases c WHERE c.event_id = e.event_id))
                   )
               AND NOT EXISTS (
                     SELECT 1
                       FROM news_events e2
                      WHERE e2.leader_item_id = i.item_id
                        AND e2.opened_at_ms >= %s
                        AND (EXISTS (SELECT 1 FROM news_verdicts v WHERE v.event_id = e2.event_id)
                          OR EXISTS (SELECT 1 FROM news_reviews r WHERE r.event_id = e2.event_id)
                          OR EXISTS (SELECT 1 FROM news_learning_cases c WHERE c.event_id = e2.event_id))
                   )
            """,
            (int(cutoff_ms), int(judged_cutoff_ms), int(judged_cutoff_ms)),
        )
        return int(cursor.rowcount or 0)
