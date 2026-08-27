"""Public-v1 Nautilus strategy seam for the one-instrument Demo process."""

from __future__ import annotations

from decimal import Decimal
from queue import Full
from types import SimpleNamespace

import pytest
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import MessageBus, TestClock
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.enums import OmsType, OrderSide, OrderType, PositionSide, TriggerType
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    InstrumentId,
    PositionId,
    TradeId,
    TraderId,
    VenueOrderId,
)
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import Money
from nautilus_trader.model.position import Position
from nautilus_trader.portfolio.portfolio import Portfolio
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.test_kit.stubs.data import TestDataStubs
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs

from tracefold.integrations.nautilus.messages import (
    AdoptIntent,
    CloseSubmitted,
    EntryFenceGranted,
    EntryFenceRequested,
    EntryFilled,
    EntryRejected,
    IntentRefused,
    IntentReleased,
    OrderOutcomeUnknown,
    PositionClosedObserved,
    PositionFlatConfirmed,
    PositionQuantityChanged,
    ReadinessChanged,
    StopAccepted,
    StopSubmitted,
    StrategyQueues,
    VenueFlatConfirmed,
    VenueFlatProofRequested,
    VenueFlatUnproven,
    strategy_queues,
)
from tracefold.integrations.nautilus.strategy import (
    SOLUSDT_PERP,
    TracefoldNautilusStrategy,
    tracefold_strategy_config,
)
from tracefold.trading import IntentOutcome, TradeIntent, deterministic_client_order_id

NOW_MS = 1_900_000_000_000


def _solusdt_perp_binance() -> CryptoPerpetual:
    values = CryptoPerpetual.to_dict(TestInstrumentProvider.btcusdt_perp_binance())
    values.update(
        id="SOLUSDT-PERP.BINANCE",
        raw_symbol="SOLUSDT",
        base_currency="SOL",
        min_notional="5.00000000 USDT",
    )
    return CryptoPerpetual.from_dict(values)


def _intent(*, case_id: str = "case-1") -> TradeIntent:
    return TradeIntent.create(
        case_id=case_id,
        case_manifest_sha256="1" * 64,
        created_at_ms=NOW_MS,
        reference_price=Decimal("10000"),
        target_notional_usd=Decimal("10"),
    )


def _outcome(intent: TradeIntent, **values: object) -> IntentOutcome:
    payload: dict[str, object] = {
        "intent_id": intent.intent_id,
        "execution_state": "PENDING",
        "commissions_by_currency": {},
        "updated_at_ms": NOW_MS,
    }
    payload.update(values)
    return IntentOutcome.model_validate(payload)


class RecordingStrategy(TracefoldNautilusStrategy):
    def __init__(self, *, queues: StrategyQueues) -> None:
        self.flat_requests: list[VenueFlatProofRequested] = []
        super().__init__(
            engine_identity="nt-v1",
            queues=queues,
            request_venue_flat=self.flat_requests.append,
        )
        self.submitted: list[tuple[object, object, object]] = []
        self.canceled: list[object] = []
        self.queried: list[object] = []

    def submit_order(self, order: object, position_id: object = None, client_id: object = None, params=None) -> None:
        self.submitted.append((order, position_id, client_id))

    def cancel_order(self, order: object, client_id: object = None, params=None) -> None:
        self.canceled.append(order)

    def query_order(self, order: object, client_id: object = None, params=None) -> None:
        self.queried.append(order)


def _registered_strategy(
    *,
    leverage: str = "1",
    instrument: CryptoPerpetual | None = None,
    queue_maxsize: int = 64,
) -> tuple[RecordingStrategy, StrategyQueues]:
    queues = strategy_queues(maxsize=queue_maxsize)
    clock = TestClock()
    clock.set_time(NOW_MS * 1_000_000)
    msgbus = MessageBus(TraderId("TRACEFOLD-001"), clock)
    cache = Cache()
    instrument = instrument or _solusdt_perp_binance()
    cache.add_instrument(instrument)
    cache.add_quote_tick(
        TestDataStubs.quote_tick(
            instrument=instrument,
            bid_price=9_999,
            ask_price=10_000,
            ts_event=NOW_MS * 1_000_000,
            ts_init=NOW_MS * 1_000_000,
        )
    )
    account = TestExecStubs.margin_account(AccountId("BINANCE-001"))
    account.set_leverage(SOLUSDT_PERP, Decimal(leverage))
    cache.add_account(account)
    portfolio = Portfolio(msgbus, cache, clock)
    strategy = RecordingStrategy(queues=queues)
    strategy.register(TraderId("TRACEFOLD-001"), portfolio, msgbus, cache, clock)
    return strategy, queues


def _fenced_strategy(*, queue_maxsize: int = 64) -> tuple[RecordingStrategy, StrategyQueues, TradeIntent]:
    strategy, queues = _registered_strategy(queue_maxsize=queue_maxsize)
    intent = _intent()
    queues.commands.put_nowait(AdoptIntent(intent=intent, outcome=_outcome(intent)))
    strategy.on_timer(None)
    request = queues.events.get_nowait()
    assert isinstance(request, EntryFenceRequested)
    fenced = _outcome(
        intent,
        execution_state="IN_FLIGHT",
        execution_phase="ENTRY",
        engine_identity="nt-v1",
        entry_client_order_id=deterministic_client_order_id(intent.intent_id, "entry"),
        entry_fenced_at_ms=NOW_MS + 1,
    )
    queues.commands.put_nowait(EntryFenceGranted(outcome=fenced, quantity=request.quantity))
    strategy.on_timer(None)
    return strategy, queues, intent


def _position_opened_event(
    strategy: RecordingStrategy,
    intent: TradeIntent,
    instrument: CryptoPerpetual,
    position_id: PositionId,
    *,
    quantity: Decimal = Decimal("0.001"),
    avg_px_open: float = 10_000.0,
    opened_at_ms: int = NOW_MS + 10,
) -> SimpleNamespace:
    return SimpleNamespace(
        instrument_id=SOLUSDT_PERP,
        account_id=AccountId("BINANCE-001"),
        strategy_id=strategy.id,
        opening_order_id=ClientOrderId(deterministic_client_order_id(intent.intent_id, "entry")),
        side=PositionSide.LONG,
        position_id=position_id,
        quantity=instrument.make_qty(quantity),
        avg_px_open=avg_px_open,
        ts_opened=opened_at_ms * 1_000_000,
    )


def _opened_strategy() -> tuple[
    RecordingStrategy,
    StrategyQueues,
    TradeIntent,
    CryptoPerpetual,
    PositionId,
]:
    strategy, queues, intent = _fenced_strategy()
    instrument = _solusdt_perp_binance()
    position_id = PositionId("SOLUSDT-PERP.BINANCE-TRACEFOLD-001")
    strategy.on_position_opened(_position_opened_event(strategy, intent, instrument, position_id))
    queues.events.get_nowait()
    queues.events.get_nowait()
    return strategy, queues, intent, instrument, position_id


def test_strategy_queues_are_bounded_and_never_silently_drop_messages() -> None:
    queues = strategy_queues(maxsize=1)
    ready = ReadinessChanged(ready=True, reason="ready", unexpected_exposure=False)
    intent = _intent()
    adopt = AdoptIntent(intent=intent, outcome=_outcome(intent))

    queues.events.put_nowait(ready)
    queues.commands.put_nowait(adopt)

    with pytest.raises(Full):
        queues.events.put_nowait(ready)
    with pytest.raises(Full):
        queues.commands.put_nowait(adopt)
    assert queues.events.get_nowait() == ready
    assert queues.commands.get_nowait() == adopt
    assert queues.commands.maxsize == queues.events.maxsize == 1


def test_strategy_config_claims_only_exact_sol_netting_instrument() -> None:
    config = tracefold_strategy_config()

    assert config.oms_type == "NETTING"
    assert config.external_order_claims == [InstrumentId.from_str("SOLUSDT-PERP.BINANCE")]

    with pytest.raises(ValueError, match="nautilus_external_order_claims_invalid"):
        TracefoldNautilusStrategy(
            engine_identity="nt-v1",
            queues=strategy_queues(),
            request_venue_flat=lambda _request: None,
            config=StrategyConfig(
                oms_type="NETTING",
                external_order_claims=[InstrumentId.from_str("ETHUSDT-PERP.BINANCE")],
            ),
        )

    assert InstrumentId.from_str("SOLUSDT-PERP.BINANCE") == SOLUSDT_PERP


def test_entry_is_submitted_only_after_the_database_grants_the_durable_fence() -> None:
    strategy, queues = _registered_strategy()
    intent = _intent()
    pending = _outcome(intent)

    queues.commands.put_nowait(AdoptIntent(intent=intent, outcome=pending))
    strategy.on_timer(None)

    request = queues.events.get_nowait()
    assert request == EntryFenceRequested(
        intent_id=intent.intent_id,
        engine_identity="nt-v1",
        quantity=Decimal("0.001"),
    )
    assert strategy.submitted == []

    fenced = _outcome(
        intent,
        execution_state="IN_FLIGHT",
        execution_phase="ENTRY",
        engine_identity="nt-v1",
        entry_client_order_id=deterministic_client_order_id(intent.intent_id, "entry"),
        entry_fenced_at_ms=NOW_MS + 1,
    )
    queues.commands.put_nowait(EntryFenceGranted(outcome=fenced, quantity=request.quantity))
    strategy.on_timer(None)

    assert len(strategy.submitted) == 1
    entry, position_id, _client_id = strategy.submitted[0]
    assert entry.order_type == OrderType.MARKET
    assert entry.side == OrderSide.BUY
    assert entry.quantity.as_decimal() == Decimal("0.001")
    assert entry.client_order_id.value == deterministic_client_order_id(intent.intent_id, "entry")
    assert entry.is_reduce_only is False
    assert position_id is None


def test_entry_preflight_refuses_quantity_below_the_venue_min_notional() -> None:
    values = CryptoPerpetual.to_dict(_solusdt_perp_binance())
    values["min_notional"] = "50.00000000 USDT"
    strategy, queues = _registered_strategy(instrument=CryptoPerpetual.from_dict(values))
    intent = _intent()

    queues.commands.put_nowait(AdoptIntent(intent=intent, outcome=_outcome(intent)))
    strategy.on_timer(None)

    assert queues.events.get_nowait() == IntentRefused(
        intent_id=intent.intent_id,
        reason_code="quantity_unexecutable",
    )
    assert strategy.submitted == []


def test_startup_readiness_requires_authoritative_one_x_leverage() -> None:
    one_x, one_x_queues = _registered_strategy(leverage="1")
    one_x.on_start()
    assert one_x_queues.events.get_nowait() == ReadinessChanged(
        ready=True,
        reason="ready",
        unexpected_exposure=False,
    )


def test_non_one_x_account_cannot_request_an_entry_fence() -> None:
    strategy, queues = _registered_strategy(leverage="2")
    intent = _intent()

    queues.commands.put_nowait(AdoptIntent(intent=intent, outcome=_outcome(intent)))
    strategy.on_timer(None)

    assert queues.events.get_nowait() == IntentRefused(
        intent_id=intent.intent_id,
        reason_code="runtime_not_ready",
    )
    assert strategy.submitted == []

    two_x, two_x_queues = _registered_strategy(leverage="2")
    two_x.on_start()
    assert two_x_queues.events.get_nowait() == ReadinessChanged(
        ready=False,
        reason="leverage_not_one",
        unexpected_exposure=False,
    )


def test_terminal_refusal_clears_runtime_for_the_next_intent() -> None:
    strategy, queues = _registered_strategy(leverage="2")
    first = _intent()
    second = _intent(case_id="case-2")

    queues.commands.put_nowait(AdoptIntent(intent=first, outcome=_outcome(first)))
    strategy.on_timer(None)
    assert isinstance(queues.events.get_nowait(), IntentRefused)

    queues.commands.put_nowait(AdoptIntent(intent=second, outcome=_outcome(second)))
    strategy.on_timer(None)
    refused = queues.events.get_nowait()
    assert isinstance(refused, IntentRefused)
    assert refused.intent_id == second.intent_id


def test_database_terminalization_releases_pending_runtime_for_the_next_intent() -> None:
    strategy, queues = _registered_strategy()
    first = _intent(case_id="case-1")
    queues.commands.put_nowait(AdoptIntent(intent=first, outcome=_outcome(first)))
    strategy.on_timer(None)
    assert isinstance(queues.events.get_nowait(), EntryFenceRequested)

    queues.commands.put_nowait(IntentReleased(intent_id=first.intent_id))
    second = _intent(case_id="case-2")
    queues.commands.put_nowait(AdoptIntent(intent=second, outcome=_outcome(second)))
    strategy.on_timer(None)

    second_request = queues.events.get_nowait()
    assert isinstance(second_request, EntryFenceRequested)
    assert second_request.intent_id == second.intent_id


def test_stale_release_for_an_old_intent_cannot_disturb_the_active_intent() -> None:
    strategy, queues, active = _fenced_strategy()

    queues.commands.put_nowait(IntentReleased(intent_id="f" * 64))
    strategy.on_timer(None)

    instrument = _solusdt_perp_binance()
    position_id = PositionId("SOLUSDT-PERP.BINANCE-TRACEFOLD-001")
    strategy.on_position_opened(_position_opened_event(strategy, active, instrument, position_id))
    opened = queues.events.get_nowait()
    assert isinstance(opened, EntryFilled)
    assert opened.intent_id == active.intent_id
    stop = queues.events.get_nowait()
    assert isinstance(stop, StopSubmitted)
    assert stop.client_order_id == deterministic_client_order_id(active.intent_id, "stop")


def test_readiness_rejects_an_unowned_open_order_on_another_symbol() -> None:
    strategy, queues = _registered_strategy()
    eth = TestInstrumentProvider.ethusdt_perp_binance()
    strategy.cache.add_instrument(eth)
    external = strategy.order_factory.market(
        instrument_id=eth.id,
        order_side=OrderSide.BUY,
        quantity=eth.make_qty(Decimal("0.01")),
        client_order_id=ClientOrderId("external-eth-order"),
    )
    strategy.cache.add_order(external, client_id=ClientId("BINANCE"))
    external.apply(
        TestEventStubs.order_submitted(
            external,
            account_id=AccountId("BINANCE-001"),
            ts_event=NOW_MS * 1_000_000,
        )
    )
    strategy.cache.update_order(external)
    external.apply(
        TestEventStubs.order_accepted(
            external,
            account_id=AccountId("BINANCE-001"),
            venue_order_id=VenueOrderId("external-eth-venue"),
            ts_event=(NOW_MS + 1) * 1_000_000,
        )
    )
    strategy.cache.update_order(external)

    strategy.on_start()

    assert queues.events.get_nowait() == ReadinessChanged(False, "external_exposure", True)


def test_position_opened_submits_a_fixed_quantity_reduce_only_native_stop() -> None:
    strategy, queues, intent = _fenced_strategy()
    instrument = _solusdt_perp_binance()
    position_id = PositionId("SOLUSDT-PERP.BINANCE-TRACEFOLD-001")

    strategy.on_position_opened(_position_opened_event(strategy, intent, instrument, position_id))

    opened = queues.events.get_nowait()
    submitted = queues.events.get_nowait()
    assert opened == EntryFilled(
        intent_id=intent.intent_id,
        actual_quantity=Decimal("0.001"),
        avg_entry_price=Decimal("10000.0"),
        position_id=position_id.value,
        opened_at_ms=NOW_MS + 10,
    )
    assert isinstance(submitted, StopSubmitted)
    assert submitted.generation == 0
    assert submitted.previous_client_order_id is None
    assert submitted.quantity == Decimal("0.001")

    stop, submitted_position_id, _client_id = strategy.submitted[-1]
    assert stop.order_type == OrderType.STOP_MARKET
    assert stop.side == OrderSide.SELL
    assert stop.quantity.as_decimal() == Decimal("0.001")
    assert stop.trigger_price.as_decimal() == Decimal("9800.0")
    assert stop.trigger_type == TriggerType.MARK_PRICE
    assert stop.is_reduce_only is True
    assert stop.client_order_id.value == deterministic_client_order_id(intent.intent_id, "stop")
    assert submitted_position_id == position_id


def test_unowned_position_open_callback_never_creates_protection_for_it() -> None:
    strategy, queues, intent = _fenced_strategy()
    instrument = _solusdt_perp_binance()
    position_id = PositionId("SOLUSDT-PERP.BINANCE-EXTERNAL-001")
    event = _position_opened_event(strategy, intent, instrument, position_id)
    event.opening_order_id = ClientOrderId("external-entry")

    strategy.on_position_opened(event)

    assert queues.events.get_nowait() == OrderOutcomeUnknown(
        intent_id=None,
        leg=None,
        observed_at_ms=NOW_MS + 10,
    )
    assert len(strategy.submitted) == 1


def test_projection_queue_overflow_never_blocks_protection_or_risk_reducing_close() -> None:
    strategy, queues, intent = _fenced_strategy(queue_maxsize=1)
    strategy.on_start()
    assert queues.events.get_nowait() == ReadinessChanged(True, "ready", False)
    occupied = ReadinessChanged(True, "occupied", False)
    queues.events.put_nowait(occupied)
    instrument = _solusdt_perp_binance()
    position_id = PositionId("SOLUSDT-PERP.BINANCE-TRACEFOLD-001")

    strategy.on_position_opened(_position_opened_event(strategy, intent, instrument, position_id))
    stop = strategy.submitted[-1][0]
    assert stop.order_type == OrderType.STOP_MARKET

    strategy.on_order_denied(
        SimpleNamespace(
            client_order_id=stop.client_order_id,
            reason="Risk engine denied stop",
            ts_event=(NOW_MS + 20) * 1_000_000,
        )
    )
    close = strategy.submitted[-1][0]
    assert close.client_order_id.value == deterministic_client_order_id(intent.intent_id, "close")
    assert close.is_reduce_only is True

    assert queues.events.get_nowait() == occupied
    strategy.on_timer(None)
    assert isinstance(queues.events.get_nowait(), EntryFilled)
    strategy.on_timer(None)
    assert queues.events.get_nowait() == ReadinessChanged(False, "projection_overflow", False)


def test_position_changed_waits_for_cancel_confirmation_then_replaces_with_a_new_id() -> None:
    strategy, queues, intent = _fenced_strategy()
    instrument = _solusdt_perp_binance()
    position_id = PositionId("SOLUSDT-PERP.BINANCE-TRACEFOLD-001")
    strategy.on_position_opened(_position_opened_event(strategy, intent, instrument, position_id))
    queues.events.get_nowait()
    first_submitted = queues.events.get_nowait()
    first_stop = strategy.submitted[-1][0]

    strategy.on_position_changed(
        SimpleNamespace(
            instrument_id=SOLUSDT_PERP,
            position_id=position_id,
            quantity=instrument.make_qty(Decimal("0.002")),
            avg_px_open=10100.0,
            ts_event=(NOW_MS + 20) * 1_000_000,
        )
    )

    changed = queues.events.get_nowait()
    assert changed == PositionQuantityChanged(
        intent_id=intent.intent_id,
        position_id=position_id.value,
        actual_quantity=Decimal("0.002"),
        avg_entry_price=Decimal("10100.0"),
        changed_at_ms=NOW_MS + 20,
    )
    assert strategy.canceled == [first_stop]
    assert len(strategy.submitted) == 2

    strategy.on_order_canceled(
        SimpleNamespace(
            client_order_id=ClientOrderId(first_submitted.client_order_id),
            ts_event=(NOW_MS + 30) * 1_000_000,
        )
    )

    replacement = queues.events.get_nowait()
    assert isinstance(replacement, StopSubmitted)
    assert replacement.generation == 1
    assert replacement.previous_client_order_id == first_submitted.client_order_id
    assert replacement.client_order_id == deterministic_client_order_id(
        intent.intent_id,
        "stop",
        previous_client_order_id=first_submitted.client_order_id,
    )
    assert replacement.quantity == Decimal("0.002")
    replacement_order = strategy.submitted[-1][0]
    assert replacement_order.quantity.as_decimal() == Decimal("0.002")
    assert replacement_order.trigger_price.as_decimal() == Decimal("9898.0")
    assert replacement_order.client_order_id.value == replacement.client_order_id


def test_stop_cancel_rejection_closes_instead_of_submitting_an_unconfirmed_replacement() -> None:
    strategy, queues, intent = _fenced_strategy()
    instrument = _solusdt_perp_binance()
    position_id = PositionId("SOLUSDT-PERP.BINANCE-TRACEFOLD-001")
    strategy.on_position_opened(_position_opened_event(strategy, intent, instrument, position_id))
    queues.events.get_nowait()
    queues.events.get_nowait()
    stop = strategy.submitted[-1][0]
    strategy.on_position_changed(
        SimpleNamespace(
            instrument_id=SOLUSDT_PERP,
            position_id=position_id,
            quantity=instrument.make_qty(Decimal("0.002")),
            avg_px_open=10100.0,
            ts_event=(NOW_MS + 20) * 1_000_000,
        )
    )
    queues.events.get_nowait()

    strategy.on_order_cancel_rejected(
        SimpleNamespace(
            client_order_id=stop.client_order_id,
            reason="order still working",
            ts_event=(NOW_MS + 21) * 1_000_000,
        )
    )

    assert len(strategy.submitted) == 3
    assert queues.events.get_nowait() == OrderOutcomeUnknown(
        intent_id=intent.intent_id,
        leg="stop",
        observed_at_ms=NOW_MS + 21,
    )
    close_event = queues.events.get_nowait()
    assert close_event == CloseSubmitted(
        intent_id=intent.intent_id,
        client_order_id=deterministic_client_order_id(intent.intent_id, "close"),
        position_id=position_id.value,
        quantity=Decimal("0.002"),
        submitted_at_ms=NOW_MS,
    )
    close = strategy.submitted[-1][0]
    assert close.order_type == OrderType.MARKET
    assert close.quantity.as_decimal() == Decimal("0.002")
    assert close.is_reduce_only is True


@pytest.mark.parametrize("callback_name", ["on_order_canceled", "on_order_expired"])
def test_unexpected_stop_terminal_event_downgrades_protection_and_closes(
    callback_name: str,
) -> None:
    strategy, queues, intent, _instrument, position_id = _opened_strategy()
    stop = strategy.submitted[-1][0]

    getattr(strategy, callback_name)(
        SimpleNamespace(
            client_order_id=stop.client_order_id,
            ts_event=(NOW_MS + 30) * 1_000_000,
        )
    )

    assert queues.events.get_nowait() == OrderOutcomeUnknown(
        intent_id=intent.intent_id,
        leg="stop",
        observed_at_ms=NOW_MS + 30,
    )
    assert queues.events.get_nowait() == CloseSubmitted(
        intent_id=intent.intent_id,
        client_order_id=deterministic_client_order_id(intent.intent_id, "close"),
        position_id=position_id.value,
        quantity=Decimal("0.001"),
        submitted_at_ms=NOW_MS,
    )
    close = strategy.submitted[-1][0]
    assert close.order_type == OrderType.MARKET
    assert close.quantity.as_decimal() == Decimal("0.001")
    assert close.is_reduce_only is True


def test_authoritative_stop_denial_records_manual_evidence_and_immediately_closes() -> None:
    strategy, queues, intent, _instrument, position_id = _opened_strategy()
    stop = strategy.submitted[-1][0]

    strategy.on_order_denied(
        SimpleNamespace(
            client_order_id=stop.client_order_id,
            reason="Risk engine denied stop",
            ts_event=(NOW_MS + 31) * 1_000_000,
        )
    )

    assert queues.events.get_nowait() == OrderOutcomeUnknown(
        intent_id=intent.intent_id,
        leg="stop",
        observed_at_ms=NOW_MS + 31,
    )
    assert queues.events.get_nowait() == CloseSubmitted(
        intent_id=intent.intent_id,
        client_order_id=deterministic_client_order_id(intent.intent_id, "close"),
        position_id=position_id.value,
        quantity=Decimal("0.001"),
        submitted_at_ms=NOW_MS,
    )
    close = strategy.submitted[-1][0]
    assert close.client_order_id.value == deterministic_client_order_id(intent.intent_id, "close")
    assert close.quantity.as_decimal() == Decimal("0.001")
    assert close.is_reduce_only is True


def test_unknown_stop_rejection_never_marks_rejected_and_closes_risk_reducing() -> None:
    strategy, queues, intent, _instrument, position_id = _opened_strategy()
    stop = strategy.submitted[-1][0]

    strategy.on_order_rejected(
        SimpleNamespace(
            client_order_id=stop.client_order_id,
            reason="-1007 TIMEOUT; execution status unknown",
            ts_event=(NOW_MS + 32) * 1_000_000,
        )
    )

    evidence = queues.events.get_nowait()
    assert evidence == OrderOutcomeUnknown(
        intent_id=intent.intent_id,
        leg="stop",
        observed_at_ms=NOW_MS + 32,
    )
    assert not isinstance(evidence, EntryRejected)
    assert queues.events.get_nowait() == CloseSubmitted(
        intent_id=intent.intent_id,
        client_order_id=deterministic_client_order_id(intent.intent_id, "close"),
        position_id=position_id.value,
        quantity=Decimal("0.001"),
        submitted_at_ms=NOW_MS,
    )
    close = strategy.submitted[-1][0]
    assert close.quantity.as_decimal() == Decimal("0.001")
    assert close.is_reduce_only is True


@pytest.mark.parametrize(
    "reason",
    ["-1007 TIMEOUT; execution status unknown", "502 Bad Gateway"],
)
def test_adapter_order_rejected_is_unknown_and_never_a_business_rejection(reason: str) -> None:
    strategy, queues, intent = _fenced_strategy()
    entry = strategy.submitted[0][0]

    strategy.on_order_rejected(
        SimpleNamespace(
            client_order_id=entry.client_order_id,
            reason=reason,
            ts_event=(NOW_MS + 40) * 1_000_000,
        )
    )

    assert queues.events.get_nowait() == OrderOutcomeUnknown(
        intent_id=intent.intent_id,
        leg="entry",
        observed_at_ms=NOW_MS + 40,
    )


def test_pre_submit_order_denial_is_the_only_authoritative_entry_rejection() -> None:
    strategy, queues, intent = _fenced_strategy()
    entry = strategy.submitted[0][0]

    strategy.on_order_denied(
        SimpleNamespace(
            client_order_id=entry.client_order_id,
            reason="Insufficient margin",
            ts_event=(NOW_MS + 41) * 1_000_000,
        )
    )

    assert queues.events.get_nowait() == EntryRejected(
        intent_id=intent.intent_id,
        client_order_id=entry.client_order_id.value,
        reason_code="risk_denied",
        observed_at_ms=NOW_MS + 41,
    )
    second = _intent(case_id="case-2")
    queues.commands.put_nowait(AdoptIntent(intent=second, outcome=_outcome(second)))
    strategy.on_timer(None)
    request = queues.events.get_nowait()
    assert isinstance(request, EntryFenceRequested)
    assert request.intent_id == second.intent_id


def test_stop_acceptance_reports_the_exact_chain_head() -> None:
    strategy, queues, intent = _fenced_strategy()
    instrument = _solusdt_perp_binance()
    position_id = PositionId("SOLUSDT-PERP.BINANCE-TRACEFOLD-001")
    strategy.on_position_opened(_position_opened_event(strategy, intent, instrument, position_id))
    queues.events.get_nowait()
    submitted = queues.events.get_nowait()
    stop = strategy.submitted[-1][0]

    stop.apply(
        TestEventStubs.order_submitted(
            stop,
            account_id=AccountId("BINANCE-001"),
            ts_event=(NOW_MS + 10) * 1_000_000,
        )
    )
    accepted = TestEventStubs.order_accepted(
        stop,
        account_id=AccountId("BINANCE-001"),
        venue_order_id=VenueOrderId("stop-venue-1"),
        ts_event=(NOW_MS + 11) * 1_000_000,
    )
    stop.apply(accepted)
    strategy.on_order_accepted(accepted)

    assert queues.events.get_nowait() == StopAccepted(
        intent_id=intent.intent_id,
        client_order_id=submitted.client_order_id,
        venue_order_id="stop-venue-1",
        quantity=Decimal("0.001"),
        trigger_price=Decimal("9800.0"),
        accepted_at_ms=NOW_MS + 11,
    )


def test_recovered_stop_must_be_accepted_not_merely_open() -> None:
    strategy, _queues, _intent_value, _instrument, _position_id = _opened_strategy()
    stop = strategy.submitted[-1][0]
    stop.apply(
        TestEventStubs.order_submitted(
            stop,
            account_id=AccountId("BINANCE-001"),
            ts_event=(NOW_MS + 10) * 1_000_000,
        )
    )
    stop.apply(
        TestEventStubs.order_accepted(
            stop,
            account_id=AccountId("BINANCE-001"),
            venue_order_id=VenueOrderId("stop-venue-1"),
            ts_event=(NOW_MS + 11) * 1_000_000,
        )
    )
    assert strategy._owned_stop_contract_matches(stop.client_order_id)

    stop.apply(TestEventStubs.order_pending_cancel(stop, ts_event=(NOW_MS + 12) * 1_000_000))

    assert stop.status.name == "PENDING_CANCEL"
    assert not strategy._owned_stop_contract_matches(stop.client_order_id)


def test_stale_stop_acceptance_fails_the_owned_stop_contract_and_closes() -> None:
    strategy, queues, intent, instrument, position_id = _opened_strategy()
    stale_stop = strategy.submitted[-1][0]
    strategy.on_position_changed(
        SimpleNamespace(
            instrument_id=SOLUSDT_PERP,
            position_id=position_id,
            quantity=instrument.make_qty(Decimal("0.002")),
            avg_px_open=10100.0,
            ts_event=(NOW_MS + 20) * 1_000_000,
        )
    )
    queues.events.get_nowait()
    strategy.on_order_canceled(
        SimpleNamespace(
            client_order_id=stale_stop.client_order_id,
            ts_event=(NOW_MS + 21) * 1_000_000,
        )
    )
    queues.events.get_nowait()

    strategy.on_order_accepted(
        SimpleNamespace(
            client_order_id=stale_stop.client_order_id,
            venue_order_id=VenueOrderId("stale-stop-venue-id"),
            ts_event=(NOW_MS + 22) * 1_000_000,
        )
    )

    assert queues.events.get_nowait() == OrderOutcomeUnknown(
        intent_id=intent.intent_id,
        leg="stop",
        observed_at_ms=NOW_MS + 22,
    )
    assert queues.events.get_nowait() == CloseSubmitted(
        intent_id=intent.intent_id,
        client_order_id=deterministic_client_order_id(intent.intent_id, "close"),
        position_id=position_id.value,
        quantity=Decimal("0.002"),
        submitted_at_ms=NOW_MS,
    )
    close = strategy.submitted[-1][0]
    assert close.order_type == OrderType.MARKET
    assert close.quantity.as_decimal() == Decimal("0.002")
    assert close.is_reduce_only is True


@pytest.mark.parametrize("invalid_field", ["order_type", "reduce_only", "quantity", "trigger"])
def test_stop_acceptance_requires_the_complete_local_protection_contract(invalid_field: str) -> None:
    strategy, queues, intent, instrument, position_id = _opened_strategy()
    client_order_id = strategy.submitted[-1][0].client_order_id
    if invalid_field == "order_type":
        candidate = strategy.order_factory.market(
            instrument_id=SOLUSDT_PERP,
            order_side=OrderSide.SELL,
            quantity=instrument.make_qty(Decimal("0.001")),
            reduce_only=True,
            client_order_id=client_order_id,
        )
    else:
        candidate = strategy.order_factory.stop_market(
            instrument_id=SOLUSDT_PERP,
            order_side=OrderSide.SELL,
            quantity=instrument.make_qty(Decimal("0.002" if invalid_field == "quantity" else "0.001")),
            trigger_price=instrument.make_price(Decimal("9700" if invalid_field == "trigger" else "9800")),
            trigger_type=TriggerType.MARK_PRICE,
            reduce_only=invalid_field != "reduce_only",
            client_order_id=client_order_id,
        )
    strategy._stop_order = candidate

    strategy.on_order_accepted(
        SimpleNamespace(
            client_order_id=client_order_id,
            venue_order_id=VenueOrderId("invalid-stop-venue-id"),
            ts_event=(NOW_MS + 23) * 1_000_000,
        )
    )

    assert queues.events.get_nowait() == OrderOutcomeUnknown(
        intent_id=intent.intent_id,
        leg="stop",
        observed_at_ms=NOW_MS + 23,
    )
    assert queues.events.get_nowait() == CloseSubmitted(
        intent_id=intent.intent_id,
        client_order_id=deterministic_client_order_id(intent.intent_id, "close"),
        position_id=position_id.value,
        quantity=Decimal("0.001"),
        submitted_at_ms=NOW_MS,
    )


def test_max_holding_close_uses_the_latest_authoritative_position_quantity_and_explicit_id() -> None:
    strategy, queues, intent = _fenced_strategy()
    instrument = _solusdt_perp_binance()
    position_id = PositionId("SOLUSDT-PERP.BINANCE-TRACEFOLD-001")
    strategy.on_position_opened(_position_opened_event(strategy, intent, instrument, position_id))
    queues.events.get_nowait()
    queues.events.get_nowait()
    close_id = deterministic_client_order_id(intent.intent_id, "close")

    close_at_ms = NOW_MS + 10 + intent.max_holding_ms
    strategy.clock.set_time(close_at_ms * 1_000_000)
    strategy.on_timer(None)

    close, submitted_position_id, _client_id = strategy.submitted[-1]
    assert close.order_type == OrderType.MARKET
    assert close.side == OrderSide.SELL
    assert close.quantity.as_decimal() == Decimal("0.001")
    assert close.is_reduce_only is True
    assert close.client_order_id.value == close_id
    assert submitted_position_id == position_id
    assert strategy.canceled == []
    assert queues.events.get_nowait() == CloseSubmitted(
        intent_id=intent.intent_id,
        client_order_id=close_id,
        position_id=position_id.value,
        quantity=Decimal("0.001"),
        submitted_at_ms=close_at_ms,
    )


def test_active_close_keeps_stop_until_venue_zero_then_retires_it_before_final_flat() -> None:
    strategy, queues, intent, _instrument, position_id = _opened_strategy()
    entry = strategy.submitted[0][0]
    stop = strategy.submitted[-1][0]
    strategy.clock.set_time((NOW_MS + 10 + intent.max_holding_ms) * 1_000_000)
    strategy.on_timer(None)
    close = strategy.submitted[-1][0]
    queues.events.get_nowait()
    assert strategy.canceled == []
    strategy.on_order_filled(
        SimpleNamespace(client_order_id=entry.client_order_id, commission=Money(Decimal("0.04"), USDT))
    )
    strategy.on_order_filled(
        SimpleNamespace(client_order_id=close.client_order_id, commission=Money(Decimal("0.06"), USDT))
    )

    strategy.on_position_closed(
        SimpleNamespace(
            instrument_id=SOLUSDT_PERP,
            account_id=AccountId("BINANCE-001"),
            position_id=position_id,
            closing_order_id=close.client_order_id,
            quantity=_solusdt_perp_binance().make_qty(Decimal(0)),
            avg_px_close=10_100.0,
            realized_pnl=None,
            ts_closed=(NOW_MS + 50) * 1_000_000,
        )
    )
    assert isinstance(queues.events.get_nowait(), PositionClosedObserved)
    queues.commands.put_nowait(
        VenueFlatConfirmed(
            intent_id=intent.intent_id,
            instrument_id=SOLUSDT_PERP.value,
            position_id=position_id.value,
            authoritative_quantity=Decimal(0),
            verified_at_ms=NOW_MS + 51,
        )
    )
    strategy.on_timer(None)

    assert strategy.canceled == [close]
    assert queues.events.empty()
    strategy.on_order_canceled(
        SimpleNamespace(
            client_order_id=close.client_order_id,
            ts_event=(NOW_MS + 52) * 1_000_000,
        )
    )
    assert strategy.canceled == [close, stop]
    assert queues.events.empty()
    strategy.on_order_canceled(
        SimpleNamespace(
            client_order_id=stop.client_order_id,
            ts_event=(NOW_MS + 53) * 1_000_000,
        )
    )
    assert queues.events.get_nowait() == PositionFlatConfirmed(
        intent_id=intent.intent_id,
        position_id=position_id.value,
        authoritative_quantity=Decimal(0),
        avg_exit_price=Decimal("10100.0"),
        realized_pnl_amount=None,
        realized_pnl_currency=None,
        commissions_by_currency=None,
        closed_at_ms=NOW_MS + 50,
        flat_verified_at_ms=NOW_MS + 51,
    )


def test_failed_targeted_flat_query_is_manual_evidence_and_never_final_flat() -> None:
    strategy, queues, intent, instrument, position_id = _opened_strategy()
    close_at_ms = NOW_MS + 10 + intent.max_holding_ms
    strategy.clock.set_time(close_at_ms * 1_000_000)
    strategy.on_timer(None)
    close = strategy.submitted[-1][0]
    queues.events.get_nowait()
    strategy.on_position_closed(
        SimpleNamespace(
            instrument_id=SOLUSDT_PERP,
            account_id=AccountId("BINANCE-001"),
            position_id=position_id,
            closing_order_id=close.client_order_id,
            quantity=instrument.make_qty(Decimal(0)),
            avg_px_close=10_100.0,
            realized_pnl=None,
            ts_closed=(NOW_MS + 50) * 1_000_000,
        )
    )
    queues.events.get_nowait()

    queues.commands.put_nowait(
        VenueFlatUnproven(
            intent_id=intent.intent_id,
            position_id=position_id.value,
            observed_at_ms=NOW_MS + 51,
        )
    )
    strategy.on_timer(None)

    assert queues.events.get_nowait() == OrderOutcomeUnknown(
        intent_id=intent.intent_id,
        leg="close",
        observed_at_ms=NOW_MS + 51,
    )
    assert strategy.canceled == []
    assert queues.events.empty()
    assert len(strategy.flat_requests) == 1

    strategy.clock.set_time((close_at_ms + 4_999) * 1_000_000)
    strategy.on_timer(None)
    assert len(strategy.flat_requests) == 1

    strategy.clock.set_time((close_at_ms + 5_000) * 1_000_000)
    strategy.on_timer(None)
    assert len(strategy.flat_requests) == 2
    assert strategy.flat_requests[-1] == strategy.flat_requests[0]


def test_fresh_position_max_holding_closes_without_a_database_command() -> None:
    strategy, queues, intent = _fenced_strategy()
    instrument = _solusdt_perp_binance()
    position_id = PositionId("SOLUSDT-PERP.BINANCE-TRACEFOLD-001")
    opened_at_ms = NOW_MS + 10
    strategy.on_position_opened(
        _position_opened_event(strategy, intent, instrument, position_id, opened_at_ms=opened_at_ms)
    )
    queues.events.get_nowait()
    queues.events.get_nowait()

    strategy.clock.set_time((opened_at_ms + intent.max_holding_ms) * 1_000_000)
    strategy.on_timer(None)

    close = strategy.submitted[-1][0]
    close_id = deterministic_client_order_id(intent.intent_id, "close")
    assert close.client_order_id.value == close_id
    assert close.quantity.as_decimal() == Decimal("0.001")
    assert close.is_reduce_only is True
    assert strategy.canceled == []
    assert queues.events.get_nowait() == CloseSubmitted(
        intent_id=intent.intent_id,
        client_order_id=close_id,
        position_id=position_id.value,
        quantity=Decimal("0.001"),
        submitted_at_ms=opened_at_ms + intent.max_holding_ms,
    )


def test_fenced_intent_recovery_queries_the_reconciled_order_and_never_resubmits() -> None:
    strategy, queues = _registered_strategy()
    intent = _intent()
    entry_id = deterministic_client_order_id(intent.intent_id, "entry")
    instrument = _solusdt_perp_binance()
    entry = strategy.order_factory.market(
        instrument_id=SOLUSDT_PERP,
        order_side=OrderSide.BUY,
        quantity=instrument.make_qty(Decimal("0.001")),
        reduce_only=False,
        client_order_id=ClientOrderId(entry_id),
    )
    entry.apply(TestEventStubs.order_submitted(entry, account_id=AccountId("BINANCE-001"), ts_event=NOW_MS * 1_000_000))
    entry.apply(
        TestEventStubs.order_accepted(
            entry,
            account_id=AccountId("BINANCE-001"),
            venue_order_id=VenueOrderId("entry-venue-1"),
            ts_event=(NOW_MS + 1) * 1_000_000,
        )
    )
    strategy.cache.add_order(entry, client_id=ClientId("BINANCE"))
    fenced = _outcome(
        intent,
        execution_state="IN_FLIGHT",
        execution_phase="ENTRY",
        engine_identity="nt-v1",
        entry_client_order_id=entry_id,
        entry_fenced_at_ms=NOW_MS,
    )

    queues.commands.put_nowait(AdoptIntent(intent=intent, outcome=fenced))
    strategy.on_timer(None)

    assert strategy.queried == [entry]
    assert strategy.submitted == []
    assert queues.events.empty()


def test_position_closed_callback_reports_observation_without_claiming_reconciled_flat() -> None:
    strategy, queues, intent = _fenced_strategy()
    instrument = _solusdt_perp_binance()
    position_id = PositionId("SOLUSDT-PERP.BINANCE-TRACEFOLD-001")
    strategy.on_position_opened(_position_opened_event(strategy, intent, instrument, position_id))
    queues.events.get_nowait()
    queues.events.get_nowait()
    stop = strategy.submitted[-1][0]

    strategy.on_position_closed(
        SimpleNamespace(
            instrument_id=SOLUSDT_PERP,
            account_id=AccountId("BINANCE-001"),
            position_id=position_id,
            closing_order_id=stop.client_order_id,
            quantity=instrument.make_qty(Decimal("0")),
            avg_px_close=10_100.0,
            realized_pnl=None,
            ts_closed=(NOW_MS + 50) * 1_000_000,
        )
    )

    assert queues.events.get_nowait() == PositionClosedObserved(
        intent_id=intent.intent_id,
        instrument_id=SOLUSDT_PERP.value,
        account_id="BINANCE-001",
        position_id=position_id.value,
        closing_client_order_id=stop.client_order_id.value,
        local_quantity=Decimal("0.000"),
        avg_exit_price=Decimal("10100.0"),
        realized_pnl_amount=None,
        realized_pnl_currency=None,
        commissions_by_currency=None,
        closed_at_ms=NOW_MS + 50,
    )
    assert strategy.flat_requests == [
        VenueFlatProofRequested(
            intent_id=intent.intent_id,
            instrument_id=SOLUSDT_PERP.value,
            account_id="BINANCE-001",
            position_id=position_id.value,
            closing_client_order_id=stop.client_order_id.value,
            observed_at_ms=NOW_MS + 50,
        )
    ]
    assert queues.events.empty()

    queues.commands.put_nowait(
        VenueFlatConfirmed(
            intent_id=intent.intent_id,
            instrument_id=SOLUSDT_PERP.value,
            position_id=position_id.value,
            authoritative_quantity=Decimal(0),
            verified_at_ms=NOW_MS + 51,
        )
    )
    strategy.on_timer(None)

    assert strategy.canceled == [stop]
    assert queues.events.empty()
    strategy.on_order_canceled(
        SimpleNamespace(
            client_order_id=stop.client_order_id,
            ts_event=(NOW_MS + 52) * 1_000_000,
        )
    )
    assert queues.events.get_nowait() == PositionFlatConfirmed(
        intent_id=intent.intent_id,
        position_id=position_id.value,
        authoritative_quantity=Decimal(0),
        avg_exit_price=Decimal("10100.0"),
        realized_pnl_amount=None,
        realized_pnl_currency=None,
        commissions_by_currency=None,
        closed_at_ms=NOW_MS + 50,
        flat_verified_at_ms=NOW_MS + 51,
    )
    second = _intent(case_id="case-2")
    queues.commands.put_nowait(AdoptIntent(intent=second, outcome=_outcome(second)))
    strategy.on_timer(None)
    assert isinstance(queues.events.get_nowait(), EntryFenceRequested)


def test_restart_adopts_reconciled_position_and_stop_when_the_db_projection_lags() -> None:
    strategy, queues = _registered_strategy()
    intent = _intent()
    instrument = _solusdt_perp_binance()
    position_id = PositionId("SOLUSDT-PERP.BINANCE-TRACEFOLD-001")
    entry_id = deterministic_client_order_id(intent.intent_id, "entry")
    stop_id = deterministic_client_order_id(intent.intent_id, "stop")
    entry = strategy.order_factory.market(
        instrument_id=SOLUSDT_PERP,
        order_side=OrderSide.BUY,
        quantity=instrument.make_qty(Decimal("0.001")),
        client_order_id=ClientOrderId(entry_id),
    )
    entry.apply(TestEventStubs.order_submitted(entry, account_id=AccountId("BINANCE-001"), ts_event=NOW_MS * 1_000_000))
    entry.apply(
        TestEventStubs.order_accepted(
            entry,
            account_id=AccountId("BINANCE-001"),
            venue_order_id=VenueOrderId("entry-venue-1"),
            ts_event=(NOW_MS + 1) * 1_000_000,
        )
    )
    fill = TestEventStubs.order_filled(
        order=entry,
        instrument=instrument,
        strategy_id=strategy.id,
        account_id=AccountId("BINANCE-001"),
        venue_order_id=VenueOrderId("entry-venue-1"),
        position_id=position_id,
        last_qty=instrument.make_qty(Decimal("0.001")),
        last_px=instrument.make_price(Decimal("10000")),
        commission=Money(0, USDT),
        ts_event=(NOW_MS + 10) * 1_000_000,
    )
    position = Position(instrument, fill)
    entry.apply(fill)
    stop = strategy.order_factory.stop_market(
        instrument_id=SOLUSDT_PERP,
        order_side=OrderSide.SELL,
        quantity=instrument.make_qty(Decimal("0.001")),
        trigger_price=instrument.make_price(Decimal("9800")),
        trigger_type=TriggerType.MARK_PRICE,
        reduce_only=True,
        client_order_id=ClientOrderId(stop_id),
    )
    # Startup reconciliation rebuilds an external accepted order without the original
    # OrderSubmitted event, so ``order.account_id`` is absent even though the accepted
    # venue event proves the account.
    stop.apply(
        TestEventStubs.order_accepted(
            stop,
            account_id=AccountId("BINANCE-001"),
            venue_order_id=VenueOrderId("stop-venue-1"),
            ts_event=(NOW_MS + 12) * 1_000_000,
        )
    )
    assert stop.account_id is None
    assert stop.last_event.account_id == AccountId("BINANCE-001")
    strategy.cache.add_order(entry, position_id=position_id, client_id=ClientId("BINANCE"))
    strategy.cache.add_order(stop, position_id=position_id, client_id=ClientId("BINANCE"))
    strategy.cache.add_position(position, OmsType.NETTING)
    strategy.on_start()
    assert queues.events.get_nowait() == ReadinessChanged(False, "external_exposure", True)
    fenced = _outcome(
        intent,
        execution_state="IN_FLIGHT",
        execution_phase="ENTRY",
        engine_identity="nt-v1",
        entry_client_order_id=entry_id,
        entry_fenced_at_ms=NOW_MS,
    )

    queues.commands.put_nowait(AdoptIntent(intent=intent, outcome=fenced))
    strategy.on_timer(None)

    assert queues.events.get_nowait() == EntryFilled(
        intent_id=intent.intent_id,
        actual_quantity=Decimal("0.001"),
        avg_entry_price=Decimal("10000.0"),
        position_id=position_id.value,
        opened_at_ms=NOW_MS + 10,
    )
    recovered_stop = queues.events.get_nowait()
    assert isinstance(recovered_stop, StopSubmitted)
    assert recovered_stop.client_order_id == stop_id
    assert recovered_stop.quantity == Decimal("0.001")
    assert queues.events.get_nowait() == StopAccepted(
        intent_id=intent.intent_id,
        client_order_id=stop_id,
        venue_order_id="stop-venue-1",
        quantity=Decimal("0.001"),
        trigger_price=Decimal("9800.0"),
        accepted_at_ms=NOW_MS + 12,
    )
    assert queues.events.get_nowait() == ReadinessChanged(True, "ready", False)
    assert strategy.queried == [entry, stop]
    assert strategy.submitted == []
    strategy.on_timer(None)
    assert queues.events.empty()


@pytest.mark.parametrize("restart_after_deadline", [False, True], ids=["before-deadline", "after-deadline"])
def test_open_protected_recovery_rebuilds_the_original_max_holding_deadline(
    restart_after_deadline: bool,
) -> None:
    strategy, queues = _registered_strategy()
    intent = _intent()
    instrument = _solusdt_perp_binance()
    position_id = PositionId("SOLUSDT-PERP.BINANCE-TRACEFOLD-001")
    entry_id = deterministic_client_order_id(intent.intent_id, "entry")
    stop_id = deterministic_client_order_id(intent.intent_id, "stop")
    entry = strategy.order_factory.market(
        instrument_id=SOLUSDT_PERP,
        order_side=OrderSide.BUY,
        quantity=instrument.make_qty(Decimal("0.001")),
        client_order_id=ClientOrderId(entry_id),
    )
    entry.apply(TestEventStubs.order_submitted(entry, account_id=AccountId("BINANCE-001"), ts_event=NOW_MS * 1_000_000))
    entry.apply(
        TestEventStubs.order_accepted(
            entry,
            account_id=AccountId("BINANCE-001"),
            venue_order_id=VenueOrderId("entry-venue-1"),
            ts_event=(NOW_MS + 1) * 1_000_000,
        )
    )
    fill = TestEventStubs.order_filled(
        order=entry,
        instrument=instrument,
        strategy_id=strategy.id,
        account_id=AccountId("BINANCE-001"),
        venue_order_id=VenueOrderId("entry-venue-1"),
        position_id=position_id,
        last_qty=instrument.make_qty(Decimal("0.001")),
        last_px=instrument.make_price(Decimal("10000")),
        commission=Money(0, USDT),
        ts_event=(NOW_MS + 10) * 1_000_000,
    )
    position = Position(instrument, fill)
    entry.apply(fill)
    stop = strategy.order_factory.stop_market(
        instrument_id=SOLUSDT_PERP,
        order_side=OrderSide.SELL,
        quantity=instrument.make_qty(Decimal("0.001")),
        trigger_price=instrument.make_price(Decimal("9800")),
        trigger_type=TriggerType.MARK_PRICE,
        reduce_only=True,
        client_order_id=ClientOrderId(stop_id),
    )
    stop.apply(
        TestEventStubs.order_submitted(
            stop,
            account_id=AccountId("BINANCE-001"),
            ts_event=(NOW_MS + 11) * 1_000_000,
        )
    )
    stop.apply(
        TestEventStubs.order_accepted(
            stop,
            account_id=AccountId("BINANCE-001"),
            venue_order_id=VenueOrderId("stop-venue-1"),
            ts_event=(NOW_MS + 12) * 1_000_000,
        )
    )
    strategy.cache.add_order(entry, position_id=position_id, client_id=ClientId("BINANCE"))
    strategy.cache.add_order(stop, position_id=position_id, client_id=ClientId("BINANCE"))
    strategy.cache.add_position(position, OmsType.NETTING)
    strategy.on_start()
    assert queues.events.get_nowait() == ReadinessChanged(
        ready=False,
        reason="external_exposure",
        unexpected_exposure=True,
    )
    protected = _outcome(
        intent,
        execution_state="OPEN_PROTECTED",
        execution_phase="PROTECTION",
        engine_identity="nt-v1",
        entry_client_order_id=entry_id,
        entry_fenced_at_ms=NOW_MS,
        stop_client_order_id=stop_id,
        stop_generation=0,
        stop_submitted_at_ms=NOW_MS + 11,
        actual_quantity=Decimal("0.001"),
        protected_quantity=Decimal("0.001"),
        avg_entry_price=Decimal("10000"),
        position_id=position_id.value,
        protection_order_id="stop-venue-1",
        stop_price=Decimal("9800"),
        opened_at_ms=NOW_MS + 10,
        protected_at_ms=NOW_MS + 12,
    )
    close_deadline_ms = NOW_MS + 10 + intent.max_holding_ms
    if restart_after_deadline:
        strategy.clock.set_time((close_deadline_ms + 1) * 1_000_000)

    queues.commands.put_nowait(AdoptIntent(intent=intent, outcome=protected))
    strategy.on_timer(None)

    assert strategy.queried == [entry, stop]
    events = []
    while not queues.events.empty():
        events.append(queues.events.get_nowait())
    assert (
        ReadinessChanged(
            ready=True,
            reason="ready",
            unexpected_exposure=False,
        )
        in events
    )

    close_id = deterministic_client_order_id(intent.intent_id, "close")
    if restart_after_deadline:
        assert not any(isinstance(event, OrderOutcomeUnknown) for event in events)
        assert any(isinstance(event, CloseSubmitted) for event in events)
        assert len(strategy.submitted) == 1
        close = strategy.submitted[0][0]
        assert close.client_order_id.value == close_id
        assert close.quantity.as_decimal() == Decimal("0.001")
        assert close.is_reduce_only
        assert strategy.canceled == []
        strategy.on_timer(None)
        assert len(strategy.submitted) == 1
        return

    assert strategy.submitted == []

    strategy.on_timer(None)
    assert queues.events.empty()

    strategy.clock.set_time(close_deadline_ms * 1_000_000)
    strategy.on_timer(None)

    assert strategy.submitted[-1][0].client_order_id.value == close_id
    assert strategy.submitted[-1][0].quantity.as_decimal() == Decimal("0.001")
    assert strategy.canceled == []


@pytest.mark.parametrize(
    ("entry_trade_id", "close_trade_id", "expected_commissions"),
    [
        ("101", "102", {"USDT": "0.10"}),
        (
            "de992c68-4aab-5c60-8c81-97c282891da4",
            "41aa1e12-47fe-5eeb-a1e4-79056efec6f3",
            None,
        ),
    ],
)
def test_restart_finds_the_deterministic_close_before_deciding_to_submit_again(
    entry_trade_id: str,
    close_trade_id: str,
    expected_commissions: dict[str, str] | None,
) -> None:
    strategy, queues = _registered_strategy()
    intent = _intent()
    instrument = _solusdt_perp_binance()
    position_id = PositionId("SOLUSDT-PERP.BINANCE-TRACEFOLD-001")
    entry_id = deterministic_client_order_id(intent.intent_id, "entry")
    stop_id = deterministic_client_order_id(intent.intent_id, "stop")
    close_id = deterministic_client_order_id(intent.intent_id, "close")

    entry = strategy.order_factory.market(
        instrument_id=SOLUSDT_PERP,
        order_side=OrderSide.BUY,
        quantity=instrument.make_qty(Decimal("0.001")),
        client_order_id=ClientOrderId(entry_id),
    )
    entry.apply(TestEventStubs.order_submitted(entry, account_id=AccountId("BINANCE-001"), ts_event=NOW_MS * 1_000_000))
    entry.apply(
        TestEventStubs.order_accepted(
            entry,
            account_id=AccountId("BINANCE-001"),
            venue_order_id=VenueOrderId("entry-venue-1"),
            ts_event=(NOW_MS + 1) * 1_000_000,
        )
    )
    entry_fill = TestEventStubs.order_filled(
        order=entry,
        instrument=instrument,
        strategy_id=strategy.id,
        account_id=AccountId("BINANCE-001"),
        venue_order_id=VenueOrderId("entry-venue-1"),
        trade_id=TradeId(entry_trade_id),
        position_id=position_id,
        last_qty=instrument.make_qty(Decimal("0.001")),
        last_px=instrument.make_price(Decimal("10000")),
        commission=Money(Decimal("0.04"), USDT),
        ts_event=(NOW_MS + 2) * 1_000_000,
    )
    position = Position(instrument, entry_fill)
    entry.apply(entry_fill)

    stop = strategy.order_factory.stop_market(
        instrument_id=SOLUSDT_PERP,
        order_side=OrderSide.SELL,
        quantity=instrument.make_qty(Decimal("0.001")),
        trigger_price=instrument.make_price(Decimal("9800")),
        trigger_type=TriggerType.MARK_PRICE,
        reduce_only=True,
        client_order_id=ClientOrderId(stop_id),
    )
    stop.apply(
        TestEventStubs.order_submitted(
            stop,
            account_id=AccountId("BINANCE-001"),
            ts_event=(NOW_MS + 3) * 1_000_000,
        )
    )
    stop.apply(
        TestEventStubs.order_accepted(
            stop,
            account_id=AccountId("BINANCE-001"),
            venue_order_id=VenueOrderId("stop-venue-1"),
            ts_event=(NOW_MS + 4) * 1_000_000,
        )
    )
    close = strategy.order_factory.market(
        instrument_id=SOLUSDT_PERP,
        order_side=OrderSide.SELL,
        quantity=instrument.make_qty(Decimal("0.001")),
        reduce_only=True,
        client_order_id=ClientOrderId(close_id),
    )
    close.apply(
        TestEventStubs.order_submitted(
            close,
            account_id=AccountId("BINANCE-001"),
            ts_event=(NOW_MS + 5) * 1_000_000,
        )
    )
    close.apply(
        TestEventStubs.order_accepted(
            close,
            account_id=AccountId("BINANCE-001"),
            venue_order_id=VenueOrderId("close-venue-1"),
            ts_event=(NOW_MS + 6) * 1_000_000,
        )
    )
    close_fill = TestEventStubs.order_filled(
        order=close,
        instrument=instrument,
        strategy_id=strategy.id,
        account_id=AccountId("BINANCE-001"),
        venue_order_id=VenueOrderId("close-venue-1"),
        trade_id=TradeId(close_trade_id),
        position_id=position_id,
        last_qty=instrument.make_qty(Decimal("0.001")),
        last_px=instrument.make_price(Decimal("10100")),
        side=OrderSide.SELL,
        commission=Money(Decimal("0.06"), USDT),
        ts_event=(NOW_MS + 7) * 1_000_000,
    )
    close.apply(close_fill)
    position.apply(close_fill)
    assert position.is_closed

    strategy.cache.add_order(entry, position_id=position_id, client_id=ClientId("BINANCE"))
    strategy.cache.add_order(stop, position_id=position_id, client_id=ClientId("BINANCE"))
    strategy.cache.add_order(close, position_id=position_id, client_id=ClientId("BINANCE"))
    strategy.cache.add_position(position, OmsType.NETTING)
    fenced = _outcome(
        intent,
        execution_state="IN_FLIGHT",
        execution_phase="ENTRY",
        engine_identity="nt-v1",
        entry_client_order_id=entry_id,
        entry_fenced_at_ms=NOW_MS,
    )

    queues.commands.put_nowait(AdoptIntent(intent=intent, outcome=fenced))
    strategy.on_timer(None)

    events = []
    while not queues.events.empty():
        events.append(queues.events.get_nowait())
    assert events, events
    assert [type(event) for event in events] == [
        EntryFilled,
        StopSubmitted,
        StopAccepted,
        CloseSubmitted,
        PositionClosedObserved,
    ]
    assert events[3].client_order_id == close_id
    assert events[4].commissions_by_currency == expected_commissions
    assert strategy.queried == [entry, stop, close]
    assert strategy.submitted == []
    assert strategy.flat_requests[-1].closing_client_order_id == close_id

    queues.commands.put_nowait(
        VenueFlatConfirmed(
            intent_id=intent.intent_id,
            instrument_id=SOLUSDT_PERP.value,
            position_id=position_id.value,
            authoritative_quantity=Decimal(0),
            verified_at_ms=NOW_MS + 8,
        )
    )
    strategy.on_timer(None)
    assert strategy.canceled == [stop]
    strategy.on_order_canceled(
        SimpleNamespace(
            client_order_id=stop.client_order_id,
            ts_event=(NOW_MS + 9) * 1_000_000,
        )
    )
    assert queues.events.get_nowait().commissions_by_currency == expected_commissions
