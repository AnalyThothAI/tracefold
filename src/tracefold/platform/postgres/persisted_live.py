from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb


class PersistedLiveEventRepository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def append(
        self,
        *,
        source_key: str,
        event_kind: str,
        payload: dict[str, Any],
        committed_at_ms: int,
        target_type: str | None = None,
        target_id: str | None = None,
    ) -> int | None:
        row = self.conn.execute(
            """
            INSERT INTO persisted_live_events(
              source_key, event_kind, target_type, target_id,
              payload_json, committed_at_ms
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT(source_key) DO NOTHING
            RETURNING cursor
            """,
            (
                str(source_key),
                str(event_kind),
                target_type,
                target_id,
                Jsonb(payload),
                int(committed_at_ms),
            ),
        ).fetchone()
        return int(row["cursor"]) if row is not None else None

    def after_cursor(self, *, cursor: int, limit: int) -> list[dict[str, Any]]:
        return list(
            self.conn.execute(
                """
                SELECT
                  cursor, event_kind, target_type, target_id,
                  payload_json, committed_at_ms
                FROM persisted_live_events
                WHERE cursor > %s
                ORDER BY cursor
                LIMIT %s
                """,
                (int(cursor), int(limit)),
            ).fetchall()
        )

    def latest(self, *, limit: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT
              cursor, event_kind, target_type, target_id,
              payload_json, committed_at_ms
            FROM persisted_live_events
            ORDER BY cursor DESC
            LIMIT %s
            """,
            (int(limit),),
        ).fetchall()
        return list(reversed(rows))
