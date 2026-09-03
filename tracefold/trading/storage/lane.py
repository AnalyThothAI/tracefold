"""Atomic persistence owned by the Source -> Case -> Signal lane."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from ..admission import AdmissionRow
from ..contracts import CURRENT_TERMINAL_STATES, CaseState, TradingCaseManifest
from .execution_stream import PreparedTradeSignal
from .sql_values import _dumps


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


class LaneStorage:
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
        # the turn's one freeze and comes back `instrument_unmapped` (#510 B).
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
              capital_disposition, capital_reason, observed_at_ms, source_observed_at_ms,
              trigger_persisted_at_ms, created_at_ms, updated_at_ms
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s,
                      'PENDING', 'not_run', 'not_run', 'not_applicable', NULL, %s, %s, %s, %s, %s)
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
        cast(Any, self).record_gate_decision(
            now_ms=now_ms,
            release_revision=release_revision,
            **{**admission, "case_id": case_id},
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
                   capital_disposition = 'not_applicable',
                   capital_reason = NULL,
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
        cast(Any, self).append_trade_signal(prepared)
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


__all__ = ["LaneStorage", "SignalLaneSnapshot"]
