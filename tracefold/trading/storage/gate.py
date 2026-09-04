"""Persistence for the candidate admission ledger.

**One row per `source_key`.** It was one row per `(source_key, gate_version, gate_config_digest)`, on
the promise that a new rulebook re-decides every source rather than inheriting an answer from a rule
that is gone. The ledger never did that — across the v6 to v8 window every frame still had exactly one
row — and the promise cost every reader a `DISTINCT ON` and a "which of these two rows is *the*
answer" rule (#537 PR-3). The rulebook that decided a row now travels in its `evidence`.

The monotonic transition lives in the `ON CONFLICT` clause rather than in the runner, because the
runner is not an authority across a restart, a second process or two turns racing on the same window:
a `DEFERRED` row may move to any terminal state, a terminal row may only have its evaluation counters
bumped, and there is no statement here that can move one back.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

# S608 exemptions below reuse closed ledger/select fragments defined in this module; all values stay bound.
from .execution_stream import _dumps

# One turn's worth of retention work. The lane persists about 90 OI facts a day, so this drains a
# 90-day backlog in a handful of turns and is a no-op on every turn after that.
_PURGE_BATCH = 500

_GATE_DECISION_COLUMNS = """
    source_key, trigger_kind, underlying_key,
    source_observed_at_ms, status, stage, reason, retryable, evidence, case_id,
    first_evaluated_at_ms, last_evaluated_at_ms, attempt_count
"""
GATE_DECISION_FOR_SOURCE_KEY_SQL: Final = f"""
    SELECT {_GATE_DECISION_COLUMNS}
      FROM trading_candidate_gate_decisions
     WHERE source_key = %s
"""  # noqa: S608 -- a module-owned column list; the source key stays bound
# One admission answer per source in the window, newest frame first. The primary key is the source
# key, so the table itself is the dedup: one index scan in frame order with a limit, where this used
# to be a full-window `DISTINCT ON` materialised and re-sorted so the two orderings could disagree.
GATE_DECISIONS_SINCE_SQL: Final = f"""
    SELECT {_GATE_DECISION_COLUMNS}
      FROM trading_candidate_gate_decisions
     WHERE trigger_kind = %s AND source_observed_at_ms >= %s
     ORDER BY source_observed_at_ms DESC, source_key
     LIMIT %s
"""  # noqa: S608 -- a module-owned column list; every predicate stays bound
# Both distributions from one pass over the window. They were two statements with the same predicate
# reading the same rows twice per request, and a third beside them scanned the whole 90-day ledger
# unbounded for two clocks one card printed (#537 PR-5). `status IS NULL` marks the reason rows, which
# is a shape the CHECK constraint cannot produce for a stored row.
GATE_DECISION_COUNTS_SQL: Final = """
    SELECT status, stage, reason, count(*) AS n
      FROM trading_candidate_gate_decisions
     WHERE trigger_kind = %s AND source_observed_at_ms >= %s
     GROUP BY GROUPING SETS ((status), (stage, reason))
"""


class CandidateGateStorage:
    conn: Any

    def record_gate_decision(
        self,
        *,
        source_key: str,
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
        source 40 times" and "the answer changed" distinguishable in the ledger. The stored `evidence`
        therefore keeps naming the rulebook that *decided* the row, not the one that last looked at it.

        The clock is the one answer that closes a row *without* replacing what it was waiting on. The
        scanner re-reads its whole overlap window every couple of seconds, so a row deferred on an
        unlisted instrument or an unavailable candle is re-evaluated the moment it passes `max_age_ms`
        and arrives here as `eligibility:trigger_stale`. Letting that overwrite the stored reason
        collapses every waiting reason into the clock within five minutes, and `candidate_reasons_*`
        then aggregates the clock instead of the bottleneck.

        So `status` and `retryable` always advance out of `DEFERRED` — the row is closed either way —
        while `stage`, `reason`, `evidence` and `case_id` advance only when the incoming answer is
        something other than the clock closing an already-open row. A source seen for the first time
        when it is already too old still records `eligibility:trigger_stale`: it never had another
        reason, and that INSERT does not reach this clause at all.
        """

        self.conn.execute(
            """
            INSERT INTO trading_candidate_gate_decisions (
              source_key, trigger_kind, underlying_key,
              source_observed_at_ms, status, stage, reason, retryable, evidence, case_id,
              first_evaluated_at_ms, last_evaluated_at_ms, attempt_count
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, 1)
            ON CONFLICT (source_key) DO UPDATE
               SET last_evaluated_at_ms = EXCLUDED.last_evaluated_at_ms,
                   attempt_count = trading_candidate_gate_decisions.attempt_count + 1,
                   status = CASE WHEN trading_candidate_gate_decisions.status = 'DEFERRED'
                                 THEN EXCLUDED.status ELSE trading_candidate_gate_decisions.status END,
                   retryable = CASE WHEN trading_candidate_gate_decisions.status = 'DEFERRED'
                                    THEN EXCLUDED.retryable ELSE trading_candidate_gate_decisions.retryable END,
                   stage = CASE WHEN trading_candidate_gate_decisions.status = 'DEFERRED'
                                 AND NOT (EXCLUDED.status = 'EXPIRED' AND EXCLUDED.reason = 'trigger_stale')
                                THEN EXCLUDED.stage ELSE trading_candidate_gate_decisions.stage END,
                   reason = CASE WHEN trading_candidate_gate_decisions.status = 'DEFERRED'
                                  AND NOT (EXCLUDED.status = 'EXPIRED' AND EXCLUDED.reason = 'trigger_stale')
                                 THEN EXCLUDED.reason ELSE trading_candidate_gate_decisions.reason END,
                   evidence = CASE WHEN trading_candidate_gate_decisions.status = 'DEFERRED'
                                    AND NOT (EXCLUDED.status = 'EXPIRED' AND EXCLUDED.reason = 'trigger_stale')
                                   THEN EXCLUDED.evidence ELSE trading_candidate_gate_decisions.evidence END,
                   case_id = CASE WHEN trading_candidate_gate_decisions.status = 'DEFERRED'
                                   AND NOT (EXCLUDED.status = 'EXPIRED' AND EXCLUDED.reason = 'trigger_stale')
                                  THEN EXCLUDED.case_id ELSE trading_candidate_gate_decisions.case_id END
            """,
            (
                source_key,
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
        grow without bound while claiming work is still pending. This is also the only writer the lane
        has for a frame that has left its scan window, now that the window is exactly `max_age_ms`.

        `stage` and `reason` are left exactly as the gate wrote them: only a `stage:reason` pair some
        rule can emit is legal, the read model aggregates on those pairs, and the status alone already
        says what the sweep has to say. Keeping the reason says more — this row was waiting on a
        listing, or on a candle, and the clock closed it.
        """

        cursor = self.conn.execute(
            """
            UPDATE trading_candidate_gate_decisions
               SET status = 'EXPIRED',
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
        """The admission answer for one source. There is exactly one, by primary key."""

        row = self.conn.execute(GATE_DECISION_FOR_SOURCE_KEY_SQL, (source_key,)).fetchone()
        return dict(row) if row is not None else None

    def gate_decisions_since(self, *, since_ms: int, trigger_kind: str = "oi", limit: int) -> list[dict[str, Any]]:
        """One admission answer per source in the window, newest frame first.

        One lane per call, defaulting to OI, and that default is the read model's whole shape. Every
        reader of this table asks an OI question: `/api/trading/gate` joins each row back to an OI
        frame by `oi:{event}:{version}`. Mixing another trigger kind in would put two populations under
        one bar. Rows of another kind stay durable evidence, queryable by source key or by SQL, and are
        deliberately not console numbers.

        Ordered by the *frame's* observation time rather than by evaluation time, because that is the
        order the frame table itself is in — a row here has to line up with the frame on the same line.
        Bounded, and the caller reports the truncation rather than quietly showing a short page.
        """

        rows = self.conn.execute(GATE_DECISIONS_SINCE_SQL, (trigger_kind, int(since_ms), int(limit))).fetchall()
        return [dict(row) for row in rows]

    def gate_decision_counts(self, *, since_ms: int, trigger_kind: str = "oi") -> dict[str, dict[str, int]]:
        """Durable status and reason distributions for one lane, keyed on when the *frame* was observed.

        The axis is the source's own observation time rather than the evaluation time, so a runner that
        restarts and re-reads a backlog cannot move yesterday's facts into today's counts. This is the
        one of the console's "24 h" figures that does not key on a creation time; collapsing it onto
        one would reintroduce that bug.

        One row per *source* is the table's own key, so this counts stored rows directly, and one
        `GROUPING SETS` pass answers both distributions: two statements over the same predicate read
        the same window twice and could report a status total the reasons beneath it did not sum to.
        """

        rows = self.conn.execute(GATE_DECISION_COUNTS_SQL, (trigger_kind, int(since_ms))).fetchall()
        status: dict[str, int] = {}
        reasons: dict[str, int] = {}
        for row in rows:
            if row["status"] is None:
                reasons[f"{row['stage']}:{row['reason']}"] = int(row["n"])
            else:
                status[str(row["status"])] = int(row["n"])
        return {"status": status, "reasons": reasons}

    def candidate_admission_report(
        self,
        *,
        now_ms: int,
        limit: int,
        trigger_kind: str = "oi",
    ) -> dict[str, Any]:
        """The whole durable half of the lane's status, assembled once.

        The counts a lane reports are otherwise keyed on a case existing, which is exactly what a lane
        that froze none has none of. This is the part that survives that, and a question about
        yesterday still has evidence.

        Two statements for one window: the bounded page of answers `/news/oi` joins each frame
        against, and the one grouped pass that produces both distributions. It was four -- the two
        count scans were separate, and `latest_gate_milestones` scanned the whole 90-day ledger with
        no lower bound on every 15 s poll for two clocks one card hint printed (#537 PR-5).
        """

        window_24h = self.gate_decision_counts(since_ms=int(now_ms) - 86_400_000, trigger_kind=trigger_kind)
        return {
            "decisions": self.gate_decisions_since(
                since_ms=int(now_ms) - 86_400_000,
                trigger_kind=trigger_kind,
                limit=int(limit),
            ),
            "candidate_counts_24h": window_24h["status"],
            "candidate_reasons_24h": window_24h["reasons"],
        }


__all__ = [
    "GATE_DECISIONS_SINCE_SQL",
    "GATE_DECISION_COUNTS_SQL",
    "GATE_DECISION_FOR_SOURCE_KEY_SQL",
    "CandidateGateStorage",
]
