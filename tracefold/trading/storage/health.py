"""The three execution facts the App watchdog reads, and nothing it could act on.

Each statement answers one question an operator was not told the answer to until #680: is the Runtime
still beating, did it keep starting over, what did it do with the last Signals, and is a plan still
open long after its own time exit. They read columns the execution Runtime already writes and
change nothing; alerting, thresholds and de-duplication are App's (#680 RC11).

Deliberately narrow. The Runtime's projection and observation shapes are being rewritten (#680 PR-1),
so this reads the two clocks of the Runtime row, the plan columns that define "open past its time
exit", and the `disposition` of `signal_disposition` observations -- and no other Runtime vocabulary.
"""

from __future__ import annotations

from typing import Any, Final, TypedDict

# The Runtime row's two clocks. One row per account slot; a missing row is itself the answer.
RUNTIME_LIVENESS_SQL: Final = """
    SELECT heartbeat_at_ns, started_at_ns
      FROM trading_execution_runtime_state
     WHERE account_slot = %s
"""
# Plans still not terminal whose own time exit is behind them by more than the grace. The partial index
# over non-terminal plans keeps this a probe of the handful of live rows.
OVERDUE_OPEN_PLANS_SQL: Final = """
    SELECT market_key, status, opened_at_ns, max_holding_ns
      FROM trading_trade_plans
     WHERE terminal_at_ns IS NULL
       AND opened_at_ns IS NOT NULL
       AND opened_at_ns + max_holding_ns + %s < %s
     ORDER BY opened_at_ns, market_key
     LIMIT %s
"""
# What the Runtime answered to each of the latest Signals, newest first.
RECENT_SIGNAL_DISPOSITIONS_SQL: Final = """
    SELECT summary ->> 'disposition' AS disposition
      FROM trading_execution_observations
     WHERE normalized_kind = 'signal_disposition'
     ORDER BY seq DESC
     LIMIT %s
"""


class RuntimeLiveness(TypedDict):
    heartbeat_at_ns: int
    started_at_ns: int


class OverduePlan(TypedDict):
    market_key: str
    status: str
    opened_at_ns: int
    max_holding_ns: int


class ExecutionHealthStorage:
    conn: Any

    def runtime_liveness(self, *, account_slot: str) -> RuntimeLiveness | None:
        """The last heartbeat and the start of the Runtime generation that wrote it, or None."""

        row = self.conn.execute(RUNTIME_LIVENESS_SQL, (account_slot,)).fetchone()
        if row is None:
            return None
        return RuntimeLiveness(heartbeat_at_ns=int(row["heartbeat_at_ns"]), started_at_ns=int(row["started_at_ns"]))

    def overdue_open_plans(self, *, now_ns: int, grace_ns: int, limit: int) -> list[OverduePlan]:
        """Plans opened more than their own `max_holding_ns` plus `grace_ns` ago and still not terminal."""

        rows = self.conn.execute(OVERDUE_OPEN_PLANS_SQL, (int(grace_ns), int(now_ns), int(limit))).fetchall()
        return [
            OverduePlan(
                market_key=str(row["market_key"]),
                status=str(row["status"]),
                opened_at_ns=int(row["opened_at_ns"]),
                max_holding_ns=int(row["max_holding_ns"]),
            )
            for row in rows
        ]

    def recent_signal_dispositions(self, *, limit: int) -> list[str]:
        """The Runtime's answer to each of the latest `limit` Signals it disposed of, newest first."""

        rows = self.conn.execute(RECENT_SIGNAL_DISPOSITIONS_SQL, (int(limit),)).fetchall()
        return [str(row["disposition"] or "") for row in rows]


__all__ = [
    "OVERDUE_OPEN_PLANS_SQL",
    "RECENT_SIGNAL_DISPOSITIONS_SQL",
    "RUNTIME_LIVENESS_SQL",
    "ExecutionHealthStorage",
    "OverduePlan",
    "RuntimeLiveness",
]
