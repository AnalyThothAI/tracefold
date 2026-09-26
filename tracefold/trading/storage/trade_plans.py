"""PostgreSQL entry intent. The caller prepares values and owns the transaction."""

from __future__ import annotations

from typing import Any

from tracefold.platform.postgres.client import require_transaction

from ..trade_plan import TradePlan

_PLAN_COLUMNS = ", ".join(TradePlan.model_fields)
_PLAN_INSERT = f"""INSERT INTO trading_trade_plans ({_PLAN_COLUMNS})
    VALUES ({", ".join(["%s"] * len(TradePlan.model_fields))})
    ON CONFLICT (entry_id) DO NOTHING RETURNING entry_id"""  # noqa: S608
_OPEN_PLAN_COLUMNS = ", ".join(f"plan.{name}" for name in TradePlan.model_fields)
MAX_OPEN_TRADE_PLANS = 1_000


def prepare_trade_plan(plan: TradePlan) -> tuple[Any, ...]:
    """Validate/materialize before entering the transaction, as for other Trading writes."""
    validated = TradePlan.model_validate(plan.model_dump())
    return tuple(validated.model_dump().values())


def prepare_trade_plan_update(plan: TradePlan) -> tuple[Any, ...]:
    plan = TradePlan.model_validate(plan.model_dump())
    return (
        plan.status,
        plan.opened_at_ns,
        plan.terminal_at_ns,
        plan.exit_reason,
        plan.updated_at_ns,
        plan.entry_id,
        plan.updated_at_ns,
    )


class TradePlanStorage:
    conn: Any

    def insert_trade_plan(self, values: tuple[Any, ...]) -> bool:
        require_transaction(self.conn, operation="insert_trade_plan")
        return self.conn.execute(_PLAN_INSERT, values).fetchone() is not None

    def trade_plan(self, entry_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            f"SELECT {_PLAN_COLUMNS} FROM trading_trade_plans WHERE entry_id = %s",  # noqa: S608
            (entry_id,),
        ).fetchone()
        return None if row is None else dict(row)

    def trade_plan_for_scope(
        self,
        *,
        account_slot: str,
        entry_scope_id: str,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            f"SELECT {_PLAN_COLUMNS} FROM trading_trade_plans "  # noqa: S608
            "WHERE account_slot=%s AND entry_scope_id=%s",
            (account_slot, entry_scope_id),
        ).fetchone()
        return None if row is None else dict(row)

    def open_trade_plans(self, *, account_slot: str, limit: int) -> tuple[dict[str, Any], ...]:
        """Every open plan of this connection, with whether its input still owes a verdict.

        A Runtime writes a Signal's or manual Command's disposition only once the venue has answered
        its entry order (#680), so a restart between the order and the answer leaves a plan whose
        input has none. `disposition_pending` is how the next generation knows to write it.
        """

        if not 1 <= limit <= MAX_OPEN_TRADE_PLANS:
            raise ValueError("trade_plan_read_limit_invalid")
        rows = self.conn.execute(
            f"""SELECT {_OPEN_PLAN_COLUMNS},
                       NOT EXISTS (
                         SELECT 1 FROM trading_execution_observations disposition
                          WHERE disposition.account_slot = plan.account_slot
                            AND ((plan.source = 'signal'
                                  AND disposition.normalized_kind = 'signal_disposition'
                                  AND disposition.signal_id = plan.entry_id)
                              OR (plan.source = 'manual'
                                  AND disposition.normalized_kind = 'control_disposition'
                                  AND disposition.command_id = plan.entry_id))
                       ) AS disposition_pending
                  FROM trading_trade_plans plan
                 WHERE plan.account_slot = %s
                   AND plan.terminal_at_ns IS NULL
                 ORDER BY plan.created_at_ns, plan.entry_id LIMIT %s""",  # noqa: S608
            (account_slot, limit),
        ).fetchall()
        return tuple(dict(row) for row in rows)

    def recent_stop_exits(self, *, account_slot: str, since_ns: int) -> dict[str, int]:
        """The latest stop-out per market since `since_ns`: what the post-stop cooldown is keyed on."""

        rows = self.conn.execute(
            """SELECT market_key, max(terminal_at_ns) AS terminal_at_ns
                 FROM trading_trade_plans
                WHERE account_slot = %s AND exit_reason = 'stop_filled' AND terminal_at_ns >= %s
                GROUP BY market_key""",
            (account_slot, int(since_ns)),
        ).fetchall()
        return {str(row["market_key"]): int(row["terminal_at_ns"]) for row in rows}

    def update_trade_plan(self, values: tuple[Any, ...]) -> bool:
        """Advance one plan; a terminal plan and an older update are both a no-op, not an error."""

        require_transaction(self.conn, operation="update_trade_plan")
        row = self.conn.execute(
            """
            UPDATE trading_trade_plans
               SET status = %s, opened_at_ns = coalesce(opened_at_ns, %s), terminal_at_ns = %s,
                   exit_reason = %s, updated_at_ns = %s
             WHERE entry_id = %s AND terminal_at_ns IS NULL AND updated_at_ns <= %s
            RETURNING entry_id
            """,
            values,
        ).fetchone()
        return row is not None


__all__ = [
    "MAX_OPEN_TRADE_PLANS",
    "TradePlanStorage",
    "prepare_trade_plan",
    "prepare_trade_plan_update",
]
