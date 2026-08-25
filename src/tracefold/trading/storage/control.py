"""Trading account control, blacklist, and daily runtime counters.

Same contract as the News repository: this class holds a connection and nothing else — no clock, no
settings, no provider. The database, not a runner's memory, is the authority for the three invariants
that matter, so the interesting methods here are the ones that let a unique index reject the caller
rather than the caller checking first and hoping.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from .sql_values import _dumps

# 'TRDG' — its own advisory-lock namespace, distinct from News' storyline locks and the app's session locks.
_TRADING_LOCK_NAMESPACE: Final = 0x54524447


class ControlStorage:
    conn: Any

    # ------------------------------------------------------------------ locks
    def lock_account(self, account_ref: str) -> None:
        """Transaction-scoped advisory lock per account.

        One account may have exactly one write in flight. The lock serialises "read outstanding state ->
        decide -> insert SUBMITTING" across runners and processes; the partial unique index is what
        makes the invariant true even if a lock is somehow not taken.
        """

        self.conn.execute("SET LOCAL lock_timeout = '2500ms'")
        self.conn.execute(
            "SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (_TRADING_LOCK_NAMESPACE, f"account:{account_ref}")
        )

    # ------------------------------------------------------------------ blacklist
    def blacklist_rows(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT base_symbol, reason, expires_at_ms FROM trading_symbol_blacklist ORDER BY base_symbol"
        ).fetchall()
        return [dict(row) for row in rows]

    def blacklist_upsert(self, *, base_symbol: str, reason: str, expires_at_ms: int | None, now_ms: int) -> None:
        self.conn.execute(
            """
            INSERT INTO trading_symbol_blacklist (base_symbol, reason, expires_at_ms, created_at_ms, updated_at_ms)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (base_symbol) DO UPDATE
               SET reason = EXCLUDED.reason,
                   expires_at_ms = EXCLUDED.expires_at_ms,
                   updated_at_ms = EXCLUDED.updated_at_ms
            """,
            (base_symbol, reason, expires_at_ms, int(now_ms), int(now_ms)),
        )

    def blacklist_delete(self, *, base_symbol: str) -> int:
        cursor = self.conn.execute("DELETE FROM trading_symbol_blacklist WHERE base_symbol = %s", (base_symbol,))
        return int(getattr(cursor, "rowcount", 0) or 0)

    # ------------------------------------------------------------------ runtime state
    def runtime_state(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT control, day_key, orders_today, dspy_calls_today, funnel, updated_at_ms "
            "FROM trading_runtime_state WHERE id = 1"
        ).fetchone()
        return dict(row) if row is not None else None

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
                   orders_today = CASE WHEN day_key = %(day)s THEN orders_today ELSE 0 END,
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

        The funnel is the PR-B deliverable ("OI raw -> ... -> paper order"), and it has to survive a
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
                   orders_today = CASE WHEN day_key = %(day)s THEN orders_today ELSE 0 END,
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

    def bump_orders_today(self, *, day_key: str, now_ms: int) -> int:
        """Increment the daily counter, resetting it when the UTC day rolls. Returns the new count."""

        row = self.conn.execute(
            """
            UPDATE trading_runtime_state
               SET orders_today = CASE WHEN day_key = %(day)s THEN orders_today + 1 ELSE 1 END,
                   dspy_calls_today = CASE WHEN day_key = %(day)s THEN dspy_calls_today ELSE 0 END,
                   funnel = CASE WHEN day_key = %(day)s THEN funnel ELSE '{}'::jsonb END,
                   day_key = %(day)s,
                   updated_at_ms = %(now)s
             WHERE id = 1
         RETURNING orders_today
            """,
            {"day": day_key, "now": int(now_ms)},
        ).fetchone()
        return int(row["orders_today"]) if row is not None else 0

    def orders_today(self, *, day_key: str) -> int:
        row = self.conn.execute("SELECT day_key, orders_today FROM trading_runtime_state WHERE id = 1").fetchone()
        if row is None or str(row["day_key"]) != day_key:
            return 0
        return int(row["orders_today"])

    def release_order_day_charge(self, *, day_key: str, now_ms: int) -> int:
        """Release only after provider rejection or proof that its call never started.

        Crashes after the call boundary, ambiguous answers, and restarts remain charged.
        """

        row = self.conn.execute(
            """
            UPDATE trading_runtime_state
               SET orders_today = greatest(orders_today - 1, 0), updated_at_ms = %(now)s
             WHERE id = 1 AND day_key = %(day)s
         RETURNING orders_today
            """,
            {"day": day_key, "now": int(now_ms)},
        ).fetchone()
        return int(row["orders_today"]) if row is not None else 0


__all__ = ["ControlStorage"]
