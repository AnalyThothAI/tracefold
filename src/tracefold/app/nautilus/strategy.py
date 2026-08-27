"""Public-v1 Nautilus strategy for Tracefold's single Binance Demo intent."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal
from queue import Empty, Full
from typing import Any, cast

from nautilus_trader.adapters.binance import BINANCE
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.enums import OrderSide, OrderStatus, OrderType, PositionSide, TriggerType
from nautilus_trader.model.identifiers import ClientId, ClientOrderId, InstrumentId, PositionId
from nautilus_trader.trading.strategy import Strategy

from tracefold.trading import IntentOutcome, IntentReasonCode, TradeIntent, deterministic_client_order_id

from .messages import (
    AdoptIntent,
    CloseSubmitted,
    EntryFenceGranted,
    EntryFenceRequested,
    EntryFilled,
    EntryRejected,
    IntentRefused,
    OrderLeg,
    OrderOutcomeUnknown,
    PositionClosedObserved,
    PositionFlatConfirmed,
    PositionQuantityChanged,
    ReadinessChanged,
    StopAccepted,
    StopSubmitted,
    StrategyCommand,
    StrategyEvent,
    StrategyQueues,
    VenueFlatConfirmed,
    VenueFlatProofRequested,
    VenueFlatUnproven,
)

SOLUSDT_PERP = InstrumentId.from_str("SOLUSDT-PERP.BINANCE")
_FLAT_PROOF_RETRY_MS = 5_000


def tracefold_strategy_config() -> StrategyConfig:
    """Return the one supported strategy ownership configuration."""

    return StrategyConfig(
        strategy_id="TRACEFOLD",
        order_id_tag="001",
        oms_type="NETTING",
        external_order_claims=[SOLUSDT_PERP],
    )


class TracefoldNautilusStrategy(Strategy):
    """Own one exact instrument and exchange typed messages with the DB thread."""

    def __init__(
        self,
        *,
        engine_identity: str,
        queues: StrategyQueues,
        request_venue_flat: Callable[[VenueFlatProofRequested], None],
        config: StrategyConfig | None = None,
    ) -> None:
        selected = config or tracefold_strategy_config()
        if selected.oms_type != "NETTING" or selected.external_order_claims != [SOLUSDT_PERP]:
            raise ValueError("nautilus_external_order_claims_invalid")
        if not engine_identity.strip():
            raise ValueError("nautilus_engine_identity_invalid")
        super().__init__(selected)
        self._engine_identity = engine_identity
        self._queues = queues
        self._request_venue_flat = request_venue_flat
        self._pending_events: deque[StrategyEvent] = deque(maxlen=queues.events.maxsize)
        self._projection_overflow = False
        self._active_intent: TradeIntent | None = None
        self._active_outcome: IntentOutcome | None = None
        self._pending_fence_quantity: Decimal | None = None
        self._orders: dict[str, tuple[str, OrderLeg]] = {}
        self._position_id: PositionId | None = None
        self._position_quantity: Decimal | None = None
        self._opened_at_ms: int | None = None
        self._recovered_past_close_deadline = False
        self._stop_order: Any = None
        self._stop_trigger_price: Decimal | None = None
        self._stop_generation: int | None = None
        self._stop_cancel_pending = False
        self._pending_stop_quantity: Decimal | None = None
        self._last_readiness: ReadinessChanged | None = None
        self._failed_stop_ids: set[str] = set()
        self._terminal_stop_ids: set[str] = set()
        self._close_order: Any = None
        self._terminal_close_ids: set[str] = set()
        self._local_close_observation: PositionClosedObserved | None = None
        self._stop_id_at_close: str | None = None
        self._pending_flat_confirmation: VenueFlatConfirmed | None = None
        self._cancel_close_for_flat = False
        self._cancel_stop_for_flat = False
        self._flat_unproven_reported = False
        self._flat_retry_at_ms: int | None = None

    def on_start(self) -> None:
        """Publish post-reconciliation readiness and start the command pump."""

        self.subscribe_quote_ticks(SOLUSDT_PERP)
        self.clock.set_timer(
            name=f"{self.id}:COMMANDS",
            interval=timedelta(milliseconds=100),
            callback=self.on_timer,
            fire_immediately=True,
        )
        self._publish_readiness()

    def on_stop(self) -> None:
        timer_name = f"{self.id}:COMMANDS"
        if timer_name in self.clock.timer_names:
            self.clock.cancel_timer(timer_name)
        self.unsubscribe_quote_ticks(SOLUSDT_PERP)

    def _readiness(self) -> ReadinessChanged:
        instrument = self.cache.instrument(SOLUSDT_PERP)
        account = self.portfolio.account(venue=SOLUSDT_PERP.venue)
        expected_position_id = None if self._position_id is None else self._position_id.value
        unexpected_position = any(
            position.instrument_id != SOLUSDT_PERP or position.id.value != expected_position_id
            for position in self.cache.positions_open()
        )
        unowned_order = any(order.client_order_id.value not in self._orders for order in self.cache.orders_open())
        unexpected = unexpected_position or unowned_order
        if self._projection_overflow:
            return ReadinessChanged(False, "projection_overflow", unexpected)
        if instrument is None or account is None:
            return ReadinessChanged(False, "account_or_instrument_missing", unexpected)
        leverage = account.leverage(SOLUSDT_PERP) if hasattr(account, "leverage") else None
        if leverage != Decimal(1):
            return ReadinessChanged(False, "leverage_not_one", unexpected)
        if unexpected:
            return ReadinessChanged(False, "external_exposure", True)
        return ReadinessChanged(True, "ready", False)

    def _publish_readiness(self) -> None:
        readiness = self._readiness()
        if readiness == self._last_readiness:
            return
        if self._emit(readiness):
            self._last_readiness = readiness

    def on_timer(self, _event: object) -> None:
        """Drain only the bounded command batch on the TradingNode thread."""

        self._flush_events()
        for _ in range(self._queues.commands.maxsize):
            try:
                command = self._queues.commands.get_nowait()
            except Empty:
                break
            self._handle_command(command)
        self._enforce_max_holding()
        self._retry_venue_flat_proof()
        if self._last_readiness is not None:
            self._publish_readiness()
        self._flush_events()

    def _handle_command(self, command: StrategyCommand) -> None:
        if isinstance(command, AdoptIntent):
            self._adopt_intent(command)
        elif isinstance(command, EntryFenceGranted):
            self._submit_fenced_entry(command)
        elif isinstance(command, VenueFlatConfirmed):
            self._confirm_venue_flat(command)
        elif isinstance(command, VenueFlatUnproven):
            self._venue_flat_unproven(command)

    def _adopt_intent(self, command: AdoptIntent) -> None:
        intent = command.intent
        outcome = command.outcome
        if intent.instrument_id != SOLUSDT_PERP.value:
            raise ValueError("nautilus_intent_instrument_invalid")
        if outcome.intent_id != intent.intent_id:
            raise ValueError("nautilus_intent_outcome_mismatch")
        if self._active_intent is not None and self._active_intent.intent_id != intent.intent_id:
            raise ValueError("nautilus_second_active_intent")

        self._active_intent = intent
        self._active_outcome = outcome
        if outcome.entry_fenced_at_ms is not None:
            self._recover_fenced_intent(outcome)
            return

        quantity = self._entry_quantity(intent)
        if quantity is None:
            return
        self._pending_fence_quantity = quantity
        self._emit(
            EntryFenceRequested(
                intent_id=intent.intent_id,
                engine_identity=self._engine_identity,
                quantity=quantity,
            )
        )

    def _entry_quantity(self, intent: TradeIntent) -> Decimal | None:
        now_ms = int(self.clock.timestamp_ms())
        if now_ms >= intent.valid_until_ms:
            self._refuse_intent(intent, "intent_expired")
            return None

        readiness = self._readiness()
        if not readiness.ready:
            if readiness.unexpected_exposure:
                self._refuse_intent(intent, "external_exposure")
            else:
                self._refuse_intent(intent, "runtime_not_ready")
            return None

        instrument = self.cache.instrument(SOLUSDT_PERP)
        quote = self.cache.quote_tick(SOLUSDT_PERP)
        if instrument is None or quote is None:
            self._refuse_intent(intent, "runtime_not_ready")
            return None

        quote_ms = int(quote.ts_event) // 1_000_000
        if quote_ms < intent.created_at_ms or quote_ms > now_ms:
            self._refuse_intent(intent, "market_unacceptable")
            return None

        bid = quote.bid_price.as_decimal()
        ask = quote.ask_price.as_decimal()
        mid = (bid + ask) / 2
        spread_bps = (ask - bid) / mid * 10_000
        drift_bps = abs(ask - intent.reference_price) / intent.reference_price * 10_000
        if spread_bps > intent.max_spread_bps or drift_bps > intent.max_entry_drift_bps:
            self._refuse_intent(intent, "market_unacceptable")
            return None

        increment = instrument.size_increment.as_decimal()
        raw_quantity = intent.target_notional_usd / ask
        quantity = (raw_quantity / increment).to_integral_value(rounding=ROUND_DOWN) * increment
        minimum = instrument.min_quantity
        min_notional = instrument.min_notional
        notional = instrument.notional_value(instrument.make_qty(quantity), instrument.make_price(ask))
        if (
            quantity <= 0
            or (minimum is not None and quantity < minimum.as_decimal())
            or (min_notional is not None and notional < min_notional)
        ):
            self._refuse_intent(intent, "quantity_unexecutable")
            return None
        return cast(Decimal, quantity)

    def _refuse_intent(self, intent: TradeIntent, reason_code: IntentReasonCode) -> None:
        self._emit(IntentRefused(intent.intent_id, reason_code))
        self._clear_active_without_exposure(intent.intent_id)

    def _clear_active_without_exposure(self, intent_id: str) -> None:
        if (
            self._active_intent is None
            or self._active_intent.intent_id != intent_id
            or (self._position_quantity is not None and self._position_quantity > 0)
        ):
            return
        self._active_intent = None
        self._active_outcome = None
        self._pending_fence_quantity = None
        self._orders.clear()

    def _submit_fenced_entry(self, command: EntryFenceGranted) -> None:
        intent = self._active_intent
        outcome = command.outcome
        expected_id = None if intent is None else deterministic_client_order_id(intent.intent_id, "entry")
        if (
            intent is None
            or self._pending_fence_quantity != command.quantity
            or outcome.intent_id != intent.intent_id
            or outcome.entry_fenced_at_ms is None
            or outcome.entry_client_order_id != expected_id
            or outcome.execution_state != "IN_FLIGHT"
            or outcome.execution_phase != "ENTRY"
        ):
            raise ValueError("nautilus_entry_fence_grant_invalid")
        if expected_id in self._orders:
            return

        instrument = self.cache.instrument(SOLUSDT_PERP)
        if instrument is None:
            raise RuntimeError("nautilus_instrument_missing_after_fence")
        entry = self.order_factory.market(
            instrument_id=SOLUSDT_PERP,
            order_side=OrderSide.BUY,
            quantity=instrument.make_qty(command.quantity),
            reduce_only=False,
            client_order_id=ClientOrderId(expected_id),
        )
        self._orders[expected_id] = (intent.intent_id, "entry")
        self._active_outcome = outcome
        self._pending_fence_quantity = None
        self.submit_order(entry, client_id=ClientId(BINANCE))

    def _recover_fenced_intent(self, outcome: IntentOutcome) -> None:
        intent = self._active_intent
        client_order_id = outcome.entry_client_order_id
        if intent is None or client_order_id is None:
            raise ValueError("nautilus_fenced_entry_identity_missing")
        order = self.cache.order(ClientOrderId(client_order_id))
        if order is None:
            self._emit(
                OrderOutcomeUnknown(
                    intent_id=intent.intent_id,
                    leg="entry",
                    observed_at_ms=int(self.clock.timestamp_ms()),
                )
            )
        elif order.venue_order_id is not None:
            self._orders[client_order_id] = (intent.intent_id, "entry")
            self.query_order(order, client_id=ClientId(BINANCE))
        else:
            self._orders[client_order_id] = (intent.intent_id, "entry")
            self._emit(
                OrderOutcomeUnknown(
                    intent_id=intent.intent_id,
                    leg="entry",
                    observed_at_ms=int(self.clock.timestamp_ms()),
                )
            )

        position = None
        if outcome.position_id is not None:
            position = self.cache.position(PositionId(outcome.position_id))
        if position is None:
            position = self.cache.position_for_order(ClientOrderId(client_order_id))
        if position is None:
            positions = [
                candidate
                for candidate in self.cache.positions_open(instrument_id=SOLUSDT_PERP)
                if candidate.opening_order_id == ClientOrderId(client_order_id)
            ]
            position = positions[0] if len(positions) == 1 else None

        if position is not None and not self._owned_position_contract_matches(position, client_order_id):
            self._emit(
                OrderOutcomeUnknown(
                    intent_id=None,
                    leg=None,
                    observed_at_ms=int(self.clock.timestamp_ms()),
                )
            )
            return

        recovered_closed_position = None
        if position is not None and position.instrument_id == SOLUSDT_PERP and position.quantity.as_decimal() > 0:
            self._position_id = position.id
            self._position_quantity = position.quantity.as_decimal()
            self._opened_at_ms = outcome.opened_at_ms or int(position.ts_opened) // 1_000_000
            deadline = self._opened_at_ms + intent.max_holding_ms
            self._recovered_past_close_deadline = (
                outcome.close_client_order_id is None and int(self.clock.timestamp_ms()) >= deadline
            )
            if outcome.position_id is None or outcome.actual_quantity is None or outcome.opened_at_ms is None:
                self._emit(
                    EntryFilled(
                        intent_id=intent.intent_id,
                        actual_quantity=self._position_quantity,
                        avg_entry_price=Decimal(str(position.avg_px_open)),
                        position_id=position.id.value,
                        opened_at_ms=self._opened_at_ms,
                    )
                )
        elif (
            position is not None
            and position.instrument_id == SOLUSDT_PERP
            and position.is_closed
            and position.opening_order_id == ClientOrderId(client_order_id)
        ):
            recovered_closed_position = position
            self._position_id = position.id
            self._position_quantity = Decimal(0)
            self._opened_at_ms = None
            recovered_entry_quantity = (
                outcome.actual_quantity
                if outcome.actual_quantity is not None and outcome.actual_quantity > 0
                else position.peak_qty.as_decimal()
            )
            if outcome.position_id is None or outcome.actual_quantity is None or outcome.opened_at_ms is None:
                self._emit(
                    EntryFilled(
                        intent_id=intent.intent_id,
                        actual_quantity=recovered_entry_quantity,
                        avg_entry_price=Decimal(str(position.avg_px_open)),
                        position_id=position.id.value,
                        opened_at_ms=int(position.ts_opened) // 1_000_000,
                    )
                )
        elif outcome.actual_quantity is not None and outcome.actual_quantity > 0:
            self._fail_protection(
                intent_id=intent.intent_id,
                client_order_id=outcome.stop_client_order_id or "missing",
                observed_at_ms=int(self.clock.timestamp_ms()),
            )

        stop_client_order_id = outcome.stop_client_order_id
        if stop_client_order_id is None:
            stop_client_order_id = deterministic_client_order_id(intent.intent_id, "stop")
        stop = self.cache.order(ClientOrderId(stop_client_order_id))
        if stop is None:
            if self._position_quantity is not None and self._position_quantity > 0:
                self._fail_protection(
                    intent_id=intent.intent_id,
                    client_order_id=stop_client_order_id,
                    observed_at_ms=int(self.clock.timestamp_ms()),
                )
        else:
            self._stop_order = stop
            self._stop_generation = outcome.stop_generation or 0
            local_trigger_price = getattr(stop, "trigger_price", None)
            self._stop_trigger_price = (
                outcome.stop_price
                if outcome.stop_price is not None
                else None
                if local_trigger_price is None
                else local_trigger_price.as_decimal()
            )
            self._orders[stop_client_order_id] = (intent.intent_id, "stop")
            recovered_stop_quantity = self._position_quantity
            if recovered_stop_quantity is not None and recovered_stop_quantity == 0 and position is not None:
                recovered_stop_quantity = (
                    outcome.actual_quantity
                    if outcome.actual_quantity is not None and outcome.actual_quantity > 0
                    else position.peak_qty.as_decimal()
                )
            terminal_stop_after_close = bool(
                recovered_closed_position is not None and self._terminal_order_proven(stop)
            )
            if terminal_stop_after_close:
                self._terminal_stop_ids.add(stop_client_order_id)
                if stop.venue_order_id is not None:
                    self.query_order(stop, client_id=ClientId(BINANCE))
            elif not self._owned_stop_contract_matches(
                stop.client_order_id,
                expected_quantity=recovered_stop_quantity,
            ):
                self._fail_protection(
                    intent_id=intent.intent_id,
                    client_order_id=stop_client_order_id,
                    observed_at_ms=int(self.clock.timestamp_ms()),
                )
            else:
                self.query_order(stop, client_id=ClientId(BINANCE))
                recovered_trigger_price = self._stop_trigger_price
                if recovered_trigger_price is None:
                    raise RuntimeError("nautilus_reconciled_stop_trigger_missing")
                if outcome.stop_client_order_id is None:
                    self._emit(
                        StopSubmitted(
                            intent_id=intent.intent_id,
                            client_order_id=stop_client_order_id,
                            generation=0,
                            previous_client_order_id=None,
                            quantity=stop.quantity.as_decimal(),
                            submitted_at_ms=max(int(stop.ts_init) // 1_000_000, intent.created_at_ms),
                        )
                    )
                if outcome.protection_order_id is None and stop.venue_order_id is not None:
                    self._emit(
                        StopAccepted(
                            intent_id=intent.intent_id,
                            client_order_id=stop_client_order_id,
                            venue_order_id=stop.venue_order_id.value,
                            quantity=stop.quantity.as_decimal(),
                            trigger_price=recovered_trigger_price,
                            accepted_at_ms=max(int(stop.ts_last) // 1_000_000, intent.created_at_ms),
                        )
                    )

        close_client_order_id = outcome.close_client_order_id or deterministic_client_order_id(
            intent.intent_id,
            "close",
        )
        close = self.cache.order(ClientOrderId(close_client_order_id))
        if close is not None:
            self._close_order = close
            self._orders[close_client_order_id] = (intent.intent_id, "close")
            if self._terminal_order_proven(close):
                self._terminal_close_ids.add(close_client_order_id)
            if outcome.close_client_order_id is None and self._position_id is not None:
                self._emit(
                    CloseSubmitted(
                        intent_id=intent.intent_id,
                        client_order_id=close_client_order_id,
                        position_id=self._position_id.value,
                        quantity=close.quantity.as_decimal(),
                        submitted_at_ms=max(int(close.ts_init) // 1_000_000, intent.created_at_ms),
                    )
                )
            if close.venue_order_id is not None:
                self.query_order(close, client_id=ClientId(BINANCE))
            elif not close.is_closed:
                self._emit(
                    OrderOutcomeUnknown(
                        intent_id=intent.intent_id,
                        leg="close",
                        observed_at_ms=int(self.clock.timestamp_ms()),
                    )
                )
        elif outcome.close_client_order_id is not None:
            self._orders[close_client_order_id] = (intent.intent_id, "close")
            self._emit(
                OrderOutcomeUnknown(
                    intent_id=intent.intent_id,
                    leg="close",
                    observed_at_ms=int(self.clock.timestamp_ms()),
                )
            )

        if recovered_closed_position is not None:
            self._recover_closed_position(recovered_closed_position, close_client_order_id, stop_client_order_id)

    def on_position_opened(self, event: Any) -> None:
        intent = self._active_intent
        if intent is None or event.instrument_id != SOLUSDT_PERP:
            return
        account = self.portfolio.account(venue=SOLUSDT_PERP.venue)
        opening_client_order_id = event.opening_order_id.value
        expected_entry_id = deterministic_client_order_id(intent.intent_id, "entry")
        if (
            opening_client_order_id != expected_entry_id
            or account is None
            or event.account_id != account.id
            or event.strategy_id != self.id
            or event.side != PositionSide.LONG
        ):
            self._emit(
                OrderOutcomeUnknown(
                    intent_id=None,
                    leg=None,
                    observed_at_ms=int(event.ts_opened) // 1_000_000,
                )
            )
            return
        quantity = event.quantity.as_decimal()
        opened_at_ms = int(event.ts_opened) // 1_000_000
        self._position_id = event.position_id
        self._position_quantity = quantity
        self._opened_at_ms = opened_at_ms
        self._recovered_past_close_deadline = False
        self._emit(
            EntryFilled(
                intent_id=intent.intent_id,
                actual_quantity=quantity,
                avg_entry_price=Decimal(str(event.avg_px_open)),
                position_id=event.position_id.value,
                opened_at_ms=opened_at_ms,
            )
        )
        self._submit_stop(
            quantity=quantity,
            avg_entry_price=Decimal(str(event.avg_px_open)),
            previous_client_order_id=None,
            generation=0,
            submitted_at_ms=opened_at_ms,
        )

    def on_position_changed(self, event: Any) -> None:
        intent = self._active_intent
        if (
            intent is None
            or event.instrument_id != SOLUSDT_PERP
            or self._position_id is None
            or event.position_id != self._position_id
        ):
            return
        quantity = event.quantity.as_decimal()
        self._position_quantity = quantity
        self._emit(
            PositionQuantityChanged(
                intent_id=intent.intent_id,
                position_id=event.position_id.value,
                actual_quantity=quantity,
                changed_at_ms=self._event_ms(event),
            )
        )
        if self._stop_order is None or self._stop_order.quantity.as_decimal() == quantity:
            return
        self._pending_stop_quantity = quantity
        if self._stop_cancel_pending:
            return
        self._stop_cancel_pending = True
        self.cancel_order(self._stop_order, client_id=ClientId(BINANCE))

    def on_position_closed(self, event: Any) -> None:
        intent = self._active_intent
        if (
            intent is None
            or self._position_id is None
            or event.position_id != self._position_id
            or event.instrument_id != SOLUSDT_PERP
        ):
            return
        pnl = event.realized_pnl
        position = self.cache.position(event.position_id)
        local_quantity = event.quantity.as_decimal()
        if local_quantity != 0:
            raise ValueError("nautilus_position_closed_quantity_nonzero")
        closing_client_order_id = event.closing_order_id.value
        close_id = deterministic_client_order_id(intent.intent_id, "close")
        current_stop_id = None if self._stop_order is None else self._stop_order.client_order_id.value
        if closing_client_order_id not in {close_id, current_stop_id}:
            self._emit(
                OrderOutcomeUnknown(
                    intent_id=intent.intent_id,
                    leg="close",
                    observed_at_ms=int(event.ts_closed) // 1_000_000,
                )
            )
            return
        if (
            closing_client_order_id == current_stop_id
            and self._stop_order is not None
            and self._terminal_order_proven(self._stop_order)
        ):
            self._terminal_stop_ids.add(closing_client_order_id)
        elif (
            closing_client_order_id == close_id
            and self._close_order is not None
            and self._terminal_order_proven(self._close_order)
        ):
            self._terminal_close_ids.add(closing_client_order_id)

        self._position_quantity = local_quantity
        self._opened_at_ms = None
        self._pending_stop_quantity = None
        self._stop_cancel_pending = False
        observation = PositionClosedObserved(
            intent_id=intent.intent_id,
            instrument_id=event.instrument_id.value,
            account_id=event.account_id.value,
            position_id=event.position_id.value,
            closing_client_order_id=closing_client_order_id,
            local_quantity=local_quantity,
            avg_exit_price=Decimal(str(event.avg_px_close)),
            realized_pnl_amount=None if pnl is None else pnl.as_decimal(),
            realized_pnl_currency=None if pnl is None else pnl.currency.code,
            commissions_by_currency=None if position is None else self._position_commissions(position),
            closed_at_ms=int(event.ts_closed) // 1_000_000,
        )
        self._record_close_observation(observation, stop_client_order_id=current_stop_id)

    def _recover_closed_position(self, position: Any, close_client_order_id: str, stop_client_order_id: str) -> None:
        intent = self._active_intent
        if intent is None:
            raise RuntimeError("nautilus_recovered_close_intent_missing")
        closing_client_order_id = position.closing_order_id.value
        if closing_client_order_id not in {close_client_order_id, stop_client_order_id}:
            self._emit(
                OrderOutcomeUnknown(
                    intent_id=None,
                    leg=None,
                    observed_at_ms=int(position.ts_closed) // 1_000_000,
                )
            )
            return
        if closing_client_order_id == stop_client_order_id:
            self._terminal_stop_ids.add(stop_client_order_id)
        pnl = position.realized_pnl
        observation = PositionClosedObserved(
            intent_id=intent.intent_id,
            instrument_id=position.instrument_id.value,
            account_id=position.account_id.value,
            position_id=position.id.value,
            closing_client_order_id=closing_client_order_id,
            local_quantity=position.quantity.as_decimal(),
            avg_exit_price=Decimal(str(position.avg_px_close)),
            realized_pnl_amount=None if pnl is None else pnl.as_decimal(),
            realized_pnl_currency=None if pnl is None else pnl.currency.code,
            commissions_by_currency=self._position_commissions(position),
            closed_at_ms=int(position.ts_closed) // 1_000_000,
        )
        self._record_close_observation(observation, stop_client_order_id=stop_client_order_id)

    def _record_close_observation(
        self,
        observation: PositionClosedObserved,
        *,
        stop_client_order_id: str | None,
    ) -> None:
        self._local_close_observation = observation
        self._stop_id_at_close = stop_client_order_id
        self._emit(observation)
        self._request_current_venue_flat_proof()

    def _request_current_venue_flat_proof(self) -> None:
        observation = self._local_close_observation
        if observation is None:
            raise RuntimeError("nautilus_flat_observation_missing")
        request = VenueFlatProofRequested(
            intent_id=observation.intent_id,
            instrument_id=observation.instrument_id,
            account_id=observation.account_id,
            position_id=observation.position_id,
            closing_client_order_id=observation.closing_client_order_id,
            observed_at_ms=observation.closed_at_ms,
        )
        try:
            self._request_venue_flat(request)
        except Exception:
            self._venue_flat_unproven(
                VenueFlatUnproven(
                    intent_id=observation.intent_id,
                    position_id=observation.position_id,
                    observed_at_ms=observation.closed_at_ms,
                )
            )

    def _retry_venue_flat_proof(self) -> None:
        retry_at_ms = self._flat_retry_at_ms
        if retry_at_ms is None or int(self.clock.timestamp_ms()) < retry_at_ms:
            return
        self._flat_retry_at_ms = None
        self._request_current_venue_flat_proof()

    def on_order_accepted(self, event: Any) -> None:
        identity = self._orders.get(event.client_order_id.value)
        if identity is None or identity[1] != "stop" or self._stop_order is None:
            return
        trigger_price = self._stop_trigger_price
        if not self._owned_stop_contract_matches(event.client_order_id):
            self._fail_protection(
                intent_id=identity[0],
                client_order_id=event.client_order_id.value,
                observed_at_ms=self._event_ms(event),
            )
            return
        if trigger_price is None:
            return
        self._emit(
            StopAccepted(
                intent_id=identity[0],
                client_order_id=event.client_order_id.value,
                venue_order_id=event.venue_order_id.value,
                quantity=self._stop_order.quantity.as_decimal(),
                trigger_price=trigger_price,
                accepted_at_ms=self._event_ms(event),
            )
        )

    def on_order_filled(self, event: Any) -> None:
        """Observe owned order terminality; detailed fills remain Nautilus-owned."""

        client_order_id = event.client_order_id.value
        identity = self._orders.get(client_order_id)
        if identity is None:
            return
        if identity[1] == "close" and self._close_order is not None and self._close_order.is_closed:
            self._terminal_close_ids.add(client_order_id)
            if self._pending_flat_confirmation is not None:
                self._retire_orders_after_flat()
        elif identity[1] == "stop" and self._stop_order is not None and self._stop_order.is_closed:
            self._terminal_stop_ids.add(client_order_id)
            if self._pending_flat_confirmation is not None:
                self._retire_orders_after_flat()

    def on_order_canceled(self, event: Any) -> None:
        identity = self._orders.get(event.client_order_id.value)
        if identity is not None and identity[1] == "close":
            self._terminal_close_ids.add(event.client_order_id.value)
            self._close_order = None
            if self._cancel_close_for_flat and self._pending_flat_confirmation is not None:
                self._cancel_close_for_flat = False
                self._retire_orders_after_flat()
                return
            self._emit(
                OrderOutcomeUnknown(
                    intent_id=identity[0],
                    leg="close",
                    observed_at_ms=self._event_ms(event),
                )
            )
            return
        if self._stop_order is None or event.client_order_id != self._stop_order.client_order_id:
            return
        if self._cancel_stop_for_flat and self._pending_flat_confirmation is not None:
            self._terminal_stop_ids.add(event.client_order_id.value)
            self._cancel_stop_for_flat = False
            self._stop_cancel_pending = False
            self._pending_stop_quantity = None
            self._stop_order = None
            self._retire_orders_after_flat()
            return
        if not self._stop_cancel_pending:
            client_order_id = event.client_order_id.value
            intent_id = self._orders[client_order_id][0]
            self._terminal_stop_ids.add(client_order_id)
            self._stop_order = None
            self._fail_protection(
                intent_id=intent_id,
                client_order_id=client_order_id,
                observed_at_ms=self._event_ms(event),
            )
            return
        quantity = self._pending_stop_quantity
        previous_id = self._stop_order.client_order_id.value
        generation = (self._stop_generation or 0) + 1
        trigger_price = self._stop_trigger_price
        self._stop_cancel_pending = False
        self._pending_stop_quantity = None
        if quantity is None or quantity <= 0 or trigger_price is None:
            return
        self._submit_stop(
            quantity=quantity,
            trigger_price=trigger_price,
            previous_client_order_id=previous_id,
            generation=generation,
            submitted_at_ms=self._event_ms(event),
        )

    def on_order_expired(self, event: Any) -> None:
        identity = self._orders.get(event.client_order_id.value)
        if identity is not None and identity[1] == "close":
            self._terminal_close_ids.add(event.client_order_id.value)
            self._close_order = None
            if self._pending_flat_confirmation is not None:
                self._cancel_close_for_flat = False
                self._retire_orders_after_flat()
                return
            self._emit(
                OrderOutcomeUnknown(
                    intent_id=identity[0],
                    leg="close",
                    observed_at_ms=self._event_ms(event),
                )
            )
            return
        if (
            identity is None
            or identity[1] != "stop"
            or self._stop_order is None
            or event.client_order_id != self._stop_order.client_order_id
        ):
            return
        if self._pending_flat_confirmation is not None:
            self._terminal_stop_ids.add(event.client_order_id.value)
            self._stop_order = None
            self._cancel_stop_for_flat = False
            self._retire_orders_after_flat()
            return
        self._terminal_stop_ids.add(event.client_order_id.value)
        self._stop_order = None
        self._fail_protection(
            intent_id=identity[0],
            client_order_id=event.client_order_id.value,
            observed_at_ms=self._event_ms(event),
        )

    def on_order_cancel_rejected(self, event: Any) -> None:
        identity = self._orders.get(event.client_order_id.value)
        if identity is None:
            return
        if identity[1] == "close":
            if self._pending_flat_confirmation is not None and self._local_close_observation is not None:
                self._venue_flat_unproven(
                    VenueFlatUnproven(
                        intent_id=identity[0],
                        position_id=self._local_close_observation.position_id,
                        observed_at_ms=self._event_ms(event),
                    )
                )
            else:
                self._emit(
                    OrderOutcomeUnknown(
                        intent_id=identity[0],
                        leg="close",
                        observed_at_ms=self._event_ms(event),
                    )
                )
            return
        if identity[1] != "stop":
            return
        self._fail_protection(
            intent_id=identity[0],
            client_order_id=event.client_order_id.value,
            observed_at_ms=self._event_ms(event),
        )

    def on_order_denied(self, event: Any) -> None:
        identity = self._orders.get(event.client_order_id.value)
        if identity is None:
            return
        if identity[1] == "stop":
            if self._pending_flat_confirmation is not None:
                self._terminal_stop_ids.add(event.client_order_id.value)
                self._stop_order = None
                self._cancel_stop_for_flat = False
                self._retire_orders_after_flat()
                return
            self._fail_protection(
                intent_id=identity[0],
                client_order_id=event.client_order_id.value,
                observed_at_ms=self._event_ms(event),
                terminal=True,
            )
            return
        if identity[1] == "close":
            self._terminal_close_ids.add(event.client_order_id.value)
            self._close_order = None
            if self._pending_flat_confirmation is not None:
                self._cancel_close_for_flat = False
                self._retire_orders_after_flat()
                return
            self._emit(
                OrderOutcomeUnknown(
                    intent_id=identity[0],
                    leg="close",
                    observed_at_ms=self._event_ms(event),
                )
            )
            return
        if identity[1] != "entry":
            return
        self._emit(
            EntryRejected(
                intent_id=identity[0],
                client_order_id=event.client_order_id.value,
                reason_code="risk_denied",
                observed_at_ms=self._event_ms(event),
            )
        )
        self._clear_active_without_exposure(identity[0])

    def on_order_rejected(self, event: Any) -> None:
        client_order_id = event.client_order_id.value
        identity = self._orders.get(client_order_id)
        if identity is not None and identity[1] == "stop":
            self._fail_protection(
                intent_id=identity[0],
                client_order_id=client_order_id,
                observed_at_ms=self._event_ms(event),
            )
            return
        self._emit(
            OrderOutcomeUnknown(
                intent_id=None if identity is None else identity[0],
                leg=None if identity is None else identity[1],
                observed_at_ms=self._event_ms(event),
            )
        )

    def _confirm_venue_flat(self, command: VenueFlatConfirmed) -> None:
        intent = self._active_intent
        observation = self._local_close_observation
        if (
            intent is None
            or observation is None
            or command.intent_id != intent.intent_id
            or command.instrument_id != SOLUSDT_PERP.value
            or command.position_id != observation.position_id
            or command.authoritative_quantity != 0
        ):
            raise ValueError("nautilus_venue_flat_confirmation_invalid")
        self._pending_flat_confirmation = command
        self._flat_retry_at_ms = None
        self._retire_orders_after_flat()

    def _retire_orders_after_flat(self) -> None:
        """Prove both risk-reducing orders terminal before releasing the active fence."""

        intent = self._active_intent
        observation = self._local_close_observation
        confirmation = self._pending_flat_confirmation
        if intent is None or observation is None or confirmation is None:
            raise RuntimeError("nautilus_flat_retirement_context_missing")
        closing_id = observation.closing_client_order_id
        close_id = deterministic_client_order_id(intent.intent_id, "close")
        stop_id = self._stop_id_at_close
        if closing_id not in (stop_id, close_id):
            raise ValueError("nautilus_flat_closing_order_unowned")

        if self._close_order is not None and self._terminal_order_proven(self._close_order):
            self._terminal_close_ids.add(self._close_order.client_order_id.value)
        if self._stop_order is not None and self._terminal_order_proven(self._stop_order):
            self._terminal_stop_ids.add(self._stop_order.client_order_id.value)

        if close_id in self._orders and close_id not in self._terminal_close_ids:
            if self._close_order is None or self._close_order.client_order_id.value != close_id:
                self._venue_flat_unproven(
                    VenueFlatUnproven(
                        intent_id=intent.intent_id,
                        position_id=observation.position_id,
                        observed_at_ms=confirmation.verified_at_ms,
                    )
                )
                return
            if not self._cancel_close_for_flat:
                self._cancel_close_for_flat = True
                self.cancel_order(self._close_order, client_id=ClientId(BINANCE))
            return

        if stop_id is None or stop_id in self._terminal_stop_ids:
            self._stop_order = None
            self._finalize_flat()
            return
        if self._stop_order is None or self._stop_order.client_order_id.value != stop_id:
            self._venue_flat_unproven(
                VenueFlatUnproven(
                    intent_id=intent.intent_id,
                    position_id=observation.position_id,
                    observed_at_ms=confirmation.verified_at_ms,
                )
            )
            return
        self._cancel_stop_for_flat = True
        self.cancel_order(self._stop_order, client_id=ClientId(BINANCE))

    def _venue_flat_unproven(self, command: VenueFlatUnproven) -> None:
        intent = self._active_intent
        observation = self._local_close_observation
        if (
            intent is None
            or observation is None
            or command.intent_id != intent.intent_id
            or command.position_id != observation.position_id
        ):
            raise ValueError("nautilus_venue_flat_failure_invalid")
        self._flat_retry_at_ms = max(int(self.clock.timestamp_ms()), command.observed_at_ms) + _FLAT_PROOF_RETRY_MS
        if self._flat_unproven_reported:
            return
        self._flat_unproven_reported = True
        self._emit(
            OrderOutcomeUnknown(
                intent_id=intent.intent_id,
                leg="close",
                observed_at_ms=command.observed_at_ms,
            )
        )

    def _finalize_flat(self) -> None:
        intent = self._active_intent
        observation = self._local_close_observation
        confirmation = self._pending_flat_confirmation
        if intent is None or observation is None or confirmation is None:
            raise RuntimeError("nautilus_flat_finalization_context_missing")
        stop_id = self._stop_id_at_close
        close_id = deterministic_client_order_id(intent.intent_id, "close")
        closing_id = observation.closing_client_order_id
        stop_terminal = stop_id is None or stop_id in self._terminal_stop_ids
        close_known = close_id in self._orders
        close_terminal = not close_known or close_id in self._terminal_close_ids
        closing_order_owned = closing_id in (stop_id, close_id)
        if not stop_terminal or not close_terminal or not closing_order_owned:
            raise RuntimeError("nautilus_flat_orders_not_terminal")
        self._emit(
            PositionFlatConfirmed(
                intent_id=intent.intent_id,
                position_id=observation.position_id,
                authoritative_quantity=confirmation.authoritative_quantity,
                avg_exit_price=observation.avg_exit_price,
                realized_pnl_amount=observation.realized_pnl_amount,
                realized_pnl_currency=observation.realized_pnl_currency,
                commissions_by_currency=observation.commissions_by_currency,
                closed_at_ms=observation.closed_at_ms,
                flat_verified_at_ms=confirmation.verified_at_ms,
            )
        )
        self._active_intent = None
        self._active_outcome = None
        self._pending_fence_quantity = None
        self._orders.clear()
        self._position_id = None
        self._position_quantity = None
        self._opened_at_ms = None
        self._stop_order = None
        self._stop_trigger_price = None
        self._stop_generation = None
        self._stop_cancel_pending = False
        self._pending_stop_quantity = None
        self._failed_stop_ids.clear()
        self._terminal_stop_ids.clear()
        self._close_order = None
        self._terminal_close_ids.clear()
        self._local_close_observation = None
        self._stop_id_at_close = None
        self._pending_flat_confirmation = None
        self._cancel_close_for_flat = False
        self._cancel_stop_for_flat = False
        self._flat_unproven_reported = False
        self._flat_retry_at_ms = None

    @staticmethod
    def _position_commissions(position: Any) -> dict[str, str] | None:
        fills = position.events
        peak_quantity = position.peak_qty.as_decimal()
        if (
            not fills
            or any(fill.commission is None or not fill.trade_id.value.isdigit() for fill in fills)
            or sum(
                (fill.last_qty.as_decimal() for fill in fills if fill.order_side == OrderSide.BUY),
                Decimal(0),
            )
            != peak_quantity
            or sum(
                (fill.last_qty.as_decimal() for fill in fills if fill.order_side == OrderSide.SELL),
                Decimal(0),
            )
            != peak_quantity
        ):
            return None
        totals: dict[str, Decimal] = {}
        for commission in (fill.commission for fill in fills):
            if commission is None:
                return None
            currency = commission.currency.code
            totals[currency] = totals.get(currency, Decimal(0)) + commission.as_decimal()
        return {currency: format(amount, "f") for currency, amount in sorted(totals.items())}

    def _owned_stop_contract_matches(
        self,
        client_order_id: ClientOrderId,
        *,
        expected_quantity: Decimal | None = None,
    ) -> bool:
        stop = self._stop_order
        account = self.portfolio.account(venue=SOLUSDT_PERP.venue)
        quantity = self._position_quantity if expected_quantity is None else expected_quantity
        trigger_price = self._stop_trigger_price
        return bool(
            stop is not None
            and stop.client_order_id == client_order_id
            and account is not None
            and stop.account_id == account.id
            and stop.venue_order_id is not None
            and stop.instrument_id == SOLUSDT_PERP
            and stop.order_type == OrderType.STOP_MARKET
            and stop.status == OrderStatus.ACCEPTED
            and stop.side == OrderSide.SELL
            and stop.is_reduce_only
            and quantity is not None
            and stop.quantity.as_decimal() == quantity
            and trigger_price is not None
            and stop.trigger_price is not None
            and stop.trigger_price.as_decimal() == trigger_price
            and stop.trigger_type == TriggerType.MARK_PRICE
        )

    @staticmethod
    def _terminal_order_proven(order: Any) -> bool:
        if order.status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.DENIED):
            return True
        return bool(order.status == OrderStatus.REJECTED and order.venue_order_id is not None)

    def _owned_position_contract_matches(self, position: Any, entry_client_order_id: str) -> bool:
        account = self.portfolio.account(venue=SOLUSDT_PERP.venue)
        return bool(
            account is not None
            and position.instrument_id == SOLUSDT_PERP
            and position.account_id == account.id
            and position.strategy_id == self.id
            and position.entry == OrderSide.BUY
            and (
                (position.is_open and position.side == PositionSide.LONG)
                or (position.is_closed and position.side == PositionSide.FLAT)
            )
            and position.opening_order_id == ClientOrderId(entry_client_order_id)
        )

    def _fail_protection(
        self,
        *,
        intent_id: str,
        client_order_id: str,
        observed_at_ms: int,
        terminal: bool = False,
    ) -> None:
        if client_order_id in self._failed_stop_ids:
            return
        self._failed_stop_ids.add(client_order_id)
        if terminal:
            self._terminal_stop_ids.add(client_order_id)
        self._emit(
            OrderOutcomeUnknown(
                intent_id=intent_id,
                leg="stop",
                observed_at_ms=observed_at_ms,
            )
        )
        intent = self._active_intent
        position_id = self._position_id
        if (
            intent is None
            or intent.intent_id != intent_id
            or position_id is None
            or self._position_quantity is None
            or self._position_quantity <= 0
            or (self._active_outcome is not None and self._active_outcome.close_client_order_id is not None)
        ):
            return
        self._stop_cancel_pending = False
        self._pending_stop_quantity = None
        self._submit_close()

    def _submit_stop(
        self,
        *,
        quantity: Decimal,
        previous_client_order_id: str | None,
        generation: int,
        submitted_at_ms: int,
        avg_entry_price: Decimal | None = None,
        trigger_price: Decimal | None = None,
    ) -> None:
        intent = self._active_intent
        position_id = self._position_id
        instrument = self.cache.instrument(SOLUSDT_PERP)
        if intent is None or position_id is None or instrument is None:
            raise RuntimeError("nautilus_stop_context_missing")
        if trigger_price is None:
            if avg_entry_price is None:
                raise RuntimeError("nautilus_stop_price_missing")
            raw_price = avg_entry_price * (Decimal(10_000 - intent.stop_loss_bps) / Decimal(10_000))
            increment = instrument.price_increment.as_decimal()
            trigger_price = (raw_price / increment).to_integral_value(rounding=ROUND_DOWN) * increment
        client_order_id = deterministic_client_order_id(
            intent.intent_id,
            "stop",
            previous_client_order_id=previous_client_order_id,
        )
        stop = self.order_factory.stop_market(
            instrument_id=SOLUSDT_PERP,
            order_side=OrderSide.SELL,
            quantity=instrument.make_qty(quantity),
            trigger_price=instrument.make_price(trigger_price),
            trigger_type=TriggerType.MARK_PRICE,
            reduce_only=True,
            client_order_id=ClientOrderId(client_order_id),
        )
        self._stop_order = stop
        self._stop_trigger_price = trigger_price
        self._stop_generation = generation
        self._orders[client_order_id] = (intent.intent_id, "stop")
        self.submit_order(stop, position_id=position_id, client_id=ClientId(BINANCE))
        self._emit(
            StopSubmitted(
                intent_id=intent.intent_id,
                client_order_id=client_order_id,
                generation=generation,
                previous_client_order_id=previous_client_order_id,
                quantity=quantity,
                submitted_at_ms=submitted_at_ms,
            )
        )

    def _submit_close(self) -> None:
        intent = self._active_intent
        quantity = self._position_quantity
        position_id = self._position_id
        if intent is None or position_id is None or quantity is None or quantity <= 0:
            raise RuntimeError("nautilus_close_context_missing")
        client_order_id = deterministic_client_order_id(intent.intent_id, "close")
        if client_order_id in self._orders:
            return
        instrument = self.cache.instrument(SOLUSDT_PERP)
        if instrument is None:
            raise RuntimeError("nautilus_instrument_missing_for_close")
        close = self.order_factory.market(
            instrument_id=SOLUSDT_PERP,
            order_side=OrderSide.SELL,
            quantity=instrument.make_qty(quantity),
            reduce_only=True,
            client_order_id=ClientOrderId(client_order_id),
        )
        self._close_order = close
        self._orders[client_order_id] = (intent.intent_id, "close")
        self.submit_order(close, position_id=position_id, client_id=ClientId(BINANCE))
        self._emit(
            CloseSubmitted(
                intent_id=intent.intent_id,
                client_order_id=client_order_id,
                position_id=position_id.value,
                quantity=quantity,
                submitted_at_ms=int(self.clock.timestamp_ms()),
            )
        )

    def _enforce_max_holding(self) -> None:
        intent = self._active_intent
        position_id = self._position_id
        quantity = self._position_quantity
        opened_at_ms = self._opened_at_ms
        if intent is None or position_id is None or quantity is None or quantity <= 0 or opened_at_ms is None:
            return
        if int(self.clock.timestamp_ms()) < opened_at_ms + intent.max_holding_ms:
            return

        close_id = deterministic_client_order_id(intent.intent_id, "close")
        if close_id in self._orders:
            return
        if self._recovered_past_close_deadline:
            self._orders[close_id] = (intent.intent_id, "close")
            self._emit(
                OrderOutcomeUnknown(
                    intent_id=intent.intent_id,
                    leg="close",
                    observed_at_ms=int(self.clock.timestamp_ms()),
                )
            )
            return

        reconciled = self.cache.order(ClientOrderId(close_id))
        if reconciled is not None:
            self._close_order = reconciled
            self._orders[close_id] = (intent.intent_id, "close")
            if reconciled.is_closed and reconciled.venue_order_id is not None:
                self._terminal_close_ids.add(close_id)
            self.query_order(reconciled, client_id=ClientId(BINANCE))
            return
        self._submit_close()

    def _event_ms(self, event: Any) -> int:
        ts_event = int(getattr(event, "ts_event", 0))
        return ts_event // 1_000_000 if ts_event else int(self.clock.timestamp_ms())

    def _emit(self, event: StrategyEvent) -> bool:
        self._flush_events()
        if len(self._pending_events) >= self._queues.events.maxsize:
            self._projection_overflow = True
            return False
        self._pending_events.append(event)
        self._flush_events()
        return True

    def _flush_events(self) -> None:
        while self._pending_events:
            try:
                self._queues.events.put_nowait(self._pending_events[0])
            except Full:
                return
            self._pending_events.popleft()


__all__ = [
    "SOLUSDT_PERP",
    "TracefoldNautilusStrategy",
    "tracefold_strategy_config",
]
