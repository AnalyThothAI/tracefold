from __future__ import annotations

from typing import Any


class StocksRadarQuery:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def stock_rows(self, *, window: str, limit: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT
              target_id,
              symbol,
              security_name,
              exchange,
              instrument_type,
              mentions,
              unique_authors,
              latest_seen_ms,
              latest_event_id,
              latest_author_handle,
              latest_text,
              source_event_ids
            FROM stocks_radar_current_rows
            WHERE window_key = %s
            ORDER BY rank
            LIMIT %s
            """,
            (str(window), int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]
