"""Focused fixtures for the dormant #433-B Runtime seam."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from nautilus_trader.accounting.factory import AccountFactory
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import MessageBus, TestClock
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.events import AccountState
from nautilus_trader.model.identifiers import AccountId, TraderId
from nautilus_trader.model.objects import AccountBalance, Money
from nautilus_trader.portfolio.portfolio import Portfolio
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.test_kit.stubs.data import TestDataStubs

from tracefold.integrations.nautilus.oi_runtime.audit_sink import AuditSink, ObservationFactory
from tracefold.integrations.nautilus.oi_runtime.config import (
    OiInstrumentRoute,
    OiRiskLimits,
    OiRuntimeProfile,
    RuntimeMode,
)
from tracefold.integrations.nautilus.oi_runtime.risk import DayStartBaseline
from tracefold.integrations.nautilus.oi_runtime.signal_client import ExecutionSignalClient
from tracefold.integrations.nautilus.oi_runtime.state import (
    RuntimeControlSnapshot,
    RuntimeReadiness,
)
from tracefold.integrations.nautilus.oi_runtime.strategy import OiNautilusStrategy
from tracefold.trading import OperatorIntentV1, TradeSignalV1

NOW_NS = 1_900_000_000_000_000_000
ACCOUNT_ID = AccountId("BINANCE-001")
_RESUMED_CONTROL_STATE = RuntimeControlSnapshot(False, False, ())


def oi_profile(mode: RuntimeMode = "paper") -> OiRuntimeProfile:
    routes = ()
    if mode != "disabled":
        routes = (
            OiInstrumentRoute(
                market_key="crypto:perp:BTC:USDT",
                instrument_id=TestInstrumentProvider.btcusdt_perp_binance().id,
                stop_distance_bps=200,
            ),
        )
    return OiRuntimeProfile(
        mode=mode,
        account_slot="binance_usdm_primary",
        account_id=ACCOUNT_ID,
        runtime_release="nautilus-1.231.0+oi-v1",
        config_sha256="a" * 64,
        cache_namespace=f"oi-{mode}-cache",
        client_order_namespace=f"oi-{mode}-orders",
        routes=routes,
        risk=OiRiskLimits(
            risk_fraction_per_trade=Decimal("0.01"),
            max_risk_per_trade_usd=Decimal("10"),
            max_total_risk_usd=Decimal("25"),
            max_positions=3,
            max_leverage=2,
            max_daily_loss_usd=Decimal("50"),
            market_stale_after_ns=10_000_000_000,
            # account_stale_after_ns == 10s, reconciliation_stale_after_ns == 15s.
            reconciliation_interval_ns=5_000_000_000,
        ),
    )


def trade_signal(*, signal_id: str = "1" * 64, expires_at_ns: int = NOW_NS + 60_000_000_000) -> TradeSignalV1:
    return TradeSignalV1(
        seq=1,
        signal_id=signal_id,
        case_id=f"case-{signal_id[:8]}",
        market_key="crypto:perp:BTC:USDT",
        direction="long",
        observed_at_ns=NOW_NS - 1_000_000,
        expires_at_ns=expires_at_ns,
    )


class SignalRows:
    def __init__(self, *values: TradeSignalV1) -> None:
        self.values = values

    def __call__(
        self,
        _account_slot: str,
        _execution_strategy: str,
        limit: int,
    ) -> tuple[TradeSignalV1, ...]:
        return self.values[:limit]


class CommandRows:
    def __init__(self, *values: OperatorIntentV1) -> None:
        self.values = values

    def __call__(
        self,
        _account_slot: str,
        _execution_strategy: str,
        limit: int,
    ) -> tuple[OperatorIntentV1, ...]:
        return self.values[:limit]


def operator_intent(
    *,
    command_id: str = "5" * 64,
    action: str = "pause_entries",
    requested_at_ns: int = NOW_NS - 1_000_000,
    expires_at_ns: int = NOW_NS + 60_000_000_000,
    scope: str = "entries",
    market_key: str | None = None,
    direction: str | None = None,
) -> OperatorIntentV1:
    return OperatorIntentV1.model_validate(
        {
            "seq": 1,
            "command_id": command_id,
            "account_slot": oi_profile().account_slot,
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


class RecordingOiStrategy(OiNautilusStrategy):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.submitted: list[tuple[Any, Any, Any]] = []
        self.canceled: list[Any] = []
        self.queried: list[Any] = []
        self.subscribed: list[Any] = []
        self.unsubscribed: list[Any] = []

    def subscribe_quote_ticks(self, instrument_id: Any, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self.subscribed.append(instrument_id)

    def unsubscribe_quote_ticks(self, instrument_id: Any, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self.unsubscribed.append(instrument_id)

    def submit_order(self, order: Any, position_id: Any = None, client_id: Any = None, params: Any = None) -> None:
        del params
        self.submitted.append((order, position_id, client_id))

    def cancel_order(self, order: Any, client_id: Any = None, params: Any = None) -> None:
        del client_id, params
        self.canceled.append(order)

    def query_order(self, order: Any, client_id: Any = None, params: Any = None) -> None:
        del client_id, params
        self.queried.append(order)


def _usdt_margin_account() -> Any:
    state = AccountState(
        account_id=ACCOUNT_ID,
        account_type=AccountType.MARGIN,
        base_currency=None,
        reported=True,
        balances=[
            AccountBalance(
                total=Money(1_000, USDT),
                locked=Money(0, USDT),
                free=Money(1_000, USDT),
            )
        ],
        margins=[],
        info={},
        event_id=UUID4(),
        ts_event=NOW_NS,
        ts_init=NOW_NS,
    )
    return AccountFactory.create(state)


def registered_oi_strategy(
    *,
    values: tuple[TradeSignalV1, ...] = (),
    commands: tuple[OperatorIntentV1, ...] = (),
    singleton: list[bool] | None = None,
    audit: AuditSink | None = None,
    signal_client: ExecutionSignalClient | None = None,
    cache: Cache | None = None,
    mark_reconciled: bool = True,
    initial_control_state: RuntimeControlSnapshot | None = _RESUMED_CONTROL_STATE,
    profile: OiRuntimeProfile | None = None,
    with_quote: bool = True,
) -> SimpleNamespace:
    profile = profile or oi_profile()
    selected_signals = signal_client or ExecutionSignalClient(
        account_slot=profile.account_slot,
        execution_strategy="oi_nautilus_v1",
    )
    if values:
        selected_signals.poll_once(SignalRows(*values))
    if commands:
        selected_signals.poll_commands_once(CommandRows(*commands))
    factory = ObservationFactory(
        account_slot=profile.account_slot,
        runtime_release=profile.runtime_release,
        execution_strategy="oi_nautilus_v1",
    )
    selected_audit = audit or AuditSink(factory=factory)
    readiness = RuntimeReadiness(reconciliation_stale_after_ns=profile.risk.reconciliation_stale_after_ns)
    if mark_reconciled:
        readiness.reconciled(
            account_observed_at_ns=NOW_NS,
            reconciliation_observed_at_ns=NOW_NS,
        )
    singleton_state = singleton or [True]
    reconciliation_requests: list[str] = []
    strategy = RecordingOiStrategy(
        profile=profile,
        signals=selected_signals,
        audit=selected_audit,
        readiness=readiness,
        # `TestClock` fires timers on the calling thread, so the harness is the callback thread.
        # The real cross-thread hand-off is proven against a live `TradingNode` in
        # `tests/integration/test_nautilus_live_clock_threads.py` (#510 F).
        dispatch_pump=lambda pump: pump(),
        singleton_ready=lambda: singleton_state[0],
        day_start=DayStartBaseline(
            utc_day="2030-03-17",
            equity_usd=Decimal("1000"),
            recorded_at_ns=NOW_NS - 1_000_000,
            event_id="4" * 64,
        ),
        request_reconciliation=reconciliation_requests.append,
        initial_control_state=initial_control_state,
    )
    clock = TestClock()
    clock.set_time(NOW_NS)
    msgbus = MessageBus(TraderId("OI-TEST"), clock)
    selected_cache = cache or Cache()
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    if selected_cache.instrument(instrument.id) is None:
        selected_cache.add_instrument(instrument)
    if with_quote and selected_cache.quote_tick(instrument.id) is None:
        selected_cache.add_quote_tick(
            TestDataStubs.quote_tick(
                instrument=instrument,
                bid_price=9_999,
                ask_price=10_000,
                ts_event=NOW_NS,
                ts_init=NOW_NS,
            )
        )
    account = _usdt_margin_account()
    account.set_leverage(instrument.id, Decimal(profile.risk.max_leverage))
    if selected_cache.account(ACCOUNT_ID) is None:
        selected_cache.add_account(account)
    portfolio = Portfolio(msgbus, selected_cache, clock)
    portfolio.initialize_orders()
    portfolio.initialize_positions()
    strategy.register(TraderId("OI-TEST"), portfolio, msgbus, selected_cache, clock)
    return SimpleNamespace(
        strategy=strategy,
        profile=profile,
        signals=selected_signals,
        audit=selected_audit,
        readiness=readiness,
        singleton=singleton_state,
        clock=clock,
        cache=selected_cache,
        portfolio=portfolio,
        instrument=instrument,
        reconciliation_requests=reconciliation_requests,
    )


__all__ = [
    "ACCOUNT_ID",
    "NOW_NS",
    "CommandRows",
    "RecordingOiStrategy",
    "SignalRows",
    "oi_profile",
    "operator_intent",
    "registered_oi_strategy",
    "trade_signal",
]
