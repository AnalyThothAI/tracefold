"""Concrete OI Runtime execution state and lifecycle invariants."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from threading import Lock
from typing import Any, Literal

from nautilus_trader.model.enums import OrderSide, OrderType
from nautilus_trader.model.identifiers import ClientOrderId, PositionId

from tracefold.trading import OperatorIntentV1, TradeSignalV1

from .config import OiInstrumentRoute, OiRuntimeProfile

PrivateReconciliationReason = Literal[
    "unknown_outcome",
    "protection_ambiguity",
    "flatten_pending",
    "unexpected_exposure",
]
PRIVATE_RECONCILIATION_REASONS = frozenset(
    {"unknown_outcome", "protection_ambiguity", "flatten_pending", "unexpected_exposure"}
)


def deterministic_client_order_id(
    *,
    namespace: str,
    profile_id: str,
    entry_id: str,
    leg: str,
) -> ClientOrderId:
    digest = hashlib.sha256(f"{namespace}:{profile_id}:{entry_id}:{leg}".encode()).hexdigest()
    return ClientOrderId(f"tf{digest[:30]}")


def protection_leg(generation: int, quantity: Decimal) -> str:
    return f"protection:{generation}:{format(quantity.normalize(), 'f')}"


def exit_leg(generation: int) -> str:
    return "exit" if generation == 0 else f"exit:{generation}"


@dataclass(frozen=True, slots=True)
class RuntimeReadinessSnapshot:
    execution_safe: bool
    entries_armed: bool
    entry_block_reason: str | None
    singleton_ready: bool
    activation_ready: bool
    startup_reconciled: bool
    portfolio_ready: bool
    control_plane_ready: bool
    audit_ready: bool
    day_start_ready: bool
    unexpected_exposure: bool
    reconciliation_observed_at_ns: int


@dataclass(frozen=True, slots=True)
class RuntimeControlSnapshot:
    entries_paused: bool
    emergency_halted: bool
    flatten_pending: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuntimeEntryRequest:
    """One concrete entry request, correlated to exactly one durable fact type."""

    entry_id: str
    market_key: str
    direction: Literal["long", "short"]
    expires_at_ns: int
    signal: TradeSignalV1 | None = None
    command: OperatorIntentV1 | None = None

    def __post_init__(self) -> None:
        if (self.signal is None) == (self.command is None):
            raise ValueError("oi_runtime_entry_source_invalid")
        if self.signal is not None:
            expected = (
                self.signal.signal_id,
                self.signal.market_key,
                self.signal.direction,
                self.signal.expires_at_ns,
            )
        else:
            command = self.command
            if (
                command is None
                or command.action != "manual_entry"
                or command.market_key is None
                or command.direction is None
            ):
                raise ValueError("oi_runtime_manual_entry_invalid")
            expected = (
                command.command_id,
                command.market_key,
                command.direction,
                command.expires_at_ns,
            )
        if expected != (self.entry_id, self.market_key, self.direction, self.expires_at_ns):
            raise ValueError("oi_runtime_entry_source_invalid")

    @classmethod
    def from_signal(cls, signal: TradeSignalV1) -> RuntimeEntryRequest:
        return cls(
            entry_id=signal.signal_id,
            market_key=signal.market_key,
            direction=signal.direction,
            expires_at_ns=signal.expires_at_ns,
            signal=signal,
        )

    @classmethod
    def from_manual_command(cls, command: OperatorIntentV1) -> RuntimeEntryRequest:
        if command.action != "manual_entry" or command.market_key is None or command.direction is None:
            raise ValueError("oi_runtime_manual_entry_invalid")
        return cls(
            entry_id=command.command_id,
            market_key=command.market_key,
            direction=command.direction,
            expires_at_ns=command.expires_at_ns,
            command=command,
        )


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
        portfolio_ready: bool,
        control_plane_ready: bool,
        audit_ready: bool,
        day_start_ready: bool,
        entries_paused: bool,
        emergency_halted: bool,
    ) -> RuntimeReadinessSnapshot:
        with self._lock:
            activation = self._activation_ready
            startup = self._startup_reconciled
            unexpected = self._unexpected_exposure
            reconciled_at = self._reconciliation_observed_at_ns
        safe_gates = (
            (singleton_ready, "singleton_unavailable"),
            (activation, "activation_missing"),
            (startup, "startup_reconciliation_unproven"),
            (portfolio_ready, "portfolio_unavailable"),
            (not unexpected, "unexpected_exposure"),
        )
        reason: str | None = None
        for passed, failed_reason in safe_gates:
            if not passed:
                reason = failed_reason
                break
        execution_safe = reason is None
        if execution_safe:
            entry_gates = (
                (not emergency_halted, "emergency_halt"),
                (not entries_paused, "entries_paused"),
                (control_plane_ready, "control_plane_unavailable"),
                (audit_ready, "audit_unavailable"),
                (day_start_ready, "day_start_baseline_missing"),
            )
            for passed, failed_reason in entry_gates:
                if not passed:
                    reason = failed_reason
                    break
        return RuntimeReadinessSnapshot(
            execution_safe=execution_safe,
            entries_armed=reason is None,
            entry_block_reason=reason,
            singleton_ready=singleton_ready,
            activation_ready=activation,
            startup_reconciled=startup,
            portfolio_ready=portfolio_ready,
            control_plane_ready=control_plane_ready,
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

    entry: RuntimeEntryRequest
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
class ExecutionState:
    entry: RuntimeEntryRequest
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
    private_reconciliation_requested: bool = False


@dataclass(slots=True)
class RuntimeExecutionState:
    """The one mutable aggregate shared by the concrete lifecycle coordinators."""

    entries_paused: bool
    emergency_halted: bool
    executions: dict[str, ExecutionState] = field(default_factory=dict)
    orders: dict[ClientOrderId, tuple[str, str]] = field(default_factory=dict)
    positions: dict[PositionId, str] = field(default_factory=dict)
    disposed_signal_ids: set[str] = field(default_factory=set)
    disposed_command_ids: set[str] = field(default_factory=set)
    pending_flatten: dict[str, OperatorIntentV1] = field(default_factory=dict)
    flatten_accept_observed: set[str] = field(default_factory=set)
    unexpected_exposure_reconciliation_requested: bool = False

    @classmethod
    def from_control_snapshot(cls, snapshot: RuntimeControlSnapshot | None) -> RuntimeExecutionState:
        control = snapshot or RuntimeControlSnapshot(True, False, ())
        if control.flatten_pending:
            raise ValueError("oi_runtime_initial_control_state_invalid")
        return cls(
            entries_paused=control.entries_paused,
            emergency_halted=control.emergency_halted,
        )

    def control_snapshot(self) -> RuntimeControlSnapshot:
        return RuntimeControlSnapshot(
            entries_paused=self.entries_paused,
            emergency_halted=self.emergency_halted,
            flatten_pending=tuple(sorted(self.pending_flatten)),
        )

    def state_for_order(self, client_order_id: ClientOrderId) -> tuple[ExecutionState, str] | None:
        identity = self.orders.get(client_order_id)
        if identity is None:
            return None
        return self.executions[identity[0]], identity[1]

    def entry_for_opening_order(self, client_order_id: ClientOrderId) -> str | None:
        identity = self.orders.get(client_order_id)
        if identity is None or identity[1] != "entry":
            return None
        return identity[0]


def entry_order_valid(
    *,
    profile: OiRuntimeProfile,
    strategy_id: Any,
    request: RuntimeEntryRequest,
    route: OiInstrumentRoute,
    order: Any,
) -> bool:
    expected_id = deterministic_client_order_id(
        namespace=profile.client_order_namespace,
        profile_id=profile.profile_id,
        entry_id=request.entry_id,
        leg="entry",
    )
    expected_side = OrderSide.BUY if request.direction == "long" else OrderSide.SELL
    return bool(
        order is not None
        and order.client_order_id == expected_id
        and order.strategy_id == strategy_id
        and order.account_id == profile.account_id
        and order.instrument_id == route.instrument_id
        and order.side == expected_side
        and order.order_type == OrderType.MARKET
        and not order.is_reduce_only
        and order.quantity.as_decimal() > 0
    )


def exit_order_valid(
    *,
    profile: OiRuntimeProfile,
    strategy_id: Any,
    cache: Any,
    state: ExecutionState,
    exit_order: Any,
    position: Any,
) -> bool:
    expected_id = deterministic_client_order_id(
        namespace=profile.client_order_namespace,
        profile_id=profile.profile_id,
        entry_id=state.entry.entry_id,
        leg=exit_leg(state.exit_generation),
    )
    expected_side = OrderSide.SELL if state.entry.direction == "long" else OrderSide.BUY
    return bool(
        exit_order is not None
        and exit_order.client_order_id == expected_id
        and exit_order.strategy_id == strategy_id
        and exit_order.account_id == profile.account_id
        and exit_order.instrument_id == state.route.instrument_id
        and exit_order.side == expected_side
        and exit_order.order_type == OrderType.MARKET
        and exit_order.is_reduce_only
        and exit_order.quantity.as_decimal() == state.position_quantity
        and cache.position_for_order(exit_order.client_order_id) == position
    )


def order_for_event(state: ExecutionState, client_order_id: ClientOrderId, leg: str) -> Any:
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


__all__ = [
    "PRIVATE_RECONCILIATION_REASONS",
    "ExecutionState",
    "PrivateReconciliationReason",
    "RecoveredExecutionSeed",
    "RecoveredProtectionSeed",
    "RuntimeControlSnapshot",
    "RuntimeEntryRequest",
    "RuntimeExecutionState",
    "RuntimeReadiness",
    "RuntimeReadinessSnapshot",
    "RuntimeReconciliationSnapshot",
    "deterministic_client_order_id",
    "entry_order_valid",
    "exit_leg",
    "exit_order_valid",
    "order_for_event",
    "protection_leg",
]
