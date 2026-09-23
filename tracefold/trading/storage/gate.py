"""Read-only access to historical OI v5 admission answers.

Analysis writes Trigger and Case facts through ``analysis.py``. No current
process writes or re-evaluates the retired gate ledger.
"""

from __future__ import annotations

from typing import Any, Final

_GATE_DECISION_COLUMNS = """
    source_key, trigger_kind, underlying_key,
    source_observed_at_ms, status, stage, reason, retryable, evidence, case_id,
    first_evaluated_at_ms, last_evaluated_at_ms, attempt_count
"""
GATE_DECISION_FOR_SOURCE_KEY_SQL: Final = f"""
    SELECT {_GATE_DECISION_COLUMNS}
      FROM trading_candidate_gate_decisions
     WHERE source_key = %s
"""  # noqa: S608 -- module-owned columns; the source key stays bound
GATE_DECISIONS_SINCE_SQL: Final = f"""
    SELECT {_GATE_DECISION_COLUMNS}
      FROM trading_candidate_gate_decisions
     WHERE trigger_kind = %s AND source_observed_at_ms >= %s
     ORDER BY source_observed_at_ms DESC, source_key
     LIMIT %s
"""  # noqa: S608 -- module-owned columns; every predicate stays bound


class HistoricalGateStorage:
    conn: Any

    def gate_decision_for_source_key(self, *, source_key: str) -> dict[str, Any] | None:
        row = self.conn.execute(GATE_DECISION_FOR_SOURCE_KEY_SQL, (source_key,)).fetchone()
        return dict(row) if row is not None else None

    def gate_decisions_since(self, *, since_ms: int, trigger_kind: str = "oi", limit: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(GATE_DECISIONS_SINCE_SQL, (trigger_kind, int(since_ms), int(limit))).fetchall()
        return [dict(row) for row in rows]


__all__ = ["GATE_DECISIONS_SINCE_SQL", "GATE_DECISION_FOR_SOURCE_KEY_SQL", "HistoricalGateStorage"]
