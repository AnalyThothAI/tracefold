"""TradeIntent and execution-outcome persistence."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, cast

from ..intent import (
    ACTIVE_INTENT_STATES,
    IntentOutcome,
    IntentReasonCode,
    ManualReviewReason,
    RejectedReason,
    TradeIntent,
    deterministic_client_order_id,
)
from ..quote_authority import (
    ExecutionQuoteAuditV1,
    ExecutionQuoteRejectionV1,
    ExecutionQuoteSnapshotV1,
    ExecutionSide,
    QuoteStage,
)
from .sql_values import _dumps

_IMMUTABLE_COLUMNS = """
intent_id, intent_version, case_id, case_manifest_sha256,
source_venue, source_identity, canonical_asset, underlying_key, binding, account_generation,
execution_binding_sha256, venue_catalog_snapshot_sha256, execution_capability_snapshot_sha256,
capability_entry_id, provider_instrument_id, instrument_id, settlement_asset,
intent_policy_sha256, execution_policy_sha256, quote_contract_sha256, protection_contract_sha256,
capital_authorization_receipt_sha256,
blacklist_revision_at_emission, blacklist_snapshot_sha256_at_emission,
blacklist_snapshot_payload_at_emission,
economic_lifecycle_id, entry_leg_id, protection_leg_id, close_leg_id,
side, leverage, created_at_ms, valid_until_ms,
reference_price, target_notional, max_risk_amount, risk_currency, stop_loss_bps, max_holding_ms,
max_entry_drift_bps, max_spread_bps
"""
_OUTCOME_COLUMNS = """
intent_id, engine_identity, execution_state, execution_phase, terminal_outcome,
reason_code, entry_client_order_id, entry_fenced_at_ms,
adopted_at_ms, entry_fence_requested_at_ms, submission_fence_version,
submission_quantity, entry_quote_q1, entry_quote_q2, entry_submitted_at_ms, entry_accepted_at_ms,
stop_client_order_id, stop_submitted_at_ms, close_client_order_id, close_submitted_at_ms,
stop_generation, actual_quantity, protected_quantity, avg_entry_price, avg_exit_price,
position_id, protection_order_id,
stop_price, opened_at_ms, protected_at_ms, closed_at_ms, flat_verified_at_ms,
realized_pnl_amount, realized_pnl_currency, commissions_by_currency, updated_at_ms
"""
_INTENT_OUTCOME_COLUMNS = ", ".join(f"intent.{column.strip()}" for column in _OUTCOME_COLUMNS.split(","))


EntryFenceDisposition = Literal["GRANTED", "REFUSED", "UNAVAILABLE"]
# Why the fence could not be taken, when nothing terminal was written. Closed, because the execution
# authority branches on it and `None` used to mean all four of these at once.
EntryFenceUnavailable = Literal[
    "intent_not_claimable",
    "runtime_not_ready",
    "intent_expired",
]
# Serialisation is deliberately absent: `ux_trading_intents_one_active` is a unique index, so a second
# live Intent cannot exist to be refused here (#348).
type EntryFenceReason = EntryFenceUnavailable | Literal["entry_fence_granted"] | IntentReasonCode


@dataclass(frozen=True, slots=True)
class EntryFence:
    """The one typed answer to "may this Intent send an economic entry now?" (#331).

    Three dispositions, never mixed:

        GRANTED      the fence is committed. `outcome` is the durable `IN_FLIGHT` projection, and only
                     after this row commits may a provider entry be sent.
        REFUSED      a terminal `REJECTED` was written at zero exposure, with a durable reason.
        UNAVAILABLE  nothing was written. The Intent is not claimable *now* — a stale dispatch, a
                     runtime that is not ready, or an expired TTL.

    `fence_entry` used to return `IntentOutcome | None`, and `None` carried every one of the
    `UNAVAILABLE` cases plus the race where another engine already fenced. The caller could only
    release the Intent and had nothing to record, so a lane that was refusing every entry because
    `nautilus_ready` was false looked exactly like one with nothing to do.
    """

    disposition: EntryFenceDisposition
    # Typed, because a bare `str` made `EntryFenceUnavailable` decorative: #348 invented an
    # `active_intent_exists` reason, documented it and parametrised a test with it, and nothing
    # objected because no annotation connected the Literal to this field. It cannot happen twice.
    reason: EntryFenceReason
    outcome: IntentOutcome | None = None

    @property
    def granted(self) -> bool:
        return self.disposition == "GRANTED"


def _require_quote_audit(
    evidence: ExecutionQuoteAuditV1,
    *,
    intent_id: str,
    instrument_id: str,
    intent_side: str,
    stage: QuoteStage,
    accepted: bool,
    reason: str | None = None,
) -> dict[str, str | int]:
    expected_side: ExecutionSide | None
    if intent_side in {"long", "buy"}:
        expected_side = "buy"
    elif intent_side in {"short", "sell"}:
        expected_side = "sell"
    else:
        expected_side = None
    expected_reason = "accepted" if accepted else reason
    if (
        evidence.intent_id != intent_id
        or evidence.instrument_id != instrument_id
        or expected_side is None
        or evidence.side != expected_side
        or evidence.stage != stage
        or evidence.reason != expected_reason
        or accepted != isinstance(evidence, ExecutionQuoteSnapshotV1)
    ):
        raise ValueError("entry_quote_audit_invalid")
    payload = _quote_audit_payload(evidence)
    if len(_dumps(payload)) > 2_048:
        raise ValueError("entry_quote_audit_invalid")
    return payload


def _quote_audit_payload(evidence: ExecutionQuoteAuditV1) -> dict[str, str | int]:
    """Map the frozen domain value explicitly onto the durable JSON contract."""

    payload: dict[str, str | int] = {
        "snapshot_version": evidence.snapshot_version,
        "stage": evidence.stage,
        "reason": evidence.reason,
        "intent_id": evidence.intent_id,
        "instrument_id": evidence.instrument_id,
        "side": evidence.side or "",
        "evaluated_at_ns": evidence.evaluated_at_ns,
    }
    if isinstance(evidence, ExecutionQuoteSnapshotV1):
        payload.update(
            {
                "side_price": str(evidence.side_price),
                "bid": str(evidence.bid),
                "ask": str(evidence.ask),
                "ts_event_ns": evidence.ts_event_ns,
                "ts_init_ns": evidence.ts_init_ns,
                "stream_generation": evidence.stream_generation,
                "receive_age_ns": evidence.receive_age_ns,
                "event_age_ns": evidence.event_age_ns,
                "source_latency_ns": evidence.source_latency_ns,
                "spread_bps": str(evidence.spread_bps),
                "reference_drift_bps": str(evidence.reference_drift_bps),
            }
        )
        return payload
    optional = {
        "observed_instrument_id": evidence.observed_instrument_id,
        "bid": evidence.bid,
        "ask": evidence.ask,
        "ts_event_ns": evidence.ts_event_ns,
        "ts_init_ns": evidence.ts_init_ns,
        "stream_generation": evidence.stream_generation,
        "receive_age_ns": evidence.receive_age_ns,
        "event_age_ns": evidence.event_age_ns,
        "source_latency_ns": evidence.source_latency_ns,
        "spread_bps": evidence.spread_bps,
        "reference_drift_bps": evidence.reference_drift_bps,
    }
    for name, value in optional.items():
        if value is not None:
            payload[name] = str(value) if isinstance(value, Decimal) else value
    return payload


class IntentStorage:
    conn: Any

    def _quote_identity(self, intent_id: str) -> tuple[str, str] | None:
        row = self.conn.execute(
            "SELECT instrument_id, side FROM trading_intents WHERE intent_id = %s",
            (intent_id,),
        ).fetchone()
        return None if row is None else (str(row["instrument_id"]), str(row["side"]))

    def insert_intent(self, intent: TradeIntent) -> bool:
        values = intent.model_dump()
        blacklist_snapshot = intent.blacklist_snapshot_payload_at_emission
        values["blacklist_snapshot_payload_at_emission"] = _dumps(blacklist_snapshot.model_dump(mode="json"))
        row = self.conn.execute(
            f"""
            INSERT INTO trading_intents ({_IMMUTABLE_COLUMNS})
            VALUES (
              %(intent_id)s, %(intent_version)s, %(case_id)s, %(case_manifest_sha256)s,
              %(source_venue)s, %(source_identity)s, %(canonical_asset)s, %(underlying_key)s,
              %(binding)s, %(account_generation)s, %(execution_binding_sha256)s,
              %(venue_catalog_snapshot_sha256)s, %(execution_capability_snapshot_sha256)s,
              %(capability_entry_id)s, %(provider_instrument_id)s, %(instrument_id)s, %(settlement_asset)s,
              %(intent_policy_sha256)s, %(execution_policy_sha256)s, %(quote_contract_sha256)s,
              %(protection_contract_sha256)s, %(capital_authorization_receipt_sha256)s,
              %(blacklist_revision_at_emission)s,
              %(blacklist_snapshot_sha256_at_emission)s,
              %(blacklist_snapshot_payload_at_emission)s::jsonb,
              %(economic_lifecycle_id)s, %(entry_leg_id)s, %(protection_leg_id)s, %(close_leg_id)s,
              %(side)s, %(leverage)s,
              %(created_at_ms)s, %(valid_until_ms)s, %(reference_price)s, %(target_notional)s,
              %(max_risk_amount)s, %(risk_currency)s,
              %(stop_loss_bps)s, %(max_holding_ms)s, %(max_entry_drift_bps)s, %(max_spread_bps)s
            )
            ON CONFLICT (intent_id) DO NOTHING
            RETURNING intent_id
            """,
            values,
        ).fetchone()
        return row is not None

    def intent(self, intent_id: str) -> TradeIntent | None:
        row = self.conn.execute(
            f"SELECT {_IMMUTABLE_COLUMNS} FROM trading_intents "
            "WHERE intent_id = %s AND intent_version = 'trade_intent_v3'",
            (intent_id,),
        ).fetchone()
        return None if row is None else TradeIntent.model_validate(dict(row))

    def intent_outcome(self, intent_id: str) -> IntentOutcome | None:
        row = self.conn.execute(
            f"SELECT {_OUTCOME_COLUMNS} FROM trading_intents WHERE intent_id = %s",
            (intent_id,),
        ).fetchone()
        return None if row is None else IntentOutcome.model_validate(dict(row))

    def intent_for_case(self, *, case_id: str) -> tuple[TradeIntent, IntentOutcome] | None:
        row = self.conn.execute(
            f"SELECT {_IMMUTABLE_COLUMNS}, {_OUTCOME_COLUMNS} FROM trading_intents "
            "WHERE case_id = %s AND intent_version = 'trade_intent_v3'",
            (case_id,),
        ).fetchone()
        if row is None:
            return None
        values = dict(row)
        return (
            TradeIntent.model_validate({name: values[name] for name in TradeIntent.model_fields}),
            IntentOutcome.model_validate({name: values[name] for name in IntentOutcome.model_fields}),
        )

    def active_intent(self) -> tuple[TradeIntent, IntentOutcome] | None:
        """Return the single non-terminal handoff, if one exists."""

        row = self.conn.execute(
            f"""
            SELECT {_IMMUTABLE_COLUMNS}, {_OUTCOME_COLUMNS}
             FROM trading_intents
             WHERE intent_version = 'trade_intent_v3' AND execution_state = ANY(%s)
             ORDER BY created_at_ms, intent_id
             LIMIT 1
               FOR UPDATE SKIP LOCKED
            """,
            (list(ACTIVE_INTENT_STATES),),
        ).fetchone()
        if row is None:
            return None
        values = dict(row)
        intent_values = {name: values[name] for name in TradeIntent.model_fields}
        outcome_values = {name: values[name] for name in IntentOutcome.model_fields}
        return TradeIntent.model_validate(intent_values), IntentOutcome.model_validate(outcome_values)

    def mark_intent_adopted(self, intent_id: str, *, now_ms: int) -> IntentOutcome | None:
        return self._outcome_update(
            f"""
            UPDATE trading_intents
               SET adopted_at_ms = COALESCE(adopted_at_ms, %(now)s),
                   updated_at_ms = GREATEST(updated_at_ms, %(now)s)
             WHERE intent_id = %(intent_id)s
               AND execution_state = 'PENDING'
               AND entry_fenced_at_ms IS NULL
         RETURNING {_OUTCOME_COLUMNS}
            """,
            {"intent_id": intent_id, "now": int(now_ms)},
        )

    def fence_entry(
        self,
        intent_id: str,
        *,
        engine_identity: str,
        submission_quantity: Decimal,
        q1_evidence: ExecutionQuoteSnapshotV1,
        requested_at_ms: int,
        now_ms: int,
    ) -> EntryFence:
        """Take the durable entry fence, or say in one closed word why it was not taken.

        The order is: prove the two capital-authority facts that must terminate the Intent at zero
        exposure (capability, deny-list), then attempt the fence itself under the runtime row's own
        lock. A `GRANTED` fence is committed before the caller may send anything to the venue; that is
        the whole at-most-once guarantee, and it is why this returns a disposition rather than a
        nullable outcome the caller has to guess at.
        """

        if submission_quantity <= 0:
            raise ValueError("submission_fence_quantity_invalid")
        if requested_at_ms > now_ms:
            raise ValueError("submission_fence_clock_invalid")

        blacklist = cast(Any, self).blacklist_snapshot(now_ms=now_ms, materialize_expiry=True)
        observation = blacklist.model_dump(mode="json")
        permission = self.conn.execute(
            """
            SELECT intent.underlying_key,
                   intent.instrument_id,
                   intent.side,
                   intent.valid_until_ms,
                   intent.execution_capability_snapshot_sha256,
                   binding.capability_snapshot_sha256 AS active_capability_snapshot_sha256,
                   (snapshot.payload -> 'included' ? intent.capability_entry_id) AS instrument_in_snapshot
              FROM trading_intents intent
              JOIN trading_binding_runtime binding ON binding.binding = intent.binding
              LEFT JOIN trading_execution_capability_snapshots snapshot
                ON snapshot.snapshot_sha256 = intent.execution_capability_snapshot_sha256
             WHERE intent.intent_id = %s
               AND intent.intent_version = 'trade_intent_v3'
               AND intent.execution_state = 'PENDING'
               AND intent.entry_fenced_at_ms IS NULL
            """,
            (intent_id,),
        ).fetchone()
        if permission is None:
            # Not PENDING, already fenced, or gone. Nothing was written and nothing should be sent.
            return EntryFence(disposition="UNAVAILABLE", reason="intent_not_claimable")
        q1_payload = _require_quote_audit(
            q1_evidence,
            intent_id=intent_id,
            instrument_id=str(permission["instrument_id"]),
            intent_side=str(permission["side"]),
            stage="Q1",
            accepted=True,
        )
        reason: IntentReasonCode | None = None
        if (
            permission["execution_capability_snapshot_sha256"] != permission["active_capability_snapshot_sha256"]
            or not permission["instrument_in_snapshot"]
        ):
            reason = "capability_mismatch"
        elif any(row.underlying_key == permission["underlying_key"] for row in blacklist.active_rows):
            reason = "blacklisted"
        if reason is not None:
            row = self.conn.execute(
                f"""
                UPDATE trading_intents
                   SET execution_state = 'TERMINAL', terminal_outcome = 'REJECTED',
                       reason_code = %(reason)s,
                       adopted_at_ms = COALESCE(adopted_at_ms, %(requested_at)s),
                       entry_fence_requested_at_ms = %(requested_at)s,
                       entry_quote_q1 = %(q1_evidence)s::jsonb,
                       blacklist_revision_at_fence = %(blacklist_revision)s,
                       blacklist_snapshot_sha256_at_fence = %(blacklist_sha)s,
                       blacklist_snapshot_payload_at_fence = %(blacklist_payload)s::jsonb,
                       updated_at_ms = %(now)s
                 WHERE intent_id = %(intent_id)s
                   AND execution_state = 'PENDING'
                   AND entry_fenced_at_ms IS NULL
             RETURNING {_OUTCOME_COLUMNS}
                """,
                {
                    "intent_id": intent_id,
                    "reason": reason,
                    "requested_at": int(requested_at_ms),
                    "q1_evidence": _dumps(q1_payload),
                    "blacklist_revision": blacklist.revision,
                    "blacklist_sha": blacklist.snapshot_sha256,
                    "blacklist_payload": _dumps(observation),
                    "now": int(now_ms),
                },
            ).fetchone()
            if row is None:
                return EntryFence(disposition="UNAVAILABLE", reason="intent_not_claimable")
            return EntryFence(
                disposition="REFUSED",
                reason=reason,
                outcome=IntentOutcome.model_validate(dict(row)),
            )
        row = self.conn.execute(
            f"""
            UPDATE trading_intents intent
               SET engine_identity = %(engine)s,
                   execution_state = 'IN_FLIGHT',
                   execution_phase = 'ENTRY',
                   entry_client_order_id = %(client_id)s,
                   entry_fenced_at_ms = %(now)s,
                   adopted_at_ms = COALESCE(adopted_at_ms, %(requested_at)s),
                   entry_fence_requested_at_ms = %(requested_at)s,
                   submission_fence_version = 'submission_fence_v1',
                   submission_quantity = %(submission_quantity)s,
                   entry_quote_q1 = %(q1_evidence)s::jsonb,
                   blacklist_revision_at_fence = %(blacklist_revision)s,
                   blacklist_snapshot_sha256_at_fence = %(blacklist_sha)s,
                   blacklist_snapshot_payload_at_fence = %(blacklist_payload)s::jsonb,
                   updated_at_ms = %(now)s
              FROM (
                    SELECT id, control
                      FROM trading_runtime_state
                     WHERE id = 1
                       FOR UPDATE
                   ) runtime,
                   trading_binding_runtime binding
             WHERE intent.intent_id = %(intent_id)s
               AND intent.intent_version = 'trade_intent_v3'
               AND intent.execution_state = 'PENDING'
               AND intent.entry_fenced_at_ms IS NULL
               AND intent.valid_until_ms > %(now)s
               AND runtime.id = 1
               AND runtime.control = 'RUNNING'
               AND binding.binding = intent.binding
               AND binding.runtime_state = 'ready'
               AND binding.account_state = 'reconciled_flat'
               AND binding.capability_state = 'ready'
               AND intent.execution_capability_snapshot_sha256 = binding.capability_snapshot_sha256
               AND intent.execution_binding_sha256 = binding.execution_binding_sha256
               AND NOT EXISTS (
                     SELECT 1 FROM trading_symbol_blacklist denied
                      WHERE ('crypto:' || denied.base_symbol) = intent.underlying_key
                        AND (denied.expires_at_ms IS NULL OR denied.expires_at_ms > %(now)s)
                   )
         RETURNING {_INTENT_OUTCOME_COLUMNS}
            """,
            {
                "intent_id": intent_id,
                "engine": engine_identity,
                "client_id": deterministic_client_order_id(intent_id, "entry"),
                "requested_at": int(requested_at_ms),
                "submission_quantity": submission_quantity,
                "q1_evidence": _dumps(q1_payload),
                "now": int(now_ms),
                "blacklist_revision": blacklist.revision,
                "blacklist_sha": blacklist.snapshot_sha256,
                "blacklist_payload": _dumps(observation),
            },
        ).fetchone()
        if row is not None:
            return EntryFence(
                disposition="GRANTED",
                reason="entry_fence_granted",
                outcome=IntentOutcome.model_validate(dict(row)),
            )
        # The UPDATE matched nothing. Say which of the guard clauses refused, from the same statement
        # snapshot, so a lane held back by an unready engine is distinguishable from one whose Intent
        # simply aged out. Serialisation is not among the answers: `ux_trading_intents_one_active` is a
        # unique index, so a second live Intent cannot exist to be refused here (#348).
        if int(permission["valid_until_ms"]) <= int(now_ms):
            return EntryFence(disposition="UNAVAILABLE", reason="intent_expired")
        return EntryFence(disposition="UNAVAILABLE", reason="runtime_not_ready")

    def record_entry_preflight_no_submit(
        self,
        intent_id: str,
        *,
        reason_code: IntentReasonCode,
        q1_evidence: ExecutionQuoteAuditV1,
        now_ms: int,
    ) -> IntentOutcome | None:
        identity = self._quote_identity(intent_id)
        if identity is None:
            return None
        q1_accepted = isinstance(q1_evidence, ExecutionQuoteSnapshotV1)
        q1_payload = _require_quote_audit(
            q1_evidence,
            intent_id=intent_id,
            instrument_id=identity[0],
            intent_side=identity[1],
            stage="Q1",
            accepted=q1_accepted,
            reason=None if q1_accepted else reason_code,
        )
        return self._outcome_update(
            f"""
            UPDATE trading_intents
               SET execution_state = 'TERMINAL',
                   execution_phase = NULL,
                   terminal_outcome = 'REJECTED',
                   reason_code = %(reason)s,
                   adopted_at_ms = COALESCE(adopted_at_ms, %(now)s),
                   entry_quote_q1 = %(q1_evidence)s::jsonb,
                   updated_at_ms = %(now)s
             WHERE intent_id = %(intent_id)s
               AND execution_state = 'PENDING'
               AND entry_fenced_at_ms IS NULL
         RETURNING {_OUTCOME_COLUMNS}
            """,
            {
                "intent_id": intent_id,
                "reason": reason_code,
                "q1_evidence": _dumps(q1_payload),
                "now": int(now_ms),
            },
        )

    def authorize_entry_submission(
        self,
        intent_id: str,
        *,
        entry_client_order_id: str,
        q2_evidence: ExecutionQuoteSnapshotV1,
        now_ms: int,
    ) -> IntentOutcome | None:
        identity = self._quote_identity(intent_id)
        if identity is None:
            return None
        q2_payload = _require_quote_audit(
            q2_evidence,
            intent_id=intent_id,
            instrument_id=identity[0],
            intent_side=identity[1],
            stage="Q2",
            accepted=True,
        )
        return self._outcome_update(
            f"""
            UPDATE trading_intents
               SET entry_quote_q2 = %(q2_evidence)s::jsonb,
                   updated_at_ms = %(now)s
             WHERE intent_id = %(intent_id)s
               AND execution_state = 'IN_FLIGHT'
               AND execution_phase = 'ENTRY'
               AND submission_fence_version = 'submission_fence_v1'
               AND entry_client_order_id = %(client_id)s
               AND entry_quote_q2 IS NULL
               AND entry_submitted_at_ms IS NULL
         RETURNING {_OUTCOME_COLUMNS}
            """,
            {
                "intent_id": intent_id,
                "client_id": entry_client_order_id,
                "q2_evidence": _dumps(q2_payload),
                "now": int(now_ms),
            },
        )

    def record_fenced_quote_no_submit(
        self,
        intent_id: str,
        *,
        entry_client_order_id: str,
        reason_code: RejectedReason,
        q2_evidence: ExecutionQuoteRejectionV1,
        now_ms: int,
    ) -> IntentOutcome | None:
        identity = self._quote_identity(intent_id)
        if identity is None:
            return None
        q2_payload = _require_quote_audit(
            q2_evidence,
            intent_id=intent_id,
            instrument_id=identity[0],
            intent_side=identity[1],
            stage="Q2",
            accepted=False,
            reason=reason_code,
        )
        return self._outcome_update(
            f"""
            UPDATE trading_intents
               SET execution_state = 'TERMINAL',
                   execution_phase = NULL,
                   terminal_outcome = 'REJECTED',
                   reason_code = %(reason)s,
                   entry_quote_q2 = %(q2_evidence)s::jsonb,
                   flat_verified_at_ms = %(now)s,
                   updated_at_ms = %(now)s
             WHERE intent_id = %(intent_id)s
               AND execution_state = 'IN_FLIGHT'
               AND execution_phase = 'ENTRY'
               AND submission_fence_version = 'submission_fence_v1'
               AND entry_client_order_id = %(client_id)s
               AND (entry_quote_q2 IS NULL OR entry_quote_q2 ->> 'reason' = 'accepted')
               AND entry_submitted_at_ms IS NULL
               AND actual_quantity IS NULL
         RETURNING {_OUTCOME_COLUMNS}
            """,
            {
                "intent_id": intent_id,
                "client_id": entry_client_order_id,
                "reason": reason_code,
                "q2_evidence": _dumps(q2_payload),
                "now": int(now_ms),
            },
        )

    def record_entry_submitted(
        self,
        intent_id: str,
        *,
        entry_client_order_id: str,
        submitted_at_ms: int,
    ) -> IntentOutcome | None:
        return self._outcome_update(
            f"""
            UPDATE trading_intents
               SET entry_submitted_at_ms = COALESCE(entry_submitted_at_ms, %(now)s),
                   updated_at_ms = GREATEST(updated_at_ms, %(now)s)
             WHERE intent_id = %(intent_id)s
               AND execution_state = 'IN_FLIGHT'
               AND execution_phase = 'ENTRY'
               AND entry_client_order_id = %(client_id)s
               AND entry_quote_q2 ->> 'reason' = 'accepted'
         RETURNING {_OUTCOME_COLUMNS}
            """,
            {"intent_id": intent_id, "client_id": entry_client_order_id, "now": int(submitted_at_ms)},
        )

    def record_entry_accepted(
        self,
        intent_id: str,
        *,
        entry_client_order_id: str,
        accepted_at_ms: int,
    ) -> IntentOutcome | None:
        return self._outcome_update(
            f"""
            UPDATE trading_intents
               SET entry_accepted_at_ms = COALESCE(entry_accepted_at_ms, %(now)s),
                   updated_at_ms = GREATEST(updated_at_ms, %(now)s)
             WHERE intent_id = %(intent_id)s
               AND execution_state = 'IN_FLIGHT'
               AND execution_phase = 'ENTRY'
               AND entry_client_order_id = %(client_id)s
               AND entry_submitted_at_ms IS NOT NULL
         RETURNING {_OUTCOME_COLUMNS}
            """,
            {"intent_id": intent_id, "client_id": entry_client_order_id, "now": int(accepted_at_ms)},
        )

    def expire_unfenced_intent(self, intent_id: str, *, now_ms: int) -> IntentOutcome | None:
        return self._outcome_update(
            f"""
            UPDATE trading_intents
               SET execution_state = 'TERMINAL',
                   execution_phase = NULL,
                   terminal_outcome = 'EXPIRED',
                   reason_code = 'intent_expired',
                   updated_at_ms = %(now)s
             WHERE intent_id = %(intent_id)s
               AND execution_state = 'PENDING'
               AND entry_fenced_at_ms IS NULL
               AND valid_until_ms <= %(now)s
         RETURNING {_OUTCOME_COLUMNS}
            """,
            {"intent_id": intent_id, "now": int(now_ms)},
        )

    def record_rejected_without_exposure(
        self,
        intent_id: str,
        *,
        reason_code: RejectedReason,
        authoritative_quantity: Decimal,
        entry_client_order_id: str | None,
        now_ms: int,
    ) -> IntentOutcome | None:
        return self._outcome_update(
            f"""
            UPDATE trading_intents
               SET execution_state = 'TERMINAL',
                   execution_phase = NULL,
                   terminal_outcome = 'REJECTED',
                   reason_code = %(reason)s,
                   flat_verified_at_ms = CASE
                     WHEN entry_fenced_at_ms IS NOT NULL THEN %(now)s
                     ELSE flat_verified_at_ms
                   END,
                   updated_at_ms = %(now)s
             WHERE intent_id = %(intent_id)s
               AND actual_quantity IS NULL
               AND %(authoritative_quantity)s = 0
               AND (
                 (execution_state = 'PENDING'
                   AND entry_fenced_at_ms IS NULL
                   AND CAST(%(entry_client_order_id)s AS text) IS NULL)
                 OR
                 (execution_state IN ('IN_FLIGHT', 'MANUAL_REVIEW')
                   AND execution_phase = 'ENTRY'
                   AND entry_fenced_at_ms IS NOT NULL
                   AND entry_client_order_id = %(entry_client_order_id)s)
               )
         RETURNING {_OUTCOME_COLUMNS}
            """,
            {
                "intent_id": intent_id,
                "reason": reason_code,
                "authoritative_quantity": authoritative_quantity,
                "entry_client_order_id": entry_client_order_id,
                "now": int(now_ms),
            },
        )

    def mark_manual_review(
        self,
        intent_id: str,
        *,
        reason_code: ManualReviewReason,
        now_ms: int,
    ) -> IntentOutcome | None:
        return self._outcome_update(
            f"""
            UPDATE trading_intents
               SET execution_state = 'MANUAL_REVIEW',
                   terminal_outcome = NULL,
                   reason_code = %(reason)s,
                   updated_at_ms = %(now)s
             WHERE intent_id = %(intent_id)s
               AND execution_state IN ('IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW')
               AND entry_fenced_at_ms IS NOT NULL
         RETURNING {_OUTCOME_COLUMNS}
            """,
            {"intent_id": intent_id, "reason": reason_code, "now": int(now_ms)},
        )

    def record_entry_fill(
        self,
        intent_id: str,
        *,
        actual_quantity: Decimal,
        avg_entry_price: Decimal,
        position_id: str,
        opened_at_ms: int,
        now_ms: int,
    ) -> IntentOutcome | None:
        return self._outcome_update(
            f"""
            UPDATE trading_intents
               SET execution_state = 'IN_FLIGHT',
                   execution_phase = 'PROTECTION',
                   reason_code = NULL,
                   actual_quantity = %(quantity)s,
                   avg_entry_price = %(price)s,
                   position_id = %(position_id)s,
                   opened_at_ms = %(opened)s,
                   updated_at_ms = %(now)s
             WHERE intent_id = %(intent_id)s
               AND execution_state IN ('IN_FLIGHT', 'MANUAL_REVIEW')
               AND execution_phase = 'ENTRY'
               AND entry_fenced_at_ms IS NOT NULL
               AND actual_quantity IS NULL
         RETURNING {_OUTCOME_COLUMNS}
            """,
            {
                "intent_id": intent_id,
                "quantity": actual_quantity,
                "price": avg_entry_price,
                "position_id": position_id,
                "opened": int(opened_at_ms),
                "now": int(now_ms),
            },
        )

    def record_stop_submitted(
        self,
        intent_id: str,
        *,
        client_order_id: str,
        generation: int,
        previous_client_order_id: str | None,
        quantity: Decimal,
        now_ms: int,
    ) -> IntentOutcome | None:
        expected = deterministic_client_order_id(intent_id, "stop")
        if generation != 0 or previous_client_order_id is not None or client_order_id != expected:
            raise ValueError("initial_stop_identity_invalid")
        return self._outcome_update(
            f"""
            UPDATE trading_intents
               SET stop_client_order_id = %(client_id)s,
                   stop_generation = %(generation)s,
                   stop_submitted_at_ms = %(now)s,
                   updated_at_ms = %(now)s
             WHERE intent_id = %(intent_id)s
               AND execution_state = 'IN_FLIGHT'
               AND execution_phase = 'PROTECTION'
               AND actual_quantity IS NOT NULL
               AND actual_quantity = %(quantity)s
               AND stop_submitted_at_ms IS NULL
         RETURNING {_OUTCOME_COLUMNS}
            """,
            {
                "intent_id": intent_id,
                "client_id": client_order_id,
                "generation": generation,
                "quantity": quantity,
                "now": int(now_ms),
            },
        )

    def record_protected(
        self,
        intent_id: str,
        *,
        accepted_client_order_id: str,
        protection_order_id: str,
        protected_quantity: Decimal,
        stop_price: Decimal,
        protected_at_ms: int,
        now_ms: int,
    ) -> IntentOutcome | None:
        return self._outcome_update(
            f"""
            UPDATE trading_intents
               SET execution_state = CASE
                     WHEN execution_phase = 'EXIT' THEN 'IN_FLIGHT'
                     ELSE 'OPEN_PROTECTED'
                   END,
                   reason_code = NULL,
                   protection_order_id = %(protection_id)s,
                   protected_quantity = %(quantity)s,
                   stop_price = %(stop_price)s,
                   protected_at_ms = %(protected)s,
                   updated_at_ms = %(now)s
             WHERE intent_id = %(intent_id)s
               AND execution_state IN ('IN_FLIGHT', 'MANUAL_REVIEW')
               AND execution_phase IN ('PROTECTION', 'EXIT')
               AND actual_quantity IS NOT NULL
               AND actual_quantity = %(quantity)s
               AND stop_client_order_id = %(client_id)s
         RETURNING {_OUTCOME_COLUMNS}
            """,
            {
                "intent_id": intent_id,
                "client_id": accepted_client_order_id,
                "protection_id": protection_order_id,
                "quantity": protected_quantity,
                "stop_price": stop_price,
                "protected": int(protected_at_ms),
                "now": int(now_ms),
            },
        )

    def record_position_changed(
        self,
        intent_id: str,
        *,
        position_id: str,
        actual_quantity: Decimal,
        avg_entry_price: Decimal,
        now_ms: int,
    ) -> IntentOutcome | None:
        return self._outcome_update(
            f"""
            UPDATE trading_intents
               SET execution_state = 'IN_FLIGHT',
                   reason_code = NULL,
                   actual_quantity = %(quantity)s,
                   avg_entry_price = %(avg_entry_price)s,
                   updated_at_ms = %(now)s
             WHERE intent_id = %(intent_id)s
               AND execution_state IN ('IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW')
               AND execution_phase IN ('PROTECTION', 'EXIT')
               AND position_id = %(position_id)s
               AND (
                    protected_quantity IS DISTINCT FROM %(quantity)s
                    OR avg_entry_price IS DISTINCT FROM %(avg_entry_price)s
               )
               AND %(quantity)s > 0
         RETURNING {_OUTCOME_COLUMNS}
            """,
            {
                "intent_id": intent_id,
                "position_id": position_id,
                "quantity": actual_quantity,
                "avg_entry_price": avg_entry_price,
                "now": int(now_ms),
            },
        )

    def prepare_stop_replacement(
        self,
        intent_id: str,
        *,
        canceled_client_order_id: str,
        submitted_client_order_id: str,
        generation: int,
        quantity: Decimal,
        now_ms: int,
    ) -> IntentOutcome | None:
        next_client_order_id = deterministic_client_order_id(
            intent_id,
            "stop",
            previous_client_order_id=canceled_client_order_id,
        )
        if generation <= 0 or submitted_client_order_id != next_client_order_id:
            raise ValueError("replacement_stop_identity_invalid")
        return self._outcome_update(
            f"""
            UPDATE trading_intents
               SET stop_client_order_id = %(next_client_id)s,
                   stop_generation = %(generation)s,
                   stop_submitted_at_ms = %(now)s,
                   protection_order_id = NULL,
                   protected_quantity = NULL,
                   stop_price = NULL,
                   protected_at_ms = NULL,
                   reason_code = NULL,
                   updated_at_ms = %(now)s
             WHERE intent_id = %(intent_id)s
               AND execution_state = 'IN_FLIGHT'
               AND execution_phase IN ('PROTECTION', 'EXIT')
               AND stop_client_order_id = %(canceled_client_id)s
               AND stop_generation = %(previous_generation)s
               AND actual_quantity = %(quantity)s
               AND actual_quantity IS DISTINCT FROM protected_quantity
         RETURNING {_OUTCOME_COLUMNS}
            """,
            {
                "intent_id": intent_id,
                "canceled_client_id": canceled_client_order_id,
                "next_client_id": submitted_client_order_id,
                "generation": generation,
                "previous_generation": generation - 1,
                "quantity": quantity,
                "now": int(now_ms),
            },
        )

    def record_close_submitted(
        self,
        intent_id: str,
        *,
        client_order_id: str,
        position_id: str,
        quantity: Decimal,
        submitted_at_ms: int,
        now_ms: int,
    ) -> IntentOutcome | None:
        expected_client_order_id = deterministic_client_order_id(intent_id, "close")
        if client_order_id != expected_client_order_id:
            raise ValueError("close_identity_invalid")
        return self._outcome_update(
            f"""
            UPDATE trading_intents
               SET execution_state = 'IN_FLIGHT',
                   execution_phase = 'EXIT',
                   close_client_order_id = %(client_id)s,
                   close_submitted_at_ms = %(submitted_at)s,
                   reason_code = NULL,
                   updated_at_ms = %(now)s
             WHERE intent_id = %(intent_id)s
               AND execution_state IN ('IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW')
               AND entry_fenced_at_ms IS NOT NULL
               AND position_id = %(position_id)s
               AND actual_quantity = %(quantity)s
               AND actual_quantity > 0
               AND close_submitted_at_ms IS NULL
         RETURNING {_OUTCOME_COLUMNS}
            """,
            {
                "intent_id": intent_id,
                "client_id": client_order_id,
                "position_id": position_id,
                "quantity": quantity,
                "submitted_at": int(submitted_at_ms),
                "now": int(now_ms),
            },
        )

    def record_closed_flat(
        self,
        intent_id: str,
        *,
        position_id: str,
        authoritative_quantity: Decimal,
        avg_exit_price: Decimal,
        closed_at_ms: int,
        flat_verified_at_ms: int,
        realized_pnl_amount: Decimal | None,
        realized_pnl_currency: str | None,
        commissions_by_currency: dict[str, str] | None,
        now_ms: int,
    ) -> IntentOutcome | None:
        return self._outcome_update(
            f"""
            UPDATE trading_intents
               SET execution_state = 'TERMINAL',
                   execution_phase = 'EXIT',
                   terminal_outcome = 'CLOSED_FLAT',
                   reason_code = NULL,
                   avg_exit_price = %(exit_price)s,
                   closed_at_ms = %(closed)s,
                   flat_verified_at_ms = %(flat)s,
                   realized_pnl_amount = %(pnl)s,
                   realized_pnl_currency = %(currency)s,
                   commissions_by_currency = COALESCE(
                     %(commissions)s::jsonb,
                     commissions_by_currency
                   ),
                   updated_at_ms = %(now)s
             WHERE intent_id = %(intent_id)s
               AND execution_state IN ('IN_FLIGHT', 'MANUAL_REVIEW')
               AND execution_phase = 'EXIT'
               AND entry_fenced_at_ms IS NOT NULL
               AND actual_quantity IS NOT NULL
               AND position_id = %(position_id)s
               AND avg_exit_price = %(exit_price)s
               AND closed_at_ms = %(closed)s
               AND realized_pnl_amount IS NOT DISTINCT FROM %(pnl)s
               AND realized_pnl_currency IS NOT DISTINCT FROM %(currency)s
               AND %(authoritative_quantity)s = 0
         RETURNING {_OUTCOME_COLUMNS}
            """,
            {
                "intent_id": intent_id,
                "position_id": position_id,
                "authoritative_quantity": authoritative_quantity,
                "exit_price": avg_exit_price,
                "closed": int(closed_at_ms),
                "flat": int(flat_verified_at_ms),
                "pnl": realized_pnl_amount,
                "currency": realized_pnl_currency,
                "commissions": (None if commissions_by_currency is None else _dumps(commissions_by_currency)),
                "now": int(now_ms),
            },
        )

    def record_position_closed_observed(
        self,
        intent_id: str,
        *,
        instrument_id: str,
        account_id: str,
        position_id: str,
        closing_client_order_id: str,
        local_quantity: Decimal,
        avg_exit_price: Decimal,
        closed_at_ms: int,
        realized_pnl_amount: Decimal | None,
        realized_pnl_currency: str | None,
        commissions_by_currency: dict[str, str] | None,
        now_ms: int,
    ) -> IntentOutcome | None:
        """Persist a venue-fill close observation without claiming fresh venue flat."""

        if not instrument_id or not account_id:
            raise ValueError("close_observation_scope_invalid")
        return self._outcome_update(
            f"""
            UPDATE trading_intents
               SET execution_state = 'IN_FLIGHT',
                   execution_phase = 'EXIT',
                   terminal_outcome = NULL,
                   reason_code = NULL,
                   avg_exit_price = %(exit_price)s,
                   closed_at_ms = %(closed)s,
                   realized_pnl_amount = %(pnl)s,
                   realized_pnl_currency = %(currency)s,
                   commissions_by_currency = COALESCE(
                     %(commissions)s::jsonb,
                     commissions_by_currency
                   ),
                   updated_at_ms = %(now)s
             WHERE intent_id = %(intent_id)s
               AND instrument_id = %(instrument_id)s
               AND execution_state IN ('IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW')
               AND entry_fenced_at_ms IS NOT NULL
               AND actual_quantity IS NOT NULL
               AND position_id = %(position_id)s
               AND %(local_quantity)s = 0
               AND (
                    stop_client_order_id = %(closing_client_order_id)s
                    OR close_client_order_id = %(closing_client_order_id)s
               )
               AND (closed_at_ms IS NULL OR closed_at_ms = %(closed)s)
         RETURNING {_OUTCOME_COLUMNS}
            """,
            {
                "intent_id": intent_id,
                "instrument_id": instrument_id,
                "position_id": position_id,
                "closing_client_order_id": closing_client_order_id,
                "local_quantity": local_quantity,
                "exit_price": avg_exit_price,
                "closed": int(closed_at_ms),
                "pnl": realized_pnl_amount,
                "currency": realized_pnl_currency,
                "commissions": (None if commissions_by_currency is None else _dumps(commissions_by_currency)),
                "now": int(now_ms),
            },
        )

    def _outcome_update(self, statement: str, params: dict[str, Any]) -> IntentOutcome | None:
        row = self.conn.execute(statement, params).fetchone()
        return None if row is None else IntentOutcome.model_validate(dict(row))


__all__ = ["EntryFence", "EntryFenceDisposition", "EntryFenceUnavailable", "IntentStorage"]
