"""Shared fixtures for the OI Runtime: a profile, its inputs, and a real Nautilus engine to run it on.

`backtest_runtime` runs the production Strategy inside a real `BacktestEngine`: real Cache, real
ExecutionEngine, real RiskEngine and a simulated venue that fills, triggers and cancels. The DB
bridge's one ordering duty -- a plan is committed before its entry order exists -- is played by
`dispatch`, which settles the Strategy's prepared plan between two pumps exactly as the bridge thread
does between two cycles. Everything else the bridge writes stays in the journal for the test to read.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from nautilus_trader.accounting.factory import AccountFactory
from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import MessageBus, TestClock
from nautilus_trader.config import LoggingConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide, TriggerType
from nautilus_trader.model.events import AccountState
from nautilus_trader.model.identifiers import AccountId, ClientOrderId, PositionId, TraderId
from nautilus_trader.model.objects import AccountBalance, Money
from nautilus_trader.model.position import Position
from nautilus_trader.portfolio.portfolio import Portfolio
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.test_kit.stubs.data import TestDataStubs
from nautilus_trader.test_kit.stubs.events import TestEventStubs

from tracefold.integrations.nautilus.oi_runtime.config import (
    ActiveRuntimeMode,
    OiExitPolicy,
    OiInstrumentRoute,
    OiRiskLimits,
    OiRuntimeProfile,
)
from tracefold.integrations.nautilus.oi_runtime.entry import deterministic_client_order_id
from tracefold.integrations.nautilus.oi_runtime.journal import ExecutionJournal, ObservationFactory, PlanReceipt
from tracefold.integrations.nautilus.oi_runtime.risk import DayStartBaseline
from tracefold.integrations.nautilus.oi_runtime.signal_client import ExecutionSignalClient
from tracefold.integrations.nautilus.oi_runtime.strategy import (
    OiNautilusStrategy,
    OpenPlan,
    RuntimeControlSnapshot,
    RuntimeInputs,
)
from tracefold.integrations.nautilus.oi_runtime.venue import VENUE_SETTLE_NS, VenueReading
from tracefold.trading import ExecutionObservationV1, OperatorIntentV1, TradePlan, TradeSignalV1

NOW_NS = 1_900_000_000_000_000_000
ACCOUNT_ID = AccountId("BINANCE-001")
MARKET = "crypto:perp:BTC:USDT"
SECOND_NS = 1_000_000_000
RESUMED = RuntimeControlSnapshot(entries_paused=False, emergency_halted=False)
INSTRUMENT = TestInstrumentProvider.btcusdt_perp_binance()


def oi_profile(mode: ActiveRuntimeMode = "paper", **risk: Any) -> OiRuntimeProfile:
    limits = OiRiskLimits(
        risk_fraction_per_trade=Decimal("0.01"),
        max_risk_per_trade_usd=Decimal("10"),
        max_positions=1,
        max_leverage=2,
        max_daily_loss_usd=Decimal("50"),
        max_spread_fraction_of_stop=Decimal("0.3"),
        post_stop_cooldown_ns=4 * 3_600 * SECOND_NS,
        market_stale_after_ns=10 * SECOND_NS,
    )
    return OiRuntimeProfile(
        mode=mode,
        account_slot="binance_usdm_primary",
        account_id=ACCOUNT_ID,
        namespace=f"oi-{mode}-identity",
        routes=(OiInstrumentRoute(market_key=MARKET, instrument_id=INSTRUMENT.id, stop_distance_bps=200),),
        exit_policy=OiExitPolicy(take_profit_bps=200, max_holding_ns=4 * 3_600 * SECOND_NS),
        risk=replace(limits, **risk),
    )


def trade_signal(
    *,
    signal_id: str = "1" * 64,
    expires_at_ns: int = NOW_NS + 60 * SECOND_NS,
    direction: str = "long",
) -> TradeSignalV1:
    return TradeSignalV1.model_validate(
        {
            "seq": 1,
            "signal_id": signal_id,
            "case_id": f"case-{signal_id[:8]}",
            "market_key": MARKET,
            "direction": direction,
            "observed_at_ns": NOW_NS - 1_000_000,
            "expires_at_ns": expires_at_ns,
        }
    )


def operator_intent(
    *,
    command_id: str = "5" * 64,
    action: str = "pause_entries",
    requested_at_ns: int = NOW_NS - 1_000_000,
    expires_at_ns: int = NOW_NS + 60 * SECOND_NS,
    scope: str = "entries",
    market_key: str | None = None,
    direction: str | None = None,
) -> OperatorIntentV1:
    return OperatorIntentV1.model_validate(
        {
            "seq": 1,
            "command_id": command_id,
            "account_slot": "binance_usdm_primary",
            "action": action,
            "scope": scope,
            "reason": "operator test",
            "operator_identity": "operator:test",
            "authentication_identity": "test:authenticated",
            "requested_at_ns": requested_at_ns,
            "expires_at_ns": expires_at_ns,
            "market_key": market_key,
            "direction": direction,
        }
    )


def open_plan(
    *,
    entry_id: str = "1" * 64,
    opened_at_ns: int | None = NOW_NS - 60 * SECOND_NS,
    created_at_ns: int = NOW_NS - 61 * SECOND_NS,
    profile: OiRuntimeProfile | None = None,
    quantity: Decimal = Decimal("0.049"),
) -> TradePlan:
    """A committed plan an earlier Runtime generation left behind."""

    profile = profile or oi_profile()
    return TradePlan(
        entry_id=entry_id,
        source="signal",
        case_id=f"case-{entry_id[:8]}",
        account_slot=profile.account_slot,
        runtime_mode_at_creation=profile.mode,
        market_key=MARKET,
        instrument_id=INSTRUMENT.id.value,
        direction="long",
        entry_client_order_id=deterministic_client_order_id(
            namespace=profile.namespace, entry_id=entry_id, leg="entry"
        ).value,
        created_at_ns=created_at_ns,
        entry_expires_at_ns=created_at_ns + 60 * SECOND_NS,
        entry_quantity=quantity,
        stop_distance_bps=200,
        risk_budget_usd=Decimal("10"),
        max_leverage_at_creation=profile.risk.max_leverage,
        take_profit_bps=profile.exit_policy.take_profit_bps,
        max_holding_ns=profile.exit_policy.max_holding_ns,
        status="prepared" if opened_at_ns is None else "open",
        opened_at_ns=opened_at_ns,
        updated_at_ns=max(created_at_ns, opened_at_ns or created_at_ns),
    )


def rows(*values: Any) -> Callable[[str, str, int], tuple[Any, ...]]:
    """A Signal or Command reader that returns these values, as the bridge's indexed poll would."""

    def read(_slot: str, _strategy: str, limit: int) -> tuple[Any, ...]:
        return values[:limit]

    return read


def quote(bid: float, ask: float, at_ns: int) -> Any:
    return TestDataStubs.quote_tick(instrument=INSTRUMENT, bid_price=bid, ask_price=ask, ts_event=at_ns, ts_init=at_ns)


def quotes(bid: float, ask: float, *, start_ns: int, count: int, step_ns: int = 100_000_000) -> list[Any]:
    return [quote(bid, ask, start_ns + index * step_ns) for index in range(count)]


@dataclass
class BacktestRuntime:
    """One production Strategy on one real `BacktestEngine`, and every durable row it offered."""

    engine: BacktestEngine
    strategy: OiNautilusStrategy
    journal: ExecutionJournal
    signals: ExecutionSignalClient
    profile: OiRuntimeProfile
    receipts: list[PlanReceipt] = field(default_factory=list)
    refuse_plans: bool = False

    def run(self) -> None:
        self.engine.run()

    def observations(self, kind: str | None = None) -> list[ExecutionObservationV1]:
        values = [row.value for row in self.journal.due(float("inf")) if isinstance(row.value, ExecutionObservationV1)]
        return [value for value in values if kind is None or value.normalized_kind == kind]

    def plans(self) -> list[TradePlan]:
        return [row.value for row in self.journal.due(float("inf")) if isinstance(row.value, TradePlan)]

    def dispositions(self) -> list[dict[str, Any]]:
        return [
            dict(value.summary)
            for value in self.observations()
            if value.normalized_kind in {"signal_disposition", "control_disposition"}
        ]

    def orders(self) -> list[Any]:
        return list(self.engine.cache.orders(strategy_id=self.strategy.id))


def backtest_runtime(
    *,
    tape: Iterable[Any],
    signals: Iterable[TradeSignalV1] = (),
    commands: Iterable[OperatorIntentV1] = (),
    open_plans: Iterable[OpenPlan] = (),
    stop_exits: dict[str, int] | None = None,
    control: RuntimeControlSnapshot = RESUMED,
    profile: OiRuntimeProfile | None = None,
    seed: Callable[[BacktestEngine, OiNautilusStrategy], None] | None = None,
    refuse_plans: bool = False,
    starting_balance: int = 1_000,
) -> BacktestRuntime:
    """The simulated venue fills straight into the Cache, so the Cache is the venue (`venue_reads=False`)."""

    profile = profile or oi_profile()
    signal_client = ExecutionSignalClient(account_slot=profile.account_slot, execution_strategy="oi_nautilus_v1")
    if commands:
        signal_client.poll_commands_once(rows(*commands))
    if signals:
        signal_client.poll_once(rows(*signals))
    journal = ExecutionJournal(factory=ObservationFactory(profile.account_slot, "oi_nautilus_v1"))
    holder: dict[str, BacktestRuntime] = {}

    def dispatch(pump: Callable[[], None]) -> None:
        pump()
        plan = journal.pending_prepare()
        if plan is None:
            return
        runtime = holder["runtime"]
        receipt = (
            PlanReceipt(plan, committed=False, reason="trade_plan_rejected")
            if runtime.refuse_plans
            else PlanReceipt(plan, committed=True)
        )
        runtime.receipts.append(receipt)
        journal.settle_prepare(receipt)
        pump()

    strategy = OiNautilusStrategy(
        profile=profile,
        signals=signal_client,
        journal=journal,
        inputs=RuntimeInputs(control=control, open_plans=tuple(open_plans), stop_exits=stop_exits or {}),
        dispatch_pump=dispatch,
        singleton_ready=lambda: True,
        venue_reads=False,
        day_start=DayStartBaseline("2030-03-17", Decimal(starting_balance), NOW_NS - 1, "4" * 64),
    )
    engine = BacktestEngine(
        BacktestEngineConfig(trader_id=TraderId("OI-TEST"), logging=LoggingConfig(bypass_logging=True))
    )
    engine.add_venue(
        venue=INSTRUMENT.id.venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(starting_balance, INSTRUMENT.quote_currency)],
        base_currency=None,
        default_leverage=Decimal(2),
    )
    engine.add_instrument(INSTRUMENT)
    engine.add_data(list(tape))
    engine.add_strategy(strategy)
    if seed is not None:
        seed(engine, strategy)
    runtime = BacktestRuntime(
        engine=engine,
        strategy=strategy,
        journal=journal,
        signals=signal_client,
        profile=profile,
        refuse_plans=refuse_plans,
    )
    holder["runtime"] = runtime
    return runtime


def seed_reconciled_position(
    engine: BacktestEngine,
    strategy: OiNautilusStrategy,
    *,
    quantity: Decimal = Decimal("0.049"),
    entry_price: Decimal = Decimal(10_000),
    stop: Decimal | None = Decimal(9_800),
    take_profit: Decimal | None = Decimal(10_200),
    stop_client_order_id: str = "RECONCILED-STOP",
    take_profit_client_order_id: str = "RECONCILED-TP",
) -> PositionId:
    """What Nautilus' startup reconciliation leaves in the Cache for a position held across a restart.

    The position is rebuilt from the venue's report as a claimed order and its fill; the resting stop
    and take-profit come back as claimed open orders. The Strategy sees exactly this at its first pump.
    """

    position_id = PositionId(f"{INSTRUMENT.id}-{strategy.id}")
    entry = strategy.order_factory.market(
        instrument_id=INSTRUMENT.id,
        order_side=OrderSide.BUY,
        quantity=INSTRUMENT.make_qty(quantity),
        client_order_id=ClientOrderId("RECONCILED-ENTRY"),
    )
    engine.cache.add_order(entry)
    entry.apply(TestEventStubs.order_submitted(entry, account_id=ACCOUNT_ID, ts_event=NOW_NS - 2))
    engine.cache.update_order(entry)
    fill = TestEventStubs.order_filled(
        order=entry,
        instrument=INSTRUMENT,
        strategy_id=strategy.id,
        account_id=ACCOUNT_ID,
        position_id=position_id,
        last_qty=INSTRUMENT.make_qty(quantity),
        last_px=INSTRUMENT.make_price(entry_price),
        commission=Money(0, INSTRUMENT.quote_currency),
        ts_event=NOW_NS - 1,
    )
    entry.apply(fill)
    engine.cache.update_order(entry)
    engine.cache.add_position(Position(INSTRUMENT, fill), OmsType.NETTING)
    for price, client_order_id, create in (
        (stop, stop_client_order_id, strategy.order_factory.stop_market),
        (take_profit, take_profit_client_order_id, strategy.order_factory.market_if_touched),
    ):
        if price is None:
            continue
        order = create(
            instrument_id=INSTRUMENT.id,
            order_side=OrderSide.SELL,
            quantity=INSTRUMENT.make_qty(quantity),
            trigger_price=INSTRUMENT.make_price(price),
            trigger_type=TriggerType.MARK_PRICE,
            reduce_only=True,
            client_order_id=ClientOrderId(client_order_id),
        )
        # `BacktestEngine.run` replays cached open orders through its matching engine, which refuses a
        # reduce-only order it cannot bind to a position; the venue holds these instead.
        engine.cache.add_order(order, position_id=position_id)
        order.apply(TestEventStubs.order_submitted(order, account_id=ACCOUNT_ID, ts_event=NOW_NS - 1))
        engine.cache.update_order(order)
        order.apply(TestEventStubs.order_accepted(order, account_id=ACCOUNT_ID, ts_event=NOW_NS - 1))
        engine.cache.update_order(order)
    return position_id


class RecordingOiStrategy(OiNautilusStrategy):
    """The production Strategy with its outbound venue calls recorded instead of sent."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.submitted: list[tuple[Any, Any]] = []
        self.canceled: list[Any] = []
        self.canceled_all: list[Any] = []
        self.closed: list[tuple[Any, list[str] | None]] = []
        self.subscribed: list[Any] = []
        self.unsubscribed: list[Any] = []

    def subscribe_quote_ticks(self, instrument_id: Any, *args: Any, **kwargs: Any) -> None:
        self.subscribed.append(instrument_id)

    def unsubscribe_quote_ticks(self, instrument_id: Any, *args: Any, **kwargs: Any) -> None:
        self.unsubscribed.append(instrument_id)

    def submit_order(self, order: Any, position_id: Any = None, client_id: Any = None, params: Any = None) -> None:
        self.cache.add_order(order, position_id=position_id)
        self.submitted.append((order, position_id))

    def cancel_order(self, order: Any, client_id: Any = None, params: Any = None) -> None:
        self.canceled.append(order)

    def cancel_all_orders(
        self, instrument_id: Any, order_side: Any = None, client_id: Any = None, params: Any = None
    ) -> None:
        self.canceled_all.append(instrument_id)

    def close_position(self, position: Any, client_id: Any = None, tags: Any = None, **kwargs: Any) -> None:
        self.closed.append((position, tags))


@dataclass
class UnitRuntime:
    """The Strategy registered on a bare Cache, clock and Portfolio: every Cache fact is the test's."""

    strategy: RecordingOiStrategy
    journal: ExecutionJournal
    signals: ExecutionSignalClient
    cache: Cache
    clock: TestClock
    profile: OiRuntimeProfile

    def pump(self) -> None:
        self.strategy.on_timer(None)

    def settle(self, *, committed: bool = True, reason: str | None = None) -> TradePlan | None:
        """Play the bridge: commit the prepared plan (or refuse it) and pump once more."""

        plan = self.journal.pending_prepare()
        if plan is not None:
            self.journal.settle_prepare(PlanReceipt(plan, committed=committed, reason=reason))
            self.pump()
        return plan

    def advance(self, ns: int) -> None:
        self.clock.set_time(self.clock.timestamp_ns() + ns)

    def observations(self, kind: str | None = None) -> list[ExecutionObservationV1]:
        values = [row.value for row in self.journal.due(float("inf")) if isinstance(row.value, ExecutionObservationV1)]
        return [value for value in values if kind is None or value.normalized_kind == kind]

    def plans(self) -> list[TradePlan]:
        return [row.value for row in self.journal.due(float("inf")) if isinstance(row.value, TradePlan)]

    def dispositions(self) -> list[dict[str, Any]]:
        return [
            dict(value.summary)
            for value in self.observations()
            if value.normalized_kind in {"signal_disposition", "control_disposition"}
        ]

    def add_quote(self, bid: float, ask: float, *, at_ns: int | None = None) -> None:
        self.cache.add_quote_tick(quote(bid, ask, self.clock.timestamp_ns() if at_ns is None else at_ns))

    def venue(self, positions: dict[str, str] | None, *, failure: str = "BinanceClientError:-1021") -> None:
        """Let everything so far settle, hand the Strategy one venue read that starts then, and pump.

        `positions` maps Binance symbols to signed quantities; `None` is a read that failed.
        """

        self.advance(VENUE_SETTLE_NS)
        started_at_ns = self.clock.timestamp_ns()
        reading = (
            VenueReading(started_at_ns, started_at_ns + 1, None, failure=failure)
            if positions is None
            else VenueReading(
                started_at_ns,
                started_at_ns + 1,
                {symbol: Decimal(quantity) for symbol, quantity in positions.items()},
            )
        )
        self.strategy.observe_venue(reading)
        self.pump()


def _usdt_margin_account(balance: int) -> Any:
    state = AccountState(
        account_id=ACCOUNT_ID,
        account_type=AccountType.MARGIN,
        base_currency=None,
        reported=True,
        balances=[AccountBalance(total=Money(balance, USDT), locked=Money(0, USDT), free=Money(balance, USDT))],
        margins=[],
        info={},
        event_id=UUID4(),
        ts_event=NOW_NS,
        ts_init=NOW_NS,
    )
    return AccountFactory.create(state)


def unit_runtime(
    *,
    signals: Iterable[TradeSignalV1] = (),
    commands: Iterable[OperatorIntentV1] = (),
    open_plans: Iterable[OpenPlan] = (),
    stop_exits: dict[str, int] | None = None,
    control: RuntimeControlSnapshot = RESUMED,
    profile: OiRuntimeProfile | None = None,
    balance: int = 1_000,
    day_start_equity: Decimal | None = None,
    with_quote: bool = True,
    singleton: list[bool] | None = None,
    venue_reads: bool = False,
) -> UnitRuntime:
    """A bare Cache. With `venue_reads`, the venue is what the test hands `UnitRuntime.venue`."""

    profile = profile or oi_profile()
    signal_client = ExecutionSignalClient(account_slot=profile.account_slot, execution_strategy="oi_nautilus_v1")
    if commands:
        signal_client.poll_commands_once(rows(*commands))
    if signals:
        signal_client.poll_once(rows(*signals))
    journal = ExecutionJournal(factory=ObservationFactory(profile.account_slot, "oi_nautilus_v1"))
    singleton_state = singleton if singleton is not None else [True]
    strategy = RecordingOiStrategy(
        profile=profile,
        signals=signal_client,
        journal=journal,
        inputs=RuntimeInputs(control=control, open_plans=tuple(open_plans), stop_exits=stop_exits or {}),
        # `TestClock` fires timers on the calling thread, so the harness is the callback thread.
        dispatch_pump=lambda pump: pump(),
        singleton_ready=lambda: singleton_state[0],
        venue_reads=venue_reads,
        day_start=DayStartBaseline(
            "2030-03-17", Decimal(balance) if day_start_equity is None else day_start_equity, NOW_NS - 1, "4" * 64
        ),
    )
    clock = TestClock()
    clock.set_time(NOW_NS)
    msgbus = MessageBus(TraderId("OI-TEST"), clock)
    cache = Cache()
    cache.add_instrument(INSTRUMENT)
    if with_quote:
        cache.add_quote_tick(quote(9_999, 10_000, NOW_NS))
    account = _usdt_margin_account(balance)
    account.set_leverage(INSTRUMENT.id, Decimal(profile.risk.max_leverage))
    cache.add_account(account)
    portfolio = Portfolio(msgbus, cache, clock)
    portfolio.initialize_orders()
    portfolio.initialize_positions()
    strategy.register(TraderId("OI-TEST"), portfolio, msgbus, cache, clock)
    return UnitRuntime(
        strategy=strategy, journal=journal, signals=signal_client, cache=cache, clock=clock, profile=profile
    )


def cached_position(
    runtime: UnitRuntime,
    *,
    quantity: Decimal = Decimal("0.049"),
    price: Decimal = Decimal(10_000),
    strategy_id: Any = None,
    client_order_id: str = "RECONCILED-ENTRY",
) -> Position:
    """A position in the Cache, as Nautilus' reconciliation or a live fill leaves it."""

    strategy = runtime.strategy
    owner = strategy.id if strategy_id is None else strategy_id
    position_id = PositionId(f"{INSTRUMENT.id}-{owner}")
    order = strategy.order_factory.market(
        instrument_id=INSTRUMENT.id,
        order_side=OrderSide.BUY,
        quantity=INSTRUMENT.make_qty(quantity),
        client_order_id=ClientOrderId(client_order_id),
    )
    runtime.cache.add_order(order)
    order.apply(TestEventStubs.order_submitted(order, account_id=ACCOUNT_ID, ts_event=NOW_NS - 2))
    runtime.cache.update_order(order)
    fill = TestEventStubs.order_filled(
        order=order,
        instrument=INSTRUMENT,
        strategy_id=owner,
        account_id=ACCOUNT_ID,
        position_id=position_id,
        last_qty=INSTRUMENT.make_qty(quantity),
        last_px=INSTRUMENT.make_price(price),
        commission=Money(0, INSTRUMENT.quote_currency),
        ts_event=NOW_NS - 1,
    )
    order.apply(fill)
    runtime.cache.update_order(order)
    position = Position(INSTRUMENT, fill)
    runtime.cache.add_position(position, OmsType.NETTING)
    return position


def close_cached_position(runtime: UnitRuntime, position: Position, order: Any, *, price: Decimal) -> None:
    """`order` fills the whole position, the Cache closes it, and the Strategy hears both events.

    A resting stop or take-profit is the Runtime's own leg; a plain market order nobody tagged is what
    Nautilus' reconciliation submits for a fill it invented, and what a close by hand on the venue
    becomes.
    """

    if order.status_string() == "INITIALIZED":
        runtime.cache.add_order(order)
        order.apply(TestEventStubs.order_submitted(order, account_id=ACCOUNT_ID, ts_event=runtime.clock.timestamp_ns()))
        runtime.cache.update_order(order)
    fill = TestEventStubs.order_filled(
        order=order,
        instrument=INSTRUMENT,
        strategy_id=runtime.strategy.id,
        account_id=ACCOUNT_ID,
        position_id=position.id,
        last_qty=position.quantity,
        last_px=INSTRUMENT.make_price(price),
        commission=Money(0, INSTRUMENT.quote_currency),
        ts_event=runtime.clock.timestamp_ns(),
    )
    order.apply(fill)
    runtime.cache.update_order(order)
    position.apply(fill)
    runtime.cache.update_position(position)
    runtime.strategy.on_order_filled(fill)
    runtime.strategy.on_position_closed(TestEventStubs.position_closed(position))


def cached_protection(
    runtime: UnitRuntime,
    *,
    leg: str,
    trigger: Decimal,
    quantity: Decimal = Decimal("0.049"),
    client_order_id: str | None = None,
) -> Any:
    """A resting reduce-only stop or take-profit the venue reports open."""

    strategy = runtime.strategy
    create = strategy.order_factory.stop_market if leg == "stop" else strategy.order_factory.market_if_touched
    order = create(
        instrument_id=INSTRUMENT.id,
        order_side=OrderSide.SELL,
        quantity=INSTRUMENT.make_qty(quantity),
        trigger_price=INSTRUMENT.make_price(trigger),
        trigger_type=TriggerType.MARK_PRICE,
        reduce_only=True,
        client_order_id=ClientOrderId(client_order_id or f"RESTING-{leg.upper()}"),
    )
    runtime.cache.add_order(order)
    order.apply(TestEventStubs.order_submitted(order, account_id=ACCOUNT_ID, ts_event=NOW_NS - 1))
    runtime.cache.update_order(order)
    order.apply(TestEventStubs.order_accepted(order, account_id=ACCOUNT_ID, ts_event=NOW_NS - 1))
    runtime.cache.update_order(order)
    return order


__all__ = [
    "ACCOUNT_ID",
    "INSTRUMENT",
    "MARKET",
    "NOW_NS",
    "RESUMED",
    "SECOND_NS",
    "BacktestRuntime",
    "RecordingOiStrategy",
    "UnitRuntime",
    "backtest_runtime",
    "cached_position",
    "cached_protection",
    "close_cached_position",
    "oi_profile",
    "open_plan",
    "operator_intent",
    "quote",
    "quotes",
    "rows",
    "seed_reconciled_position",
    "trade_signal",
    "unit_runtime",
]
