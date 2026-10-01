"""News-owned point-in-time projection read by App composition for Trading.
The row contracts below are the published shape of that projection. They are `TypedDict`s
because the rows *are* the SELECT lists — naming the columns is the whole contract, and a runtime
model here would coerce values PostgreSQL already typed and turn a nullable LEFT JOIN column into a
different value. Nothing outside this module may add, rename or retype a key without editing them,
which is what makes the App-side mapper break at type-check time instead of at 03:00 in a runner.
A version string sat above them for thirteen revisions, restating in prose what the `TypedDict`s
already state in types, and it was never compared with anything in production (#537 PR-4).

The outbox freezes News facts in their owning transaction. Analysis applies Trading
identity and policy after acknowledging the public projection.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, TypedDict, cast


class TradeEventRow(TypedDict):
    event_id: int
    kind: str
    source_fact_key: str
    source_revision: str
    payload_sha256: str
    payload: dict[str, object]
    source_recorded_at_ms: int
    conflict_sha256: str | None


class TradeProjectionStorage:
    conn: Any

    def enqueue_trade_event(
        self,
        *,
        kind: str,
        source_fact_key: str,
        source_revision: str,
        payload: Mapping[str, object],
        source_recorded_at_ms: int,
    ) -> bool:
        """Freeze a public fact in the producer's own transaction.

        Retries retain the first payload and first recorded time. A different
        payload on the same identity is visible as a conflict, never an overwrite.
        """

        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        cursor = self.conn.execute(
            """
            INSERT INTO news_trade_events
              (kind, source_fact_key, source_revision, payload_sha256, payload, source_recorded_at_ms)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (kind, source_fact_key, source_revision) DO UPDATE
              SET conflict_sha256 = CASE
                  WHEN news_trade_events.payload_sha256 <> EXCLUDED.payload_sha256
                  THEN EXCLUDED.payload_sha256 ELSE news_trade_events.conflict_sha256 END
            RETURNING payload_sha256
            """,
            (kind, source_fact_key, source_revision, digest, serialized, int(source_recorded_at_ms)),
        )
        row = cursor.fetchone()
        return row is not None and row["payload_sha256"] == digest

    def unacknowledged_trade_events(self, *, limit: int) -> list[TradeEventRow]:
        """Drain the unconfirmed set, never a sequence high-water mark."""

        if not 1 <= limit <= 512:
            raise ValueError("trade_event_batch_invalid")
        rows = self.conn.execute(
            """
            SELECT event_id, kind, source_fact_key, source_revision, payload_sha256,
                   payload, source_recorded_at_ms, conflict_sha256
              FROM news_trade_events
             WHERE acknowledged_at_ms IS NULL AND rejected_reason IS NULL
             ORDER BY event_id LIMIT %s
            """,
            (limit,),
        ).fetchall()
        return [cast(TradeEventRow, dict(row)) for row in rows]

    def acknowledge_trade_event(self, *, event_id: int, payload_sha256: str, now_ms: int) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE news_trade_events SET acknowledged_at_ms = %s
             WHERE event_id = %s AND payload_sha256 = %s
               AND acknowledged_at_ms IS NULL AND rejected_reason IS NULL
            """,
            (int(now_ms), int(event_id), payload_sha256),
        )
        return bool(cursor.rowcount)

    def reject_trade_event(self, *, event_id: int, payload_sha256: str, reason: str) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE news_trade_events SET rejected_reason = %s
             WHERE event_id = %s AND payload_sha256 = %s
               AND acknowledged_at_ms IS NULL AND rejected_reason IS NULL
            """,
            (reason, int(event_id), payload_sha256),
        )
        return bool(cursor.rowcount)
