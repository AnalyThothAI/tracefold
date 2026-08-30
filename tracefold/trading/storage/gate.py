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

# S608 exemptions below reuse closed ledger/select fragments defined in this module; all values stay bound.
from .query_sql import GATE_DECISION_FOR_SOURCE_KEY_SQL, LATEST_GATE_DECISION_PER_SOURCE_SQL, gate_decisions_since_sql
from .sql_values import _dumps

# One turn's worth of retention work. The lane persists about 90 OI facts a day, so this drains a
# 90-day backlog in a handful of turns and is a no-op on every turn after that.
_PURGE_BATCH = 500

# One row per source, whatever configurations have looked at it.
#
# The table deliberately keeps a row per `(source_key, gate_version, gate_config_digest)` — that is what
# stops a threshold edit from rewriting the record of what the previous threshold decided. But a report
# that groups over the raw table counts a frame once per configuration that ever saw it, so after any
# edit the "upstream frames" total stops being a frame count and one frame can appear under two
# different statuses at once.
#
# `CASE_CREATED` wins over recency, and that ordering is the whole subtlety. A source that produced a
# case under one configuration is re-read under the next one and refused as `already_consumed`, which
# is correct as an admission answer and wrong as *the* answer about the frame: it did become a case,
# and a report that showed the newer row would forget it.
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
        release_revision: str,
        now_ms: int,
    ) -> None:
        """Write or advance one admission decision. Re-evaluation never appends and never regresses.

        A terminal row keeps its status, stage, reason, evidence and case link; only
        `last_evaluated_at_ms` and `attempt_count` move. That is what makes "the scanner re-read this
        source 40 times" and "the answer changed" distinguishable in the ledger.

        The clock is the one answer that closes a row *without* replacing what it was waiting on
        (#268). `expire_stale_gate_decisions` already knew that and left `stage`/`reason` alone, but
        the sweep never got to say it: the scanner re-reads its whole overlap window every couple of
        seconds, so a frame deferred on `routing:no_native_perp` was re-evaluated by `admit_trigger`
        the moment it passed `max_age_ms` and arrived here as `eligibility:trigger_stale`, which the
        old clause wrote over the top. Every reason a source can *wait* on — an unlisted instrument,
        an unavailable candle, a deny-list entry — therefore collapsed into `trigger_stale` within
        five minutes, and `candidate_reasons_*` aggregated the clock instead of the bottleneck.

        So `status` and `retryable` always advance out of `DEFERRED` — the row is closed either way —
        while `stage`, `reason`, `evidence` and `case_id` advance only when the incoming answer is
        something other than the clock closing an already-open row. A source seen for the first time
        when it is already too old still records `eligibility:trigger_stale`: it never had another
        reason, and that INSERT does not reach this clause at all.
        """

        self.conn.execute(
            """
            INSERT INTO trading_candidate_gate_decisions (
              source_key, gate_version, gate_config_digest, trigger_kind, underlying_key,
              source_observed_at_ms, status, stage, reason, retryable, evidence, case_id,
              first_evaluated_at_ms, last_evaluated_at_ms, attempt_count, release_revision
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, 1, %s)
            ON CONFLICT (source_key, gate_version, gate_config_digest) DO UPDATE
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
                                  THEN EXCLUDED.case_id ELSE trading_candidate_gate_decisions.case_id END,
                   release_revision = CASE WHEN trading_candidate_gate_decisions.status = 'DEFERRED'
                                           THEN EXCLUDED.release_revision
                                           ELSE trading_candidate_gate_decisions.release_revision END
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
                release_revision,
            ),
        )

    def expire_stale_gate_decisions(self, *, stale_before_ms: int, now_ms: int) -> int:
        """Close every open decision whose source can no longer trigger.

        A `DEFERRED` row promises that a later scan could answer differently. Once the frame is past the
        trigger budget that promise is false, and leaving the row open would make the ledger's open set
        grow without bound while claiming work is still pending. Deliberately not scoped to one
        `gate_config_digest`: a threshold edit starts new rows, and the rows written under the previous
        digest still need closing.

        `stage` and `reason` are left exactly as the gate wrote them. Overwriting the reason with
        `trigger_stale` produced `stage:reason` pairs no rule can emit — `routing:trigger_stale`,
        `market_context:trigger_stale` — which the read model aggregates on and no label covers. The
        status is what the sweep has to say, and keeping the reason says more: this row was waiting on
        a listing, or on a candle, and the clock closed it.
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
        """The one admission answer for one source, across every configuration that has seen it.

        A source evaluated under two configurations has two rows, and the console asks "what happened
        to this frame". `CASE_CREATED` is that answer whenever one exists — a source that produced a
        case is re-read under the next configuration and refused as `already_consumed`, and showing
        that newer row would report a refusal for a frame that is linked to a live case. Otherwise the
        most recent decision wins.
        """

        row = self.conn.execute(GATE_DECISION_FOR_SOURCE_KEY_SQL, (source_key,)).fetchone()
        return dict(row) if row is not None else None

    def gate_decisions_since(self, *, since_ms: int, trigger_kind: str = "oi", limit: int) -> list[dict[str, Any]]:
        """One admission answer per source in the window, newest frame first (#269).

        One lane per call, defaulting to OI, and that default is the read model's whole shape. Since
        #273 the News lane writes rows here too, but every reader of this table is asking an OI
        question: `/api/trading/gate` joins each row back to an OI frame by `oi:{event}:{version}`,
        and the console funnel counts the capital lane a frame at a time. Mixing a News trigger into
        either would put two populations under one bar — the same error as counting a 24 h rolling
        window and a UTC day in one chart. News rows are durable evidence, queryable by source key or
        by SQL; they are deliberately not console numbers.

        The same one-row-per-source rule the counts use, so the table a reader scrolls and the
        distribution above it cannot disagree: a frame two configurations have looked at appears once,
        and `CASE_CREATED` is that appearance whenever one exists.

        Ordered by the *frame's* observation time rather than by evaluation time, because that is the
        order the frame table itself is in — a row here has to line up with the frame on the same line.
        Bounded, and the caller reports the truncation rather than quietly showing a short page.
        """

        rows = self.conn.execute(
            gate_decisions_since_sql(),
            (trigger_kind, int(since_ms), int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    def gate_decision_counts(self, *, since_ms: int, trigger_kind: str = "oi") -> dict[str, dict[str, int]]:
        """Durable status and reason distributions for one lane, keyed on when the *frame* was observed.

        The axis is the source's own observation time rather than the evaluation time, so a runner that
        restarts and re-reads a backlog cannot move yesterday's facts into today's counts — the exact
        failure mode that made `funnel_today` unable to explain a cross-midnight question.

        One row per *source*, not per stored row. Grouping the raw table
        counted a frame once per configuration that had ever looked at it, so a single threshold edit
        turned the "upstream frames" total into something that was not a frame count.
        """

        status_rows = self.conn.execute(
            f"SELECT status, count(*) AS n FROM ({LATEST_GATE_DECISION_PER_SOURCE_SQL}) latest GROUP BY status",  # noqa: S608
            (trigger_kind, int(since_ms)),
        ).fetchall()
        reason_rows = self.conn.execute(
            f"SELECT stage, reason, count(*) AS n "  # noqa: S608
            f"FROM ({LATEST_GATE_DECISION_PER_SOURCE_SQL}) latest GROUP BY stage, reason",
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
            f"""
            SELECT max(source_observed_at_ms) AS latest_source_at_ms,
                   max(source_observed_at_ms) FILTER (WHERE status = 'CASE_CREATED')
                     AS latest_gate_eligible_at_ms
              FROM ({LATEST_GATE_DECISION_PER_SOURCE_SQL}) latest
            """,  # noqa: S608
            (trigger_kind, 0),
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
