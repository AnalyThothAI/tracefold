"""Trading account control, deny-list, and the single runtime authority row.

Same contract as the News repository: this class holds a connection and nothing else — no clock, no
settings, no provider. The database, not a runner's memory, is the authority for the three invariants
that matter, so the interesting methods here are the ones that let a unique index reject the caller
rather than the caller checking first and hoping.

The per-day counters are gone (#331). `dspy_calls_today` budgeted a model call the capital lane no
longer makes, and `funnel` was a polling-driven JSONB document that reset at UTC midnight and counted
one entry per re-read of the same frame — a business projection whose magnitudes were a function of
the poll interval. Both product statistics now come from bounded aggregation over the durable
admission ledger and the Case table.
"""

from __future__ import annotations

from typing import Any

from ..contracts import CapitalRuntimeV1


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
    def capital_runtime(self) -> CapitalRuntimeV1 | None:
        row = self.conn.execute(
            "SELECT control, blacklist_revision, arm_epoch, updated_at_ms FROM trading_runtime_state WHERE id = 1"
        ).fetchone()
        return CapitalRuntimeV1(**dict(row)) if row is not None else None

    def capital_control(self) -> str | None:
        row = self.conn.execute("SELECT control FROM trading_runtime_state WHERE id = 1").fetchone()
        return str(row["control"]) if row is not None else None

    def set_control(self, *, control: str, now_ms: int) -> None:
        if control == "RUNNING":
            raise ValueError("capital_running_requires_operator_arm")
        runtime = self.conn.execute(
            "SELECT control, arm_epoch FROM trading_runtime_state WHERE id = 1 FOR UPDATE"
        ).fetchone()
        if runtime is None:
            raise RuntimeError("trading_runtime_state_missing")
        entering_paused = control == "PAUSED" and runtime["control"] != "PAUSED"
        self.conn.execute(
            """
            UPDATE trading_runtime_state
               SET control = %s,
                   arm_epoch = arm_epoch + CASE WHEN %s THEN 1 ELSE 0 END,
                   updated_at_ms = %s
             WHERE id = 1
            """,
            (control, entering_paused, int(now_ms)),
        )
        if entering_paused:
            self.conn.execute(
                "UPDATE trading_binding_runtime SET active_arm_receipt_sha256 = NULL, updated_at_ms = %s",
                (int(now_ms),),
            )


__all__ = ["ControlStorage"]
