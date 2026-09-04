"""Atomic persistence owned by the Source -> Case -> Signal lane.

`LaneStorage` inherits the admission ledger and the execution stream because two of its writes are
atomic compositions with them: creating a Case also writes that Case's `CASE_CREATED` admission row,
and committing a Signal also appends the Signal. They are one transaction each, so they are one class.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..admission import AdmissionRow
from ..contracts import CURRENT_TERMINAL_STATES, CaseState, TradingCaseManifest
from .execution_stream import ExecutionStreamStorage, PreparedTradeSignal, _dumps
from .gate import CandidateGateStorage


@dataclass(frozen=True, slots=True)
class SignalLaneSnapshot:
    """The one durable fact that prevents duplicate Alpha work: which sources already have a Case."""

    cased_source_keys: frozenset[str]


class LaneStorage(CandidateGateStorage, ExecutionStreamStorage):
    conn: Any

    def signal_lane_snapshot(self, *, since_ms: int) -> SignalLaneSnapshot:
        """Which sources in the scan window already produced a Case. One read, one fact.

        It also read every Runtime's published route catalogue, so the lane could refuse a market no
        Runtime lists. The Runtime answers that itself, by name, on the entry path, and the projection
        needed a `None`-means-no-catalogue special case that no other reader had (#537 PR-3).
        """

        rows = self.conn.execute(
            """
            SELECT primary_source_key
              FROM trading_cases
             WHERE created_at_ms >= %s OR state IN ('PENDING', 'RUNNING')
            """,
            (int(since_ms),),
        ).fetchall()
        return SignalLaneSnapshot(cased_source_keys=frozenset(str(row["primary_source_key"]) for row in rows))

    def create_case(
        self,
        *,
        case_id: str,
        manifest: TradingCaseManifest,
        admission: AdmissionRow,
        now_ms: int,
    ) -> bool:
        """Insert one immutable Case and its CASE_CREATED admission row in one transaction.

        The policy identity is written once, inside `manifest`. It was also three columns beside it,
        and the columns were never the ones `_decide_one` compared (#537 PR-3).
        """

        trigger = manifest.primary_trigger
        cursor = self.conn.execute(
            """
            INSERT INTO trading_cases (
              case_id, underlying_key, trigger_kind, primary_source_key,
              manifest, manifest_sha256, state, policy_decision, policy_reason,
              observed_at_ms, source_observed_at_ms,
              trigger_persisted_at_ms, created_at_ms, updated_at_ms
            ) VALUES (%s, %s, %s, %s, %s::jsonb, %s,
                      'PENDING', 'not_run', 'not_run', %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                case_id,
                manifest.underlying_key,
                manifest.trigger_kind,
                trigger.source_key,
                _dumps(manifest.model_dump(mode="json")),
                manifest.digest(),
                int(manifest.cutoff_ms),
                int(trigger.observed_at_ms),
                int(trigger.persisted_at_ms),
                int(now_ms),
                int(now_ms),
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) <= 0:
            return False
        self.record_gate_decision(
            source_key=admission["source_key"],
            trigger_kind=admission["trigger_kind"],
            underlying_key=admission["underlying_key"],
            source_observed_at_ms=admission["source_observed_at_ms"],
            status=admission["status"],
            stage=admission["stage"],
            reason=admission["reason"],
            retryable=admission["retryable"],
            evidence=admission["evidence"],
            case_id=case_id,
            now_ms=now_ms,
        )
        return True

    def settle_case(
        self,
        *,
        case_id: str,
        run_id: str,
        state: CaseState,
        policy_decision: str,
        policy_reason: str,
        policy_checks: Mapping[str, Any] | None,
        now_ms: int,
    ) -> bool:
        if state not in CURRENT_TERMINAL_STATES:
            raise ValueError(f"trading_case_terminal_state_retired:{state}")
        cursor = self.conn.execute(
            """
            UPDATE trading_cases
               SET state = %s,
                   policy_decision = %s,
                   policy_reason = %s,
                   policy_checks = coalesce(%s::jsonb, policy_checks),
                   decided_at_ms = %s,
                   updated_at_ms = %s
             WHERE case_id = %s AND run_id = %s AND state IN ('PENDING', 'RUNNING')
            """,
            (
                state.value,
                policy_decision,
                policy_reason,
                None if policy_checks is None else _dumps(dict(policy_checks)),
                int(now_ms),
                int(now_ms),
                case_id,
                run_id,
            ),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def commit_signal(
        self,
        *,
        case_id: str,
        run_id: str,
        policy_reason: str,
        policy_checks: Mapping[str, Any],
        prepared: PreparedTradeSignal,
        now_ms: int,
    ) -> bool:
        """Commit exactly one Signal and the Case terminal transition, or neither.

        The claim is `case_id + run_id` on a still-undecided Case, taken `FOR UPDATE`. Re-deriving the
        caller's manifest digest and comparing it to the stored one said nothing that predicate does
        not: the run holding the lease is the run that froze the manifest (#520 PR-C).
        """

        row = self.conn.execute(
            """
            SELECT case_id
              FROM trading_cases
             WHERE case_id = %s AND run_id = %s AND state IN ('PENDING', 'RUNNING')
             FOR UPDATE
            """,
            (case_id, run_id),
        ).fetchone()
        if row is None:
            return False
        if prepared.value.case_id != case_id:
            raise RuntimeError("trading_case_signal_identity_invalid")
        self.append_trade_signal(prepared)
        if not self.settle_case(
            case_id=case_id,
            run_id=run_id,
            state=CaseState.SIGNAL_EMITTED,
            policy_decision="long",
            policy_reason=policy_reason,
            policy_checks=policy_checks,
            now_ms=now_ms,
        ):
            raise RuntimeError("trading_case_signal_transition_failed")
        return True

    def claim_case(self, *, run_id: str, now_ms: int) -> dict[str, Any] | None:
        """Take the oldest undecided Case for this run.

        `run_id` is the claim and `state IN ('PENDING', 'RUNNING')` on the terminal transition is what
        prevents two workers settling the same Case twice — the lease said so itself, and then expired
        on a wall clock nothing else in this lane reads. Re-running an undecided Case is safe, so a
        `RUNNING` row another run abandoned is simply claimed again by the next turn (#537 PR-3).
        """

        row = self.conn.execute(
            """
            UPDATE trading_cases
               SET state = 'RUNNING',
                   run_id = %s,
                   updated_at_ms = %s
             WHERE case_id = (
                     SELECT case_id FROM trading_cases
                      WHERE state IN ('PENDING', 'RUNNING')
                      ORDER BY created_at_ms, case_id
                      FOR UPDATE SKIP LOCKED
                      LIMIT 1
                   )
         RETURNING *
            """,
            (run_id, int(now_ms)),
        ).fetchone()
        return dict(row) if row is not None else None

    def case(self, *, case_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM trading_cases WHERE case_id = %s", (case_id,)).fetchone()
        return dict(row) if row is not None else None

    def cases(self, *, state: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if state:
            rows = self.conn.execute(
                "SELECT * FROM trading_cases WHERE state = %s ORDER BY created_at_ms DESC LIMIT %s",
                (state, int(limit)),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM trading_cases ORDER BY created_at_ms DESC LIMIT %s", (int(limit),)
            ).fetchall()
        return [dict(row) for row in rows]

    def seed_restore_drill_case(self, *, case_id: str) -> None:
        """Seed one current Signal Case for the isolated application restore drill."""

        self.conn.execute(
            """
            INSERT INTO trading_cases (
              case_id, underlying_key, trigger_kind, primary_source_key,
              manifest, manifest_sha256, state,
              policy_decision, policy_reason, observed_at_ms, created_at_ms, updated_at_ms
            ) VALUES (
              %s, 'restore:RESTORE', 'oi', 'restore-source',
              '{"restore":"case","manifest_version":"trading_manifest_v11","market_key":"crypto:perp:RESTORE:USDT"}'::jsonb,
              %s, 'SIGNAL_EMITTED', 'long', 'restore_drill', 10, 10, 10
            )
            """,
            (case_id, "a" * 64),
        )

    def latest_case_created_at_ms(self) -> int | None:
        """When the Signal lane last froze a Case, as the Decision Plane's only durable liveness.

        `trading_decision_runtime` was a one-row heartbeat the lane wrote every turn and every reader
        treated as a state machine; a missing row stopped the lane outright (#520). Serve, the CLI and
        Workers are separate processes, so the only honest cross-process answer is a durable fact the
        lane already writes -- the newest Case.
        """

        row = self.conn.execute("SELECT max(created_at_ms) AS latest FROM trading_cases").fetchone()
        latest = None if row is None else row["latest"]
        return None if latest is None else int(latest)


__all__ = ["LaneStorage", "SignalLaneSnapshot"]
