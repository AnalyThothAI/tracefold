"""Decision runtime persistence."""

from __future__ import annotations

from typing import Any

from ..contracts import DecisionRuntimeV1


class CatalogStorage:
    conn: Any

    def decision_runtime(self) -> DecisionRuntimeV1 | None:
        row = self.conn.execute(
            "SELECT state, heartbeat_at_ms, reason, updated_at_ms FROM trading_decision_runtime WHERE id = 1"
        ).fetchone()
        return DecisionRuntimeV1(**dict(row)) if row is not None else None

    def set_decision_runtime(
        self,
        *,
        state: str,
        heartbeat_at_ms: int | None,
        reason: str | None,
        now_ms: int,
    ) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_decision_runtime
               SET state = %(state)s,
                   heartbeat_at_ms = %(heartbeat)s,
                   reason = %(reason)s,
                   updated_at_ms = %(now)s
             WHERE id = 1
         RETURNING id
            """,
            {
                "state": state,
                "heartbeat": None if heartbeat_at_ms is None else int(heartbeat_at_ms),
                "reason": reason,
                "now": int(now_ms),
            },
        ).fetchone()
        return row is not None


__all__ = ["CatalogStorage"]
