"""Trading account control, blacklist, and daily runtime counters.

Same contract as the News repository: this class holds a connection and nothing else — no clock, no
settings, no provider. The database, not a runner's memory, is the authority for the three invariants
that matter, so the interesting methods here are the ones that let a unique index reject the caller
rather than the caller checking first and hoping.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .sql_values import _dumps


class ControlStorage:
    conn: Any

    # ------------------------------------------------------------------ blacklist
    def blacklist_rows(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT base_symbol, reason, created_at_ms, expires_at_ms "
            "FROM trading_symbol_blacklist ORDER BY base_symbol"
        ).fetchall()
        return [dict(row) for row in rows]

    def blacklist_upsert(self, *, base_symbol: str, reason: str, expires_at_ms: int | None, now_ms: int) -> None:
        self.conn.execute("SELECT id FROM trading_runtime_state WHERE id = 1 FOR UPDATE").fetchone()
        changed = self.conn.execute(
            """
            INSERT INTO trading_symbol_blacklist (base_symbol, reason, expires_at_ms, created_at_ms, updated_at_ms)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (base_symbol) DO UPDATE
               SET reason = EXCLUDED.reason,
                   expires_at_ms = EXCLUDED.expires_at_ms,
                   updated_at_ms = EXCLUDED.updated_at_ms
             WHERE (trading_symbol_blacklist.reason, trading_symbol_blacklist.expires_at_ms)
                   IS DISTINCT FROM (EXCLUDED.reason, EXCLUDED.expires_at_ms)
         RETURNING base_symbol
            """,
            (base_symbol, reason, expires_at_ms, int(now_ms), int(now_ms)),
        ).fetchone()
        if changed is not None:
            self.conn.execute(
                "UPDATE trading_runtime_state SET blacklist_revision = blacklist_revision + 1, updated_at_ms = %s "
                "WHERE id = 1",
                (int(now_ms),),
            )

    def blacklist_delete(self, *, base_symbol: str, now_ms: int) -> int:
        self.conn.execute("SELECT id FROM trading_runtime_state WHERE id = 1 FOR UPDATE").fetchone()
        cursor = self.conn.execute("DELETE FROM trading_symbol_blacklist WHERE base_symbol = %s", (base_symbol,))
        removed = int(getattr(cursor, "rowcount", 0) or 0)
        if removed:
            self.conn.execute(
                "UPDATE trading_runtime_state SET blacklist_revision = blacklist_revision + 1, updated_at_ms = %s "
                "WHERE id = 1",
                (int(now_ms),),
            )
        return removed

    # ------------------------------------------------------------------ runtime state
    def runtime_state(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT control, day_key, dspy_calls_today, funnel, "
            "nautilus_heartbeat_at_ms, nautilus_ready, nautilus_readiness_reason, "
            "nautilus_unexpected_exposure, active_capability_snapshot_sha256, "
            "active_capability_included_count, nautilus_bootstrap_account_zero_at_ms, "
            "blacklist_revision, updated_at_ms "
            "FROM trading_runtime_state WHERE id = 1"
        ).fetchone()
        return dict(row) if row is not None else None

    def nautilus_runtime_state(self, *, for_update: bool = False) -> dict[str, Any] | None:
        """Read only the control columns granted to the execution process."""

        row = self.conn.execute(
            "SELECT control, nautilus_heartbeat_at_ms, nautilus_ready, "
            "nautilus_readiness_reason, nautilus_unexpected_exposure, "
            "active_capability_snapshot_sha256, active_capability_included_count, "
            "nautilus_bootstrap_account_zero_at_ms, blacklist_revision "
            "FROM trading_runtime_state WHERE id = 1" + (" FOR UPDATE" if for_update else "")
        ).fetchone()
        return dict(row) if row is not None else None

    def set_nautilus_runtime(
        self,
        *,
        heartbeat_at_ms: int,
        ready: bool,
        readiness_reason: str | None,
        unexpected_exposure: bool,
        now_ms: int,
    ) -> None:
        self.conn.execute(
            """
            UPDATE trading_runtime_state
               SET nautilus_heartbeat_at_ms = %(heartbeat)s,
                   nautilus_ready = %(ready)s,
                   nautilus_readiness_reason = %(reason)s,
                   nautilus_unexpected_exposure = %(unexpected)s,
                   updated_at_ms = %(now)s
             WHERE id = 1
            """,
            {
                "heartbeat": int(heartbeat_at_ms),
                "ready": bool(ready),
                "reason": readiness_reason,
                "unexpected": bool(unexpected_exposure),
                "now": int(now_ms),
            },
        )

    def set_nautilus_bootstrap_account_zero(self, *, verified_at_ms: int | None, now_ms: int) -> None:
        """Project a bootstrap proof only while no capability snapshot is active."""

        self.conn.execute(
            "UPDATE trading_runtime_state "
            "SET nautilus_bootstrap_account_zero_at_ms = %s, updated_at_ms = %s "
            "WHERE id = 1 AND active_capability_snapshot_sha256 IS NULL",
            (None if verified_at_ms is None else int(verified_at_ms), int(now_ms)),
        )

    def bump_dspy_calls(self, *, day_key: str, now_ms: int) -> int:
        """Increment today's model-call budget. The day roll clears **every** per-day field.

        Each counter used to roll only the fields it cared about, so whichever one happened to run
        first on a new UTC day stamped the new `day_key` and left the others holding yesterday's
        numbers under today's key — a model budget that could be exhausted before the first call.
        """

        row = self.conn.execute(
            """
            UPDATE trading_runtime_state
               SET dspy_calls_today = CASE WHEN day_key = %(day)s THEN dspy_calls_today + 1 ELSE 1 END,
                   funnel = CASE WHEN day_key = %(day)s THEN funnel ELSE '{}'::jsonb END,
                   day_key = %(day)s,
                   updated_at_ms = %(now)s
             WHERE id = 1
         RETURNING dspy_calls_today
            """,
            {"day": day_key, "now": int(now_ms)},
        ).fetchone()
        return int(row["dspy_calls_today"]) if row is not None else 0

    def dspy_calls_today(self, *, day_key: str) -> int:
        row = self.conn.execute("SELECT day_key, dspy_calls_today FROM trading_runtime_state WHERE id = 1").fetchone()
        if row is None or str(row["day_key"]) != day_key:
            return 0
        return int(row["dspy_calls_today"])

    def merge_funnel(self, *, day_key: str, counts: Mapping[str, int], now_ms: int) -> None:
        """Accumulate one turn's named rejections into the day's funnel.

        The funnel is the durable OI raw -> Case -> Intent admission trail, and it has to survive a
        deploy: an in-memory counter resets exactly when someone is trying to read it. The day key
        resets the document, so the row stays one bounded object rather than growing forever.
        """

        if not counts:
            return
        payload = {str(key): int(value) for key, value in counts.items()}
        self.conn.execute(
            """
            UPDATE trading_runtime_state
               SET funnel = (
                     SELECT coalesce(jsonb_object_agg(k, v), '{}'::jsonb)
                       FROM (
                         SELECT k,
                                sum(v)::bigint AS v
                           FROM (
                             SELECT key AS k, value::text::bigint AS v
                               FROM jsonb_each(CASE WHEN day_key = %(day)s THEN funnel ELSE '{}'::jsonb END)
                             UNION ALL
                             SELECT key AS k, value::text::bigint AS v FROM jsonb_each(%(counts)s::jsonb)
                           ) merged
                          GROUP BY k
                       ) totals
                   ),
                   dspy_calls_today = CASE WHEN day_key = %(day)s THEN dspy_calls_today ELSE 0 END,
                   day_key = %(day)s,
                   updated_at_ms = %(now)s
             WHERE id = 1
            """,
            {"day": day_key, "counts": _dumps(payload), "now": int(now_ms)},
        )

    def set_control(self, *, control: str, now_ms: int) -> None:
        self.conn.execute(
            "UPDATE trading_runtime_state SET control = %s, updated_at_ms = %s WHERE id = 1",
            (control, int(now_ms)),
        )


__all__ = ["ControlStorage"]
