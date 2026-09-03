"""Translate concrete Runtime and Nautilus facts into durable observations."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any, Final

from tracefold.trading import OperatorIntentV1, TradeSignalV1

from .audit_sink import AuditSink
from .signal_client import ExecutionSignalClient
from .state import ExecutionState, RuntimeEntryRequest, RuntimeExecutionState, RuntimeReconciliationSnapshot

# Refusals that state the Runtime's own clock, not a verdict on the request. Each is answered by the
# next private reconciliation, the next quote, or that day's baseline write, all of which happen
# inside a Signal's TTL, so the Signal keeps no durable disposition and the next indexed poll offers
# it again; `expires_at_ns` still closes it with a terminal `expired`. Everything not listed here is
# terminal, including every deterministic refusal and every readiness gate a redelivery could only
# re-answer the same way. Writing a disposition for these is what made five of 2026-09-02's six
# Signals single-delivery deaths (#510 B); `market_subscription_pending` is the same shape for the
# quote stream an admission just opened, and it stops being retryable after `QUOTE_WARMUP_NS`.
RETRYABLE_ENTRY_REASONS: Final[frozenset[str]] = frozenset(
    {
        "account_stale",
        "market_stale",
        "market_subscription_pending",
        "oi_runtime_account_missing",
        "oi_runtime_account_balance_missing",
    }
)


class AuditBackpressure(RuntimeError):
    pass


class RuntimeObservationWriter:
    """The sole native-event/disposition to ExecutionObservationV1 translator."""

    def __init__(
        self,
        *,
        audit: AuditSink,
        signals: ExecutionSignalClient,
        state: RuntimeExecutionState,
        timestamp_ns: Callable[[], int],
    ) -> None:
        self._audit = audit
        self._factory = audit.factory
        self._signals = signals
        self._state = state
        self._timestamp_ns = timestamp_ns

    @staticmethod
    def correlation(state: ExecutionState) -> dict[str, str]:
        if state.entry.command is not None:
            return {"command_id": state.entry.command.command_id}
        return {"signal_id": state.entry.entry_id}

    @staticmethod
    def event_ns(event: Any) -> int:
        return int(getattr(event, "ts_event", getattr(event, "ts_init", 0)))

    def dispose_command(self, command: OperatorIntentV1, disposition: str, reason: str) -> None:
        if command.command_id in self._state.disposed_command_ids:
            return
        now_ns = self._timestamp_ns()
        value = self._factory.create(
            normalized_kind="control_disposition",
            command_id=command.command_id,
            occurred_at_ns=now_ns,
            observed_at_ns=now_ns,
            summary={"action": command.action, "disposition": disposition, "reason": reason},
            payload={
                "command_id": command.command_id,
                "action": command.action,
                "disposition": disposition,
                "reason": reason,
            },
            event_identity="final",
        )
        if not self._audit.offer(value):
            self._signals.retry_command(command)
            raise AuditBackpressure("oi_runtime_audit_backpressure")
        self._state.disposed_command_ids.add(command.command_id)

    def dispose_signal(self, signal: TradeSignalV1, reason: str) -> None:
        if signal.signal_id in self._state.disposed_signal_ids:
            return
        now_ns = self._timestamp_ns()
        value = self._factory.create(
            normalized_kind="signal_disposition",
            signal_id=signal.signal_id,
            occurred_at_ns=now_ns,
            observed_at_ns=now_ns,
            summary={"disposition": reason},
            payload={"signal_id": signal.signal_id, "disposition": reason},
            event_identity="final",
        )
        if not self._audit.offer(value):
            self._signals.retry(signal)
            raise AuditBackpressure("oi_runtime_audit_backpressure")
        self._state.disposed_signal_ids.add(signal.signal_id)

    def dispose_entry(self, request: RuntimeEntryRequest, reason: str) -> None:
        if reason in RETRYABLE_ENTRY_REASONS:
            self._release_entry(request)
            return
        if request.signal is not None:
            self.dispose_signal(request.signal, reason)
            return
        command = request.command
        if command is None:
            raise RuntimeError("oi_runtime_entry_source_invalid")
        disposition = (
            "accepted" if reason in {"accepted", "replayed_query_first", "unknown_query_first"} else "rejected"
        )
        self.dispose_command(command, disposition, reason)

    def _release_entry(self, request: RuntimeEntryRequest) -> None:
        """Drop the in-process claim without a durable verdict, so the next poll redelivers it.

        Never `disposed_signal_ids` / `disposed_command_ids`: the entry path checks those before doing
        any work, and a retryable refusal has to be reconsidered.
        """

        if request.signal is not None:
            self._signals.release(request.signal.signal_id)
            return
        command = request.command
        if command is None:
            raise RuntimeError("oi_runtime_entry_source_invalid")
        self._signals.release_command(command.command_id)

    def order(self, state: ExecutionState, order: Any, leg: str, status: str) -> None:
        occurred_at_ns = int(order.ts_init)
        self._audit.offer(
            self._factory.create(
                normalized_kind="protection" if leg == "protection" else "order",
                **self.correlation(state),
                occurred_at_ns=occurred_at_ns,
                observed_at_ns=occurred_at_ns,
                native_identity_references=(order.client_order_id.value,),
                summary={"leg": leg, "status": status},
                payload={"client_order_id": order.client_order_id.value, "leg": leg, "status": status},
                event_identity=status,
            )
        )

    def native_order_event(self, state: ExecutionState, leg: str, status: str, event: Any) -> None:
        now_ns = self.event_ns(event)
        references = [event.client_order_id.value]
        venue_order_id = getattr(event, "venue_order_id", None)
        if venue_order_id is not None:
            references.append(venue_order_id.value)
        self._audit.offer(
            self._factory.create(
                normalized_kind="order" if leg in {"entry", "exit"} else "protection",
                **self.correlation(state),
                occurred_at_ns=now_ns,
                observed_at_ns=max(now_ns, self._timestamp_ns()),
                native_identity_references=references,
                summary={"leg": leg, "status": status},
                payload={
                    "leg": leg,
                    "status": status,
                    "client_order_id": event.client_order_id.value,
                },
                event_identity=f"{status}:{event.client_order_id.value}",
            )
        )

    def rejected_order_event(self, state: ExecutionState, leg: str, status: str, event: Any) -> None:
        now_ns = self.event_ns(event)
        self._audit.offer(
            self._factory.create(
                normalized_kind="order" if leg in {"entry", "exit"} else "protection",
                **self.correlation(state),
                occurred_at_ns=now_ns,
                observed_at_ns=now_ns,
                native_identity_references=(event.client_order_id.value,),
                summary={"leg": leg, "status": status},
                payload={"client_order_id": event.client_order_id.value, "status": status},
                event_identity=status,
            )
        )

    def fill(self, state: ExecutionState, leg: str, event: Any) -> None:
        now_ns = self.event_ns(event)
        references = [event.client_order_id.value]
        for name in ("venue_order_id", "trade_id", "position_id"):
            value = getattr(event, name, None)
            if value is not None:
                references.append(value.value)
        self._audit.offer(
            self._factory.create(
                normalized_kind="fill",
                **self.correlation(state),
                occurred_at_ns=now_ns,
                observed_at_ns=max(now_ns, self._timestamp_ns()),
                native_identity_references=references,
                summary={
                    "leg": leg,
                    "last_quantity": str(event.last_qty),
                    "last_price": str(event.last_px),
                },
                payload={
                    "leg": leg,
                    "client_order_id": event.client_order_id.value,
                    "last_quantity": str(event.last_qty),
                    "last_price": str(event.last_px),
                },
                event_identity=f"fill:{getattr(event, 'trade_id', event.client_order_id)}",
            )
        )

    def position(self, state: ExecutionState, status: str, occurred_at_ns: int) -> None:
        references = () if state.position_id is None else (state.position_id.value,)
        self._audit.offer(
            self._factory.create(
                normalized_kind="position",
                **self.correlation(state),
                occurred_at_ns=occurred_at_ns,
                observed_at_ns=max(occurred_at_ns, self._timestamp_ns()),
                native_identity_references=references,
                summary={"status": status, "quantity": str(state.position_quantity)},
                payload={"status": status, "quantity": str(state.position_quantity)},
                event_identity=f"{status}:{state.position_quantity}:{occurred_at_ns}",
            )
        )

    def protection_submitted(
        self,
        state: ExecutionState,
        *,
        client_order_id: Any,
        quantity: Decimal,
        trigger_price: Decimal,
        event_identity: str,
    ) -> None:
        now_ns = self._timestamp_ns()
        self._audit.offer(
            self._factory.create(
                normalized_kind="protection",
                **self.correlation(state),
                occurred_at_ns=now_ns,
                observed_at_ns=now_ns,
                native_identity_references=(client_order_id.value,),
                summary={"explicit_quantity": str(quantity), "reduce_only": True},
                payload={
                    "client_order_id": client_order_id.value,
                    "quantity": str(quantity),
                    "trigger_price": str(trigger_price),
                    "reduce_only": True,
                },
                event_identity=event_identity,
            )
        )

    def unclaimed_flatten_order(self, *, command_id: str, position: Any, order: Any) -> None:
        """Record the reduce-only close of exposure no durable entry identity claims."""

        occurred_at_ns = int(order.ts_init)
        self._audit.offer(
            self._factory.create(
                normalized_kind="order",
                command_id=command_id,
                occurred_at_ns=occurred_at_ns,
                observed_at_ns=max(occurred_at_ns, self._timestamp_ns()),
                native_identity_references=(order.client_order_id.value, position.id.value),
                summary={
                    "leg": "unclaimed_flatten",
                    "status": "submitted",
                    "instrument_id": position.instrument_id.value,
                    "side": "long" if position.is_long else "short",
                    "quantity": str(order.quantity),
                },
                payload={
                    "client_order_id": order.client_order_id.value,
                    "position_id": position.id.value,
                    "instrument_id": position.instrument_id.value,
                    "leg": "unclaimed_flatten",
                    "status": "submitted",
                    "quantity": str(order.quantity),
                },
                event_identity=f"unclaimed_flatten:{order.client_order_id.value}",
            )
        )

    def flatten_accepted(self, command_id: str) -> bool:
        now_ns = self._timestamp_ns()
        return self._audit.offer(
            self._factory.create(
                normalized_kind="readiness",
                command_id=command_id,
                occurred_at_ns=now_ns,
                observed_at_ns=now_ns,
                summary={"action": "flatten", "control_stage": "runtime_accepted"},
                payload={"command_id": command_id, "action": "flatten", "stage": "runtime_accepted"},
                event_identity="runtime_accepted",
            )
        )

    def flatten_completed(self, command: OperatorIntentV1, snapshot: RuntimeReconciliationSnapshot) -> bool:
        return self._audit.offer(
            self._factory.create(
                normalized_kind="control_disposition",
                command_id=command.command_id,
                occurred_at_ns=min(snapshot.account_observed_at_ns, snapshot.reconciliation_observed_at_ns),
                observed_at_ns=max(snapshot.account_observed_at_ns, snapshot.reconciliation_observed_at_ns),
                summary={"disposition": "completed", "reason": "binance_account_flat"},
                payload={
                    "command_id": command.command_id,
                    "disposition": "completed",
                    "reason": "binance_account_flat",
                    "account_observed_at_ns": snapshot.account_observed_at_ns,
                },
                event_identity="final",
            )
        )


__all__ = ["RETRYABLE_ENTRY_REASONS", "AuditBackpressure", "RuntimeObservationWriter"]
