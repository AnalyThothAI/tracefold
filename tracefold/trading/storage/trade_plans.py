"""PostgreSQL ownership plans. The caller prepares values and owns the transaction."""

from __future__ import annotations

from typing import Any

from tracefold.platform.postgres.client import require_transaction

from ..trade_plan import TradePlan

_PLAN_COLUMNS = ", ".join(TradePlan.model_fields)
_PLAN_INSERT = f"""INSERT INTO trading_trade_plans ({_PLAN_COLUMNS})
    VALUES ({", ".join(["%s"] * len(TradePlan.model_fields))})
    ON CONFLICT (entry_id) DO NOTHING RETURNING entry_id"""  # noqa: S608


def prepare_trade_plan(plan: TradePlan) -> tuple[Any, ...]:
    """Validate/materialize before entering the transaction, as for other Trading writes."""
    validated = TradePlan.model_validate(plan.model_dump())
    return tuple(validated.model_dump().values())


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

    def active_trade_plans(self, *, account_slot: str, mode: str, limit: int) -> tuple[dict[str, Any], ...]:
        if not 1 <= limit <= 1_000:
            raise ValueError("trade_plan_read_limit_invalid")
        rows = self.conn.execute(
            f"""SELECT {_PLAN_COLUMNS} FROM trading_trade_plans
                WHERE account_slot = %s AND runtime_mode_at_creation = %s AND terminal_at_ns IS NULL
                ORDER BY created_at_ns, entry_id LIMIT %s""",  # noqa: S608
            (account_slot, mode, limit),
        ).fetchall()
        return tuple(dict(row) for row in rows)

    def update_trade_plan(self, values: tuple[Any, ...]) -> bool:
        require_transaction(self.conn, operation="update_trade_plan")
        row = self.conn.execute(
            """
            UPDATE trading_trade_plans
               SET status = %s, opened_at_ns = %s, terminal_at_ns = %s,
                   exit_reason = %s, history_gap_reason = coalesce(history_gap_reason, %s),
                   updated_at_ns = %s
             WHERE entry_id = %s AND terminal_at_ns IS NULL AND updated_at_ns <= %s
            RETURNING entry_id
            """,
            values,
        ).fetchone()
        return row is not None


def prepare_trade_plan_update(plan: TradePlan) -> tuple[Any, ...]:
    plan = TradePlan.model_validate(plan.model_dump())
    return (
        plan.status,
        plan.opened_at_ns,
        plan.terminal_at_ns,
        plan.exit_reason,
        plan.history_gap_reason,
        plan.updated_at_ns,
        plan.entry_id,
        plan.updated_at_ns,
    )


__all__ = ["TradePlanStorage", "prepare_trade_plan", "prepare_trade_plan_update"]
