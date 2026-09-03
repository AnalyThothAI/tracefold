"""Concrete flatten, reduce-only exit, retry, and completion owner."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import ClientId, ClientOrderId, PositionId

from tracefold.trading import OperatorIntentV1

from .config import OiRuntimeProfile
from .observations import RuntimeObservationWriter
from .state import (
    ExecutionState,
    PrivateReconciliationReason,
    RuntimeExecutionState,
    RuntimeReconciliationSnapshot,
    deterministic_client_order_id,
    exit_leg,
    exit_order_valid,
)

# A reduce-only market close on an open position fills; a repeated rejection is a venue fact the
# operator has to see, not something to retry on the 100 ms callback pump.
_MAX_UNCLAIMED_FLATTEN_ATTEMPTS = 3


class ExitCoordinator:
    """Own every risk-reducing exit from intent through private-flat completion."""

    def __init__(
        self,
        *,
        engine: Any,
        profile: OiRuntimeProfile,
        state: RuntimeExecutionState,
        observations: RuntimeObservationWriter,
        request_reconciliation: Callable[[PrivateReconciliationReason], None],
        halt_for_unexpected_exposure: Callable[[], None],
    ) -> None:
        self._engine = engine
        self._profile = profile
        self._state = state
        self._observations = observations
        self._request_reconciliation = request_reconciliation
        self._halt_for_unexpected_exposure = halt_for_unexpected_exposure

    def start_flatten(self, command: OperatorIntentV1) -> None:
        self._state.entries_paused = True
        self._state.pending_flatten[command.command_id] = command
        self._request_reconciliation("flatten_pending")

    def advance_pending(self) -> None:
        """Converge the whole account slot; ownership only constrains opening new exposure."""

        if not self._state.pending_flatten:
            return
        for command_id in tuple(self._state.pending_flatten):
            if command_id not in self._state.flatten_accept_observed and self._observations.flatten_accepted(
                command_id
            ):
                self._state.flatten_accept_observed.add(command_id)
        for execution in self._state.executions.values():
            if execution.position_id is not None and execution.position_quantity > 0:
                self.flatten(execution.position_id)
            if execution.entry_order is not None and not execution.entry_order.is_closed:
                self._engine.cancel_order(execution.entry_order, client_id=ClientId("BINANCE"))
        for position in self._engine.cache.positions_open(account_id=self._profile.account_id):
            if position.id not in self._state.positions:
                self._close_unclaimed(position)
        working = self._working_flatten_order_ids()
        for order in (
            *self._engine.cache.orders_open(account_id=self._profile.account_id),
            *self._engine.cache.orders_inflight(account_id=self._profile.account_id),
        ):
            if order.client_order_id in working or order.is_closed or order.is_pending_cancel:
                continue
            self._engine.cancel_order(order, client_id=ClientId("BINANCE"))

    def _working_flatten_order_ids(self) -> frozenset[ClientOrderId]:
        exits = {client_order_id for client_order_id, identity in self._state.orders.items() if identity[1] == "exit"}
        exits.update(order.client_order_id for order in self._state.unclaimed_flatten_orders.values())
        return frozenset(exits)

    def _close_unclaimed(self, position: Any) -> None:
        """Reduce-only close for exposure no durable entry identity claims."""

        existing = self._state.unclaimed_flatten_orders.get(position.id)
        if existing is not None and not existing.is_closed:
            return
        attempts = self._state.unclaimed_flatten_attempts.get(position.id, 0)
        if attempts >= _MAX_UNCLAIMED_FLATTEN_ATTEMPTS:
            return
        instrument = self._engine.cache.instrument(position.instrument_id)
        if instrument is None:
            raise RuntimeError("oi_runtime_instrument_missing")
        order = self._engine.order_factory.market(
            instrument_id=position.instrument_id,
            order_side=OrderSide.SELL if position.is_long else OrderSide.BUY,
            quantity=instrument.make_qty(abs(Decimal(str(position.quantity)))),
            reduce_only=True,
        )
        self._state.unclaimed_flatten_orders[position.id] = order
        self._state.unclaimed_flatten_attempts[position.id] = attempts + 1
        try:
            self._engine.submit_order(order, position_id=position.id, client_id=ClientId("BINANCE"))
        except Exception:
            self._request_reconciliation("unknown_outcome")
            self._engine.query_order(order, client_id=ClientId("BINANCE"))
        command_id = min(self._state.pending_flatten, default=None)
        if command_id is not None:
            self._observations.unclaimed_flatten_order(command_id=command_id, position=position, order=order)

    def complete_from_reconciliation(self, snapshot: RuntimeReconciliationSnapshot) -> None:
        if not self._state.pending_flatten or snapshot.executions:
            return
        if self._engine.cache.positions_open(account_id=self._profile.account_id):
            return
        if self._engine.cache.orders_open(account_id=self._profile.account_id) or self._engine.cache.orders_inflight(
            account_id=self._profile.account_id
        ):
            return
        for command_id, command in tuple(self._state.pending_flatten.items()):
            fresh_at_ns = min(snapshot.account_observed_at_ns, snapshot.reconciliation_observed_at_ns)
            if fresh_at_ns <= command.requested_at_ns:
                continue
            if not self._observations.flatten_completed(command, snapshot):
                continue
            self._state.pending_flatten.pop(command_id)
            self._state.flatten_accept_observed.discard(command_id)
            self._state.disposed_command_ids.add(command_id)
        if not self._state.pending_flatten:
            self._state.unclaimed_flatten_orders.clear()
            self._state.unclaimed_flatten_attempts.clear()

    def retry_failed(self) -> None:
        for state in self._state.executions.values():
            if state.exit_retry_required and state.position_id is not None and state.position_quantity > 0:
                self.flatten(state.position_id)

    def flatten(self, position_id: PositionId) -> None:
        """Risk-reducing exit remains available when audit or singleton entry gates fail."""

        entry_id = self._state.positions.get(position_id)
        if entry_id is None:
            raise ValueError("oi_runtime_position_not_owned")
        state = self._state.executions[entry_id]
        if state.position_quantity <= 0:
            return
        # The one place a reduce-only exit is asked for on an owned position, so the one place that
        # can say why the `PositionClosed` this produces is not the protective stop (#528 A).
        state.exit_reason = "flatten"
        instrument = self._engine.cache.instrument(state.route.instrument_id)
        if instrument is None:
            raise RuntimeError("oi_runtime_instrument_missing")
        side = OrderSide.SELL if state.entry.direction == "long" else OrderSide.BUY
        client_order_id = deterministic_client_order_id(
            namespace=self._profile.client_order_namespace,
            entry_id=entry_id,
            leg=exit_leg(state.exit_generation),
        )
        existing = state.exit_order
        if existing is not None:
            if existing.is_closed:
                state.exit_order = None
                state.exit_generation += 1
                state.exit_retry_required = True
                return
            state.exit_retry_required = False
            self._state.orders[client_order_id] = (entry_id, "exit")
            self._engine.query_order(existing, client_id=ClientId("BINANCE"))
            self._observations.order(state, existing, "exit", "replayed_query_first")
            return
        cached = self._engine.cache.order(client_order_id)
        if cached is not None:
            position = self._engine.cache.position(position_id)
            if position is None or not exit_order_valid(
                profile=self._profile,
                strategy_id=self._engine.id,
                cache=self._engine.cache,
                state=state,
                exit_order=cached,
                position=position,
            ):
                self._halt_for_unexpected_exposure()
                return
            if cached.is_closed:
                state.exit_generation += 1
                state.exit_retry_required = True
                return
            state.exit_order = cached
            state.exit_retry_required = False
            self._state.orders[client_order_id] = (entry_id, "exit")
            self._engine.query_order(cached, client_id=ClientId("BINANCE"))
            self._observations.order(state, cached, "exit", "replayed_query_first")
            return
        order = self._engine.order_factory.market(
            instrument_id=state.route.instrument_id,
            order_side=side,
            quantity=instrument.make_qty(state.position_quantity),
            reduce_only=True,
            client_order_id=client_order_id,
        )
        state.exit_order = order
        state.exit_retry_required = False
        self._state.orders[client_order_id] = (entry_id, "exit")
        try:
            self._engine.submit_order(order, position_id=position_id, client_id=ClientId("BINANCE"))
        except Exception:
            self._request_reconciliation("unknown_outcome")
            self._engine.query_order(order, client_id=ClientId("BINANCE"))
        self._observations.order(state, order, "exit", "submitted_or_unknown")

    def known_terminal(self, state: ExecutionState, client_order_id: ClientOrderId) -> None:
        if state.exit_order is None or state.exit_order.client_order_id != client_order_id:
            return
        state.exit_order = None
        if state.exit_retry_budget > 0:
            state.exit_generation += 1
            state.exit_retry_budget -= 1
            state.exit_retry_required = state.position_quantity > 0
        else:
            state.exit_retry_required = False
        self._halt_for_unexpected_exposure()
        self._request_reconciliation("unknown_outcome")


__all__ = ["ExitCoordinator"]
