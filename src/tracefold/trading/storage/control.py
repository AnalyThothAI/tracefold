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
            "SELECT control, "
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

    def set_nautilus_bootstrap_account_zero(
        self,
        *,
        verified_at_ms: int | None,
        now_ms: int,
        expected_capability_snapshot_sha256: str | None,
    ) -> bool:
        """Project a zero-claim proof only for the unchanged paused authority."""

        row = self.conn.execute(
            """
            UPDATE trading_runtime_state
               SET nautilus_bootstrap_account_zero_at_ms = %(verified)s,
                   updated_at_ms = %(now)s
             WHERE id = 1
               AND active_capability_snapshot_sha256 IS NOT DISTINCT FROM %(expected)s
               AND (
                 %(clear)s
                 OR (
                   control = 'PAUSED'
                   AND NOT EXISTS (
                     SELECT 1 FROM trading_intents
                      WHERE execution_state IN ('PENDING', 'IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW')
                   )
                 )
               )
         RETURNING id
            """,
            {
                "verified": None if verified_at_ms is None else int(verified_at_ms),
                "clear": verified_at_ms is None,
                "now": int(now_ms),
                "expected": expected_capability_snapshot_sha256,
            },
        ).fetchone()
        return row is not None

    def set_control(self, *, control: str, now_ms: int) -> None:
        self.conn.execute(
            "UPDATE trading_runtime_state SET control = %s, updated_at_ms = %s WHERE id = 1",
            (control, int(now_ms)),
        )


__all__ = ["ControlStorage"]
