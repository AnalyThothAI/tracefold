"""Nautilus 1.231.0 reconciliation must never close a position the venue still holds (#680 PR-3).

On 2026-09-23 an open Binance Demo APTUSDT long (1188.3) was closed in the Nautilus Cache twice while
the venue still held it, and the Runtime then canceled its stop and take-profit. These tests replay both
paths offline on the real pieces: a real `LiveExecutionEngine` and Cache, the production execution
engine configuration, and the real Binance USD-M execution client -- the one the Runtime builds --
whose HTTP transport answers from this file instead of Binance.

* Path A (05:57): a user-data re-subscribe asks for a full mass status. The adapter asks Binance for
  the trades of `APTUSDT-PERP` and of `APTUSDT` -- one market, two spellings -- and returns every fill
  twice; the mass-status fill adjustment replays 2376.6 against the venue's 1188.3 and adds a synthetic
  SELL that closes the position. The Runtime's client names each venue trade once.
* Path B (12:19): positionRisk answers `-1021`, the adapter swallows it and reports no position, and
  the 5 s position check closed the position as flat. The production engine no longer generates an
  order to match a position report.

The engine's own coroutines (`_check_positions_consistency`) are driven directly: they are what its
five-second timer runs, and no public entry point runs one pass of them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import msgspec
import pytest
from nautilus_trader.adapters.binance import BINANCE, BinanceLiveExecClientFactory
from nautilus_trader.adapters.binance.http.client import BinanceHttpClient
from nautilus_trader.adapters.binance.http.error import BinanceClientError
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.config import StrategyConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import GenerateFillReports, GeneratePositionStatusReports, ModifyOrder
from nautilus_trader.execution.reports import ExecutionMassStatus, OrderStatusReport, PositionStatusReport
from nautilus_trader.live.execution_engine import LiveExecutionEngine
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.enums import (
    AccountType,
    OmsType,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    TimeInForce,
    TriggerType,
)
from nautilus_trader.model.events import AccountState
from nautilus_trader.model.identifiers import (
    ClientId,
    ClientOrderId,
    InstrumentId,
    PositionId,
    Symbol,
    TradeId,
    TraderId,
    Venue,
    VenueOrderId,
)
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import AccountBalance, Currency, Money, Price, Quantity
from nautilus_trader.model.position import Position
from nautilus_trader.portfolio.portfolio import Portfolio
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.trading.strategy import Strategy

from tests.nautilus_oi_runtime_fixtures import oi_profile
from tracefold.integrations.nautilus.oi_runtime.binance import OiBinanceExecClientFactory
from tracefold.integrations.nautilus.oi_runtime.config import BinanceRuntimeCredentials, build_oi_node_config

APT = InstrumentId.from_str("APTUSDT-PERP.BINANCE")
INSTRUMENT = CryptoPerpetual(
    instrument_id=APT,
    raw_symbol=Symbol("APTUSDT"),
    base_currency=Currency.from_str("USDT"),
    quote_currency=USDT,
    settlement_currency=USDT,
    is_inverse=False,
    price_precision=4,
    size_precision=1,
    price_increment=Price.from_str("0.0001"),
    size_increment=Quantity.from_str("0.1"),
    ts_event=0,
    ts_init=0,
)
TRADER = TraderId("OI-F46FB62A731F")
# The venue's timestamps of the 05:01:36.929 entry, and its order and trade ids.
FILL_MS = 1_790_139_696_929
FILL_NS = FILL_MS * 1_000_000
ENTRY_ORDER_ID = 478_532_087
ENTRY_TRADES = ((62_685_281, "950.7"), (62_685_282, "237.6"))
PRIOR_ROUND_TRIP = (
    (400_000_001, 500_000_001, "BUY", FILL_MS - 86_400_000),
    (400_000_002, 500_000_002, "SELL", FILL_MS - 86_000_000),
)
ENTRY_ID = ClientOrderId("tf8669471b453bb889fc5d6e627cc6e8")
STOP_ID = ClientOrderId("tf3e81d410310d57486ffcddc2d83622")
TAKE_PROFIT_ID = ClientOrderId("tfabeba1516dd961017381d76b1f67cd")
PROTECTION = (
    (STOP_ID, 1_000_000_215_275_001, OrderType.STOP_MARKET, "0.8310"),
    (TAKE_PROFIT_ID, 1_000_000_215_275_002, OrderType.MARKET_IF_TOUCHED, "0.8562"),
)
TAKE_PROFIT_CHILD_ORDER_ID = 308_654_865
TAKE_PROFIT_CHILD_TRADE_ID = 63_772_472


class _Venue:
    """Binance's side of every signed request the adapter makes, as the APT account stood."""

    def __init__(
        self,
        *,
        position_risk_error: bool = False,
        prior_round_trip: bool = False,
        triggered_take_profit: bool = False,
        wrong_algo_child: bool = False,
        missing_child_trade: bool = False,
    ) -> None:
        self.position_risk_error = position_risk_error
        self.prior_round_trip = prior_round_trip
        self.triggered_take_profit = triggered_take_profit
        self.wrong_algo_child = wrong_algo_child
        self.missing_child_trade = missing_child_trade
        self.user_trade_symbols: list[str] = []
        self.position_amount = "0" if triggered_take_profit else "1188.3"

    async def send_request(
        self, _client: Any, _method: Any, url_path: str, payload: dict[str, str] | None = None, **_: Any
    ) -> bytes:
        params = payload or {}
        if url_path.endswith("/positionRisk"):
            if self.position_risk_error:
                raise BinanceClientError(
                    status=400,
                    message={"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."},
                    headers={},
                )
            return msgspec.json.encode([_position_risk("APTUSDT", self.position_amount)])
        if url_path.endswith("/userTrades"):
            self.user_trade_symbols.append(params["symbol"])
            trades = [_trade(trade_id, ENTRY_ORDER_ID, "BUY", qty, FILL_MS) for trade_id, qty in ENTRY_TRADES]
            if self.prior_round_trip:
                trades = [
                    _trade(trade_id, order_id, side, "500.0", at_ms, price="0.9000")
                    for order_id, trade_id, side, at_ms in PRIOR_ROUND_TRIP
                ] + trades
            if self.triggered_take_profit and not self.missing_child_trade:
                trades.append(
                    _trade(
                        TAKE_PROFIT_CHILD_TRADE_ID,
                        TAKE_PROFIT_CHILD_ORDER_ID,
                        "SELL",
                        "1188.3",
                        FILL_MS + 2_000,
                        price="0.8562",
                    )
                )
            return msgspec.json.encode(trades)
        if self.triggered_take_profit:
            if url_path.endswith("/openOrders") or url_path.endswith("/openAlgoOrders"):
                return msgspec.json.encode([])
            if url_path.endswith("/allOrders"):
                return msgspec.json.encode(
                    [
                        _binance_order(ENTRY_ORDER_ID, ENTRY_ID.value, "BUY", "0.8394", FILL_MS),
                        _binance_order(
                            TAKE_PROFIT_CHILD_ORDER_ID,
                            TAKE_PROFIT_ID.value,
                            "SELL",
                            "0.8562",
                            FILL_MS + 2_000,
                        ),
                    ]
                )
            if url_path.endswith("/allAlgoOrders"):
                return msgspec.json.encode([])
            if url_path.endswith("/algoOrder"):
                assert params["algoId"] == PROTECTION[1][1]
                return msgspec.json.encode(
                    {
                        "algoId": PROTECTION[1][1],
                        "clientAlgoId": TAKE_PROFIT_ID.value,
                        "algoType": "CONDITIONAL",
                        "orderType": "TAKE_PROFIT_MARKET",
                        "symbol": "APTUSDT",
                        "side": "SELL",
                        "positionSide": "BOTH",
                        "reduceOnly": True,
                        "workingType": "MARK_PRICE",
                        "quantity": "1188.3",
                        "triggerPrice": "0.8562",
                        "algoStatus": "FINISHED",
                        "actualOrderId": str(TAKE_PROFIT_CHILD_ORDER_ID + int(self.wrong_algo_child)),
                        "triggerTime": FILL_MS + 2_000,
                    }
                )
        raise AssertionError(f"unexpected Binance request {url_path}")


def _position_risk(symbol: str, amount: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "positionSide": "BOTH",
        "positionAmt": amount,
        "entryPrice": "0.8394",
        "markPrice": "0.8400",
        "unRealizedProfit": "0",
        "liquidationPrice": "0",
        "isolatedMargin": "0",
        "updateTime": FILL_MS,
    }


def _trade(trade_id: int, order_id: int, side: str, qty: str, at_ms: int, *, price: str = "0.8394") -> dict[str, Any]:
    return {
        "symbol": "APTUSDT",
        "id": trade_id,
        "orderId": order_id,
        "side": side,
        "price": price,
        "qty": qty,
        "quoteQty": "0",
        "commission": "0.1",
        "commissionAsset": "USDT",
        "time": at_ms,
        "buyer": side == "BUY",
        "maker": False,
        "realizedPnl": "0",
        "positionSide": "BOTH",
    }


def _binance_order(order_id: int, client_order_id: str, side: str, price: str, at_ms: int) -> dict[str, Any]:
    return {
        "symbol": "APTUSDT",
        "orderId": order_id,
        "clientOrderId": client_order_id,
        "price": "0",
        "origQty": "1188.3",
        "executedQty": "1188.3",
        "status": "FILLED",
        "timeInForce": "GTC",
        "type": "MARKET",
        "side": side,
        "stopPrice": "0",
        "time": at_ms,
        "updateTime": at_ms,
        "avgPrice": price,
        "reduceOnly": side == "SELL",
        "positionSide": "BOTH",
    }


class _Recorder(Strategy):
    """The Runtime's claim on APT, with nothing but a record of what Nautilus told it."""

    def __init__(self) -> None:
        super().__init__(
            StrategyConfig(
                strategy_id="OI-RUNTIME", order_id_tag="F46", oms_type="NETTING", external_order_claims=[APT]
            )
        )
        self.closed: list[str] = []
        self.filled: list[str] = []

    def on_order_filled(self, event: Any) -> None:
        self.filled.append(str(event.client_order_id))

    def on_position_closed(self, event: Any) -> None:
        self.closed.append(str(event.closing_order_id))


class _Account:
    """One real execution engine, Cache and Binance client, holding the 05:01:37 APT state."""

    def __init__(self, *, factory: Any, venue: _Venue, generate_missing_orders: bool | None = None) -> None:
        self.loop = asyncio.new_event_loop()
        self.clock = LiveClock()
        self.msgbus = MessageBus(TRADER, self.clock)
        self.cache = Cache(database=None)
        self.portfolio = Portfolio(self.msgbus, self.cache, self.clock)
        profile = oi_profile("paper")
        node = build_oi_node_config(profile, BinanceRuntimeCredentials("paper-key", "paper-secret"))
        engine_config = node.exec_engine
        if generate_missing_orders is not None:
            engine_config = msgspec.structs.replace(engine_config, generate_missing_orders=generate_missing_orders)
        self.engine = LiveExecutionEngine(
            loop=self.loop, msgbus=self.msgbus, cache=self.cache, clock=self.clock, config=engine_config
        )
        if venue.triggered_take_profit:
            # Historical fixture timestamps must remain in the full report as time passes.
            self.engine.reconciliation_lookback_mins = 0
        self.client = factory.create(
            loop=self.loop,
            name=BINANCE,
            config=node.exec_clients[BINANCE],
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
        )
        self.engine.register_client(self.client)
        self.cache.add_instrument(INSTRUMENT)
        self.account_id = self.client.account_id
        self.portfolio.update_account(
            AccountState(
                account_id=self.account_id,
                account_type=AccountType.MARGIN,
                base_currency=None,
                reported=True,
                balances=[AccountBalance(Money(5_000, USDT), Money(0, USDT), Money(5_000, USDT))],
                margins=[],
                info={},
                event_id=UUID4(),
                ts_event=0,
                ts_init=0,
            )
        )
        self.strategy = _Recorder()
        self.strategy.register(
            trader_id=TRADER, portfolio=self.portfolio, msgbus=self.msgbus, cache=self.cache, clock=self.clock
        )
        self.engine.register_oms_type(self.strategy)
        self.engine.register_external_order_claims(self.strategy)
        self.strategy.start()
        self.venue = venue
        self._hold_the_apt_long()

    def _hold_the_apt_long(self) -> None:
        """The entry filled in two trades, and its reduce-only stop and take-profit rest on the venue."""

        entry = self.strategy.order_factory.market(
            instrument_id=APT, order_side=OrderSide.BUY, quantity=Quantity.from_str("1188.3"), client_order_id=ENTRY_ID
        )
        self.cache.add_order(entry, None, ClientId(BINANCE))
        entry.apply(TestEventStubs.order_submitted(entry, account_id=self.account_id, ts_event=FILL_NS - 300_000_000))
        entry.apply(
            TestEventStubs.order_accepted(
                entry, account_id=self.account_id, venue_order_id=VenueOrderId(str(ENTRY_ORDER_ID)), ts_event=FILL_NS
            )
        )
        position_id = PositionId(f"{APT}-{self.strategy.id}")
        position: Position | None = None
        for trade_id, qty in ENTRY_TRADES:
            fill = TestEventStubs.order_filled(
                order=entry,
                instrument=INSTRUMENT,
                strategy_id=self.strategy.id,
                account_id=self.account_id,
                venue_order_id=VenueOrderId(str(ENTRY_ORDER_ID)),
                trade_id=TradeId(str(trade_id)),
                position_id=position_id,
                last_qty=Quantity.from_str(qty),
                last_px=Price.from_str("0.8394"),
                commission=Money("0.1", USDT),
                ts_event=FILL_NS,
            )
            entry.apply(fill)
            if position is None:
                position = Position(INSTRUMENT, fill)
                self.cache.add_position(position, OmsType.NETTING)
            else:
                position.apply(fill)
                self.cache.update_position(position)
        self.cache.update_order(entry)
        for client_order_id, venue_order_id, order_type, trigger in PROTECTION:
            create = (
                self.strategy.order_factory.stop_market
                if order_type == OrderType.STOP_MARKET
                else self.strategy.order_factory.market_if_touched
            )
            order = create(
                instrument_id=APT,
                order_side=OrderSide.SELL,
                quantity=Quantity.from_str("1188.3"),
                trigger_price=Price.from_str(trigger),
                trigger_type=TriggerType.MARK_PRICE,
                reduce_only=True,
                client_order_id=client_order_id,
            )
            self.cache.add_order(order, position_id, ClientId(BINANCE))
            order.apply(TestEventStubs.order_submitted(order, account_id=self.account_id, ts_event=FILL_NS + 1))
            order.apply(
                TestEventStubs.order_accepted(
                    order,
                    account_id=self.account_id,
                    venue_order_id=VenueOrderId(str(venue_order_id)),
                    ts_event=FILL_NS + 300_000_000,
                )
            )
            self.cache.update_order(order)

    def fill_reports(self) -> list[Any]:
        """The adapter's answer to the mass status a user-data re-subscribe requests (no lookback)."""

        command = GenerateFillReports(
            instrument_id=None,
            venue_order_id=None,
            start=None,
            end=None,
            command_id=UUID4(),
            ts_init=self.clock.timestamp_ns(),
        )
        return list(self.loop.run_until_complete(self.client.generate_fill_reports(command)))

    def resubscribe_mass_status(self) -> None:
        """Path A: `_reconcile_after_resubscribe`'s full mass status, with the adapter's own fill reports."""

        now_ns = self.clock.timestamp_ns()
        status = ExecutionMassStatus(
            client_id=ClientId(BINANCE),
            account_id=self.account_id,
            venue=Venue(BINANCE),
            report_id=UUID4(),
            ts_init=now_ns,
        )
        status.add_order_reports(
            [
                _order_report(
                    self.account_id,
                    ENTRY_ID,
                    ENTRY_ORDER_ID,
                    OrderSide.BUY,
                    OrderType.MARKET,
                    OrderStatus.FILLED,
                    "1188.3",
                    FILL_NS,
                    now_ns,
                ),
                *[
                    _order_report(
                        self.account_id,
                        client_order_id,
                        venue_order_id,
                        OrderSide.SELL,
                        order_type,
                        OrderStatus.ACCEPTED,
                        "0",
                        FILL_NS + 300_000_000,
                        now_ns,
                        trigger=trigger,
                    )
                    for client_order_id, venue_order_id, order_type, trigger in PROTECTION
                ],
            ]
        )
        if self.venue.prior_round_trip:
            status.add_order_reports(
                [
                    _order_report(
                        self.account_id,
                        ClientOrderId(f"old{order_id}"),
                        order_id,
                        OrderSide.BUY if side == "BUY" else OrderSide.SELL,
                        OrderType.MARKET,
                        OrderStatus.FILLED,
                        "500.0",
                        at_ms * 1_000_000,
                        now_ns,
                        price="0.9000",
                    )
                    for order_id, _trade_id, side, at_ms in PRIOR_ROUND_TRIP
                ]
            )
        status.add_fill_reports(self.fill_reports())
        status.add_position_reports([self.venue_long()])
        self.engine.reconcile_execution_mass_status(status)

    def venue_long(self) -> PositionStatusReport:
        now_ns = self.clock.timestamp_ns()
        return PositionStatusReport(
            account_id=self.account_id,
            instrument_id=APT,
            position_side=PositionSide.LONG,
            quantity=Quantity.from_str("1188.3"),
            report_id=UUID4(),
            ts_last=now_ns,
            ts_init=now_ns,
            avg_px_open=Decimal("0.8394"),
        )

    def position_checks(self, count: int = 5) -> None:
        """What the engine's 5 s timer runs: the production position check, `count` times."""

        for _ in range(count):
            self.loop.run_until_complete(self.engine._check_positions_consistency())

    def open_positions(self) -> list[tuple[str, str]]:
        return [(position.id.value, str(position.signed_qty)) for position in self.cache.positions_open()]

    def open_protection(self) -> list[str]:
        return sorted(order.client_order_id.value for order in self.cache.orders_open(instrument_id=APT))

    def close(self) -> None:
        self.strategy.stop()
        self.loop.close()


def _order_report(
    account_id: Any,
    client_order_id: ClientOrderId,
    venue_order_id: int,
    side: OrderSide,
    order_type: OrderType,
    status: OrderStatus,
    filled: str,
    at_ns: int,
    now_ns: int,
    *,
    trigger: str | None = None,
    price: str = "0.8394",
) -> OrderStatusReport:
    quantity = "1188.3" if filled in {"0", "1188.3"} else filled
    return OrderStatusReport(
        account_id=account_id,
        instrument_id=APT,
        client_order_id=client_order_id,
        venue_order_id=VenueOrderId(str(venue_order_id)),
        order_side=side,
        order_type=order_type,
        time_in_force=TimeInForce.GTC,
        order_status=status,
        quantity=Quantity.from_str(quantity),
        filled_qty=Quantity.from_str(filled),
        avg_px=Decimal(price) if filled != "0" else None,
        trigger_price=None if trigger is None else Price.from_str(trigger),
        trigger_type=TriggerType.MARK_PRICE if trigger is not None else TriggerType.NO_TRIGGER,
        reduce_only=trigger is not None,
        report_id=UUID4(),
        ts_accepted=at_ns,
        ts_last=at_ns,
        ts_init=now_ns,
    )


@pytest.fixture(name="account")
def _account(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    built: list[_Account] = []

    def build(
        *, factory: Any = OiBinanceExecClientFactory, generate_missing_orders: bool | None = None, **venue: Any
    ) -> _Account:
        answers = _Venue(**venue)

        async def send_request(client: Any, method: Any, url_path: str, payload: Any = None, **kwargs: Any) -> bytes:
            return await answers.send_request(client, method, url_path, payload, **kwargs)

        monkeypatch.setattr(BinanceHttpClient, "send_request", send_request)
        value = _Account(factory=factory, venue=answers, generate_missing_orders=generate_missing_orders)
        built.append(value)
        return value

    yield build
    for value in built:
        value.close()


_HELD = [("APTUSDT-PERP.BINANCE-OI-RUNTIME-F46", "1188.3")]
_PROTECTION = sorted([STOP_ID.value, TAKE_PROFIT_ID.value])


def test_the_pinned_binance_adapter_still_reports_every_fill_twice(account: Any) -> None:
    """The upstream defect the Runtime's client exists for; when this fails, Nautilus fixed it."""

    stock = account(factory=BinanceLiveExecClientFactory)

    reports = stock.fill_reports()

    assert sorted(stock.venue.user_trade_symbols) == ["APTUSDT", "APTUSDT"]
    assert sorted(report.trade_id.value for report in reports) == ["62685281", "62685281", "62685282", "62685282"]
    # ... and the resubscribe mass status built from them closes the position the venue still holds.
    stock.resubscribe_mass_status()
    assert stock.open_positions() == []
    assert len(stock.strategy.closed) == 1


def test_path_a_a_resubscribe_mass_status_leaves_the_venue_position_and_its_protection(account: Any) -> None:
    runtime = account()

    reports = runtime.fill_reports()
    assert sorted(report.trade_id.value for report in reports) == ["62685281", "62685282"]

    runtime.resubscribe_mass_status()
    assert runtime.open_positions() == _HELD
    assert runtime.open_protection() == _PROTECTION
    assert runtime.strategy.closed == []
    # And Nautilus' steady-state position check agrees with the venue afterwards.
    runtime.position_checks()
    assert runtime.open_positions() == _HELD
    assert runtime.strategy.closed == []


def test_path_a_with_an_earlier_round_trip_in_the_window_stays_harmless(account: Any) -> None:
    runtime = account(prior_round_trip=True)

    runtime.resubscribe_mass_status()

    assert runtime.open_positions() == _HELD
    assert runtime.open_protection() == _PROTECTION
    assert runtime.strategy.closed == []


def test_path_b_a_position_risk_error_while_holding_never_closes_the_position(account: Any) -> None:
    runtime = account(position_risk_error=True)

    runtime.position_checks()

    assert runtime.open_positions() == _HELD
    assert runtime.open_protection() == _PROTECTION
    assert runtime.strategy.closed == []


def test_failed_position_reads_do_not_consume_a_flat_verdict_and_recover_on_new_evidence(account: Any) -> None:
    runtime = account(position_risk_error=True)
    runtime.position_checks(count=10)
    assert runtime.open_positions() == _HELD
    assert runtime.strategy.closed == []
    runtime.venue.position_risk_error = False
    runtime.position_checks(count=2)
    assert runtime.open_positions() == _HELD
    assert runtime.open_protection() == _PROTECTION


def test_native_reconciliation_connects_a_triggered_algo_child_fill_to_the_cached_take_profit(account: Any) -> None:
    """The INJ failure shape: venue flat, Cache long, and the trigger created a real MARKET child."""

    runtime = account(triggered_take_profit=True)
    runtime.position_checks(count=4)
    assert runtime.open_positions() == _HELD

    recovered = runtime.loop.run_until_complete(_reconcile_with_event_queue(runtime))

    assert recovered is True
    assert runtime.open_positions() == []
    assert runtime.strategy.closed == [TAKE_PROFIT_ID.value]
    assert runtime.strategy.filled == [TAKE_PROFIT_ID.value]
    child = runtime.cache.order(TAKE_PROFIT_ID)
    assert child is not None and child.is_closed
    assert child.venue_order_id == VenueOrderId(str(TAKE_PROFIT_CHILD_ORDER_ID))


def test_triggered_algo_child_requires_signed_parent_child_receipt(account: Any) -> None:
    runtime = account(triggered_take_profit=True, wrong_algo_child=True)

    assert runtime.loop.run_until_complete(_reconcile_with_event_queue(runtime)) is False
    assert runtime.open_positions() == _HELD
    assert runtime.strategy.closed == []
    assert runtime.cache.order(TAKE_PROFIT_ID).venue_order_id == VenueOrderId(str(PROTECTION[1][1]))


def test_triggered_algo_child_without_venue_trade_cannot_infer_a_close(account: Any) -> None:
    runtime = account(triggered_take_profit=True, missing_child_trade=True)

    assert runtime.loop.run_until_complete(_reconcile_with_event_queue(runtime)) is False
    assert runtime.open_positions() == _HELD
    assert runtime.strategy.filled == []


async def _reconcile_with_event_queue(runtime: _Account) -> bool:
    """Run Nautilus' real live order-event consumer while the public reconciliation runs."""

    consumer = asyncio.create_task(runtime.engine._run_evt_queue())
    try:
        return await runtime.engine.reconcile_execution_state()
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)


def test_path_b_is_the_generated_flat_order_the_production_config_turns_off(account: Any) -> None:
    """With Nautilus' default the same `-1021` closes the position with a synthetic fill."""

    default = account(factory=BinanceLiveExecClientFactory, position_risk_error=True, generate_missing_orders=True)

    default.position_checks(count=1)

    assert default.open_positions() == []
    assert len(default.strategy.closed) == 1


def test_pinned_adapter_preserves_position_read_failure_and_true_empty_report(account: Any) -> None:
    runtime = account(position_risk_error=True)
    command = GeneratePositionStatusReports(
        instrument_id=None,
        start=None,
        end=None,
        command_id=UUID4(),
        ts_init=runtime.clock.timestamp_ns(),
    )
    with pytest.raises(BinanceClientError):
        runtime.loop.run_until_complete(runtime.client.generate_position_status_reports(command))
    assert runtime.open_positions() == _HELD

    runtime.venue.position_risk_error = False
    reports = runtime.loop.run_until_complete(runtime.client.generate_position_status_reports(command))
    assert len(reports) == 1 and reports[0].signed_decimal_qty == Decimal("1188.3")

    specific = GeneratePositionStatusReports(
        instrument_id=APT,
        start=None,
        end=None,
        command_id=UUID4(),
        ts_init=runtime.clock.timestamp_ns(),
    )
    runtime.venue.position_amount = "0"
    [flat] = runtime.loop.run_until_complete(runtime.client.generate_position_status_reports(specific))
    assert flat.position_side == PositionSide.FLAT and flat.quantity == Quantity.zero()


def test_pinned_adapter_replaces_mark_price_protection_through_algo_submit_and_cancel(
    account: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = account()
    new_algo = AsyncMock(return_value=None)
    cancel_algo = AsyncMock(return_value=SimpleNamespace(algoId=1, code=None, msg=None))
    modify = AsyncMock(return_value=None)
    monkeypatch.setattr(runtime.client._http_account, "new_algo_order", new_algo)
    monkeypatch.setattr(runtime.client._http_account, "cancel_algo_order", cancel_algo)
    monkeypatch.setattr(runtime.client._http_account, "modify_order", modify)
    rejected: list[str] = []
    monkeypatch.setattr(
        runtime.client,
        "generate_order_modify_rejected",
        lambda *_args: rejected.append(str(_args[4])),
    )
    position = runtime.cache.positions_open()[0]
    for client_order_id, _venue_order_id, order_type, trigger in PROTECTION:
        old = runtime.cache.order(client_order_id)
        assert old is not None
        attempted_modify = ModifyOrder(
            TRADER,
            runtime.strategy.id,
            APT,
            client_order_id,
            old.venue_order_id,
            Quantity.from_str("1200.0"),
            None,
            Price.from_str(trigger),
            UUID4(),
            runtime.clock.timestamp_ns(),
        )
        runtime.loop.run_until_complete(runtime.client._modify_order(attempted_modify))
        assert rejected[-1].startswith("only LIMIT orders supported")

        create = (
            runtime.strategy.order_factory.stop_market
            if order_type == OrderType.STOP_MARKET
            else runtime.strategy.order_factory.market_if_touched
        )
        replacement = create(
            instrument_id=APT,
            order_side=OrderSide.SELL,
            quantity=Quantity.from_str("1200.0"),
            trigger_price=Price.from_str(trigger),
            trigger_type=TriggerType.MARK_PRICE,
            reduce_only=True,
        )
        runtime.cache.add_order(replacement, position.id, ClientId(BINANCE))
        runtime.loop.run_until_complete(runtime.client._submit_order_inner(replacement, None))
        sent = new_algo.await_args.kwargs
        assert sent["client_algo_id"] == replacement.client_order_id.value
        assert sent["order_type"].value == (
            "STOP_MARKET" if order_type == OrderType.STOP_MARKET else "TAKE_PROFIT_MARKET"
        )
        assert sent["working_type"] == "MARK_PRICE" and sent["reduce_only"] == "True"
        assert sent["quantity"] == "1200.0" and old.is_open

        runtime.loop.run_until_complete(runtime.client._cancel_order_single(APT, client_order_id, old.venue_order_id))
        assert cancel_algo.await_args.kwargs == {
            "algo_id": int(old.venue_order_id.value),
            "client_algo_id": client_order_id.value,
        }
    assert new_algo.await_count == cancel_algo.await_count == 2
    assert modify.await_count == 0


def test_a_reduce_only_fill_on_a_flat_cache_never_opens_a_mirror_position(account: Any) -> None:
    """What `/flatten account` relies on to close a position only the venue still holds (#680 PR-3)."""

    stock = account(factory=BinanceLiveExecClientFactory)
    stock.resubscribe_mass_status()
    assert stock.open_positions() == []

    close = stock.strategy.order_factory.market(
        instrument_id=APT,
        order_side=OrderSide.SELL,
        quantity=Quantity.from_str("1188.3"),
        reduce_only=True,
        tags=["operator_flatten"],
    )
    stock.cache.add_order(close, None, ClientId(BINANCE))
    # The engine's own event handler, as its queue consumer would call it for the venue's answers.
    stock.engine._handle_event(TestEventStubs.order_submitted(close, account_id=stock.account_id))
    stock.engine._handle_event(
        TestEventStubs.order_accepted(close, account_id=stock.account_id, venue_order_id=VenueOrderId("478600000"))
    )
    stock.engine._handle_event(
        TestEventStubs.order_filled(
            order=close,
            instrument=INSTRUMENT,
            strategy_id=stock.strategy.id,
            account_id=stock.account_id,
            venue_order_id=VenueOrderId("478600000"),
            trade_id=TradeId("62690000"),
            last_qty=Quantity.from_str("1188.3"),
            last_px=Price.from_str("0.8400"),
        )
    )

    assert close.is_closed
    assert stock.open_positions() == []
