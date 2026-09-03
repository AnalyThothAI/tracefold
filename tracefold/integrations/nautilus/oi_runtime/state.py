"""Concrete OI Runtime execution state and lifecycle invariants."""

from __future__ import annotations

import hashlib
from collections.abc import Container
from dataclasses import dataclass, field
from decimal import Decimal
from threading import Lock
from typing import Any, Literal

from nautilus_trader.model.enums import OrderSide, OrderType
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, PositionId

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
# How long an entry may keep a freshly opened quote subscription alive while waiting for the venue's
# first tick. Binance closed the WebSocket with 1008 `Too many requests` when the Runtime subscribed
# to all ~500 routed perpetuals at start, so a stream is opened per admitted entry instead (#510 E).
# Two seconds is inside every Signal TTL and is spent as retries, never as a blocked event loop.
QUOTE_WARMUP_NS = 2_000_000_000


def deterministic_client_order_id(
    *,
    namespace: str,
    entry_id: str,
    leg: str,
) -> ClientOrderId:
    """One venue order id per (account slot, mode, intent, leg); the namespace carries the first two."""

    digest = hashlib.sha256(f"{namespace}:{entry_id}:{leg}".encode()).hexdigest()
    return ClientOrderId(f"tf{digest[:30]}")


def protection_leg(generation: int, quantity: Decimal) -> str:
    return f"protection:{generation}:{format(quantity.normalize(), 'f')}"


def exit_leg(generation: int) -> str:
    return "exit" if generation == 0 else f"exit:{generation}"


@dataclass(frozen=True, slots=True)
class RuntimeReadinessSnapshot:
    """The two readiness facts this Runtime derives, plus the raw facts they are derived from.

    `alive` is the third and belongs to the composition root's loop, which is the only thing that
    knows the node, the event loop and the database session are all still up. The five booleans that
    used to sit here - singleton, portfolio, control plane, audit and day start - were true whenever
    the process could run at all, or gated entries on something that is not a risk (#520 PR-B).
    """

    execution_safe: bool
    entries_armed: bool
    entry_block_reason: str | None
    startup_reconciled: bool
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

    def __init__(self, *, reconciliation_stale_after_ns: int) -> None:
        if reconciliation_stale_after_ns <= 0:
            raise ValueError("oi_runtime_reconciliation_staleness_invalid")
        self._reconciliation_stale_after_ns = reconciliation_stale_after_ns
        self._startup_reconciled = False
        self._unexpected_exposure = False
        self._account_observed_at_ns = 0
        self._reconciliation_observed_at_ns = 0
        self._lock = Lock()

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

    def facts_clock(self) -> tuple[int, int]:
        with self._lock:
            return self._account_observed_at_ns, self._reconciliation_observed_at_ns

    def snapshot(
        self,
        *,
        now_ns: int,
        singleton_ready: bool,
        entries_paused: bool,
        emergency_halted: bool,
    ) -> RuntimeReadinessSnapshot:
        """Derive `execution_safe` and `entries_armed` from facts, never from a startup ritual.

        `execution_safe` means this Runtime's picture of the account is current and undisputed:
        startup reconciliation happened, the private scan behind it is still fresh, no exposure it
        does not own showed up, and it still holds the account slot. `entries_armed` adds only what
        an operator asked for. Everything else an entry needs - equity, quotes, the day baseline, a
        writable audit - is answered on the entry path itself against that request's own facts.
        """

        with self._lock:
            startup = self._startup_reconciled
            unexpected = self._unexpected_exposure
            reconciled_at = self._reconciliation_observed_at_ns
        safe_gates = (
            (startup, "startup_reconciliation_unproven"),
            (now_ns - reconciled_at <= self._reconciliation_stale_after_ns, "reconciliation_stale"),
            (not unexpected, "unexpected_exposure"),
            (singleton_ready, "singleton_lost"),
        )
        reason: str | None = None
        for passed, failed_reason in safe_gates:
            if not passed:
                reason = failed_reason
                break
        execution_safe = reason is None
        if execution_safe:
            for passed, failed_reason in (
                (not emergency_halted, "emergency_halted"),
                (not entries_paused, "entries_paused"),
            ):
                if not passed:
                    reason = failed_reason
                    break
        return RuntimeReadinessSnapshot(
            execution_safe=execution_safe,
            entries_armed=reason is None,
            entry_block_reason=reason,
            startup_reconciled=startup,
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
    account_slot: str
    account_observed_at_ns: int
    reconciliation_observed_at_ns: int
    executions: tuple[RecoveredExecutionSeed, ...] = ()


@dataclass(slots=True)
class ExecutionState:
    entry: RuntimeEntryRequest
    route: OiInstrumentRoute
    entry_client_order_id: ClientOrderId
    submitted_at_ns: int
    disposition_reason: str
    # A restart reclaims the position from durable order facts plus the Binance reports, and the
    # filled entry market order is in neither: Nautilus Cache is process memory and Binance only
    # reports open orders. `None` therefore means "this execution was recovered, not submitted".
    entry_order: Any = None
    active: bool = True
    entry_query_pending: bool = False
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
    # Why this execution's exposure is being closed, written by whichever exit entry point asked for
    # it. `None` means no reduce-only exit was requested, so a close is the protective stop filling.
    exit_reason: Literal["flatten"] | None = None
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
    # Reduce-only closes this Runtime submitted for positions it does not own; `/flatten account`
    # converges the whole account slot, so these have no entry identity to hang off.
    unclaimed_flatten_orders: dict[PositionId, Any] = field(default_factory=dict)
    unclaimed_flatten_attempts: dict[PositionId, int] = field(default_factory=dict)
    # Instrument -> the clock at which this Runtime asked the venue for its quotes. Only the event
    # loop touches it, like every other field here.
    quote_subscriptions: dict[InstrumentId, int] = field(default_factory=dict)
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


def unowned_cache_exposure(
    *,
    cache: Any,
    account_id: Any,
    strategy_id: Any,
    owned_order_ids: Container[ClientOrderId],
    owned_position_ids: Container[PositionId],
) -> tuple[frozenset[ClientOrderId], frozenset[PositionId]]:
    """Open/in-flight orders and open positions on this account slot that this Runtime does not own.

    One scan and one definition of "ours": the identity is in this Runtime's own map and Nautilus
    agrees the strategy holding it is this one. The same question was asked in four places - the risk
    facts, both recovery paths, and the operator projection - and each had drifted its own way about
    in-flight orders and the strategy check (#510 E).
    """

    orders = frozenset(
        order.client_order_id
        for order in (
            *cache.orders_open(account_id=account_id),
            *cache.orders_inflight(account_id=account_id),
        )
        if order.client_order_id not in owned_order_ids or order.strategy_id != strategy_id
    )
    positions = frozenset(
        position.id
        for position in cache.positions_open(account_id=account_id)
        if position.id not in owned_position_ids or position.strategy_id != strategy_id
    )
    return orders, positions


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
        # Same as a reclaimed stop: a reduce-only exit read back from Binance carries no Cache
        # position index until it fills.
        and cache.position_for_order(exit_order.client_order_id) in {None, position}
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
    "QUOTE_WARMUP_NS",
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
    "unowned_cache_exposure",
]
