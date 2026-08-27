"""TradeIntent and execution-outcome persistence."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..intent import IntentOutcome, ManualReviewReason, RejectedReason, TradeIntent, deterministic_client_order_id
from .sql_values import _dumps

_IMMUTABLE_COLUMNS = """
intent_id, intent_version, case_id, case_manifest_sha256, intent_policy_sha256,
execution_environment, instrument_id, side, created_at_ms, valid_until_ms,
reference_price, target_notional_usd, stop_loss_bps, max_holding_ms,
max_entry_drift_bps, max_spread_bps
"""
_OUTCOME_COLUMNS = """
intent_id, engine_identity, execution_state, execution_phase, terminal_outcome,
reason_code, entry_client_order_id, entry_fenced_at_ms,
stop_client_order_id, stop_submitted_at_ms, close_client_order_id, close_submitted_at_ms,
stop_generation, actual_quantity, protected_quantity, avg_entry_price, avg_exit_price,
position_id, protection_order_id,
stop_price, opened_at_ms, protected_at_ms, closed_at_ms, flat_verified_at_ms,
realized_pnl_amount, realized_pnl_currency, commissions_by_currency, updated_at_ms
"""


class IntentStorage:
    conn: Any

    def insert_intent(self, intent: TradeIntent) -> bool:
        values = intent.model_dump()
        row = self.conn.execute(
            f"""
            INSERT INTO trading_intents ({_IMMUTABLE_COLUMNS})
            VALUES (
              %(intent_id)s, %(intent_version)s, %(case_id)s, %(case_manifest_sha256)s,
              %(intent_policy_sha256)s, %(execution_environment)s, %(instrument_id)s, %(side)s,
              %(created_at_ms)s, %(valid_until_ms)s, %(reference_price)s, %(target_notional_usd)s,
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
            f"SELECT {_IMMUTABLE_COLUMNS} FROM trading_intents WHERE intent_id = %s",
            (intent_id,),
        ).fetchone()
        return None if row is None else TradeIntent.model_validate(dict(row))

    def intent_outcome(self, intent_id: str) -> IntentOutcome | None:
        row = self.conn.execute(
            f"SELECT {_OUTCOME_COLUMNS} FROM trading_intents WHERE intent_id = %s",
            (intent_id,),
        ).fetchone()
        return None if row is None else IntentOutcome.model_validate(dict(row))

    def active_intent(self) -> tuple[TradeIntent, IntentOutcome] | None:
        """Return the single non-terminal handoff, if one exists."""

        row = self.conn.execute(
            f"""
            SELECT {_IMMUTABLE_COLUMNS}, {_OUTCOME_COLUMNS}
             FROM trading_intents
             WHERE execution_state IN ('PENDING', 'IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW')
             ORDER BY created_at_ms, intent_id
             LIMIT 1
               FOR UPDATE SKIP LOCKED
            """
        ).fetchone()
        if row is None:
            return None
        values = dict(row)
        intent_values = {name: values[name] for name in TradeIntent.model_fields}
        outcome_values = {name: values[name] for name in IntentOutcome.model_fields}
        return TradeIntent.model_validate(intent_values), IntentOutcome.model_validate(outcome_values)

    def fence_entry(self, intent_id: str, *, engine_identity: str, now_ms: int) -> IntentOutcome | None:
        row = self.conn.execute(
            f"""
            UPDATE trading_intents intent
               SET engine_identity = %(engine)s,
                   execution_state = 'IN_FLIGHT',
                   execution_phase = 'ENTRY',
                   entry_client_order_id = %(client_id)s,
                   entry_fenced_at_ms = %(now)s,
                   updated_at_ms = %(now)s
              FROM (
                    SELECT id, control, nautilus_ready, nautilus_unexpected_exposure
                      FROM trading_runtime_state
                     WHERE id = 1
                       FOR UPDATE
                   ) runtime
             WHERE intent.intent_id = %(intent_id)s
               AND intent.execution_state = 'PENDING'
               AND intent.entry_fenced_at_ms IS NULL
               AND intent.valid_until_ms > %(now)s
               AND runtime.id = 1
               AND runtime.control = 'RUNNING'
               AND runtime.nautilus_ready
               AND NOT runtime.nautilus_unexpected_exposure
               AND NOT EXISTS (
                     SELECT 1
                       FROM trading_intents prior
                      WHERE prior.entry_fenced_at_ms >= %(day_start)s
                        AND prior.entry_fenced_at_ms < %(day_end)s
                   )
         RETURNING {_OUTCOME_COLUMNS}
            """,
            {
                "intent_id": intent_id,
                "engine": engine_identity,
                "client_id": deterministic_client_order_id(intent_id, "entry"),
                "now": int(now_ms),
                "day_start": int(now_ms) // 86_400_000 * 86_400_000,
                "day_end": (int(now_ms) // 86_400_000 + 1) * 86_400_000,
            },
        ).fetchone()
        return None if row is None else IntentOutcome.model_validate(dict(row))

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
        now_ms: int,
    ) -> IntentOutcome | None:
        return self._outcome_update(
            f"""
            UPDATE trading_intents
               SET execution_state = 'IN_FLIGHT',
                   reason_code = NULL,
                   actual_quantity = %(quantity)s,
                   updated_at_ms = %(now)s
             WHERE intent_id = %(intent_id)s
               AND execution_state IN ('IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW')
               AND execution_phase IN ('PROTECTION', 'EXIT')
               AND position_id = %(position_id)s
               AND protected_quantity IS DISTINCT FROM %(quantity)s
               AND %(quantity)s > 0
         RETURNING {_OUTCOME_COLUMNS}
            """,
            {
                "intent_id": intent_id,
                "position_id": position_id,
                "quantity": actual_quantity,
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


__all__ = ["IntentStorage"]
