"""Concrete position and reduce-only stop lifecycle owner."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any, Literal

from nautilus_trader.model.enums import OrderSide, OrderType, PositionSide, TriggerType
from nautilus_trader.model.identifiers import ClientId, ClientOrderId

from .config import OiRuntimeProfile
from .exit import ExitCoordinator
from .observations import RuntimeObservationWriter
from .quotes import QuoteStreamCoordinator
from .risk import decimal_value
from .state import (
    ExecutionState,
    PrivateReconciliationReason,
    RuntimeExecutionState,
    RuntimeReadiness,
    deterministic_client_order_id,
    protection_leg,
)


def _closed_decimal(value: Any) -> Decimal | None:
    """A `PositionClosed` price or money that Nautilus may not have; never a fabricated zero."""

    return None if value is None else decimal_value(value)


class ProtectionCoordinator:
    """Own stop creation, replacement, retirement, and failure convergence."""

    def __init__(
        self,
        *,
        engine: Any,
        profile: OiRuntimeProfile,
        state: RuntimeExecutionState,
        readiness: RuntimeReadiness,
        observations: RuntimeObservationWriter,
        exits: ExitCoordinator,
        quotes: QuoteStreamCoordinator,
        request_reconciliation: Callable[[PrivateReconciliationReason], None],
    ) -> None:
        self._engine = engine
        self._profile = profile
        self._state = state
        self._readiness = readiness
        self._observations = observations
        self._exits = exits
        self._quotes = quotes
        self._request_reconciliation = request_reconciliation

    def status(
        self,
        *,
        positions_count: int,
        unexpected_exposure: bool,
    ) -> Literal["not_applicable", "protected", "pending", "unprotected", "unknown"]:
        if positions_count < 0:
            raise ValueError("oi_runtime_positions_count_invalid")
        if positions_count == 0:
            return "not_applicable"
        owned = tuple(state for state in self._state.executions.values() if state.position_id is not None)
        if unexpected_exposure or len(owned) != positions_count:
            return "unknown"
        protected = tuple(
            state.stop_order is not None
            and state.stop_order.is_open
            and state.stop_quantity == state.position_quantity
            and state.position_quantity > 0
            for state in owned
        )
        if all(protected):
            return "protected"
        if any(state.pending_stop_order is not None for state in owned):
            return "pending"
        return "unprotected"

    def position_opened(self, event: Any) -> None:
        entry_id = self._state.entry_for_opening_order(event.opening_order_id)
        if entry_id is None:
            self._readiness.halt_for_unexpected_exposure()
            return
        state = self._state.executions[entry_id]
        if event.instrument_id != state.route.instrument_id or event.account_id != self._profile.account_id:
            self._readiness.halt_for_unexpected_exposure()
            return
        expected_side = PositionSide.LONG if state.entry.direction == "long" else PositionSide.SHORT
        if event.strategy_id != self._engine.id or event.side != expected_side:
            self._readiness.halt_for_unexpected_exposure()
            return
        state.position_id = event.position_id
        state.position_quantity = abs(Decimal(str(event.quantity)))
        state.avg_entry_price = Decimal(str(event.avg_px_open))
        self._state.positions[event.position_id] = entry_id
        self.request_stop(state, state.position_quantity, state.avg_entry_price)
        self._observations.position(state, "opened", int(event.ts_opened))

    def position_changed(self, event: Any) -> None:
        entry_id = self._state.positions.get(event.position_id)
        if entry_id is None:
            self._readiness.halt_for_unexpected_exposure()
            return
        state = self._state.executions[entry_id]
        quantity = abs(Decimal(str(event.quantity)))
        avg_price = Decimal(str(event.avg_px_open))
        state.position_quantity = quantity
        state.avg_entry_price = avg_price
        self._observations.position(state, "changed", self._observations.event_ns(event))
        self.request_stop(state, quantity, avg_price)

    def position_closed(self, event: Any) -> None:
        entry_id = self._state.positions.pop(event.position_id, None)
        if entry_id is None:
            self._close_unclaimed_position(event)
            self._readiness.halt_for_unexpected_exposure()
            return
        state = self._state.executions[entry_id]
        closed_quantity = state.position_quantity
        state.position_quantity = Decimal(0)
        state.active = False
        if state.stop_order is not None and not state.stop_order.is_closed:
            self._engine.cancel_order(state.stop_order, client_id=ClientId("BINANCE"))
        if state.pending_stop_order is not None and not state.pending_stop_order.is_closed:
            self._engine.cancel_order(state.pending_stop_order, client_id=ClientId("BINANCE"))
        for retiring in state.retiring_stop_orders.values():
            if not retiring.is_closed:
                self._engine.cancel_order(retiring, client_id=ClientId("BINANCE"))
        state.exit_retry_required = False
        self._observations.position(
            state,
            "closed",
            int(event.ts_closed),
            quantity=closed_quantity,
            exit_price=_closed_decimal(event.avg_px_close),
            realized_pnl_usd=_closed_decimal(event.realized_pnl),
            # Only `ExitCoordinator.flatten` annotates a reason; anything else that takes this
            # position off the venue is the reduce-only stop this coordinator placed.
            exit_reason=state.exit_reason or "stop_filled",
        )
        state.exit_reason = None
        # Nothing on this instrument needs a mark any more, so the Runtime stops paying for its
        # quotes; the next admitted entry re-opens the stream (#510 E).
        self._quotes.release(state.route.instrument_id)
        if self._state.pending_flatten:
            self._request_reconciliation("flatten_pending")

    def _close_unclaimed_position(self, event: Any) -> None:
        """Record the close of exposure this Runtime flattened without owning it (#528 A)."""

        if event.position_id not in self._state.unclaimed_flatten_orders:
            return
        command_id = min(self._state.pending_flatten, default=None)
        if command_id is None:
            return
        self._observations.unclaimed_position_closed(
            command_id=command_id,
            position_id=event.position_id,
            quantity=abs(Decimal(str(event.peak_qty))),
            occurred_at_ns=int(event.ts_closed),
            exit_price=_closed_decimal(event.avg_px_close),
            realized_pnl_usd=_closed_decimal(event.realized_pnl),
        )

    def request_stop(self, state: ExecutionState, quantity: Decimal, avg_price: Decimal) -> None:
        if quantity <= 0:
            return
        state.desired_stop = (quantity, avg_price)
        if state.pending_stop_order is not None:
            return
        if state.stop_order is not None and state.stop_quantity == quantity and state.stop_avg_price == avg_price:
            return
        self._submit_stop(state, quantity, avg_price)

    def _submit_stop(self, state: ExecutionState, quantity: Decimal, avg_price: Decimal) -> None:
        instrument = self._engine.cache.instrument(state.route.instrument_id)
        if instrument is None:
            self._readiness.halt_for_unexpected_exposure()
            return
        desired = self.desired_trigger_price(state, avg_price)
        if desired is None:
            self._readiness.halt_for_unexpected_exposure()
            return
        side = OrderSide.SELL if state.entry.direction == "long" else OrderSide.BUY
        quantity_value = instrument.make_qty(quantity)
        trigger_price = instrument.make_price(desired)
        state.protection_generation += 1
        leg = protection_leg(state.protection_generation)
        client_order_id = deterministic_client_order_id(
            namespace=self._profile.client_order_namespace,
            entry_id=state.entry.entry_id,
            leg=leg,
        )
        existing = self._engine.cache.order(client_order_id)
        if existing is not None:
            self._replay_stop(
                state=state,
                existing=existing,
                client_order_id=client_order_id,
                quantity=quantity_value.as_decimal(),
                avg_price=avg_price,
                trigger_price=trigger_price.as_decimal(),
            )
            return
        order = self._engine.order_factory.stop_market(
            instrument_id=state.route.instrument_id,
            order_side=side,
            quantity=quantity_value,
            trigger_price=trigger_price,
            trigger_type=TriggerType.LAST_PRICE,
            reduce_only=True,
            client_order_id=client_order_id,
        )
        state.pending_stop_order = order
        state.pending_stop_quantity = quantity_value.as_decimal()
        state.pending_stop_avg_price = avg_price
        self._state.orders[client_order_id] = (state.entry.entry_id, "protection")
        try:
            self._engine.submit_order(order, position_id=state.position_id, client_id=ClientId("BINANCE"))
        except Exception:
            self._request_reconciliation("protection_ambiguity")
            self._engine.query_order(order, client_id=ClientId("BINANCE"))
            if state.position_id is not None:
                self._exits.flatten(state.position_id)
        self._observations.protection_submitted(
            state,
            client_order_id=client_order_id,
            quantity=quantity_value.as_decimal(),
            trigger_price=trigger_price.as_decimal(),
            event_identity=leg,
        )

    def _replay_stop(
        self,
        *,
        state: ExecutionState,
        existing: Any,
        client_order_id: ClientOrderId,
        quantity: Decimal,
        avg_price: Decimal,
        trigger_price: Decimal,
    ) -> None:
        self._state.orders[client_order_id] = (state.entry.entry_id, "protection")
        if not self.stop_valid(
            state=state,
            protection=existing,
            quantity=quantity,
            expected_trigger=trigger_price,
            expected_leg=protection_leg(state.protection_generation),
            require_open=False,
        ):
            self._request_reconciliation("protection_ambiguity")
            if not existing.is_closed:
                self._engine.query_order(existing, client_id=ClientId("BINANCE"))
            self._observations.order(state, existing, "protection", "replayed_invalid_flatten")
            if state.position_id is not None:
                self._exits.flatten(state.position_id)
            return
        state.pending_stop_order = existing
        state.pending_stop_quantity = quantity
        state.pending_stop_avg_price = avg_price
        self._request_reconciliation("protection_ambiguity")
        self._engine.query_order(existing, client_id=ClientId("BINANCE"))
        self._observations.order(state, existing, "protection", "replayed_query_first")
        if existing.is_open:
            self.accept_pending(state, client_order_id)
        elif state.stop_order is None and state.position_id is not None:
            self._exits.flatten(state.position_id)

    def accept_pending(self, state: ExecutionState, client_order_id: ClientOrderId) -> None:
        pending = state.pending_stop_order
        if pending is None or pending.client_order_id != client_order_id:
            return
        previous = state.stop_order
        state.stop_order = pending
        state.stop_quantity = state.pending_stop_quantity
        state.stop_avg_price = state.pending_stop_avg_price
        state.pending_stop_order = None
        state.pending_stop_quantity = Decimal(0)
        state.pending_stop_avg_price = None
        if previous is not None and previous.client_order_id != client_order_id and not previous.is_closed:
            state.retiring_stop_orders[previous.client_order_id] = previous
            self._engine.cancel_order(previous, client_id=ClientId("BINANCE"))
        desired = state.desired_stop
        if desired is not None and (desired[0] != state.stop_quantity or desired[1] != state.stop_avg_price):
            self._submit_stop(state, *desired)

    def known_terminal(self, state: ExecutionState, client_order_id: ClientOrderId) -> None:
        if state.retiring_stop_orders.pop(client_order_id, None) is not None:
            return
        if state.pending_stop_order is not None and client_order_id == state.pending_stop_order.client_order_id:
            state.pending_stop_order = None
            state.pending_stop_quantity = Decimal(0)
            state.pending_stop_avg_price = None
        elif state.stop_order is not None and client_order_id == state.stop_order.client_order_id:
            state.stop_order = None
            state.stop_quantity = Decimal(0)
            state.stop_avg_price = None
        else:
            return
        if state.position_quantity > 0 and state.position_id is not None:
            self._request_reconciliation("protection_ambiguity")
            self._exits.flatten(state.position_id)

    def desired_trigger_price(self, state: ExecutionState, avg_price: Decimal) -> Decimal | None:
        """The one stop trigger this execution's route, direction and entry price imply."""

        instrument = self._engine.cache.instrument(state.route.instrument_id)
        if instrument is None:
            return None
        distance = Decimal(state.route.stop_distance_bps) / Decimal(10_000)
        factor = Decimal(1) - distance if state.entry.direction == "long" else Decimal(1) + distance
        return instrument.make_price(avg_price * factor).as_decimal()

    def stop_valid(
        self,
        *,
        state: ExecutionState,
        protection: Any,
        quantity: Decimal,
        expected_trigger: Decimal | None,
        expected_leg: str | None,
        require_open: bool,
    ) -> bool:
        """The one shape a reduce-only stop must have to count as this execution's protection.

        There were two of these: a recovery check that proved the deterministic client order id and
        the Cache position binding, and a steady check that proved the trigger against the current
        average entry price. Neither was a superset of the other, so the same order could satisfy one
        and be refused by the other, and a tightening applied to one silently left the other alone
        (#510 E). `require_open` is the one real difference and it is a pair: a stop that must be live
        at the venue has to be open in Cache and carry this account, while a stop that has been
        submitted and not yet accepted carries no account id at all.
        """

        instrument = self._engine.cache.instrument(state.route.instrument_id)
        if instrument is None or protection is None or quantity <= 0 or state.position_id is None:
            return False
        if expected_leg is not None:
            expected_id = deterministic_client_order_id(
                namespace=self._profile.client_order_namespace,
                entry_id=state.entry.entry_id,
                leg=expected_leg,
            )
            if protection.client_order_id != expected_id:
                return False
        if require_open:
            current = self._engine.cache.order(protection.client_order_id)
            if current is None or not current.is_open:
                return False
        # A stop reclaimed from the Binance open-order report is added to Cache without any position
        # index (`LiveExecutionEngine._reconcile_order_report`), so an unbound stop is normal after a
        # restart. A stop bound to some other position is not this execution's.
        bound_position = self._engine.cache.position_for_order(protection.client_order_id)
        expected_side = OrderSide.SELL if state.entry.direction == "long" else OrderSide.BUY
        return bool(
            not protection.is_closed
            and protection.strategy_id == self._engine.id
            and (
                protection.account_id == self._profile.account_id
                if require_open
                else protection.account_id in {None, self._profile.account_id}
            )
            and (bound_position is None or bound_position.id == state.position_id)
            and protection.instrument_id == state.route.instrument_id
            and protection.side == expected_side
            and protection.order_type == OrderType.STOP_MARKET
            and protection.trigger_type == TriggerType.LAST_PRICE
            and (
                expected_trigger is None
                or (expected_trigger > 0 and protection.trigger_price == instrument.make_price(expected_trigger))
            )
            and protection.is_reduce_only
            and protection.quantity.as_decimal() == quantity
        )


__all__ = ["ProtectionCoordinator"]
