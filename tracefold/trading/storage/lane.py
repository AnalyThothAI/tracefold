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
from ..contracts import CURRENT_TERMINAL_STATES, CaseState, DecisionRuntimeV1, TradingCaseManifest
from .execution_stream import ExecutionStreamStorage, PreparedTradeSignal, _dumps
from .gate import CandidateGateStorage


@dataclass(frozen=True, slots=True)
class SignalLaneSnapshot:
    """Only durable facts needed to prevent duplicate or wasted Alpha work."""

    cased_source_keys: frozenset[str]
    underlyings_in_flight: frozenset[str]
    # The union of every published Runtime route catalogue, or `None` when no Runtime has published
    # one. `None` and "empty catalogue" have to be different answers: with execution disabled there is
    # no projection row at all, the Signal is a notification card, and refusing every market would
    # silently delete the product.
    executable_market_keys: frozenset[str] | None = None


class LaneStorage(CandidateGateStorage, ExecutionStreamStorage):
    conn: Any

    def signal_lane_snapshot(self, *, since_ms: int) -> SignalLaneSnapshot:
        rows = self.conn.execute(
            """
            SELECT primary_source_key, underlying_key, state
              FROM trading_cases
             WHERE created_at_ms >= %s OR state IN ('PENDING', 'RUNNING')
            """,
            (int(since_ms),),
        ).fetchall()
        # One read, two facts: whether this Source already has a Case, and whether any configured
        # Runtime can execute its market at all. A Case frozen for a market no Runtime lists spends
        # the turn's one freeze and comes back `instrument_unmapped`.
        routes = self.conn.execute(
            """
            SELECT coalesce(jsonb_agg(DISTINCT market_key), '[]'::jsonb) AS market_keys
              FROM trading_execution_runtime_state,
                   LATERAL jsonb_array_elements_text(routes) AS market_key
            """
        ).fetchone()
        executable = frozenset(str(value) for value in (routes["market_keys"] if routes is not None else ()))
        return SignalLaneSnapshot(
            cased_source_keys=frozenset(str(row["primary_source_key"]) for row in rows),
            underlyings_in_flight=frozenset(
                str(row["underlying_key"]) for row in rows if str(row["state"]) in {"PENDING", "RUNNING"}
            ),
            executable_market_keys=executable or None,
        )

    def create_case(
        self,
        *,
        case_id: str,
        manifest: TradingCaseManifest,
        admission: AdmissionRow,
        release_revision: str,
        now_ms: int,
    ) -> bool:
        """Insert one immutable Case and its CASE_CREATED admission row in one transaction."""

        trigger = manifest.primary_trigger
        cursor = self.conn.execute(
            """
            INSERT INTO trading_cases (
              case_id, underlying_key, trigger_kind, strategy_id, strategy_version,
              strategy_config_digest, primary_source_key, supplemental_source_keys,
              manifest, manifest_sha256, state, policy_decision, policy_reason,
              observed_at_ms, source_observed_at_ms,
              trigger_persisted_at_ms, created_at_ms, updated_at_ms
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s,
                      'PENDING', 'not_run', 'not_run', %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                case_id,
                manifest.underlying_key,
                manifest.trigger_kind,
                manifest.policy_id,
                manifest.policy_version,
                manifest.policy_config_digest,
                trigger.source_key,
                _dumps([]),
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
            gate_version=admission["gate_version"],
            gate_config_digest=admission["gate_config_digest"],
            trigger_kind=admission["trigger_kind"],
            underlying_key=admission["underlying_key"],
            source_observed_at_ms=admission["source_observed_at_ms"],
            status=admission["status"],
            stage=admission["stage"],
            reason=admission["reason"],
            retryable=admission["retryable"],
            evidence=admission["evidence"],
            case_id=case_id,
            release_revision=release_revision,
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
        manifest: TradingCaseManifest,
        policy_reason: str,
        policy_checks: Mapping[str, Any],
        prepared: PreparedTradeSignal,
        now_ms: int,
    ) -> bool:
        """Commit exactly one Signal and the Case terminal transition, or neither."""

        row = self.conn.execute(
            """
            SELECT manifest_sha256
              FROM trading_cases
             WHERE case_id = %s AND run_id = %s AND state IN ('PENDING', 'RUNNING')
             FOR UPDATE
            """,
            (case_id, run_id),
        ).fetchone()
        if row is None:
            return False
        if str(row["manifest_sha256"]) != manifest.digest():
            raise RuntimeError("trading_case_signal_claim_invalid")
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

    def claim_case(self, *, run_id: str, lease_ms: int, now_ms: int) -> dict[str, Any] | None:
        """Take the oldest claimable Case under a short lease.

        A `RUNNING` Case whose lease expired may be reclaimed: re-running an undecided Case is safe,
        and the state predicate on the terminal transition — not the lease — is what prevents two
        workers handing the same Case over twice.
        """

        row = self.conn.execute(
            """
            UPDATE trading_cases
               SET state = 'RUNNING',
                   run_id = %s,
                   lease_expires_at_ms = %s,
                   attempt_count = attempt_count + 1,
                   updated_at_ms = %s
             WHERE case_id = (
                     SELECT case_id FROM trading_cases
                      WHERE state = 'PENDING'
                         OR (state = 'RUNNING' AND coalesce(lease_expires_at_ms, 0) < %s)
                      ORDER BY created_at_ms, case_id
                      FOR UPDATE SKIP LOCKED
                      LIMIT 1
                   )
         RETURNING *
            """,
            (run_id, int(now_ms) + int(lease_ms), int(now_ms), int(now_ms)),
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
              supplemental_source_keys, manifest, manifest_sha256, state,
              policy_decision, policy_reason, observed_at_ms, created_at_ms, updated_at_ms,
              strategy_id, strategy_version, strategy_config_digest
            ) VALUES (
              %s, 'restore:RESTORE', 'oi', 'restore-source', '[]'::jsonb,
              '{"restore":"case","manifest_version":"trading_manifest_v11","market_key":"crypto:perp:RESTORE:USDT"}'::jsonb,
              %s, 'SIGNAL_EMITTED', 'long', 'restore_drill',
              10, 10, 10, 'restore_strategy', 'restore_v1', %s
            )
            """,
            (case_id, "a" * 64, "b" * 64),
        )

    def decision_runtime(self) -> DecisionRuntimeV1 | None:
        row = self.conn.execute(
            "SELECT state, heartbeat_at_ms, reason, updated_at_ms FROM trading_decision_runtime WHERE id = 1"
        ).fetchone()
        return DecisionRuntimeV1(**dict(row)) if row is not None else None

    def set_decision_runtime(
        self,
        *,
        state: str,
        heartbeat_at_ms: int | None,
        reason: str | None,
        now_ms: int,
    ) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_decision_runtime
               SET state = %(state)s,
                   heartbeat_at_ms = %(heartbeat)s,
                   reason = %(reason)s,
                   updated_at_ms = %(now)s
             WHERE id = 1
         RETURNING id
            """,
            {
                "state": state,
                "heartbeat": None if heartbeat_at_ms is None else int(heartbeat_at_ms),
                "reason": reason,
                "now": int(now_ms),
            },
        ).fetchone()
        return row is not None


__all__ = ["LaneStorage", "SignalLaneSnapshot"]
