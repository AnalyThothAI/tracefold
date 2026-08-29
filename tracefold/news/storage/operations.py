"""OpenNews ingest incidents, broker state, and bounded retention operations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .sql_values import _dumps


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
                "not_applicable" if cause_class in {"broker_backpressure", "triage_circuit_open"} else "pending",
                int(now_ms),
                int(now_ms),
            ),
        ).fetchone()
        return int(row["incident_id"])

    def close_open_incidents(self, *, cause_classes: Sequence[str] | None, now_ms: int) -> int:
        if cause_classes is None:
            cursor = self.conn.execute(
                """
                UPDATE news_opennews_incidents
                   SET closed_at_ms = %s, recovery_to_at_ms = COALESCE(recovery_to_at_ms, %s),
                       updated_at_ms = %s
                 WHERE closed_at_ms IS NULL
                """,
                (int(now_ms), int(now_ms), int(now_ms)),
            )
        else:
            cursor = self.conn.execute(
                """
                UPDATE news_opennews_incidents
                   SET closed_at_ms = %s, recovery_to_at_ms = COALESCE(recovery_to_at_ms, %s),
                       updated_at_ms = %s
                 WHERE closed_at_ms IS NULL AND cause_class = ANY(%s)
                """,
                (int(now_ms), int(now_ms), int(now_ms), list(cause_classes)),
            )
        return int(cursor.rowcount or 0)

    def pending_recovery_incidents(self, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT incident_id, cause_class, opened_at_ms, closed_at_ms, recovery_from_at_ms, recovery_to_at_ms
              FROM news_opennews_incidents
             WHERE recovery_status = 'pending' AND closed_at_ms IS NOT NULL
             ORDER BY incident_id
             LIMIT %s
            """,
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]

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
    ) -> None:
        self.conn.execute(
            """
            UPDATE news_opennews_incidents
               SET recovery_status = %s, recovered_count = recovered_count + %s, last_error_code = %s,
                   recovery_from_at_ms = COALESCE(%s, recovery_from_at_ms),
                   recovery_to_at_ms = COALESCE(%s, recovery_to_at_ms), updated_at_ms = %s
             WHERE incident_id = %s
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
