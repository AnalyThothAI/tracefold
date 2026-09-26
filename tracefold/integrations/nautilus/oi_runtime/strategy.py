"""The OI Runtime's Nautilus Strategy: Nautilus owns every order and position, this owns intent (#680).

The Nautilus Cache is the only in-process execution state. It is rebuilt from the venue by Nautilus'
startup reconciliation before this Strategy starts and kept converged by Nautilus' five-second checks
after, so a restart and a steady tick are the same situation and this module has one path for both.
What it holds itself is intent, not execution: the non-terminal TradePlans (durable, loaded at start),
the inputs still waiting on a verdict, and the operator's control switches.

Everything this module decides is level-triggered from the Cache and idempotent:

* entry: a Signal or manual Command passes the gates in order, waits within its TTL for a quote and a
  narrow enough spread, and becomes one committed plan and one market order with a deterministic
  client order id -- never a second one;
* protection: a position whose entry order is terminal gets one reduce-only `STOP_MARKET` and one
  reduce-only `TAKE_PROFIT_MARKET`, both triggered on the mark price, placed once from the average
  fill price; a missing one is placed again, a present one is never compared, resized or replaced;
* exits: past its maximum holding time a position is closed with a reduce-only market order; when a
  position is closed by one of this Runtime's own closing legs (stop, take-profit, time exit, operator
  flatten), every order left on its instrument is canceled and its plan ends with that leg's reason;
* venue truth (#680 PR-3): the Cache is compared with the venue's own positions, read by the root every
  30 s. A close no closing leg explains -- a venue-side close, or a fill Nautilus' reconciliation
  invented -- keeps the instrument's stop and take-profit and its plan until a venue read confirms the
  instrument flat; only then are they canceled and the plan ended. A venue position the Cache does not
  hold, or a Cache position the venue does not, is unexpected exposure once two reads in a row agree
  on it;
* uncertainty: exposure no plan claims blocks new entries and is recorded, and so does a venue this
  Runtime could not read or has not yet seen agree with the Cache. Nothing is ever flattened because
  the picture is unclear.

No callback here lets an exception escape into Nautilus or the event loop, and nothing here reads a
private (`_`-prefixed) member of a Nautilus object.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from threading import Lock
from typing import Any, Literal

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.enums import OrderSide, OrderStatus, OrderType, TriggerType
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId
from nautilus_trader.trading.strategy import Strategy

from tracefold.trading.execution_contracts import (
    OperatorIntentV1,
    TradeSignalV3,
    entry_condition_allows,
)
from tracefold.trading.storage.execution_stream import ExecutionAccountSnapshot, ExecutionExposureFinding
from tracefold.trading.trade_plan import ExitReason, PlanOrderBinding, TradePlan

from .account_projection import (
    OrderLeg,
    account_snapshot,
    exposure_findings,
    open_and_inflight_orders,
    position_claimed,
)
from .config import CONTINUOUS_CHECK_SECONDS, OiInstrumentRoute, OiRiskLimits, OiRuntimeProfile
from .entry import (
    RuntimeEntryRequest,
    deterministic_client_order_id,
    entry_quantity,
    initial_plan_order_bindings,
    protective_trigger,
    spread_bps,
)
from .funding import FundingCashflow
from .journal import ExecutionJournal
from .observations import RuntimeObservations, bounded_text, spread_detail
from .risk import DayStartBaseline, account_equity_usd, decimal_value
from .signal_client import ExecutionSignalClient
from .venue import VENUE_SETTLE_NS, VENUE_STALE_AFTER_NS, VenueReading

_STRATEGY_ID = "OI-RUNTIME"
_CALLBACK_BATCH = 16
_PUMP_INTERVAL_MS = 100
_CONVERGE_INTERVAL_NS = int(CONTINUOUS_CHECK_SECONDS * 1_000_000_000)
_RECOVERY_INITIAL_DELAY_NS = 5_000_000_000
_RECOVERY_MAX_DELAY_NS = 60_000_000_000
# The venue refusing a protective order because its trigger is already crossed (`-2021 Order would
# immediately trigger`). The stop or take-profit condition is then already met, so the position is
# closed at market under that leg's reason instead of retrying a trigger that can never rest.
_IMMEDIATE_TRIGGER_MARKERS = ("-2021", "immediately trigger")

EntryVerdict = Literal["refuse", "defer", "admit"]


def oi_strategy_config(profile: OiRuntimeProfile) -> StrategyConfig:
    """One Strategy claims every routed instrument, so every order Nautilus reconciles there is its own."""

    claims = sorted((route.instrument_id for route in profile.routes), key=lambda item: item.value)
    tag = hashlib.sha256(profile.namespace.encode()).hexdigest()[:3].upper()
    return StrategyConfig(
        strategy_id=_STRATEGY_ID,
        order_id_tag=tag,
        oms_type="NETTING",
        external_order_claims=claims,
    )


@dataclass(frozen=True, slots=True)
class RuntimeControlSnapshot:
    entries_paused: bool
    emergency_halted: bool


@dataclass(frozen=True, slots=True)
class OpenPlan:
    """A plan that has not ended, and whether its input still owes a durable verdict."""

    plan: TradePlan
    disposition_pending: bool
    signal: TradeSignalV3 | None = None
    final_check_started: bool = False


@dataclass(frozen=True, slots=True)
class RuntimeInputs:
    """What a Runtime generation reads from PostgreSQL once, before its Strategy starts."""

    control: RuntimeControlSnapshot
    open_plans: tuple[OpenPlan, ...] = ()
    order_bindings: tuple[PlanOrderBinding, ...] = ()
    # market_key -> the latest stop-out inside the cooldown window.
    stop_exits: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeView:
    """What the durable projection and the probe publish about this instant."""

    entries_armed: bool
    entry_block_reason: str | None
    unexpected_exposure: bool
    positions_count: int
    open_orders_count: int
    protection_status: Literal["not_applicable", "protected", "pending", "unprotected", "unknown"]
    account_snapshot: ExecutionAccountSnapshot
    convergence_checked_at_ns: int | None
    convergence_failure: str | None
    venue_read_started_at_ns: int | None
    venue_read_completed_at_ns: int | None
    venue_read_failure: str | None


@dataclass(frozen=True, slots=True)
class _RecoveryBackoff:
    signature: tuple[str, Decimal, Decimal]
    delay_ns: int
    next_attempt_ns: int


@dataclass(slots=True)
class _Deferred:
    request: RuntimeEntryRequest
    reason: str
    detail: dict[str, str]


@dataclass(frozen=True, slots=True)
class _Verdict:
    action: EntryVerdict
    reason: str = ""
    detail: dict[str, str] = field(default_factory=dict)
    plan: TradePlan | None = None


def _quote_verdict(
    request: RuntimeEntryRequest,
    quote: Any | None,
    *,
    now_ns: int,
    risk: OiRiskLimits,
    stop_distance_bps: int,
) -> _Verdict:
    """One side-effect-free market check for admission and commit-before-submit."""

    if quote is None or now_ns - int(quote.ts_event) > risk.market_stale_after_ns:
        return _Verdict("defer", "market_unavailable")
    spread = spread_bps(quote)
    if spread is None:
        return _Verdict("defer", "market_unavailable")
    if request.entry_envelope is not None:
        envelope = request.entry_envelope
        executable = decimal_value(quote.ask_price if request.direction == "long" else quote.bid_price)
        if not entry_condition_allows(direction=request.direction, executable=executable, envelope=envelope):
            return _Verdict("refuse", "entry_structure_lost")
        drift = abs(executable / envelope.reference_price - Decimal(1)) * Decimal(10_000)
        if drift > envelope.max_price_drift_bps:
            return _Verdict("refuse", "entry_price_outside_envelope")
    if spread > risk.max_spread_fraction_of_stop * Decimal(stop_distance_bps):
        return _Verdict("defer", "spread_limit", spread_detail(spread))
    return _Verdict("admit", detail=spread_detail(spread))


class OiNautilusStrategy(Strategy):
    """Route inputs and Nautilus events to intent; never call PostgreSQL synchronously."""

    def __init__(
        self,
        *,
        profile: OiRuntimeProfile,
        signals: ExecutionSignalClient,
        journal: ExecutionJournal,
        inputs: RuntimeInputs,
        # Whoever owns the one thread allowed to run the pump. On the pinned `nautilus-trader`
        # 1.231.0 order and position callbacks run on the asyncio event loop while a `LiveClock`
        # timer runs on a Rust-owned thread, so the live root passes `loop.call_soon_threadsafe`; a
        # single-threaded `BacktestEngine` passes direct invocation (#510 F).
        dispatch_pump: Callable[[Callable[[], None]], None],
        singleton_ready: Callable[[], bool],
        # Whether the venue's positions arrive through `observe_venue` (the live root, which reads
        # Binance) or the venue is the Cache itself (a `BacktestEngine`, whose simulated venue fills
        # straight into it). Named at every construction, so the live path cannot default into trusting
        # the Cache.
        venue_reads: bool,
        day_start: DayStartBaseline | None = None,
        config: StrategyConfig | None = None,
    ) -> None:
        selected = config or oi_strategy_config(profile)
        claims = sorted((route.instrument_id for route in profile.routes), key=lambda item: item.value)
        if selected.oms_type != "NETTING" or selected.external_order_claims != claims:
            raise ValueError("oi_runtime_strategy_claims_invalid")
        super().__init__(selected)
        self._profile = profile
        self._signals = signals
        self._journal = journal
        self._dispatch_pump = dispatch_pump
        self._singleton_ready = singleton_ready
        self._routes: dict[str, OiInstrumentRoute] = {route.market_key: route for route in profile.routes}
        self._observations = RuntimeObservations(journal=journal, signals=signals, timestamp_ns=self._now_ns)
        self._entries_paused = inputs.control.entries_paused
        self._emergency_halted = inputs.control.emergency_halted
        self._plans: dict[str, TradePlan] = {value.plan.entry_id: value.plan for value in inputs.open_plans}
        self._order_bindings: dict[str, PlanOrderBinding] = {}
        for plan in self._plans.values():
            self._bind_plan_orders(plan)
        for binding in inputs.order_bindings:
            self._register_order_binding(binding)
        self._stopped = False
        self._owed: set[str] = {value.plan.entry_id for value in inputs.open_plans if value.disposition_pending}
        self._stop_exits: dict[str, int] = dict(inputs.stop_exits)
        self._deferred: dict[str, _Deferred] = {}
        self._submitting: RuntimeEntryRequest | None = None
        self._awaiting_final: dict[str, RuntimeEntryRequest] = {}
        self._final_requested: set[str] = set()
        self._final_retry_at_ns: dict[str, int] = {}
        # A persisted final check may have been followed by a venue submission before the
        # previous process died. A missing cache order is not proof that submission failed.
        self._submission_unknown: set[str] = {
            value.plan.entry_id
            for value in inputs.open_plans
            if value.plan.status == "prepared" and value.signal is not None and value.final_check_started
        }
        for value in inputs.open_plans:
            if value.plan.status == "prepared" and value.signal is not None and not value.final_check_started:
                self._awaiting_final[value.plan.entry_id] = RuntimeEntryRequest.from_signal(value.signal)
        self._unexpected: tuple[str, ...] = ()
        self._findings: tuple[ExecutionExposureFinding, ...] = ()
        self._subscribed: set[InstrumentId] = set()
        self._converge_due_ns = 0
        self._convergence_checked_at_ns: int | None = None
        self._convergence_failure: str | None = None
        self._recovery_requested_read_ns = 0
        self._recovery_attempts: dict[str, _RecoveryBackoff] = {}
        self._day_start = day_start
        self._day_start_lock = Lock()
        # Venue truth (#680 PR-3). The latest successful read and the last failure's name; the read the
        # last convergence judged; per Binance symbol, a disagreement seen once (`suspect`) and one seen
        # on two reads in a row (`mismatch`, which is unexpected exposure).
        self._venue_reads = venue_reads
        self._venue: VenueReading | None = None
        self._venue_failure: str | None = None
        self._venue_judged_at_ns = 0
        self._venue_suspect: dict[str, str] = {}
        self._venue_mismatch: dict[str, str] = {}
        # When something last moved on an instrument, by this process's clock: a read that began before
        # it settled describes a different moment than the Cache does.
        self._activity_ns: dict[InstrumentId, int] = {}
        # A plan whose position the Cache closed without one of this Runtime's closing legs, waiting for
        # a venue read to confirm the instrument flat: entry id and the Cache's close time.
        self._unattributed_closes: dict[InstrumentId, tuple[str, int]] = {}
        # The last fill of one of this Runtime's own closing legs on a plan's instrument: entry id,
        # reason and fill time. A plan whose position the Cache never saw close ends with it.
        self._closing_fills: dict[InstrumentId, tuple[str, ExitReason, int]] = {}

    # -- lifecycle ---------------------------------------------------------------------------------

    def on_start(self) -> None:
        for plan in self._plans.values():
            self._subscribe(InstrumentId.from_str(plan.instrument_id))
        self.clock.set_timer(
            name=self._timer_name,
            interval=timedelta(milliseconds=_PUMP_INTERVAL_MS),
            callback=self.on_timer,
            fire_immediately=True,
        )

    def on_stop(self) -> None:
        self._stopped = True
        if self._timer_name in self.clock.timer_names:
            self.clock.cancel_timer(self._timer_name)
        for instrument_id in tuple(self._subscribed):
            self._subscribed.discard(instrument_id)
            self.unsubscribe_quote_ticks(instrument_id)

    @property
    def _timer_name(self) -> str:
        return f"{self.id}:OI-PUMP"

    def on_timer(self, _event: object) -> None:
        """Hand the pump to the callback thread; live, the timer thread is not it (#510 F)."""

        self._dispatch_pump(self._pump)

    def _pump(self) -> None:
        """Converge first, so no input is judged against a picture older than the last event."""

        if self._stopped:
            return
        now_ns = self._now_ns()
        if now_ns >= self._converge_due_ns:
            self._converge_due_ns = now_ns + _CONVERGE_INTERVAL_NS
            self._guard("converge", lambda: self._converge(now_ns))
        self._guard("commands", lambda: self._drain_commands(now_ns))
        if self._signals.queued_command_count == 0 and self._signals.command_scan_complete:
            self._guard("signals", lambda: self._drain_signals(now_ns))
        for deferred in tuple(self._deferred.values()):
            self._admit_guarded(deferred.request, now_ns)
        self._guard("entry_submit", lambda: self._submit_committed(now_ns))
        self._guard("quotes", self._sweep_quotes)

    def _drain_commands(self, now_ns: int) -> None:
        for _ in range(_CALLBACK_BATCH):
            command = self._signals.next_command_nowait()
            if command is None:
                return
            self._guard("command", partial(self._route_command, command, now_ns))

    def _drain_signals(self, now_ns: int) -> None:
        for _ in range(_CALLBACK_BATCH):
            signal = self._signals.next_nowait()
            if signal is None:
                return
            self._admit_guarded(RuntimeEntryRequest.from_signal(signal), now_ns)

    def _guard(self, step: str, run: Callable[[], object]) -> None:
        """No step's exception reaches Nautilus or the event loop; the next pump runs it again."""

        try:
            run()
        except Exception as exc:
            if step == "converge":
                self._convergence_failure = type(exc).__name__
            self.log.exception(f"OI Runtime step failed ({step})", exc)
        else:
            if step == "converge":
                self._convergence_failure = None

    def _now_ns(self) -> int:
        return int(self.clock.timestamp_ns())

    # -- operator commands -------------------------------------------------------------------------

    def _route_command(self, command: OperatorIntentV1, now_ns: int) -> None:
        if command.account_slot != self._profile.account_slot:
            self._observations.reject_command(command, "account_slot_mismatch")
            return
        if command.expires_at_ns <= now_ns:
            self._observations.reject_command(command, "expired")
            return
        if command.action == "pause_entries":
            self._entries_paused = True
            self._observations.accept_command(command, "entries_paused")
        elif command.action == "resume_entries":
            if self._emergency_halted:
                self._observations.reject_command(command, "emergency_halt_sticky")
                return
            self._entries_paused = False
            self._observations.accept_command(command, "entries_resumed")
        elif command.action == "emergency_halt":
            self._entries_paused = True
            self._emergency_halted = True
            self._observations.accept_command(command, "emergency_halted")
        elif command.action == "manual_entry":
            self._admit_guarded(RuntimeEntryRequest.from_manual_command(command), now_ns)
        elif command.action == "flatten" and command.scope == "account":
            self._flatten(command, now_ns)
        else:
            self._observations.reject_command(command, "flatten_scope_unsupported")

    def _flatten(self, command: OperatorIntentV1, now_ns: int) -> None:
        """Pause entries, cancel working entries and close every position this account holds.

        Every Cache position of this Strategy is closed with a reduce-only market order, and so is every
        position the latest venue read (if it is fresh) reports on an instrument where the Cache holds
        none -- the exposure a close Nautilus invented would otherwise leave out of reach. Reduce-only is
        what makes the second safe on a read up to two minutes old: the venue refuses an order that would
        open or flip anything, and Nautilus never opens a netting position from a reduce-only fill.
        Protective orders stay until the venue says each instrument is flat, so a close the venue refuses
        leaves the position protected.
        """

        self._entries_paused = True
        owned = 0
        unowned = 0
        held: set[InstrumentId] = set()
        for position in self.cache.positions_open():
            held.add(position.instrument_id)
            if position.strategy_id != self.id:
                unowned += 1
                continue
            owned += 1
            self._touch(position.instrument_id)
            self._close_position_with_reason(position, "operator_flatten")
        venue, venue_only, unroutable = self._flatten_venue_only(held, now_ns)
        for plan in self._plans.values():
            entry = self.cache.order(ClientOrderId(plan.entry_client_order_id))
            if entry is not None and (entry.is_open or entry.is_inflight) and not entry.is_pending_cancel:
                self.cancel_order(entry)
        detail = {
            "positions": str(owned),
            "unowned_positions": str(unowned),
            "venue_positions": venue,
            "venue_only_positions": str(venue_only),
        }
        if unroutable:
            detail["venue_unroutable_positions"] = str(unroutable)
        self._observations.accept_command(command, "flatten_submitted", detail)

    def _flatten_venue_only(self, held: set[InstrumentId], now_ns: int) -> tuple[str, int, int]:
        """Close what only the venue holds; say whether the venue was read (`read`, `unknown`, `cache`)."""

        if not self._venue_reads:
            return "cache", 0, 0
        reading = self._fresh_venue(now_ns)
        if reading is None or reading.positions is None:
            return "unknown", 0, 0
        held_symbols = {self._venue_symbol(instrument_id) for instrument_id in held}
        instruments = self._venue_instruments()
        closed = 0
        unroutable = 0
        for symbol, quantity in sorted(reading.positions.items()):
            if not quantity or symbol in held_symbols:
                continue
            instrument_id = instruments.get(symbol)
            instrument = None if instrument_id is None else self.cache.instrument(instrument_id)
            if instrument is None:
                # A symbol this generation loaded no instrument for: nothing here can route an order.
                unroutable += 1
                continue
            order = self.order_factory.market(
                instrument_id=instrument.id,
                order_side=OrderSide.SELL if quantity > 0 else OrderSide.BUY,
                quantity=instrument.make_qty(abs(quantity)),
                reduce_only=True,
                tags=["operator_flatten"],
            )
            self._observations.order(
                correlation={},
                client_order_id=order.client_order_id.value,
                leg="exit",
                status="submitted",
                occurred_at_ns=now_ns,
            )
            self._touch(instrument.id)
            self.submit_order(order)
            closed += 1
        return "read", closed, unroutable

    # -- entry -------------------------------------------------------------------------------------

    def _admit_guarded(self, request: RuntimeEntryRequest, now_ns: int) -> None:
        try:
            self._admit(request, now_ns)
        except Exception as exc:
            self.log.exception(f"OI Runtime entry failed ({request.entry_id})", exc)
            self._deferred.pop(request.entry_id, None)
            self._guard("entry_error", lambda: self._answer(request, "runtime_error"))

    def _admit(self, request: RuntimeEntryRequest, now_ns: int) -> None:
        entry_id = request.entry_id
        if entry_id in self._plans or (self._submitting is not None and self._submitting.entry_id == entry_id):
            self._deferred.pop(entry_id, None)
            return
        if request.expires_at_ns <= now_ns:
            deferred = self._deferred.pop(entry_id, None)
            if deferred is None:
                self._answer(request, "expired")
            else:
                self._answer(request, deferred.reason, deferred.detail)
            return
        verdict = self._gate(request, now_ns)
        if verdict.action == "defer":
            self._deferred[entry_id] = _Deferred(request, verdict.reason, verdict.detail)
            return
        self._deferred.pop(entry_id, None)
        if verdict.action == "refuse":
            self._answer(request, verdict.reason, verdict.detail)
            return
        plan = verdict.plan
        if plan is None or not self._journal.prepare(plan):
            self._deferred[entry_id] = _Deferred(request, "trade_plan_busy", {})
            return
        self._submitting = request

    def _entry_authority_verdict(self, request: RuntimeEntryRequest, now_ns: int) -> _Verdict:
        """Read current authority once at either entry decision point, without mutating it."""

        if request.account_slot is not None and request.account_slot != self._profile.account_slot:
            return _Verdict("refuse", "account_slot_mismatch")
        if request.asset_id is not None and request.asset_id in self._profile.excluded_asset_ids:
            return _Verdict("refuse", "asset_excluded")
        if request.entry_envelope is not None and request.entry_envelope.root_expires_at_ns <= now_ns:
            return _Verdict("refuse", "root_expired")
        if self._emergency_halted:
            return _Verdict("refuse", "emergency_halted")
        if self._entries_paused:
            return _Verdict("refuse", "entries_paused")
        if not self._singleton_ready():
            return _Verdict("defer", "singleton_lost")
        if self._unexpected:
            return _Verdict("refuse", "unexpected_exposure")
        if self._convergence_checked_at_ns is None or self._convergence_failure is not None:
            return _Verdict("defer", "convergence_unverified")
        if self._venue_unverified(now_ns):
            return _Verdict("defer", "venue_unverified")
        if request.source == "signal":
            stopped_at = self._stop_exits.get(request.market_key)
            if stopped_at is not None and now_ns < stopped_at + self._profile.risk.post_stop_cooldown_ns:
                return _Verdict("refuse", "post_stop_cooldown")
        return _Verdict("admit")

    def _gate(self, request: RuntimeEntryRequest, now_ns: int) -> _Verdict:
        """The first gate an entry fails, in the order an operator reads them, or the plan it becomes."""

        risk = self._profile.risk
        authority = self._entry_authority_verdict(request, now_ns)
        if authority.action != "admit":
            return authority
        equity = account_equity_usd(cache=self.cache, account_id=self._profile.account_id)
        if equity is None or equity <= 0:
            return _Verdict("defer", "account_unavailable")
        # This reporting baseline cannot decide whether current equity can size an entry.
        try:
            self.day_start_baseline(equity_usd=equity, now_ns=now_ns)
        except ValueError as exc:
            self.log.warning(f"OI Runtime day-start reporting unavailable ({type(exc).__name__})")
        allowed_risk = equity * risk.risk_fraction_per_trade
        if allowed_risk <= 0:
            return _Verdict("refuse", "risk_non_positive")
        route = self._routes.get(request.market_key)
        if route is None:
            return _Verdict("refuse", "instrument_unmapped")
        if (
            request.native_symbol is not None
            and route.instrument_id.value.split("-PERP.", 1)[0] != request.native_symbol
        ):
            return _Verdict("refuse", "mapping_changed")
        if request.mapping_semantics_digest is not None and not self._mapping_current(request):
            return _Verdict("refuse", "mapping_changed")
        self._subscribe(route.instrument_id)
        instrument = self.cache.instrument(route.instrument_id)
        if instrument is None:
            return _Verdict("defer", "instrument_unavailable")
        quote = self.cache.quote_tick(route.instrument_id)
        stop_distance_bps = (
            request.exit_plan.stop_distance_bps if request.exit_plan is not None else route.stop_distance_bps
        )
        market = _quote_verdict(request, quote, now_ns=now_ns, risk=risk, stop_distance_bps=stop_distance_bps)
        if market.action != "admit":
            return market
        if quote is None:
            return _Verdict("defer", "market_unavailable")
        if self._instrument_busy(route.instrument_id):
            return _Verdict("refuse", "exposure_already_present")
        if self._submitting is not None:
            return _Verdict("defer", "trade_plan_busy")
        quantity = entry_quantity(
            direction=request.direction,
            quote=quote,
            instrument=instrument,
            stop_distance_bps=stop_distance_bps,
            allowed_risk_usd=allowed_risk,
            equity_usd=equity,
            max_leverage=risk.max_leverage,
            existing_notional_usd=self._gross_notional(),
        )
        if isinstance(quantity, str):
            return _Verdict("refuse", quantity)
        client_order_id = deterministic_client_order_id(
            namespace=self._profile.namespace, entry_id=request.entry_id, leg="entry"
        )
        plan = TradePlan(
            entry_id=request.entry_id,
            entry_scope_id=request.entry_scope_id,
            source=request.source,
            case_id=request.case_id,
            account_slot=self._profile.account_slot,
            market_key=request.market_key,
            instrument_id=route.instrument_id.value,
            direction=request.direction,
            entry_client_order_id=client_order_id.value,
            created_at_ns=now_ns,
            entry_expires_at_ns=request.expires_at_ns,
            entry_quantity=quantity.as_decimal(),
            stop_distance_bps=stop_distance_bps,
            risk_budget_usd=allowed_risk,
            max_leverage_at_creation=risk.max_leverage,
            exit_policy_id=(
                request.exit_plan.version if request.exit_plan is not None else self._profile.exit_policy.policy_id
            ),
            take_profit_bps=(
                request.exit_plan.take_profit_bps
                if request.exit_plan is not None
                else self._profile.exit_policy.take_profit_bps
            ),
            max_holding_ns=(
                request.exit_plan.max_holding_ns
                if request.exit_plan is not None
                else self._profile.exit_policy.max_holding_ns
            ),
            updated_at_ns=now_ns,
        )
        return _Verdict("admit", plan=plan, detail=market.detail)

    def _submit_committed(self, now_ns: int) -> None:
        """A Signal order needs both a durable plan and a fresh durable validity check."""

        checked = self._journal.take_entry_validity()
        if checked is not None:
            request = self._awaiting_final.pop(checked.entry_id, None)
            self._final_requested.discard(checked.entry_id)
            self._final_retry_at_ns.pop(checked.entry_id, None)
            plan = self._plans.get(checked.entry_id)
            if request is None or plan is None:
                self.log.error(f"OI Runtime final check without plan ({checked.entry_id})")
            elif not checked.allowed:
                self._close_plan(plan, "not_submitted", terminal_at_ns=now_ns, now_ns=now_ns)
                self._dispose_owed(plan, checked.reason)
            else:
                self._send_prepared_entry(plan, request, now_ns)
        receipt = self._journal.take_receipt()
        if receipt is not None:
            request, self._submitting = self._submitting, None
            plan = receipt.plan
            if request is None or request.entry_id != plan.entry_id:
                self.log.error(f"OI Runtime receipt without its request ({plan.entry_id})")
            elif not receipt.committed:
                self._answer(request, receipt.reason or "trade_plan_rejected")
            else:
                self._plans[plan.entry_id] = plan
                self._bind_plan_orders(plan)
                self._owed.add(plan.entry_id)
                if request.exit_plan is not None:
                    self._awaiting_final[plan.entry_id] = request
                else:
                    # Operator manual entries have no Signal exit plan.
                    self._send_prepared_entry(plan, request, now_ns)
        self._request_waiting_final(now_ns)

    def _request_waiting_final(self, now_ns: int) -> None:
        for entry_id in tuple(self._awaiting_final):
            plan = self._plans[entry_id]
            if plan.entry_expires_at_ns <= now_ns:
                self._awaiting_final.pop(entry_id, None)
                self._final_retry_at_ns.pop(entry_id, None)
                self._close_plan(plan, "not_submitted", terminal_at_ns=now_ns, now_ns=now_ns)
                self._dispose_owed(plan, "expired")
                continue
            if entry_id not in self._final_requested and now_ns >= self._final_retry_at_ns.get(entry_id, 0):
                if self._journal.request_entry_validity(self._plans[entry_id]):
                    self._final_requested.add(entry_id)
                return

    def _mapping_current(self, request: RuntimeEntryRequest) -> bool:
        route = self._routes.get(request.market_key)
        return route is not None and self._profile.route_semantics(route) == (
            request.asset_id,
            request.mapping_semantics_digest,
        )

    def _send_prepared_entry(
        self,
        plan: TradePlan,
        request: RuntimeEntryRequest,
        now_ns: int,
    ) -> None:
        """Use the frozen quantity only if every current local gate still holds."""

        now_ns = max(now_ns, self._now_ns())
        instrument_id = InstrumentId.from_str(plan.instrument_id)
        instrument = self.cache.instrument(instrument_id)
        risk = self._profile.risk
        refusal: str | None = None
        retry = False
        if plan.entry_expires_at_ns <= now_ns:
            refusal = "expired"
        else:
            authority = self._entry_authority_verdict(request, now_ns)
            if authority.action != "admit":
                refusal, retry = authority.reason, authority.action == "defer"
            elif (
                request.native_symbol is not None and instrument_id.value.split("-PERP.", 1)[0] != request.native_symbol
            ) or (request.mapping_semantics_digest is not None and not self._mapping_current(request)):
                refusal = "mapping_changed"
            elif instrument is None:
                refusal, retry = "instrument_unavailable", True
            elif self.cache.positions_open(instrument_id=instrument_id) or any(
                order.client_order_id.value != plan.entry_client_order_id
                for order in (
                    *self.cache.orders_open(instrument_id=instrument_id),
                    *self.cache.orders_inflight(instrument_id=instrument_id),
                )
            ):
                refusal = "exposure_already_present"
            else:
                equity = account_equity_usd(cache=self.cache, account_id=self._profile.account_id)
                if equity is None or equity <= 0:
                    refusal, retry = "account_unavailable", True
                else:
                    quote = self.cache.quote_tick(instrument_id)
                    market = _quote_verdict(
                        request, quote, now_ns=now_ns, risk=risk, stop_distance_bps=plan.stop_distance_bps
                    )
                    if market.action != "admit":
                        refusal, retry = market.reason, market.action == "defer"
                    elif quote is None:
                        refusal, retry = "market_unavailable", True
                    else:
                        executable = decimal_value(quote.ask_price if plan.direction == "long" else quote.bid_price)
                        frozen_loss = (
                            plan.entry_quantity * executable * Decimal(plan.stop_distance_bps) / Decimal(10_000)
                        )
                        if frozen_loss > min(plan.risk_budget_usd, equity * risk.risk_fraction_per_trade):
                            refusal = "frozen_risk_exceeded"
        if retry and request.source == "signal" and plan.entry_expires_at_ns > now_ns:
            self._awaiting_final[plan.entry_id] = request
            self._final_retry_at_ns[plan.entry_id] = now_ns + 1_000_000_000
            return
        if refusal is not None or instrument is None:
            self._close_plan(plan, "not_submitted", terminal_at_ns=now_ns, now_ns=now_ns)
            self._dispose_owed(plan, refusal or "instrument_unavailable")
            return
        order = self.order_factory.market(
            instrument_id=instrument_id,
            order_side=OrderSide.BUY if plan.direction == "long" else OrderSide.SELL,
            quantity=instrument.make_qty(plan.entry_quantity),
            reduce_only=False,
            client_order_id=ClientOrderId(plan.entry_client_order_id),
        )
        self._submit_plan_order(order, plan, leg="entry")

    def _answer(self, request: RuntimeEntryRequest, reason: str, detail: dict[str, str] | None = None) -> None:
        self._observations.dispose_entry(source=request.source, entry_id=request.entry_id, reason=reason, detail=detail)

    def _dispose_owed(self, plan: TradePlan, reason: str, detail: dict[str, str] | None = None) -> None:
        """Write a plan's input verdict once: the first venue answer, or how the plan ended without one."""

        if plan.entry_id not in self._owed:
            return
        self._owed.discard(plan.entry_id)
        self._observations.dispose_entry(source=plan.source, entry_id=plan.entry_id, reason=reason, detail=detail)

    def _register_order_binding(self, binding: PlanOrderBinding) -> None:
        if binding.account_slot != self._profile.account_slot:
            raise ValueError("plan_order_account_mismatch")
        previous = self._order_bindings.get(binding.client_order_id)
        if previous is not None and previous != binding:
            raise ValueError("plan_order_identity_conflict")
        self._order_bindings[binding.client_order_id] = binding

    def _bind_plan_orders(self, plan: TradePlan) -> None:
        for binding in initial_plan_order_bindings(plan, namespace=self._profile.namespace):
            self._register_order_binding(binding)

    def _submit_plan_order(
        self,
        order: Any,
        plan: TradePlan,
        *,
        leg: Literal["entry", "stop", "take_profit", "exit"],
        exit_reason: Literal["stop_filled", "take_profit", "time_exit", "operator_flatten"] | None = None,
        position_id: Any = None,
    ) -> None:
        binding = PlanOrderBinding(
            account_slot=plan.account_slot,
            entry_id=plan.entry_id,
            source=plan.source,
            instrument_id=plan.instrument_id,
            client_order_id=order.client_order_id.value,
            leg=leg,
            exit_reason="stop_filled" if leg == "stop" else "take_profit" if leg == "take_profit" else exit_reason,
        )
        self._register_order_binding(binding)
        self._observations.order(
            correlation=self._correlation(binding),
            client_order_id=binding.client_order_id,
            leg=leg,
            status="submitted",
            occurred_at_ns=self._now_ns(),
            trigger_price=order.trigger_price if leg in {"stop", "take_profit"} else None,
            binding=binding,
        )
        self.submit_order(order, position_id=position_id)

    def _close_position_with_reason(
        self,
        position: Any,
        reason: Literal["stop_filled", "take_profit", "time_exit", "operator_flatten"],
    ) -> None:
        if position.is_closed:
            return
        binding, _ = self._order_context(position.opening_order_id, position.instrument_id)
        plan = None if binding is None else self._plans.get(binding.entry_id)
        deterministic = (
            None
            if plan is None
            else deterministic_client_order_id(
                namespace=self._profile.namespace, entry_id=plan.entry_id, leg=f"exit:{reason}"
            )
        )
        order = self.order_factory.market(
            instrument_id=position.instrument_id,
            order_side=OrderSide.SELL if position.is_long else OrderSide.BUY,
            quantity=position.quantity,
            reduce_only=True,
            tags=[reason],
            client_order_id=deterministic
            if deterministic is not None and self.cache.order(deterministic) is None
            else None,
        )
        if plan is None:
            self._observations.order(
                correlation={},
                client_order_id=order.client_order_id.value,
                leg="exit",
                status="submitted",
                occurred_at_ns=self._now_ns(),
            )
            self.submit_order(order, position_id=position.id)
        else:
            self._submit_plan_order(order, plan, leg="exit", exit_reason=reason, position_id=position.id)

    # -- Nautilus events ---------------------------------------------------------------------------

    def on_order_accepted(self, event: Any) -> None:
        self._guard("order_accepted", lambda: self._order_event(event, "accepted"))

    def on_order_canceled(self, event: Any) -> None:
        self._guard("order_canceled", lambda: self._order_event(event, "canceled"))

    def on_order_expired(self, event: Any) -> None:
        self._guard("order_expired", lambda: self._order_refused(event, "expired"))

    def on_order_rejected(self, event: Any) -> None:
        self._guard("order_rejected", lambda: self._order_refused(event, "rejected"))

    def on_order_denied(self, event: Any) -> None:
        self._guard("order_denied", lambda: self._order_refused(event, "denied"))

    def on_order_filled(self, event: Any) -> None:
        self._guard("order_filled", lambda: self._order_filled(event))

    def on_position_opened(self, event: Any) -> None:
        self._guard("position_opened", lambda: self._position_opened(event))

    def on_position_changed(self, event: Any) -> None:
        self._guard("position_changed", lambda: self._position_changed(event))

    def on_position_closed(self, event: Any) -> None:
        self._guard("position_closed", lambda: self._position_closed(event))

    def order_binding(self, client_order_id: str) -> PlanOrderBinding | None:
        """Immutable exact-order intent, available during startup before callbacks."""
        return self._order_bindings.get(client_order_id)

    def _order_context(
        self, client_order_id: ClientOrderId, instrument_id: InstrumentId
    ) -> tuple[PlanOrderBinding | None, OrderLeg]:
        binding = self._order_bindings.get(client_order_id.value)
        if binding is None or binding.instrument_id != instrument_id.value:
            return None, "unknown"
        return binding, binding.leg

    def _order_event(self, event: Any, status: str) -> None:
        binding, leg = self._order_context(event.client_order_id, event.instrument_id)
        plan = None if binding is None else self._plans.get(binding.entry_id)
        venue_order_id = getattr(event, "venue_order_id", None)
        self._observations.order(
            correlation=self._correlation(binding),
            client_order_id=event.client_order_id.value,
            leg=leg,
            status=status,
            occurred_at_ns=int(event.ts_event),
            venue_order_id=None if venue_order_id is None else venue_order_id.value,
            binding=binding,
        )
        if plan is not None and leg == "entry" and status == "accepted":
            self._dispose_owed(plan, "accepted")
        if leg in {"stop", "take_profit"} and status == "accepted":
            # Retire an old protective leg promptly after its replacement is live.
            self._converge_due_ns = 0

    def _order_filled(self, event: Any) -> None:
        binding, leg = self._order_context(event.client_order_id, event.instrument_id)
        plan = None if binding is None else self._plans.get(binding.entry_id)
        self._touch(event.instrument_id)
        self._observations.fill(correlation=self._correlation(binding), leg=leg, event=event)
        if plan is not None and leg == "entry":
            self._dispose_owed(plan, "accepted")
        if plan is not None and leg in {"stop", "take_profit", "exit"}:
            reason = self._exit_reason(event.client_order_id)
            if reason != "external":
                self._closing_fills[event.instrument_id] = (plan.entry_id, reason, int(event.ts_event))
        self._converge_due_ns = 0

    def _order_refused(self, event: Any, status: str) -> None:
        """A venue or pre-trade refusal. A refused entry ends its plan now, in the venue's own words."""

        binding, leg = self._order_context(event.client_order_id, event.instrument_id)
        plan = None if binding is None else self._plans.get(binding.entry_id)
        reason = str(getattr(event, "reason", "") or "")
        self._observations.order(
            correlation=self._correlation(binding),
            client_order_id=event.client_order_id.value,
            leg=leg,
            status=status,
            occurred_at_ns=int(event.ts_event),
            reason=reason or None,
            binding=binding,
        )
        # A refusal waits for the regular five-second convergence: a venue that keeps refusing a
        # replacement is then asked once per interval, never once per pump.
        if plan is None:
            return
        instrument_id = InstrumentId.from_str(plan.instrument_id)
        position = self._position_on(instrument_id)
        if leg == "entry" and position is None:
            now_ns = self._now_ns()
            self._close_plan(plan, "not_submitted", terminal_at_ns=int(event.ts_event), now_ns=now_ns)
            detail = {"venue_reason": bounded_text(reason)} if reason else {}
            self._dispose_owed(plan, "venue_rejected", detail)
            return
        immediate = any(marker in reason.lower() for marker in _IMMEDIATE_TRIGGER_MARKERS)
        if (
            leg in {"stop", "take_profit"}
            and immediate
            and position is not None
            and position_claimed(position, plan, self.id)
        ):
            self._close_position_with_reason(position, "stop_filled" if leg == "stop" else "take_profit")

    def _position_opened(self, event: Any) -> None:
        binding, _ = self._order_context(event.opening_order_id, event.instrument_id)
        plan = None if binding is None else self._plans.get(binding.entry_id)
        self._touch(event.instrument_id)
        self._converge_due_ns = 0
        if plan is None:
            return
        opened = plan if plan.opened_at_ns is not None else self._mark_open(plan, int(event.ts_opened))
        self._observations.position(
            correlation=self._correlation(opened),
            position_id=event.position_id.value,
            status="opened",
            occurred_at_ns=int(event.ts_opened),
            quantity=event.quantity,
            average_entry_price=event.avg_px_open,
        )
        self._dispose_owed(opened, "accepted")
        # A partial entry already holds venue exposure. Protect its actual
        # quantity now rather than waiting for the next quote or final fill.
        self._converge(self._now_ns())

    def _position_changed(self, event: Any) -> None:
        self._touch(event.instrument_id)
        self._converge_due_ns = 0
        self._converge(self._now_ns())

    def _position_closed(self, event: Any) -> None:
        """The Cache closed a position: end its plan now if one of this Runtime's legs closed it.

        A close by the plan's stop, take-profit, time exit or an operator flatten is a venue fill of an
        order this Runtime sent, so every order left on the instrument is canceled and the plan ends with
        that leg's reason. Any other close -- one placed on the venue by hand, or a fill Nautilus'
        reconciliation invented to repair a disagreement (#680 PR-3) -- is not evidence the venue is
        flat: the stop and take-profit stay, the plan stays open, and the convergence ends both only once
        a venue read confirms the instrument flat. Until then the instrument is unexpected exposure.
        """

        instrument_id = event.instrument_id
        now_ns = self._now_ns()
        self._touch(instrument_id)
        binding, _ = self._order_context(event.opening_order_id, instrument_id)
        plan = None if binding is None else self._plans.get(binding.entry_id)
        position = self.cache.position(event.position_id)
        if position is not None and position.opening_order_id != event.opening_order_id:
            position = None
        reason = (
            self._position_exit_reason(position)
            if position is not None
            else self._exit_reason(event.closing_order_id, entry_id=None if binding is None else binding.entry_id)
        )
        self._converge_due_ns = 0
        if plan is not None and plan.opened_at_ns is None:
            plan = self._mark_open(plan, int(event.ts_opened))
        self._observations.position(
            correlation=self._correlation(binding),
            position_id=event.position_id.value,
            status="closed",
            occurred_at_ns=int(event.ts_closed),
            quantity=event.peak_qty,
            average_entry_price=event.avg_px_open,
            exit_price=event.avg_px_close,
            exit_reason=reason,
        )
        if plan is not None:
            self._dispose_owed(plan, "accepted")
        else:
            # A delayed close of a previous lifecycle cannot cancel the current Plan's protection.
            return
        if reason == "external" and not self._venue_confirms_flat(instrument_id, now_ns):
            self.log.warning(
                f"OI Runtime position {event.position_id.value} closed by an order none of its legs sent "
                f"({event.closing_order_id}); protection stays until the venue reads flat"
            )
            if plan is not None:
                self._unattributed_closes[instrument_id] = (plan.entry_id, int(event.ts_closed))
            return
        self._unattributed_closes.pop(instrument_id, None)
        self._closing_fills.pop(instrument_id, None)
        self._cancel_working_orders(instrument_id)
        if plan is None:
            return
        self._close_plan(plan, reason, terminal_at_ns=int(event.ts_closed), now_ns=now_ns)

    def _exit_reason(self, closing_order_id: ClientOrderId | None, *, entry_id: str | None = None) -> ExitReason:
        binding = None if closing_order_id is None else self._order_bindings.get(closing_order_id.value)
        if entry_id is not None and binding is not None and binding.entry_id != entry_id:
            return "external"
        return "external" if binding is None or binding.exit_reason is None else binding.exit_reason

    def _position_exit_reason(self, position: Any) -> ExitReason:
        """Preserve multiple real closing legs instead of naming only the final fill's leg."""

        events = position.events
        if not events:
            return "external"
        opening, _ = self._order_context(position.opening_order_id, position.instrument_id)
        if opening is None:
            return "external"
        opening_side = events[0].order_side
        reasons = {
            self._exit_reason(fill.client_order_id, entry_id=opening.entry_id)
            for fill in events
            if fill.order_side != opening_side and fill.last_qty.as_decimal() > 0
        }
        if len(reasons) > 1:
            return "mixed_exit"
        return next(iter(reasons), "external")

    # -- the invariant -----------------------------------------------------------------------------

    def _converge(self, now_ns: int) -> None:
        """Read the Cache, judge it against the venue, make it match intent, and name what nothing claims.

        Startup and steady state are the same call: the first pump after Nautilus reconciled runs it.
        """

        positions: dict[InstrumentId, list[Any]] = defaultdict(list)
        for position in self.cache.positions_open():
            positions[position.instrument_id].append(position)
            # Every position is marked, claimed or not: equity and the daily loss depend on it.
            if self.cache.instrument(position.instrument_id) is not None:
                self._subscribe(position.instrument_id)
        open_orders, inflight = open_and_inflight_orders(self.cache)
        working: dict[InstrumentId, list[Any]] = defaultdict(list)
        for order in (*open_orders, *inflight):
            working[order.instrument_id].append(order)
        self._judge_venue(positions)
        unexpected: list[str] = list(self._venue_mismatch.values())
        planned: dict[InstrumentId, list[TradePlan]] = defaultdict(list)
        for plan in self._plans.values():
            planned[InstrumentId.from_str(plan.instrument_id)].append(plan)
        for instrument_id, candidates in planned.items():
            if len(candidates) != 1:
                unexpected.append(f"ambiguous:{instrument_id.value}")
                continue
            self._converge_plan(
                candidates[0], positions.get(instrument_id, []), working.get(instrument_id, []), now_ns, unexpected
            )
        for instrument_id, held in positions.items():
            if instrument_id not in planned:
                unexpected.extend(f"position:{position.id.value}" for position in held)
        for instrument_id, orders in working.items():
            if instrument_id in planned:
                continue
            if instrument_id in positions:
                unexpected.extend(
                    f"order:{order.client_order_id.value}" for order in orders if not order.is_reduce_only
                )
                continue
            # No position in the Cache and no plan. An order that can add exposure goes now; a reduce-only
            # one may be the only protection a position the Cache lost still has, so it goes only once
            # the venue says the instrument is flat, and until then it is named (#680 PR-3).
            if self._venue_confirms_flat(instrument_id, now_ns):
                self._cancel_working_orders(instrument_id)
                continue
            for order in orders:
                if (
                    order.strategy_id == self.id
                    and not order.is_reduce_only
                    and order.is_open
                    and not order.is_pending_cancel
                ):
                    self.cancel_order(order)
            unexpected.extend(f"order:{order.client_order_id.value}" for order in orders if not order.is_pending_cancel)
        codes = tuple(sorted(set(unexpected)))
        findings = exposure_findings(
            codes,
            positions={position.id.value: position for held in positions.values() for position in held},
            orders={order.client_order_id.value: order for held in working.values() for order in held},
            plans={instrument: tuple(values) for instrument, values in planned.items()},
            venue_instruments=self._venue_instruments(),
            observed_at_ns=now_ns,
        )
        self._set_unexpected(codes, findings, now_ns)
        self._convergence_checked_at_ns = now_ns

    def _converge_plan(
        self,
        plan: TradePlan,
        positions: list[Any],
        orders: list[Any],
        now_ns: int,
        unexpected: list[str],
    ) -> None:
        instrument_id = InstrumentId.from_str(plan.instrument_id)
        entry = self.cache.order(ClientOrderId(plan.entry_client_order_id))
        entry_working = entry is not None and (entry.is_open or entry.is_inflight)
        unexpected.extend(
            f"order:{order.client_order_id.value}"
            for order in orders
            if (binding := self._order_bindings.get(order.client_order_id.value)) is None
            or binding.entry_id != plan.entry_id
        )
        own = [position for position in positions if position_claimed(position, plan, self.id)]
        unexpected.extend(f"ownership:{position.id.value}" for position in positions if position not in own)
        if plan.entry_id in self._submission_unknown:
            if entry is None and not own:
                unexpected.append(f"submission_unknown:{plan.entry_client_order_id}")
                return
            self._submission_unknown.discard(plan.entry_id)
        if plan.entry_id in self._awaiting_final and not own and not entry_working:
            return
        if not own:
            if entry_working or (entry is not None and not entry.is_closed) or positions:
                return
            never_opened = plan.opened_at_ns is None and entry is not None and entry.filled_qty.as_decimal() == 0
            if not never_opened and not self._venue_confirms_flat(instrument_id, now_ns):
                # The Cache is flat and the venue has not said so: the plan and every order resting for
                # it stay, and the instrument is named until a venue read settles it (#680 PR-3).
                unexpected.append(f"unconfirmed_close:{instrument_id.value}")
                return
            self._cancel_working_orders(instrument_id)
            self._end_flat(plan, entry, now_ns)
            return
        position = own[0]
        if plan.opened_at_ns is None:
            filled = entry is not None and entry.filled_qty.as_decimal() > 0
            plan = self._mark_open(plan, int(position.ts_opened) if filled else plan.created_at_ns)
        self._dispose_owed(plan, "accepted")
        if self._venue_reads and self._venue_confirms_flat(instrument_id, now_ns):
            # The Cache holds a position the venue says is not there: there is nothing to protect or
            # exit, and a reduce-only order would only be refused, again, every convergence. The
            # disagreement itself is already unexpected exposure.
            return
        self._ensure_protection(plan, position, orders, now_ns)
        opened_at_ns = plan.opened_at_ns or plan.created_at_ns
        if now_ns >= opened_at_ns + plan.max_holding_ns:
            if entry_working and entry is not None and not entry.is_pending_cancel:
                self.cancel_order(entry)
            self._time_exit(position, orders)

    def _end_flat(self, plan: TradePlan, entry: Any, now_ns: int) -> None:
        """End a plan whose instrument is flat on the venue, with the best account of how it got there.

        One of this Runtime's closing legs filled for it (the Cache may never have seen the position
        close, if Nautilus had closed it first): that leg's reason and fill time. A close no leg explains:
        `external`, at the Cache's close time. Neither: nobody saw the end.
        """

        instrument_id = InstrumentId.from_str(plan.instrument_id)
        closing = self._closing_fills.pop(instrument_id, None)
        unattributed = self._unattributed_closes.pop(instrument_id, None)
        opened_at_ns = plan.opened_at_ns or plan.created_at_ns
        if closing is not None and closing[0] == plan.entry_id:
            self._dispose_owed(plan, "accepted")
            self._close_plan(plan, closing[1], terminal_at_ns=max(closing[2], opened_at_ns), now_ns=now_ns)
            return
        if unattributed is not None and unattributed[0] == plan.entry_id:
            self._dispose_owed(plan, "accepted")
            self._close_plan(plan, "external", terminal_at_ns=max(unattributed[1], opened_at_ns), now_ns=now_ns)
            return
        # Startup reconciliation completes before the Strategy starts, so its real historical
        # fills do not produce this Strategy's on_order_filled/on_position_closed callbacks.
        # Only a unique closed Position opened by this plan's entry can settle it here.
        historical = [
            position
            for position in self.cache.positions_closed(instrument_id=instrument_id, strategy_id=self.id)
            if position.opening_order_id == ClientOrderId(plan.entry_client_order_id)
            and int(position.ts_opened) >= plan.created_at_ns
            and int(position.ts_closed) >= int(position.ts_opened)
        ]
        if len(historical) == 1:
            position = historical[0]
            order = self.cache.order(position.closing_order_id) if position.closing_order_id is not None else None
            if order is not None and order.filled_qty.as_decimal() > 0:
                if plan.opened_at_ns is None:
                    plan = self._mark_open(plan, int(position.ts_opened))
                self._dispose_owed(plan, "accepted")
                self._close_plan(
                    plan,
                    self._position_exit_reason(position),
                    terminal_at_ns=max(int(position.ts_closed), plan.opened_at_ns or plan.created_at_ns),
                    now_ns=now_ns,
                )
                return
        self._end_unobserved(plan, entry, now_ns)

    def _end_unobserved(self, plan: TradePlan, entry: Any, now_ns: int) -> None:
        """A plan whose instrument is flat and whose entry is not working, and whose end nobody saw.

        An entry order the venue refused or canceled unfilled never opened anything; any other plan was
        open or may have been, and its close happened where this Runtime could not see it.
        """

        if plan.opened_at_ns is None and entry is not None and entry.filled_qty.as_decimal() == 0:
            reason = str(getattr(entry.last_event, "reason", "") or "")
            refused = entry.status in {OrderStatus.REJECTED, OrderStatus.DENIED}
            self._close_plan(plan, "not_submitted", terminal_at_ns=now_ns, now_ns=now_ns)
            self._dispose_owed(
                plan,
                "venue_rejected" if refused else "entry_canceled",
                {"venue_reason": bounded_text(reason)} if reason else {},
            )
            return
        traded = plan.opened_at_ns is not None or (entry is not None and entry.filled_qty.as_decimal() > 0)
        self._close_plan(plan, "venue_unknown", terminal_at_ns=now_ns, now_ns=now_ns)
        self._dispose_owed(plan, "accepted" if traded else "entry_outcome_unknown")

    def _ensure_protection(self, plan: TradePlan, position: Any, orders: list[Any], now_ns: int) -> None:
        closing_side = OrderSide.SELL if position.is_long else OrderSide.BUY
        protective = [
            order
            for order in orders
            if order.strategy_id == self.id
            and order.is_reduce_only
            and order.side == closing_side
            and (binding := self._order_bindings.get(order.client_order_id.value)) is not None
            and binding.entry_id == plan.entry_id
        ]
        instrument = self.cache.instrument(position.instrument_id)
        if instrument is None:
            return
        average = decimal_value(position.avg_px_open)
        for leg, order_type in (("stop", OrderType.STOP_MARKET), ("take_profit", OrderType.MARKET_IF_TOUCHED)):
            leg_orders = [order for order in protective if order.order_type == order_type]
            trigger = instrument.make_price(
                protective_trigger(
                    direction=plan.direction,
                    average_entry_price=average,
                    distance_bps=plan.stop_distance_bps if leg == "stop" else plan.take_profit_bps,
                    leg=leg,
                )
            )

            def matches(order: Any, expected_trigger: Any = trigger) -> bool:
                return (
                    order.quantity == position.quantity
                    and order.trigger_price == expected_trigger
                    and order.trigger_type == TriggerType.MARK_PRICE
                )

            accepted = next(
                (
                    order
                    for order in leg_orders
                    if matches(order) and order.is_open and not order.is_pending_cancel and not order.is_pending_update
                ),
                None,
            )
            if accepted is not None:
                # Binance USD-M only modifies LIMIT orders. Keep the old stop/TP live until the
                # replacement is accepted, then retire it; both are reduce-only while they overlap.
                for order in leg_orders:
                    if order is not accepted and order.is_open and not order.is_pending_cancel:
                        self.cancel_order(order)
                continue
            if any(matches(order) and order.is_inflight for order in leg_orders):
                continue
            self._submit_protection(plan, position, instrument, closing_side, leg, average, now_ns)

    def _submit_protection(
        self,
        plan: TradePlan,
        position: Any,
        instrument: Any,
        closing_side: OrderSide,
        leg: Literal["stop", "take_profit"],
        average: Decimal,
        now_ns: int,
    ) -> None:
        """One reduce-only order on the mark price, at the plan's distance from the average fill.

        The first attempt carries the plan's deterministic id for that leg. A replacement -- the first
        was canceled or refused -- takes a Nautilus-generated id, because a client order id names one
        order for good.
        """

        trigger = instrument.make_price(
            protective_trigger(
                direction=plan.direction,
                average_entry_price=average,
                distance_bps=plan.stop_distance_bps if leg == "stop" else plan.take_profit_bps,
                leg=leg,
            )
        )
        deterministic = deterministic_client_order_id(
            namespace=self._profile.namespace, entry_id=plan.entry_id, leg=leg
        )
        client_order_id = deterministic if self.cache.order(deterministic) is None else None
        create = self.order_factory.stop_market if leg == "stop" else self.order_factory.market_if_touched
        order = create(
            instrument_id=position.instrument_id,
            order_side=closing_side,
            quantity=position.quantity,
            trigger_price=trigger,
            trigger_type=TriggerType.MARK_PRICE,
            reduce_only=True,
            client_order_id=client_order_id,
        )
        self._submit_plan_order(order, plan, leg=leg, position_id=position.id)

    def _time_exit(self, position: Any, orders: list[Any]) -> None:
        if any(
            order.strategy_id == self.id and order.order_type == OrderType.MARKET and order.is_reduce_only
            for order in orders
        ):
            return
        self._close_position_with_reason(position, "time_exit")

    def _cancel_working_orders(self, instrument_id: InstrumentId) -> None:
        pending = [
            order
            for order in (
                *self.cache.orders_open(instrument_id=instrument_id, strategy_id=self.id),
                *self.cache.orders_inflight(instrument_id=instrument_id, strategy_id=self.id),
            )
            if not order.is_pending_cancel
        ]
        if pending:
            self.cancel_all_orders(instrument_id)

    def _set_unexpected(
        self, unexpected: tuple[str, ...], findings: tuple[ExecutionExposureFinding, ...], now_ns: int
    ) -> None:
        if len(unexpected) != len(findings):
            raise ValueError("oi_runtime_findings_incomplete")
        changed = unexpected != self._unexpected
        self._unexpected = unexpected
        self._findings = findings
        if changed:
            if unexpected:
                self.log.warning(f"OI Runtime exposure findings: {', '.join(unexpected)}")
            self._observations.exposure(unexpected=unexpected, observed_at_ns=now_ns)

    # -- venue truth (#680 PR-3) -------------------------------------------------------------------

    def observe_funding(self, flow: FundingCashflow) -> bool:
        return self._observations.funding(flow)

    def observe_funding_coverage(self, start_ms: int, end_ms: int) -> bool:
        return self._observations.funding_coverage(start_ms, end_ms)

    def observe_venue(self, reading: VenueReading) -> None:
        """Take one venue read from the root's reader, on the callback thread; it is judged next pump.

        A failed read changes nothing but the log: it is not evidence of anything, least of all of a
        flat account, and the last successful read stays the one entries and orphan cancels rely on
        until it is too old to.
        """

        if reading.positions is None:
            if reading.failure != self._venue_failure:
                self.log.warning(
                    f"OI Runtime cannot read the venue's positions ({reading.failure}); they are unknown, not flat"
                )
            self._venue_failure = reading.failure
            return
        if self._venue_failure is not None:
            self.log.warning("OI Runtime reads the venue's positions again")
            self._venue_failure = None
        if self._venue is None or reading.started_at_ns >= self._venue.started_at_ns:
            self._venue = reading
            self._converge_due_ns = 0

    def take_recovery_request(self, now_ns: int) -> int | None:
        """Retry fresh, attributable discrepancies with bounded delay in this generation."""

        reading = self._fresh_venue(now_ns)
        if (
            self._stopped
            or reading is None
            or self._venue_failure is not None
            or self._convergence_failure is not None
            or self._convergence_checked_at_ns is None
            or reading.completed_at_ns <= self._recovery_requested_read_ns
        ):
            return None
        # Oldest attempted instrument first: a busy first symbol must not
        # consume every account-wide single-flight recovery opportunity.
        for symbol in sorted(
            self._venue_mismatch,
            key=lambda symbol: (
                self._recovery_attempts[symbol].next_attempt_ns - self._recovery_attempts[symbol].delay_ns
                if symbol in self._recovery_attempts
                else 0
            ),
        ):
            instrument_id = self._venue_instruments().get(symbol)
            if instrument_id is None:
                continue
            candidates = [plan for plan in self._plans.values() if plan.instrument_id == instrument_id.value]
            if len(candidates) != 1:
                continue
            venue_quantity = reading.quantity(symbol)
            if venue_quantity and (
                (venue_quantity > 0 and candidates[0].direction != "long")
                or (venue_quantity < 0 and candidates[0].direction != "short")
            ):
                continue
            cached_positions = self.cache.positions_open(instrument_id=instrument_id)
            if any(not position_claimed(position, candidates[0], self.id) for position in cached_positions):
                continue
            cache_quantity = sum(position.signed_decimal_qty() for position in cached_positions)
            signature = (candidates[0].entry_id, venue_quantity, cache_quantity)
            previous = self._recovery_attempts.get(symbol)
            delay_ns = _RECOVERY_INITIAL_DELAY_NS
            if previous is not None and previous.signature == signature:
                if now_ns < previous.next_attempt_ns:
                    continue
                delay_ns = min(previous.delay_ns * 2, _RECOVERY_MAX_DELAY_NS)
            self._recovery_attempts[symbol] = _RecoveryBackoff(signature, delay_ns, now_ns + delay_ns)
            self._recovery_requested_read_ns = reading.completed_at_ns
            return reading.completed_at_ns
        return None

    def _touch(self, instrument_id: InstrumentId) -> None:
        self._activity_ns[instrument_id] = self._now_ns()

    def _settled(self, instrument_id: InstrumentId, reading: VenueReading) -> bool:
        """Did this read start after the instrument's last local activity had settled?"""

        return self._activity_ns.get(instrument_id, 0) + VENUE_SETTLE_NS <= reading.started_at_ns

    def _fresh_venue(self, now_ns: int) -> VenueReading | None:
        reading = self._venue
        if reading is None or now_ns - reading.completed_at_ns > VENUE_STALE_AFTER_NS:
            return None
        return reading

    def _venue_unverified(self, now_ns: int) -> bool:
        """Entries need a fresh venue read that agreed with the Cache on every instrument."""

        return self._venue_reads and (self._fresh_venue(now_ns) is None or bool(self._venue_suspect))

    def _venue_confirms_flat(self, instrument_id: InstrumentId, now_ns: int) -> bool:
        """Does the venue itself say this instrument holds nothing, as of after its last activity?

        Without venue reads the venue is the Cache (a backtest), and the Cache answers.
        """

        if not self._venue_reads:
            return not self.cache.positions_open(instrument_id=instrument_id)
        reading = self._fresh_venue(now_ns)
        return (
            reading is not None
            and self._settled(instrument_id, reading)
            and reading.quantity(self._venue_symbol(instrument_id)) == 0
        )

    def _judge_venue(self, positions: Mapping[InstrumentId, list[Any]]) -> None:
        """Compare the newest venue read, once, with the Cache's net position per Binance symbol.

        A disagreement is a suspect the first time and a mismatch -- unexpected exposure -- when the next
        read agrees with it, so a read that crossed a fill in flight is never an alarm; one agreeing read
        clears either. An instrument something moved on since the read began keeps its last verdict.
        """

        reading = self._venue
        if reading is None or reading.positions is None or reading.completed_at_ns == self._venue_judged_at_ns:
            return
        self._venue_judged_at_ns = reading.completed_at_ns
        held: dict[str, Decimal] = defaultdict(Decimal)
        instruments: dict[str, InstrumentId] = {}
        for instrument_id, open_positions in positions.items():
            symbol = self._venue_symbol(instrument_id)
            instruments[symbol] = instrument_id
            for position in open_positions:
                held[symbol] += decimal_value(position.signed_decimal_qty())
        if any(symbol not in instruments for symbol in reading.positions):
            instruments = {**self._venue_instruments(), **instruments}
        suspects: dict[str, str] = {}
        for symbol in sorted({*held, *reading.positions, *self._venue_suspect, *self._venue_mismatch}):
            instrument_id = instruments.get(symbol)
            if instrument_id is not None and not self._settled(instrument_id, reading):
                if symbol in self._venue_suspect:
                    suspects[symbol] = self._venue_suspect[symbol]
                continue
            venue_quantity = reading.quantity(symbol)
            cache_quantity = held.get(symbol, Decimal(0))
            if venue_quantity == cache_quantity:
                self._venue_mismatch.pop(symbol, None)
                continue
            finding = f"venue:{symbol}:venue={_quantity_text(venue_quantity)}:cache={_quantity_text(cache_quantity)}"
            if symbol in self._venue_mismatch or self._venue_suspect.get(symbol) == finding:
                if self._venue_mismatch.get(symbol) != finding:
                    self.log.warning(f"OI Runtime venue and Cache disagree: {finding}")
                self._venue_mismatch[symbol] = finding
            else:
                suspects[symbol] = finding
        self._venue_suspect = suspects
        self._recovery_attempts = {
            symbol: attempt for symbol, attempt in self._recovery_attempts.items() if symbol in self._venue_mismatch
        }

    def _venue_symbol(self, instrument_id: InstrumentId) -> str:
        """The venue's spelling of an instrument (`APTUSDT` for `APTUSDT-PERP.BINANCE`)."""

        instrument = self.cache.instrument(instrument_id)
        if instrument is not None:
            return str(instrument.raw_symbol.value)
        return instrument_id.symbol.value.removesuffix("-PERP")

    def _venue_instruments(self) -> dict[str, InstrumentId]:
        return {str(instrument.raw_symbol.value): instrument.id for instrument in self.cache.instruments()}

    # -- plans -------------------------------------------------------------------------------------

    def _mark_open(self, plan: TradePlan, opened_at_ns: int) -> TradePlan:
        opened = plan.opened(opened_at_ns=max(opened_at_ns, plan.created_at_ns), now_ns=self._now_ns())
        self._plans[plan.entry_id] = opened
        self._journal.offer_plan(opened)
        return opened

    def _close_plan(self, plan: TradePlan, reason: ExitReason, *, terminal_at_ns: int, now_ns: int) -> None:
        closed = plan.closed(reason=reason, terminal_at_ns=terminal_at_ns, now_ns=now_ns)
        self._plans.pop(plan.entry_id, None)
        self._journal.offer_plan(closed)
        if reason == "stop_filled":
            self._stop_exits[plan.market_key] = max(
                self._stop_exits.get(plan.market_key, 0), closed.terminal_at_ns or 0
            )

    def _plan_on(self, instrument_id: InstrumentId) -> TradePlan | None:
        value = instrument_id.value
        return next((plan for plan in self._plans.values() if plan.instrument_id == value), None)

    def _position_on(self, instrument_id: InstrumentId) -> Any:
        return next(iter(self.cache.positions_open(instrument_id=instrument_id)), None)

    @staticmethod
    def _correlation(plan: TradePlan | PlanOrderBinding | None) -> dict[str, str]:
        return {} if plan is None else RuntimeObservations.correlation(plan.source, plan.entry_id)

    def _instrument_busy(self, instrument_id: InstrumentId) -> bool:
        """An entry never shares its instrument: a resting close-all stop there would close it too."""

        return bool(
            self._plan_on(instrument_id) is not None
            or self.cache.positions_open(instrument_id=instrument_id)
            or self.cache.orders_open(instrument_id=instrument_id)
            or self.cache.orders_inflight(instrument_id=instrument_id)
        )

    def _gross_notional(self) -> Decimal:
        total = Decimal(0)
        for position in self.cache.positions_open():
            total += abs(decimal_value(position.quantity)) * decimal_value(position.avg_px_open)
        return total

    # -- quotes ------------------------------------------------------------------------------------

    def _subscribe(self, instrument_id: InstrumentId) -> None:
        """Stream quotes only for instruments an entry is waiting on or a plan holds (#510 E)."""

        if instrument_id in self._subscribed:
            return
        self._subscribed.add(instrument_id)
        self.subscribe_quote_ticks(instrument_id)

    def _sweep_quotes(self) -> None:
        needed = {InstrumentId.from_str(plan.instrument_id) for plan in self._plans.values()}
        needed.update(position.instrument_id for position in self.cache.positions_open())
        for request in (
            *(value.request for value in self._deferred.values()),
            *(() if self._submitting is None else (self._submitting,)),
        ):
            route = self._routes.get(request.market_key)
            if route is not None:
                needed.add(route.instrument_id)
        for instrument_id in tuple(self._subscribed - needed):
            self._subscribed.discard(instrument_id)
            self.unsubscribe_quote_ticks(instrument_id)

    # -- control, baseline and the published view --------------------------------------------------

    def control_state(self) -> RuntimeControlSnapshot:
        return RuntimeControlSnapshot(entries_paused=self._entries_paused, emergency_halted=self._emergency_halted)

    def update_day_start(self, baseline: DayStartBaseline) -> None:
        """Accept a baseline already loaded durably by the background owner."""

        with self._day_start_lock:
            if self._day_start is None or baseline.utc_day >= self._day_start.utc_day:
                self._day_start = baseline

    def day_start_baseline(self, *, equity_usd: Decimal, now_ns: int) -> DayStartBaseline:
        """Today's baseline, recorded from current equity when none has arrived yet (#520 PR-B)."""

        current = self._current_day_start(now_ns)
        if current is not None:
            return current
        baseline, observation = self._journal.factory.day_start_baseline(
            utc_day=_utc_day(now_ns),
            equity_usd=equity_usd,
            recorded_at_ns=now_ns,
        )
        self._journal.offer(observation)
        with self._day_start_lock:
            self._day_start = baseline
        return baseline

    def _current_day_start(self, now_ns: int) -> DayStartBaseline | None:
        with self._day_start_lock:
            baseline = self._day_start
        return baseline if baseline is not None and baseline.utc_day == _utc_day(now_ns) else None

    def entry_block_reason(self, now_ns: int | None = None) -> str | None:
        at_ns = self._now_ns() if now_ns is None else now_ns
        for blocked, reason in (
            (self._emergency_halted, "emergency_halted"),
            (self._entries_paused, "entries_paused"),
            (not self._singleton_ready(), "singleton_lost"),
            (bool(self._unexpected), "unexpected_exposure"),
            (
                self._convergence_checked_at_ns is None or self._convergence_failure is not None,
                "convergence_unverified",
            ),
            (self._venue_unverified(at_ns), "venue_unverified"),
        ):
            if blocked:
                return reason
        return None

    def runtime_view(self, now_ns: int) -> RuntimeView:
        """One bounded account projection, including venue-only positions and named findings."""

        planned: dict[InstrumentId, list[TradePlan]] = defaultdict(list)
        for plan in self._plans.values():
            planned[InstrumentId.from_str(plan.instrument_id)].append(plan)
        # Keep the last successful venue rows visible even after a later failure or expiry.
        # Entry and recovery gates still use _fresh_venue; this is historical display evidence.
        reading = self._venue if self._venue_reads else None
        snapshot = account_snapshot(
            cache=self.cache,
            account_id=self._profile.account_id,
            plans={instrument: tuple(values) for instrument, values in planned.items()},
            strategy_id=self.id,
            order_bindings=self._order_bindings,
            venue_positions=None if reading is None else reading.positions,
            venue_instruments=self._venue_instruments(),
            findings=self._findings,
            baseline=self._current_day_start(now_ns),
            now_ns=now_ns,
            market_stale_after_ns=self._profile.risk.market_stale_after_ns,
        )
        statuses = {position.protection_status for position in snapshot.positions}
        protection: Literal["not_applicable", "protected", "pending", "unprotected", "unknown"]
        if not snapshot.positions_total:
            protection = "not_applicable"
        elif "unprotected" in statuses:
            protection = "unprotected"
        elif snapshot.positions_total > len(snapshot.positions):
            protection = "unknown"
        elif "pending" in statuses:
            protection = "pending"
        elif statuses == {"protected"}:
            protection = "protected"
        else:
            protection = "unknown"
        reason = self.entry_block_reason(now_ns)
        return RuntimeView(
            entries_armed=reason is None,
            entry_block_reason=reason,
            unexpected_exposure=bool(snapshot.findings_total),
            positions_count=snapshot.positions_total,
            open_orders_count=snapshot.open_orders_count,
            protection_status=protection,
            account_snapshot=snapshot,
            convergence_checked_at_ns=self._convergence_checked_at_ns,
            convergence_failure=self._convergence_failure,
            venue_read_started_at_ns=None if self._venue is None else self._venue.started_at_ns,
            venue_read_completed_at_ns=None if self._venue is None else self._venue.completed_at_ns,
            venue_read_failure=self._venue_failure,
        )


def _utc_day(now_ns: int) -> str:
    return datetime.fromtimestamp(now_ns // 1_000_000_000, tz=UTC).date().isoformat()


def _quantity_text(value: Decimal) -> str:
    return format(value.normalize(), "f") if value else "0"


__all__ = [
    "OiNautilusStrategy",
    "OpenPlan",
    "RuntimeControlSnapshot",
    "RuntimeInputs",
    "RuntimeView",
    "oi_strategy_config",
]
