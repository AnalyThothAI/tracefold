"""Small OI Strategy: Signal admission, native orders, protection, and audit."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Lock
from typing import Any, Literal

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.enums import OrderSide, OrderType, PositionSide, TriggerType
from nautilus_trader.model.identifiers import ClientId, ClientOrderId, PositionId
from nautilus_trader.trading.strategy import Strategy

from tracefold.trading import OperatorIntentV1, TradeSignalV1

from .audit_sink import AuditSink
from .config import OiInstrumentRoute, OiRuntimeProfile
from .risk import DayStartBaseline, NautilusRiskFacts, OiFuturesRiskPolicy, fixed_risk_quantity
from .signal_client import ExecutionSignalClient

_CALLBACK_BATCH = 16
_PUMP_INTERVAL_MS = 100
_AMBIGUOUS_QUERY_AFTER_NS = 5_000_000_000
_AMBIGUOUS_REASONS = ("-1007", "503", "timeout", "timed out", "response unknown")
_MAX_ENTRY_DRIFT_BPS = Decimal(25)
_MAX_SPREAD_BPS = Decimal(30)


class _AuditBackpressure(RuntimeError):
    pass


def deterministic_client_order_id(
    *,
    namespace: str,
    profile_id: str,
    signal_id: str,
    leg: str,
) -> ClientOrderId:
    digest = hashlib.sha256(f"{namespace}:{profile_id}:{signal_id}:{leg}".encode()).hexdigest()
    return ClientOrderId(f"tf{digest[:30]}")


def _protection_leg(generation: int, quantity: Decimal) -> str:
    return f"protection:{generation}:{format(quantity.normalize(), 'f')}"


def _exit_leg(generation: int) -> str:
    return "exit" if generation == 0 else f"exit:{generation}"


def oi_strategy_config(profile: OiRuntimeProfile) -> StrategyConfig:
    claims = sorted((route.instrument_id for route in profile.routes), key=lambda item: item.value)
    tag = hashlib.sha256(profile.profile_id.encode()).hexdigest()[:3].upper()
    return StrategyConfig(
        strategy_id="OI-RUNTIME",
        order_id_tag=tag,
        oms_type="NETTING",
        external_order_claims=claims,
    )


@dataclass(frozen=True, slots=True)
class RuntimeReadinessSnapshot:
    ready: bool
    reason: str
    singleton_ready: bool
    activation_ready: bool
    startup_reconciled: bool
    audit_ready: bool
    day_start_ready: bool
    unexpected_exposure: bool
    reconciliation_observed_at_ns: int


@dataclass(frozen=True, slots=True)
class RuntimeControlSnapshot:
    entries_paused: bool
    emergency_halted: bool
    flatten_pending: tuple[str, ...]


class RuntimeReadiness:
    """Thread-safe mechanical gates; it contains no capital or order lifecycle."""

    def __init__(self) -> None:
        self._activation_ready = False
        self._startup_reconciled = False
        self._unexpected_exposure = False
        self._account_observed_at_ns = 0
        self._reconciliation_observed_at_ns = 0
        self._lock = Lock()

    def activate(self) -> None:
        with self._lock:
            self._activation_ready = True

    def reconciled(self, *, account_observed_at_ns: int, reconciliation_observed_at_ns: int) -> None:
        if account_observed_at_ns <= 0 or reconciliation_observed_at_ns <= 0:
            raise ValueError("oi_runtime_reconciliation_clock_invalid")
        with self._lock:
            self._startup_reconciled = True
            self._unexpected_exposure = False
            self._account_observed_at_ns = account_observed_at_ns
            self._reconciliation_observed_at_ns = reconciliation_observed_at_ns

    def halt_for_unexpected_exposure(self) -> None:
        with self._lock:
            self._unexpected_exposure = True

    def reconciliation_failed(self) -> None:
        with self._lock:
            self._startup_reconciled = False

    def facts_clock(self) -> tuple[int, int]:
        with self._lock:
            return self._account_observed_at_ns, self._reconciliation_observed_at_ns

    def snapshot(
        self,
        *,
        singleton_ready: bool,
        audit_ready: bool,
        day_start_ready: bool,
    ) -> RuntimeReadinessSnapshot:
        with self._lock:
            activation = self._activation_ready
            startup = self._startup_reconciled
            unexpected = self._unexpected_exposure
            reconciled_at = self._reconciliation_observed_at_ns
        gates = (
            (singleton_ready, "singleton_unavailable"),
            (activation, "activation_missing"),
            (startup, "startup_reconciliation_unproven"),
            (audit_ready, "audit_unavailable"),
            (day_start_ready, "day_start_baseline_missing"),
            (not unexpected, "unexpected_exposure"),
        )
        reason = "ready"
        for passed, failed_reason in gates:
            if not passed:
                reason = failed_reason
                break
        return RuntimeReadinessSnapshot(
            ready=reason == "ready",
            reason=reason,
            singleton_ready=singleton_ready,
            activation_ready=activation,
            startup_reconciled=startup,
            audit_ready=audit_ready,
            day_start_ready=day_start_ready,
            unexpected_exposure=unexpected,
            reconciliation_observed_at_ns=reconciled_at,
        )


@dataclass(frozen=True, slots=True)
class RecoveredProtectionSeed:
    role: Literal["active", "pending", "retiring"]
    client_order_id: ClientOrderId
    quantity: Decimal
    trigger_price: Decimal
    generation: int


@dataclass(frozen=True, slots=True)
class RecoveredExecutionSeed:
    """Durable identities needed to reclaim one execution from Nautilus Cache."""

    signal: TradeSignalV1
    entry_client_order_id: ClientOrderId
    position_id: PositionId | None = None
    protections: tuple[RecoveredProtectionSeed, ...] = ()
    exit_client_order_id: ClientOrderId | None = None
    exit_generation: int = 0


@dataclass(frozen=True, slots=True)
class RuntimeReconciliationSnapshot:
    runtime_profile_id: str
    account_observed_at_ns: int
    reconciliation_observed_at_ns: int
    executions: tuple[RecoveredExecutionSeed, ...] = ()


@dataclass(slots=True)
class _ExecutionState:
    signal: TradeSignalV1
    route: OiInstrumentRoute
    entry_order: Any
    submitted_at_ns: int
    disposition_reason: str
    active: bool = True
    entry_query_pending: bool = True
    position_id: PositionId | None = None
    position_quantity: Decimal = Decimal(0)
    avg_entry_price: Decimal | None = None
    stop_order: Any = None
    stop_quantity: Decimal = Decimal(0)
    stop_avg_price: Decimal | None = None
    pending_stop_order: Any = None
    pending_stop_quantity: Decimal = Decimal(0)
    pending_stop_avg_price: Decimal | None = None
    retiring_stop_orders: dict[ClientOrderId, Any] = field(default_factory=dict)
    desired_stop: tuple[Decimal, Decimal] | None = None
    protection_generation: int = 0
    exit_order: Any = None
    exit_generation: int = 0
    exit_retry_required: bool = False
    exit_retry_budget: int = 1


class OiNautilusStrategy(Strategy):
    """Use Nautilus Risk/OMS/Cache/Portfolio; never synchronously call PostgreSQL."""

    def __init__(
        self,
        *,
        profile: OiRuntimeProfile,
        signals: ExecutionSignalClient,
        audit: AuditSink,
        readiness: RuntimeReadiness,
        singleton_ready: Any,
        day_start: DayStartBaseline | None,
        startup_reconciliation: RuntimeReconciliationSnapshot | None = None,
        continuous_reconciliation: Callable[[], RuntimeReconciliationSnapshot | None] | None = None,
        initial_control_state: RuntimeControlSnapshot | None = None,
        config: StrategyConfig | None = None,
    ) -> None:
        if profile.mode == "disabled":
            raise ValueError("oi_runtime_disabled_strategy_invalid")
        selected = config or oi_strategy_config(profile)
        claims = sorted((route.instrument_id for route in profile.routes), key=lambda item: item.value)
        if selected.oms_type != "NETTING" or selected.external_order_claims != claims:
            raise ValueError("oi_runtime_strategy_claims_invalid")
        super().__init__(selected)
        self._profile = profile
        self._signals = signals
        self._audit = audit
        self._readiness = readiness
        self._singleton_ready = singleton_ready
        self._day_start = day_start
        self._day_start_lock = Lock()
        self._startup_reconciliation = startup_reconciliation
        self._continuous_reconciliation = continuous_reconciliation
        self._routes = {route.market_key: route for route in profile.routes}
        self._stop_bps = {route.instrument_id: route.stop_distance_bps for route in profile.routes}
        self._policy = OiFuturesRiskPolicy(profile.risk)
        self._observations = audit.factory
        if (
            self._observations.runtime_profile_id != profile.profile_id
            or self._observations.runtime_release != profile.runtime_release
            or self._observations.execution_strategy != "oi_nautilus_v1"
        ):
            raise ValueError("oi_runtime_audit_identity_invalid")
        self._states: dict[str, _ExecutionState] = {}
        self._orders: dict[ClientOrderId, tuple[str, str]] = {}
        self._positions: dict[PositionId, str] = {}
        self._disposed: set[str] = set()
        self._disposed_commands: set[str] = set()
        control_state = initial_control_state or RuntimeControlSnapshot(False, False, ())
        if control_state.flatten_pending:
            raise ValueError("oi_runtime_initial_control_state_invalid")
        self._entries_paused = control_state.entries_paused
        self._emergency_halted = control_state.emergency_halted
        self._pending_flatten: dict[str, OperatorIntentV1] = {}
        self._flatten_accept_observed: set[str] = set()

    def on_start(self) -> None:
        if self._startup_reconciliation is not None:
            self.reconcile_runtime(self._startup_reconciliation)
        self._refresh_continuous_reconciliation()
        for route in self._profile.routes:
            self.subscribe_quote_ticks(route.instrument_id)
        self.clock.set_timer(
            name=f"{self.id}:OI-PUMP",
            interval=timedelta(milliseconds=_PUMP_INTERVAL_MS),
            callback=self.on_timer,
            fire_immediately=True,
        )

    def on_stop(self) -> None:
        timer_name = f"{self.id}:OI-PUMP"
        if timer_name in self.clock.timer_names:
            self.clock.cancel_timer(timer_name)
        for route in self._profile.routes:
            self.unsubscribe_quote_ticks(route.instrument_id)

    def on_timer(self, _event: object) -> None:
        self._refresh_continuous_reconciliation()
        for _ in range(_CALLBACK_BATCH):
            command = self._signals.next_command_nowait()
            if command is None:
                break
            try:
                self._handle_command(command)
            except _AuditBackpressure:
                break
        self._advance_pending_flatten()
        for _ in range(_CALLBACK_BATCH):
            signal = self._signals.next_nowait()
            if signal is None:
                break
            try:
                self._handle_signal(signal)
            except _AuditBackpressure:
                break
        self._query_aged_entries()
        self._retry_failed_exits()
        self._verify_owned_exposure()

    def readiness(self) -> RuntimeReadinessSnapshot:
        return self._readiness.snapshot(
            singleton_ready=bool(self._singleton_ready()),
            audit_ready=self._audit.can_accept_exposure(),
            day_start_ready=self._current_day_start() is not None,
        )

    def control_state(self) -> RuntimeControlSnapshot:
        return RuntimeControlSnapshot(
            entries_paused=self._entries_paused,
            emergency_halted=self._emergency_halted,
            flatten_pending=tuple(sorted(self._pending_flatten)),
        )

    def update_day_start(self, baseline: DayStartBaseline) -> None:
        """Accept a baseline already loaded durably by the background owner."""

        with self._day_start_lock:
            if self._day_start is not None and baseline.utc_day < self._day_start.utc_day:
                raise ValueError("oi_runtime_day_start_baseline_stale")
            self._day_start = baseline

    def _current_day_start(self) -> DayStartBaseline | None:
        utc_day = datetime.fromtimestamp(int(self.clock.timestamp_ns()) // 1_000_000_000, tz=UTC).date().isoformat()
        with self._day_start_lock:
            baseline = self._day_start
        return baseline if baseline is not None and baseline.utc_day == utc_day else None

    def _entry_order_valid(self, *, signal: TradeSignalV1, route: OiInstrumentRoute, entry: Any) -> bool:
        expected_id = deterministic_client_order_id(
            namespace=self._profile.client_order_namespace,
            profile_id=self._profile.profile_id,
            signal_id=signal.signal_id,
            leg="entry",
        )
        expected_side = OrderSide.BUY if signal.direction == "long" else OrderSide.SELL
        return bool(
            entry is not None
            and entry.client_order_id == expected_id
            and entry.strategy_id == self.id
            and entry.account_id == self._profile.account_id
            and entry.instrument_id == route.instrument_id
            and entry.side == expected_side
            and entry.order_type == OrderType.MARKET
            and not entry.is_reduce_only
            and entry.quantity.as_decimal() > 0
        )

    def _exit_order_valid(self, *, state: _ExecutionState, exit_order: Any, position: Any) -> bool:
        expected_id = deterministic_client_order_id(
            namespace=self._profile.client_order_namespace,
            profile_id=self._profile.profile_id,
            signal_id=state.signal.signal_id,
            leg=_exit_leg(state.exit_generation),
        )
        expected_side = OrderSide.SELL if state.signal.direction == "long" else OrderSide.BUY
        return bool(
            exit_order is not None
            and exit_order.client_order_id == expected_id
            and exit_order.strategy_id == self.id
            and exit_order.account_id == self._profile.account_id
            and exit_order.instrument_id == state.route.instrument_id
            and exit_order.side == expected_side
            and exit_order.order_type == OrderType.MARKET
            and exit_order.is_reduce_only
            and exit_order.quantity.as_decimal() == state.position_quantity
            and self.cache.position_for_order(exit_order.client_order_id) == position
        )

    def _refresh_continuous_reconciliation(self) -> None:
        source = self._continuous_reconciliation
        if source is None:
            return
        try:
            snapshot = source()
        except Exception:
            self._readiness.reconciliation_failed()
            return
        if snapshot is None:
            return
        _, reconciled_at_ns = self._readiness.facts_clock()
        if snapshot.reconciliation_observed_at_ns <= reconciled_at_ns:
            return
        self.reconcile_runtime(snapshot)

    def reconcile_runtime(self, snapshot: RuntimeReconciliationSnapshot) -> bool:
        """Rebuild runtime ownership only from durable identities and current Cache state."""

        if snapshot.runtime_profile_id != self._profile.profile_id:
            self._readiness.halt_for_unexpected_exposure()
            return False
        states: dict[str, _ExecutionState] = {}
        orders: dict[ClientOrderId, tuple[str, str]] = {}
        positions: dict[PositionId, str] = {}
        for seed in snapshot.executions:
            signal = seed.signal
            route = self._routes.get(signal.market_key)
            expected_entry = deterministic_client_order_id(
                namespace=self._profile.client_order_namespace,
                profile_id=self._profile.profile_id,
                signal_id=signal.signal_id,
                leg="entry",
            )
            entry = self.cache.order(seed.entry_client_order_id)
            if (
                route is None
                or signal.signal_id in states
                or seed.entry_client_order_id != expected_entry
                or not self._entry_order_valid(signal=signal, route=route, entry=entry)
            ):
                self._readiness.halt_for_unexpected_exposure()
                return False
            state = _ExecutionState(
                signal=signal,
                route=route,
                entry_order=entry,
                submitted_at_ns=snapshot.reconciliation_observed_at_ns,
                disposition_reason="recovered",
                entry_query_pending=bool(entry.is_inflight or entry.is_active_local),
            )
            states[signal.signal_id] = state
            orders[seed.entry_client_order_id] = (signal.signal_id, "entry")
            if seed.position_id is None:
                if seed.protections or seed.exit_client_order_id is not None:
                    self._readiness.halt_for_unexpected_exposure()
                    return False
                state.active = not entry.is_closed
                continue
            position = self.cache.position(seed.position_id)
            expected_side = PositionSide.LONG if signal.direction == "long" else PositionSide.SHORT
            if (
                position is None
                or not position.is_open
                or position.account_id != self._profile.account_id
                or position.strategy_id != self.id
                or position.instrument_id != route.instrument_id
                or position.side != expected_side
            ):
                self._readiness.halt_for_unexpected_exposure()
                return False
            state.position_id = seed.position_id
            state.position_quantity = abs(Decimal(str(position.quantity)))
            state.avg_entry_price = Decimal(str(position.avg_px_open))
            state.desired_stop = (state.position_quantity, state.avg_entry_price)
            positions[seed.position_id] = signal.signal_id
            instrument = self.cache.instrument(route.instrument_id)
            active = tuple(value for value in seed.protections if value.role == "active")
            pending = tuple(value for value in seed.protections if value.role == "pending")
            if instrument is None or len(active) != 1 or len(pending) > 1:
                self._readiness.halt_for_unexpected_exposure()
                self._states = states
                self._orders = orders
                self._positions = positions
                self.flatten_position(seed.position_id)
                return False
            distance = Decimal(route.stop_distance_bps) / Decimal(10_000)
            desired_trigger = instrument.make_price(
                state.avg_entry_price * (Decimal(1) - distance if signal.direction == "long" else Decimal(1) + distance)
            ).as_decimal()
            target = pending[0] if pending else active[0]
            if target.quantity != state.position_quantity or target.trigger_price != desired_trigger:
                self._readiness.halt_for_unexpected_exposure()
                self._states = states
                self._orders = orders
                self._positions = positions
                self.flatten_position(seed.position_id)
                return False
            for protection_seed in seed.protections:
                protection = self.cache.order(protection_seed.client_order_id)
                if not self._recovered_protection_valid(
                    state=state,
                    seed=protection_seed,
                    protection=protection,
                ):
                    self._readiness.halt_for_unexpected_exposure()
                    self._states = states
                    self._orders = orders
                    self._positions = positions
                    self.flatten_position(seed.position_id)
                    return False
                orders[protection_seed.client_order_id] = (signal.signal_id, "protection")
                state.protection_generation = max(state.protection_generation, protection_seed.generation)
                if protection_seed.role == "active":
                    if not protection.is_open:
                        self._readiness.halt_for_unexpected_exposure()
                        self._states = states
                        self._orders = orders
                        self._positions = positions
                        self.flatten_position(seed.position_id)
                        return False
                    state.stop_order = protection
                    state.stop_quantity = protection_seed.quantity
                    state.stop_avg_price = None if pending else state.avg_entry_price
                elif protection_seed.role == "pending":
                    if protection.is_open:
                        self._readiness.halt_for_unexpected_exposure()
                        self._states = states
                        self._orders = orders
                        self._positions = positions
                        self.flatten_position(seed.position_id)
                        return False
                    state.pending_stop_order = protection
                    state.pending_stop_quantity = protection_seed.quantity
                    state.pending_stop_avg_price = state.avg_entry_price
                else:
                    state.retiring_stop_orders[protection_seed.client_order_id] = protection
            state.exit_generation = seed.exit_generation
            if seed.exit_client_order_id is not None:
                expected_exit = deterministic_client_order_id(
                    namespace=self._profile.client_order_namespace,
                    profile_id=self._profile.profile_id,
                    signal_id=signal.signal_id,
                    leg=_exit_leg(seed.exit_generation),
                )
                exit_order = self.cache.order(seed.exit_client_order_id)
                if (
                    seed.exit_generation < 0
                    or seed.exit_client_order_id != expected_exit
                    or not self._exit_order_valid(state=state, exit_order=exit_order, position=position)
                ):
                    self._readiness.halt_for_unexpected_exposure()
                    return False
                if exit_order.is_closed:
                    state.exit_generation += 1
                    state.exit_retry_budget -= 1
                    state.exit_retry_required = True
                else:
                    state.exit_order = exit_order
                    orders[seed.exit_client_order_id] = (signal.signal_id, "exit")
        owned_order_ids = frozenset(orders)
        owned_position_ids = frozenset(positions)
        open_orders = tuple(self.cache.orders_open(account_id=self._profile.account_id))
        inflight_orders = tuple(self.cache.orders_inflight(account_id=self._profile.account_id))
        open_positions = tuple(self.cache.positions_open(account_id=self._profile.account_id))
        if any(order.client_order_id not in owned_order_ids or order.strategy_id != self.id for order in open_orders):
            self._readiness.halt_for_unexpected_exposure()
            return False
        if any(
            order.client_order_id not in owned_order_ids or order.strategy_id != self.id for order in inflight_orders
        ):
            self._readiness.halt_for_unexpected_exposure()
            return False
        if any(position.id not in owned_position_ids or position.strategy_id != self.id for position in open_positions):
            self._readiness.halt_for_unexpected_exposure()
            return False
        self._states = states
        self._orders = orders
        self._positions = positions
        for state in states.values():
            if state.entry_query_pending:
                self.query_order(state.entry_order, client_id=ClientId("BINANCE"))
            if state.pending_stop_order is not None:
                self.query_order(state.pending_stop_order, client_id=ClientId("BINANCE"))
            for retiring in state.retiring_stop_orders.values():
                self.cancel_order(retiring, client_id=ClientId("BINANCE"))
            if state.exit_order is not None and state.exit_order.is_inflight:
                self.query_order(state.exit_order, client_id=ClientId("BINANCE"))
        self._readiness.reconciled(
            account_observed_at_ns=snapshot.account_observed_at_ns,
            reconciliation_observed_at_ns=snapshot.reconciliation_observed_at_ns,
        )
        self._complete_flatten_from_reconciliation(snapshot)
        return True

    def _recovered_protection_valid(
        self,
        *,
        state: _ExecutionState,
        seed: RecoveredProtectionSeed,
        protection: Any,
    ) -> bool:
        instrument = self.cache.instrument(state.route.instrument_id)
        if instrument is None or state.position_id is None or state.avg_entry_price is None:
            return False
        expected_id = deterministic_client_order_id(
            namespace=self._profile.client_order_namespace,
            profile_id=self._profile.profile_id,
            signal_id=state.signal.signal_id,
            leg=_protection_leg(seed.generation, seed.quantity),
        )
        expected_side = OrderSide.SELL if state.signal.direction == "long" else OrderSide.BUY
        bound_position = None if protection is None else self.cache.position_for_order(protection.client_order_id)
        return bool(
            seed.generation > 0
            and seed.quantity > 0
            and seed.client_order_id == expected_id
            and protection is not None
            and not protection.is_closed
            and protection.strategy_id == self.id
            and protection.account_id == self._profile.account_id
            and bound_position is not None
            and bound_position.id == state.position_id
            and protection.instrument_id == state.route.instrument_id
            and protection.side == expected_side
            and protection.order_type == OrderType.STOP_MARKET
            and protection.trigger_type == TriggerType.LAST_PRICE
            and seed.trigger_price > 0
            and protection.trigger_price == instrument.make_price(seed.trigger_price)
            and protection.is_reduce_only
            and protection.quantity.as_decimal() == seed.quantity
        )

    def _protection_order_valid(
        self,
        *,
        state: _ExecutionState,
        protection: Any,
        quantity: Decimal,
        avg_price: Decimal | None,
        require_open: bool,
    ) -> bool:
        instrument = self.cache.instrument(state.route.instrument_id)
        if instrument is None or protection is None or quantity <= 0:
            return False
        current = self.cache.order(protection.client_order_id)
        expected_side = OrderSide.SELL if state.signal.direction == "long" else OrderSide.BUY
        distance = Decimal(state.route.stop_distance_bps) / Decimal(10_000)
        expected_trigger = (
            None
            if avg_price is None
            else instrument.make_price(
                avg_price * (Decimal(1) - distance if state.signal.direction == "long" else Decimal(1) + distance)
            )
        )
        return bool(
            not protection.is_closed
            and (not require_open or (current is not None and current.is_open))
            and protection.strategy_id == self.id
            and (
                protection.account_id == self._profile.account_id
                if require_open
                else protection.account_id in {None, self._profile.account_id}
            )
            and protection.instrument_id == state.route.instrument_id
            and protection.side == expected_side
            and protection.order_type == OrderType.STOP_MARKET
            and protection.trigger_type == TriggerType.LAST_PRICE
            and (expected_trigger is None or protection.trigger_price == expected_trigger)
            and protection.is_reduce_only
            and protection.quantity.as_decimal() == quantity
        )

    def _verify_owned_exposure(self) -> bool:
        owned_orders = frozenset(self._orders)
        owned_positions = frozenset(self._positions)
        if any(
            order.client_order_id not in owned_orders or order.strategy_id != self.id
            for order in (
                *self.cache.orders_open(account_id=self._profile.account_id),
                *self.cache.orders_inflight(account_id=self._profile.account_id),
            )
        ) or any(
            position.id not in owned_positions or position.strategy_id != self.id
            for position in self.cache.positions_open(account_id=self._profile.account_id)
        ):
            self._readiness.halt_for_unexpected_exposure()
            return False
        safe = True
        for state in self._states.values():
            if state.position_id is None or state.position_quantity <= 0:
                continue
            active_valid = self._protection_order_valid(
                state=state,
                protection=state.stop_order,
                quantity=state.stop_quantity,
                avg_price=state.stop_avg_price,
                require_open=True,
            )
            fully_protected = (
                active_valid
                and state.stop_quantity == state.position_quantity
                and state.stop_avg_price == state.avg_entry_price
            )
            if fully_protected:
                continue
            pending_valid = self._protection_order_valid(
                state=state,
                protection=state.pending_stop_order,
                quantity=state.pending_stop_quantity,
                avg_price=state.pending_stop_avg_price,
                require_open=False,
            )
            if pending_valid and (state.stop_order is None or active_valid):
                safe = False
                continue
            self._readiness.halt_for_unexpected_exposure()
            self.flatten_position(state.position_id)
            safe = False
        return safe

    def _handle_signal(self, signal: TradeSignalV1) -> None:
        now_ns = int(self.clock.timestamp_ns())
        if signal.signal_id in self._disposed:
            return
        existing_state = self._states.get(signal.signal_id)
        if existing_state is not None:
            self._dispose_signal(signal, existing_state.disposition_reason)
            return
        route = self._routes.get(signal.market_key)
        if route is None:
            self._dispose_signal(signal, "instrument_unmapped")
            return
        if any(state.active and state.route.instrument_id == route.instrument_id for state in self._states.values()):
            self._dispose_signal(signal, "instrument_busy")
            return
        client_order_id = deterministic_client_order_id(
            namespace=self._profile.client_order_namespace,
            profile_id=self._profile.profile_id,
            signal_id=signal.signal_id,
            leg="entry",
        )
        existing = self.cache.order(client_order_id)
        if existing is not None:
            if not self._entry_order_valid(signal=signal, route=route, entry=existing):
                self._readiness.halt_for_unexpected_exposure()
                self._dispose_signal(signal, "cached_entry_invalid")
                return
            position = self.cache.position_for_order(existing.client_order_id)
            expected_position_side = PositionSide.LONG if signal.direction == "long" else PositionSide.SHORT
            if (
                position is not None
                and position.is_open
                and (
                    position.account_id != self._profile.account_id
                    or position.strategy_id != self.id
                    or position.instrument_id != route.instrument_id
                    or position.side != expected_position_side
                )
            ):
                self._readiness.halt_for_unexpected_exposure()
                self._dispose_signal(signal, "cached_position_invalid")
                return
            self._orders[client_order_id] = (signal.signal_id, "entry")
            state = _ExecutionState(
                signal=signal,
                route=route,
                entry_order=existing,
                submitted_at_ns=now_ns,
                disposition_reason="replayed_query_first",
                active=bool(position is not None and position.is_open) or not existing.is_closed,
                entry_query_pending=bool(existing.is_inflight or existing.is_active_local),
            )
            self._states[signal.signal_id] = state
            if position is not None and position.is_open:
                state.position_id = position.id
                state.position_quantity = abs(Decimal(str(position.quantity)))
                state.avg_entry_price = Decimal(str(position.avg_px_open))
                self._positions[position.id] = signal.signal_id
                self._readiness.halt_for_unexpected_exposure()
                self.flatten_position(position.id)
            self.query_order(existing, client_id=ClientId("BINANCE"))
            self._dispose_signal(signal, "replayed_query_first")
            return
        if signal.expires_at_ns <= now_ns:
            self._dispose_signal(signal, "expired")
            return
        if self._emergency_halted:
            self._dispose_signal(signal, "operator_halt")
            return
        if self._entries_paused:
            self._dispose_signal(signal, "operator_paused")
            return
        exposure_ready = self._verify_owned_exposure()
        ready = self.readiness()
        if not exposure_ready or not ready.ready:
            self._dispose_signal(signal, ready.reason if not ready.ready else "protection_unproven")
            return
        day_start = self._current_day_start()
        if day_start is None:
            self._dispose_signal(signal, "day_start_baseline_missing")
            return
        instrument = self.cache.instrument(route.instrument_id)
        quote = self.cache.quote_tick(route.instrument_id)
        if instrument is None or quote is None:
            self._dispose_signal(signal, "instrument_or_market_missing")
            return
        account_clock, reconciliation_clock = self._readiness.facts_clock()
        try:
            facts = NautilusRiskFacts.collect(
                cache=self.cache,
                portfolio=self.portfolio,
                account_id=self._profile.account_id,
                strategy_id=self.id,
                routes=self._stop_bps,
                candidate_instrument_id=route.instrument_id,
                owned_order_ids=frozenset(self._orders),
                owned_position_ids=frozenset(self._positions),
                account_observed_at_ns=account_clock,
                reconciliation_observed_at_ns=reconciliation_clock,
            )
        except RuntimeError as exc:
            self._dispose_signal(signal, str(exc))
            return
        if facts.unexpected_exposure:
            self._readiness.halt_for_unexpected_exposure()
        requested_risk = min(
            facts.equity_usd * self._profile.risk.risk_fraction_per_trade,
            self._profile.risk.max_risk_per_trade_usd,
        )
        decision = self._policy.evaluate_entry(
            facts=facts,
            baseline=day_start,
            now_ns=now_ns,
            requested_risk_usd=requested_risk,
            requested_leverage=self._profile.risk.max_leverage,
            candidate_is_new_position=True,
        )
        if decision.action in {"deny", "halt"}:
            self._dispose_signal(signal, decision.reason)
            return
        bid = Decimal(str(quote.bid_price))
        ask = Decimal(str(quote.ask_price))
        midpoint = (bid + ask) / Decimal(2)
        spread_bps = (ask - bid) * Decimal(10_000) / midpoint
        if spread_bps > _MAX_SPREAD_BPS:
            self._dispose_signal(signal, "spread_limit")
            return
        executable_price = ask if signal.direction == "long" else bid
        price = executable_price * (Decimal(1) + _MAX_ENTRY_DRIFT_BPS / Decimal(10_000))
        try:
            raw_quantity = fixed_risk_quantity(
                price=price,
                stop_distance_bps=route.stop_distance_bps,
                allowed_risk_usd=decision.allowed_risk_usd,
                equity_usd=facts.equity_usd,
                max_leverage=self._profile.risk.max_leverage,
                existing_notional_usd=(
                    facts.gross_position_notional_usd
                    + facts.open_order_notional_usd
                    + facts.inflight_order_notional_usd
                ),
                size_increment=instrument.size_increment.as_decimal(),
            )
        except ValueError as exc:
            self._dispose_signal(signal, str(exc))
            return
        quantity = instrument.make_qty(raw_quantity)
        if quantity.as_decimal() <= 0:
            self._dispose_signal(signal, "quantity_below_increment")
            return
        stop_fraction = Decimal(route.stop_distance_bps) / Decimal(10_000)
        quantity_notional = quantity.as_decimal() * price
        existing_notional = (
            facts.gross_position_notional_usd + facts.open_order_notional_usd + facts.inflight_order_notional_usd
        )
        if quantity_notional * stop_fraction > decision.allowed_risk_usd:
            self._dispose_signal(signal, "quantity_exceeds_risk_after_rounding")
            return
        if existing_notional + quantity_notional > facts.equity_usd * self._profile.risk.max_leverage:
            self._dispose_signal(signal, "quantity_exceeds_leverage_after_rounding")
            return
        if instrument.min_quantity is not None and quantity < instrument.min_quantity:
            self._dispose_signal(signal, "quantity_below_minimum")
            return
        if instrument.min_notional is not None and quantity.as_decimal() * price < instrument.min_notional.as_decimal():
            self._dispose_signal(signal, "notional_below_minimum")
            return
        side = OrderSide.BUY if signal.direction == "long" else OrderSide.SELL
        order = self.order_factory.market(
            instrument_id=route.instrument_id,
            order_side=side,
            quantity=quantity,
            reduce_only=False,
            client_order_id=client_order_id,
        )
        self._orders[client_order_id] = (signal.signal_id, "entry")
        state = _ExecutionState(
            signal=signal,
            route=route,
            entry_order=order,
            submitted_at_ns=now_ns,
            disposition_reason="accepted",
        )
        self._states[signal.signal_id] = state
        try:
            self.submit_order(order, client_id=ClientId("BINANCE"))
        except Exception:
            state.disposition_reason = "unknown_query_first"
            self.query_order(order, client_id=ClientId("BINANCE"))
            self._observe_order(state, order, "entry", "unknown_query_first")
            self._dispose_signal(signal, "unknown_query_first")
            return
        self._observe_order(state, order, "entry", "submitted")
        self._dispose_signal(signal, "accepted")

    def _handle_command(self, command: OperatorIntentV1) -> None:
        now_ns = int(self.clock.timestamp_ns())
        if command.command_id in self._disposed_commands or command.command_id in self._pending_flatten:
            return
        if command.target_profile_id != self._profile.profile_id:
            self._dispose_command(command, "rejected", "profile_mismatch")
            return
        if command.expires_at_ns <= now_ns:
            self._dispose_command(command, "rejected", "expired")
            return
        if command.action == "pause_entries":
            self._entries_paused = True
            self._dispose_command(command, "accepted", "entries_paused")
            return
        if command.action == "resume_entries":
            self._entries_paused = False
            self._dispose_command(command, "accepted", "entries_resumed")
            return
        if command.action == "emergency_halt":
            self._entries_paused = True
            self._emergency_halted = True
            self._dispose_command(command, "accepted", "emergency_halted")
            return
        if command.action == "manual_entry":
            self._dispose_command(command, "rejected", "manual_entry_not_enabled")
            return
        if command.action != "flatten" or command.scope != "account":
            self._dispose_command(command, "rejected", "flatten_scope_unsupported")
            return
        self._entries_paused = True
        self._pending_flatten[command.command_id] = command
        self._advance_pending_flatten()

    def _advance_pending_flatten(self) -> None:
        if not self._pending_flatten:
            return
        now_ns = int(self.clock.timestamp_ns())
        for command_id, _command in tuple(self._pending_flatten.items()):
            if command_id not in self._flatten_accept_observed:
                accepted = self._observations.create(
                    normalized_kind="readiness",
                    command_id=command_id,
                    occurred_at_ns=now_ns,
                    observed_at_ns=now_ns,
                    summary={"action": "flatten", "control_stage": "runtime_accepted"},
                    payload={"command_id": command_id, "action": "flatten", "stage": "runtime_accepted"},
                    event_identity="runtime_accepted",
                )
                if self._audit.offer(accepted):
                    self._flatten_accept_observed.add(command_id)
            for state in self._states.values():
                if state.position_id is not None and state.position_quantity > 0:
                    self.flatten_position(state.position_id)
                elif not state.entry_order.is_closed:
                    self.cancel_order(state.entry_order, client_id=ClientId("BINANCE"))

    def _complete_flatten_from_reconciliation(self, snapshot: RuntimeReconciliationSnapshot) -> None:
        if not self._pending_flatten or snapshot.executions:
            return
        if self.cache.positions_open(account_id=self._profile.account_id):
            return
        if self.cache.orders_open(account_id=self._profile.account_id) or self.cache.orders_inflight(
            account_id=self._profile.account_id
        ):
            return
        for command_id, command in tuple(self._pending_flatten.items()):
            fresh_at_ns = min(snapshot.account_observed_at_ns, snapshot.reconciliation_observed_at_ns)
            if fresh_at_ns <= command.requested_at_ns:
                continue
            completed = self._observations.create(
                normalized_kind="control_disposition",
                command_id=command_id,
                occurred_at_ns=min(snapshot.account_observed_at_ns, snapshot.reconciliation_observed_at_ns),
                observed_at_ns=max(snapshot.account_observed_at_ns, snapshot.reconciliation_observed_at_ns),
                summary={"disposition": "completed", "reason": "binance_account_flat"},
                payload={
                    "command_id": command_id,
                    "disposition": "completed",
                    "reason": "binance_account_flat",
                    "account_observed_at_ns": snapshot.account_observed_at_ns,
                },
                event_identity="final",
            )
            if not self._audit.offer(completed):
                continue
            self._pending_flatten.pop(command_id)
            self._flatten_accept_observed.discard(command_id)
            self._disposed_commands.add(command_id)

    def _dispose_command(self, command: OperatorIntentV1, disposition: str, reason: str) -> None:
        if command.command_id in self._disposed_commands:
            return
        now_ns = int(self.clock.timestamp_ns())
        value = self._observations.create(
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
            raise _AuditBackpressure("oi_runtime_audit_backpressure")
        self._disposed_commands.add(command.command_id)

    def _dispose_signal(self, signal: TradeSignalV1, reason: str) -> None:
        if signal.signal_id in self._disposed:
            return
        now_ns = int(self.clock.timestamp_ns())
        value = self._observations.create(
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
            raise _AuditBackpressure("oi_runtime_audit_backpressure")
        self._disposed.add(signal.signal_id)

    def _observe_order(self, state: _ExecutionState, order: Any, leg: str, status: str) -> None:
        occurred_at_ns = int(order.ts_init)
        self._audit.offer(
            self._observations.create(
                normalized_kind="protection" if leg == "protection" else "order",
                signal_id=state.signal.signal_id,
                occurred_at_ns=occurred_at_ns,
                observed_at_ns=occurred_at_ns,
                native_identity_references=(order.client_order_id.value,),
                summary={"leg": leg, "status": status},
                payload={"client_order_id": order.client_order_id.value, "leg": leg, "status": status},
                event_identity=status,
            )
        )

    def _query_aged_entries(self) -> None:
        now_ns = int(self.clock.timestamp_ns())
        for state in self._states.values():
            if not state.entry_query_pending or state.entry_order.is_closed:
                state.entry_query_pending = False
                continue
            if now_ns - state.submitted_at_ns < _AMBIGUOUS_QUERY_AFTER_NS:
                continue
            self.query_order(state.entry_order, client_id=ClientId("BINANCE"))
            state.submitted_at_ns = now_ns

    def on_position_opened(self, event: Any) -> None:
        signal_id = self._signal_for_opening_order(event.opening_order_id)
        if signal_id is None:
            self._readiness.halt_for_unexpected_exposure()
            return
        state = self._states[signal_id]
        if event.instrument_id != state.route.instrument_id or event.account_id != self._profile.account_id:
            self._readiness.halt_for_unexpected_exposure()
            return
        expected_side = PositionSide.LONG if state.signal.direction == "long" else PositionSide.SHORT
        if event.strategy_id != self.id or event.side != expected_side:
            self._readiness.halt_for_unexpected_exposure()
            return
        state.position_id = event.position_id
        state.position_quantity = abs(Decimal(str(event.quantity)))
        state.avg_entry_price = Decimal(str(event.avg_px_open))
        self._positions[event.position_id] = signal_id
        self._request_stop(state, state.position_quantity, state.avg_entry_price)
        self._observe_position(state, "opened", int(event.ts_opened))

    def on_position_changed(self, event: Any) -> None:
        signal_id = self._positions.get(event.position_id)
        if signal_id is None:
            self._readiness.halt_for_unexpected_exposure()
            return
        state = self._states[signal_id]
        quantity = abs(Decimal(str(event.quantity)))
        avg_price = Decimal(str(event.avg_px_open))
        state.position_quantity = quantity
        state.avg_entry_price = avg_price
        self._observe_position(state, "changed", self._event_ns(event))
        self._request_stop(state, quantity, avg_price)

    def on_position_closed(self, event: Any) -> None:
        signal_id = self._positions.pop(event.position_id, None)
        if signal_id is None:
            self._readiness.halt_for_unexpected_exposure()
            return
        state = self._states[signal_id]
        state.position_quantity = Decimal(0)
        state.active = False
        if state.stop_order is not None and not state.stop_order.is_closed:
            self.cancel_order(state.stop_order, client_id=ClientId("BINANCE"))
        if state.pending_stop_order is not None and not state.pending_stop_order.is_closed:
            self.cancel_order(state.pending_stop_order, client_id=ClientId("BINANCE"))
        for retiring in state.retiring_stop_orders.values():
            if not retiring.is_closed:
                self.cancel_order(retiring, client_id=ClientId("BINANCE"))
        state.exit_retry_required = False
        self._observe_position(state, "closed", int(event.ts_closed))

    def on_order_canceled(self, event: Any) -> None:
        identity = self._orders.get(event.client_order_id)
        if identity is None:
            return
        state = self._states[identity[0]]
        self._observe_native_order_event(state, identity[1], "canceled", event)
        self._handle_known_order_terminal(state, event.client_order_id, identity[1])

    def on_order_accepted(self, event: Any) -> None:
        identity = self._orders.get(event.client_order_id)
        if identity is None:
            return
        state = self._states[identity[0]]
        if identity[1] == "entry":
            state.entry_query_pending = False
        elif identity[1] == "protection":
            self._accept_pending_stop(state, event.client_order_id)
        self._observe_native_order_event(state, identity[1], "accepted", event)

    def on_order_filled(self, event: Any) -> None:
        identity = self._orders.get(event.client_order_id)
        if identity is None:
            return
        state = self._states[identity[0]]
        if identity[1] == "entry":
            state.entry_query_pending = False
        now_ns = self._event_ns(event)
        references = [event.client_order_id.value]
        for name in ("venue_order_id", "trade_id", "position_id"):
            value = getattr(event, name, None)
            if value is not None:
                references.append(value.value)
        self._audit.offer(
            self._observations.create(
                normalized_kind="fill",
                signal_id=state.signal.signal_id,
                occurred_at_ns=now_ns,
                observed_at_ns=max(now_ns, int(self.clock.timestamp_ns())),
                native_identity_references=references,
                summary={
                    "leg": identity[1],
                    "last_quantity": str(event.last_qty),
                    "last_price": str(event.last_px),
                },
                payload={
                    "leg": identity[1],
                    "client_order_id": event.client_order_id.value,
                    "last_quantity": str(event.last_qty),
                    "last_price": str(event.last_px),
                },
                event_identity=f"fill:{getattr(event, 'trade_id', event.client_order_id)}",
            )
        )

    def on_order_rejected(self, event: Any) -> None:
        self._handle_order_rejected(event, "rejected")

    def on_order_denied(self, event: Any) -> None:
        self._handle_order_rejected(event, "denied")

    def on_order_expired(self, event: Any) -> None:
        self._handle_order_rejected(event, "expired")

    def _handle_order_rejected(self, event: Any, status: str) -> None:
        identity = self._orders.get(event.client_order_id)
        if identity is None:
            return
        state = self._states[identity[0]]
        reason = str(getattr(event, "reason", "")).lower()
        order = self._order_for_event(state, event.client_order_id, identity[1])
        if status == "rejected" and order is not None and any(token in reason for token in _AMBIGUOUS_REASONS):
            if identity[1] == "entry":
                state.entry_query_pending = True
                state.submitted_at_ns = int(self.clock.timestamp_ns())
            self.query_order(order, client_id=ClientId("BINANCE"))
            self._observe_order(state, order, identity[1], "unknown_query_first")
            if (
                identity[1] == "protection"
                and event.client_order_id not in state.retiring_stop_orders
                and state.position_quantity > 0
                and state.position_id is not None
            ):
                self.flatten_position(state.position_id)
            return
        self._handle_known_order_terminal(state, event.client_order_id, identity[1])
        now_ns = self._event_ns(event)
        self._audit.offer(
            self._observations.create(
                normalized_kind="order" if identity[1] in {"entry", "exit"} else "protection",
                signal_id=state.signal.signal_id,
                occurred_at_ns=now_ns,
                observed_at_ns=now_ns,
                native_identity_references=(event.client_order_id.value,),
                summary={"leg": identity[1], "status": status},
                payload={"client_order_id": event.client_order_id.value, "status": status},
                event_identity=status,
            )
        )

    def _handle_known_order_terminal(
        self,
        state: _ExecutionState,
        client_order_id: ClientOrderId,
        leg: str,
    ) -> None:
        if leg == "entry":
            state.entry_query_pending = False
            if state.position_quantity <= 0:
                state.active = False
            return
        if leg == "exit":
            if state.exit_order is not None and state.exit_order.client_order_id == client_order_id:
                state.exit_order = None
                if state.exit_retry_budget > 0:
                    state.exit_generation += 1
                    state.exit_retry_budget -= 1
                    state.exit_retry_required = state.position_quantity > 0
                else:
                    state.exit_retry_required = False
                self._readiness.halt_for_unexpected_exposure()
            return
        if leg != "protection":
            return
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
            self.flatten_position(state.position_id)

    def _observe_native_order_event(
        self,
        state: _ExecutionState,
        leg: str,
        status: str,
        event: Any,
    ) -> None:
        now_ns = self._event_ns(event)
        references = [event.client_order_id.value]
        venue_order_id = getattr(event, "venue_order_id", None)
        if venue_order_id is not None:
            references.append(venue_order_id.value)
        self._audit.offer(
            self._observations.create(
                normalized_kind="order" if leg in {"entry", "exit"} else "protection",
                signal_id=state.signal.signal_id,
                occurred_at_ns=now_ns,
                observed_at_ns=max(now_ns, int(self.clock.timestamp_ns())),
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

    def _request_stop(self, state: _ExecutionState, quantity: Decimal, avg_price: Decimal) -> None:
        if quantity <= 0:
            return
        state.desired_stop = (quantity, avg_price)
        if state.pending_stop_order is not None:
            return
        if state.stop_order is not None and state.stop_quantity == quantity and state.stop_avg_price == avg_price:
            return
        self._submit_stop(state, quantity, avg_price)

    def _submit_stop(self, state: _ExecutionState, quantity: Decimal, avg_price: Decimal) -> None:
        instrument = self.cache.instrument(state.route.instrument_id)
        if instrument is None:
            self._readiness.halt_for_unexpected_exposure()
            return
        distance = Decimal(state.route.stop_distance_bps) / Decimal(10_000)
        trigger = avg_price * (Decimal(1) - distance if state.signal.direction == "long" else Decimal(1) + distance)
        side = OrderSide.SELL if state.signal.direction == "long" else OrderSide.BUY
        quantity_value = instrument.make_qty(quantity)
        trigger_price = instrument.make_price(trigger)
        state.protection_generation += 1
        leg = _protection_leg(state.protection_generation, quantity_value.as_decimal())
        client_order_id = deterministic_client_order_id(
            namespace=self._profile.client_order_namespace,
            profile_id=self._profile.profile_id,
            signal_id=state.signal.signal_id,
            leg=leg,
        )
        existing = self.cache.order(client_order_id)
        if existing is not None:
            recovered = RecoveredProtectionSeed(
                role="pending",
                client_order_id=client_order_id,
                quantity=quantity_value.as_decimal(),
                trigger_price=trigger_price.as_decimal(),
                generation=state.protection_generation,
            )
            if not self._recovered_protection_valid(state=state, seed=recovered, protection=existing):
                self._orders[client_order_id] = (state.signal.signal_id, "protection")
                if not existing.is_closed:
                    self.query_order(existing, client_id=ClientId("BINANCE"))
                self._observe_order(state, existing, "protection", "replayed_invalid_flatten")
                if state.position_id is not None:
                    self.flatten_position(state.position_id)
                return
            state.pending_stop_order = existing
            state.pending_stop_quantity = quantity_value.as_decimal()
            state.pending_stop_avg_price = avg_price
            self._orders[client_order_id] = (state.signal.signal_id, "protection")
            self.query_order(existing, client_id=ClientId("BINANCE"))
            self._observe_order(state, existing, "protection", "replayed_query_first")
            if existing.is_open:
                self._accept_pending_stop(state, client_order_id)
            elif state.stop_order is None and state.position_id is not None:
                self.flatten_position(state.position_id)
            return
        order = self.order_factory.stop_market(
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
        self._orders[client_order_id] = (state.signal.signal_id, "protection")
        try:
            self.submit_order(order, position_id=state.position_id, client_id=ClientId("BINANCE"))
        except Exception:
            self.query_order(order, client_id=ClientId("BINANCE"))
            if state.position_id is not None:
                self.flatten_position(state.position_id)
        now_ns = int(self.clock.timestamp_ns())
        self._audit.offer(
            self._observations.create(
                normalized_kind="protection",
                signal_id=state.signal.signal_id,
                occurred_at_ns=now_ns,
                observed_at_ns=now_ns,
                native_identity_references=(client_order_id.value,),
                summary={"explicit_quantity": str(quantity_value.as_decimal()), "reduce_only": True},
                payload={
                    "client_order_id": client_order_id.value,
                    "quantity": str(quantity_value.as_decimal()),
                    "trigger_price": str(trigger_price.as_decimal()),
                    "reduce_only": True,
                },
                event_identity=leg,
            )
        )

    def _accept_pending_stop(self, state: _ExecutionState, client_order_id: ClientOrderId) -> None:
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
            self.cancel_order(previous, client_id=ClientId("BINANCE"))
        desired = state.desired_stop
        if desired is not None and (desired[0] != state.stop_quantity or desired[1] != state.stop_avg_price):
            self._submit_stop(state, *desired)

    @staticmethod
    def _order_for_event(state: _ExecutionState, client_order_id: ClientOrderId, leg: str) -> Any:
        if leg == "entry":
            return state.entry_order
        if leg == "exit":
            return state.exit_order
        for candidate in (
            state.pending_stop_order,
            state.stop_order,
            state.retiring_stop_orders.get(client_order_id),
        ):
            if candidate is not None and candidate.client_order_id == client_order_id:
                return candidate
        return None

    def _retry_failed_exits(self) -> None:
        for state in self._states.values():
            if state.exit_retry_required and state.position_id is not None and state.position_quantity > 0:
                self.flatten_position(state.position_id)

    def flatten_position(self, position_id: PositionId) -> None:
        """Risk-reducing exit remains available when audit or singleton entry gates fail."""

        signal_id = self._positions.get(position_id)
        if signal_id is None:
            raise ValueError("oi_runtime_position_not_owned")
        state = self._states[signal_id]
        if state.position_quantity <= 0:
            return
        instrument = self.cache.instrument(state.route.instrument_id)
        if instrument is None:
            raise RuntimeError("oi_runtime_instrument_missing")
        side = OrderSide.SELL if state.signal.direction == "long" else OrderSide.BUY
        client_order_id = deterministic_client_order_id(
            namespace=self._profile.client_order_namespace,
            profile_id=self._profile.profile_id,
            signal_id=signal_id,
            leg=_exit_leg(state.exit_generation),
        )
        existing = state.exit_order
        if existing is not None:
            if existing.is_closed:
                state.exit_order = None
                state.exit_generation += 1
                state.exit_retry_required = True
                return
            state.exit_order = existing
            state.exit_retry_required = False
            self._orders[client_order_id] = (signal_id, "exit")
            self.query_order(existing, client_id=ClientId("BINANCE"))
            self._observe_order(state, existing, "exit", "replayed_query_first")
            return
        cached = self.cache.order(client_order_id)
        if cached is not None:
            position = self.cache.position(position_id)
            if position is None or not self._exit_order_valid(state=state, exit_order=cached, position=position):
                self._readiness.halt_for_unexpected_exposure()
                return
            if cached.is_closed:
                state.exit_generation += 1
                state.exit_retry_required = True
                return
            state.exit_order = cached
            state.exit_retry_required = False
            self._orders[client_order_id] = (signal_id, "exit")
            self.query_order(cached, client_id=ClientId("BINANCE"))
            self._observe_order(state, cached, "exit", "replayed_query_first")
            return
        order = self.order_factory.market(
            instrument_id=state.route.instrument_id,
            order_side=side,
            quantity=instrument.make_qty(state.position_quantity),
            reduce_only=True,
            client_order_id=client_order_id,
        )
        state.exit_order = order
        state.exit_retry_required = False
        self._orders[client_order_id] = (signal_id, "exit")
        try:
            self.submit_order(order, position_id=position_id, client_id=ClientId("BINANCE"))
        except Exception:
            self.query_order(order, client_id=ClientId("BINANCE"))
        self._observe_order(state, order, "exit", "submitted_or_unknown")

    def _signal_for_opening_order(self, client_order_id: ClientOrderId) -> str | None:
        identity = self._orders.get(client_order_id)
        if identity is None or identity[1] != "entry":
            return None
        return identity[0]

    def _observe_position(self, state: _ExecutionState, status: str, occurred_at_ns: int) -> None:
        observed_at_ns = max(occurred_at_ns, int(self.clock.timestamp_ns()))
        references = () if state.position_id is None else (state.position_id.value,)
        self._audit.offer(
            self._observations.create(
                normalized_kind="position",
                signal_id=state.signal.signal_id,
                occurred_at_ns=occurred_at_ns,
                observed_at_ns=observed_at_ns,
                native_identity_references=references,
                summary={"status": status, "quantity": str(state.position_quantity)},
                payload={"status": status, "quantity": str(state.position_quantity)},
                event_identity=f"{status}:{state.position_quantity}:{occurred_at_ns}",
            )
        )

    @staticmethod
    def _event_ns(event: Any) -> int:
        return int(getattr(event, "ts_event", getattr(event, "ts_init", 0)))


__all__ = [
    "OiNautilusStrategy",
    "RecoveredExecutionSeed",
    "RecoveredProtectionSeed",
    "RuntimeControlSnapshot",
    "RuntimeReadiness",
    "RuntimeReadinessSnapshot",
    "RuntimeReconciliationSnapshot",
    "deterministic_client_order_id",
    "oi_strategy_config",
]
