"""Trading order observations and aggregate status reads."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .sql_values import _dumps


class QueryStorage:
    conn: Any

    # ------------------------------------------------------------------ observations
    def record_observation(
        self,
        *,
        order_id: str,
        observation_kind: str,
        content_sha256: str,
        content: Mapping[str, Any],
        now_ms: int,
    ) -> None:
        """An unchanged remote answer bumps a counter; it does not append another row forever."""

        self.conn.execute(
            """
            INSERT INTO trading_order_observations (
              order_id, observation_kind, content_sha256, content, first_seen_at_ms, last_seen_at_ms, seen_count
            ) VALUES (%s, %s, %s, %s::jsonb, %s, %s, 1)
            ON CONFLICT (order_id, observation_kind, content_sha256) DO UPDATE
               SET last_seen_at_ms = EXCLUDED.last_seen_at_ms,
                   seen_count = trading_order_observations.seen_count + 1
            """,
            (order_id, observation_kind, content_sha256, _dumps(dict(content)), int(now_ms), int(now_ms)),
        )

    def observations(self, *, order_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT observation_kind, content_sha256, content, first_seen_at_ms, last_seen_at_ms, seen_count "
            "FROM trading_order_observations WHERE order_id = %s ORDER BY last_seen_at_ms DESC LIMIT %s",
            (order_id, int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ status
    def status_counts(self, *, since_ms: int) -> dict[str, Any]:
        cases = self.conn.execute(
            "SELECT state, count(*) AS n FROM trading_cases WHERE created_at_ms >= %s GROUP BY state",
            (int(since_ms),),
        ).fetchall()
        orders = self.conn.execute(
            "SELECT state, count(*) AS n FROM trading_orders WHERE created_at_ms >= %s GROUP BY state",
            (int(since_ms),),
        ).fetchall()
        kinds = self.conn.execute(
            "SELECT case_kind, count(*) AS n FROM trading_cases WHERE created_at_ms >= %s GROUP BY case_kind",
            (int(since_ms),),
        ).fetchall()
        # Only *measured* exits enter the realised-PnL denominator. An operator-resolved order closed
        # a position — so it cools the symbol down — but nobody computed a return for it, and counting
        # it turned one +150 bps winner beside three resolutions into a reported mean of 37.5.
        realized = self.conn.execute(
            "SELECT count(*) AS n, coalesce(sum(realized_bps), 0) AS total_bps "
            "FROM trading_orders WHERE realized_bps IS NOT NULL "
            "AND position_closed_at_ms IS NOT NULL AND position_closed_at_ms >= %s",
            (int(since_ms),),
        ).fetchone()
        return {
            "cases_by_state": {str(row["state"]): int(row["n"]) for row in cases},
            "cases_by_kind": {str(row["case_kind"]): int(row["n"]) for row in kinds},
            "orders_by_state": {str(row["state"]): int(row["n"]) for row in orders},
            "closed_orders": 0 if realized is None else int(realized["n"]),
            "closed_realized_bps": 0 if realized is None else int(realized["total_bps"]),
        }


__all__ = ["QueryStorage"]
