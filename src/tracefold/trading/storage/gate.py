"""Persistence for the candidate admission ledger (#264).

One row per `(source_key, gate_version, gate_config_digest)`. The monotonic transition lives in the
`ON CONFLICT` clause rather than in the runner, because the runner is not an authority across a
restart, a second process or two turns racing on the same overlap window: a `DEFERRED` row may move to
any terminal state, a terminal row may only have its evaluation counters bumped, and there is no
statement here that can move one back.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .sql_values import _dumps

# One turn's worth of retention work. The lane persists about 90 OI facts a day, so this drains a
# 90-day backlog in a handful of turns and is a no-op on every turn after that.
_PURGE_BATCH = 500


class CandidateGateStorage:
    conn: Any

    def record_gate_decision(
        self,
        *,
        source_key: str,
        gate_version: str,
        gate_config_digest: str,
        trigger_kind: str,
        underlying_key: str | None,
        source_observed_at_ms: int,
        status: str,
        stage: str,
        reason: str,
        retryable: bool,
        evidence: Mapping[str, Any],
        case_id: str | None,
        now_ms: int,
    ) -> None:
        """Write or advance one admission decision. Re-evaluation never appends and never regresses.

        A terminal row keeps its status, stage, reason, evidence and case link; only
        `last_evaluated_at_ms` and `attempt_count` move. That is what makes "the scanner re-read this
        source 40 times" and "the answer changed" distinguishable in the ledger.
        """

        self.conn.execute(
            """
            INSERT INTO trading_candidate_gate_decisions (
              source_key, gate_version, gate_config_digest, trigger_kind, underlying_key,
              source_observed_at_ms, status, stage, reason, retryable, evidence, case_id,
              first_evaluated_at_ms, last_evaluated_at_ms, attempt_count
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, 1)
            ON CONFLICT (source_key, gate_version, gate_config_digest) DO UPDATE
               SET last_evaluated_at_ms = EXCLUDED.last_evaluated_at_ms,
                   attempt_count = trading_candidate_gate_decisions.attempt_count + 1,
                   status = CASE WHEN trading_candidate_gate_decisions.status = 'DEFERRED'
                                 THEN EXCLUDED.status ELSE trading_candidate_gate_decisions.status END,
                   stage = CASE WHEN trading_candidate_gate_decisions.status = 'DEFERRED'
                                THEN EXCLUDED.stage ELSE trading_candidate_gate_decisions.stage END,
                   reason = CASE WHEN trading_candidate_gate_decisions.status = 'DEFERRED'
                                 THEN EXCLUDED.reason ELSE trading_candidate_gate_decisions.reason END,
                   retryable = CASE WHEN trading_candidate_gate_decisions.status = 'DEFERRED'
                                    THEN EXCLUDED.retryable ELSE trading_candidate_gate_decisions.retryable END,
                   evidence = CASE WHEN trading_candidate_gate_decisions.status = 'DEFERRED'
                                   THEN EXCLUDED.evidence ELSE trading_candidate_gate_decisions.evidence END,
                   case_id = CASE WHEN trading_candidate_gate_decisions.status = 'DEFERRED'
                                  THEN EXCLUDED.case_id ELSE trading_candidate_gate_decisions.case_id END
            """,
            (
                source_key,
                gate_version,
                gate_config_digest,
                trigger_kind,
                underlying_key,
                int(source_observed_at_ms),
                status,
                stage,
                reason,
                bool(retryable),
                _dumps(dict(evidence)),
                case_id,
                int(now_ms),
                int(now_ms),
            ),
        )

    def expire_stale_gate_decisions(self, *, stale_before_ms: int, now_ms: int) -> int:
        """Close every open decision whose source can no longer trigger.

        A `DEFERRED` row promises that a later scan could answer differently. Once the frame is past the
        trigger budget that promise is false, and leaving the row open would make the ledger's open set
        grow without bound while claiming work is still pending. Deliberately not scoped to one
        `gate_config_digest`: a threshold edit starts new rows, and the rows written under the previous
        digest still need closing.
        """

        cursor = self.conn.execute(
            """
            UPDATE trading_candidate_gate_decisions
               SET status = 'EXPIRED',
                   reason = 'trigger_stale',
                   retryable = false,
                   last_evaluated_at_ms = %s
             WHERE status = 'DEFERRED'
               AND source_observed_at_ms < %s
            """,
            (int(now_ms), int(stale_before_ms)),
        )
        return int(cursor.rowcount or 0)

    def purge_gate_decisions(self, *, observed_before_ms: int, batch_size: int = _PURGE_BATCH) -> int:
        """Retention, bounded per call. `case_id` is a plain reference, so no case row is touched."""

        cursor = self.conn.execute(
            """
            DELETE FROM trading_candidate_gate_decisions
             WHERE ctid IN (
                   SELECT ctid FROM trading_candidate_gate_decisions
                    WHERE source_observed_at_ms < %s
                    ORDER BY source_observed_at_ms
                    LIMIT %s
             )
            """,
            (int(observed_before_ms), int(batch_size)),
        )
        return int(cursor.rowcount or 0)

    def gate_decision_for_source_key(self, *, source_key: str) -> dict[str, Any] | None:
        """The current admission answer for one source, newest configuration first.

        A source evaluated under two configurations has two rows; the console asks "what happened to
        this frame", and the answer is the decision taken under the configuration that ran last.
        """

        row = self.conn.execute(
            """
            SELECT source_key, gate_version, gate_config_digest, trigger_kind, underlying_key,
                   source_observed_at_ms, status, stage, reason, retryable, evidence, case_id,
                   first_evaluated_at_ms, last_evaluated_at_ms, attempt_count
              FROM trading_candidate_gate_decisions
             WHERE source_key = %s
             ORDER BY last_evaluated_at_ms DESC, gate_config_digest
             LIMIT 1
            """,
            (source_key,),
        ).fetchone()
        return dict(row) if row is not None else None

    def gate_decision_counts(self, *, since_ms: int, trigger_kind: str = "oi") -> dict[str, dict[str, int]]:
        """Durable status and reason distributions for one lane, keyed on when the *frame* was observed.

        The axis is the source's own observation time rather than the evaluation time, so a runner that
        restarts and re-reads a backlog cannot move yesterday's facts into today's counts — the exact
        failure mode that made `funnel_today` unable to explain a cross-midnight question.
        """

        status_rows = self.conn.execute(
            """
            SELECT status, count(*) AS n
              FROM trading_candidate_gate_decisions
             WHERE trigger_kind = %s AND source_observed_at_ms >= %s
             GROUP BY status
            """,
            (trigger_kind, int(since_ms)),
        ).fetchall()
        reason_rows = self.conn.execute(
            """
            SELECT stage, reason, count(*) AS n
              FROM trading_candidate_gate_decisions
             WHERE trigger_kind = %s AND source_observed_at_ms >= %s
             GROUP BY stage, reason
            """,
            (trigger_kind, int(since_ms)),
        ).fetchall()
        return {
            "status": {str(row["status"]): int(row["n"]) for row in status_rows},
            "reasons": {f"{row['stage']}:{row['reason']}": int(row["n"]) for row in reason_rows},
        }

    def latest_gate_milestones(self, *, trigger_kind: str = "oi") -> dict[str, int | None]:
        """When the lane last saw a source at all, and when one last cleared the gate.

        Two numbers an operator otherwise infers from the absence of orders. `latest_source_at_ms` says
        the upstream is alive; `latest_gate_eligible_at_ms` says admission is reachable. A lane with the
        first and not the second has a gate problem, not a data problem.
        """

        row = self.conn.execute(
            """
            SELECT max(source_observed_at_ms) AS latest_source_at_ms,
                   max(source_observed_at_ms) FILTER (WHERE status = 'CASE_CREATED')
                     AS latest_gate_eligible_at_ms
              FROM trading_candidate_gate_decisions
             WHERE trigger_kind = %s
            """,
            (trigger_kind,),
        ).fetchone()
        if row is None:
            return {"latest_source_at_ms": None, "latest_gate_eligible_at_ms": None}
        return {
            "latest_source_at_ms": (None if row["latest_source_at_ms"] is None else int(row["latest_source_at_ms"])),
            "latest_gate_eligible_at_ms": (
                None if row["latest_gate_eligible_at_ms"] is None else int(row["latest_gate_eligible_at_ms"])
            ),
        }

    def candidate_admission_report(self, *, now_ms: int, trigger_kind: str = "oi") -> dict[str, Any]:
        """The whole durable half of the lane's status, assembled once.

        The counts a lane reports are otherwise keyed on a case or an order existing, which is exactly
        what a lane with neither has none of — and `trading_runtime_state.funnel` resets on the UTC day
        key, so a question about yesterday had no evidence at all. This is the part that survives both.
        """

        window_24h = self.gate_decision_counts(since_ms=int(now_ms) - 86_400_000, trigger_kind=trigger_kind)
        window_7d = self.gate_decision_counts(since_ms=int(now_ms) - 7 * 86_400_000, trigger_kind=trigger_kind)
        return {
            "candidate_counts_24h": window_24h["status"],
            "candidate_counts_7d": window_7d["status"],
            "candidate_reasons_24h": window_24h["reasons"],
            "candidate_reasons_7d": window_7d["reasons"],
            **self.latest_gate_milestones(trigger_kind=trigger_kind),
        }


__all__ = ["CandidateGateStorage"]
