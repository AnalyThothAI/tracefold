"""PostgreSQL bridge plus the disabled half of the OI Runtime composition seam."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from threading import Event, Lock, Thread
from typing import Any

from loguru import logger
from psycopg import InterfaceError, OperationalError
from psycopg.errors import IntegrityError

from tracefold.app.repository_session import RepositorySession
from tracefold.app.repository_session import repositories as open_repositories
from tracefold.integrations.nautilus.oi_runtime.audit_sink import (
    AuditAppendRejected,
    AuditSink,
    ObservationFactory,
    day_start_baseline_from_observation,
)
from tracefold.integrations.nautilus.oi_runtime.config import OiRuntimeProfile
from tracefold.integrations.nautilus.oi_runtime.risk import DayStartBaseline
from tracefold.integrations.nautilus.oi_runtime.signal_client import (
    ExecutionSignalClient,
    install_execution_stream_listener,
    wait_for_execution_stream_wake,
)
from tracefold.integrations.nautilus.oi_runtime.singleton import AccountSlotSingleton
from tracefold.integrations.nautilus.oi_runtime.state import RuntimeControlSnapshot
from tracefold.trading import ExecutionObservationV1, OperatorIntentV1, TradeSignalV1
from tracefold.trading.storage.execution_stream import (
    EXECUTION_STREAM_NOTIFY_CHANNEL,
    MAX_EXECUTION_READ_BATCH,
    ExecutionRuntimeState,
    materialize_execution_observation,
    materialize_operator_intents,
    materialize_trade_signals,
    prepare_execution_observations,
)

# How often the current row is rewritten when nothing about it changed. It is well inside the public
# five-second stale budget, so a Runtime that stops projecting reads as stale rather than as healthy.
RUNTIME_HEARTBEAT_INTERVAL_NS = 500_000_000
# Recovery reads the durable entry-order facts that can still hold Binance exposure. Seven days is the
# widest gap this single-host deployment can be down and still find its own position on the venue;
# an entry order older than that has long since been filled and closed or cancelled by the venue.
_RECOVERY_ENTRY_FACT_WINDOW_NS = 7 * 24 * 60 * 60 * 1_000_000_000
# This connection is the only PostgreSQL caller a Runtime holding live exposure has left, and reading
# Commands on it is how an operator flattens. A statement that has not finished in one private
# reconciliation period is broken, not slow: PostgreSQL cancels it, psycopg raises the
# `OperationalError` the run loop already treats as a lost session, and the session is replaced.
_STATEMENT_TIMEOUT_MS = 5_000


class RuntimeStateProjector:
    """Durable current state: computed on the event loop, read and written by the bridge thread.

    Two facts used to be synchronous PostgreSQL calls on the trading event loop, over a third
    connection, every 500 ms: the generation-fenced `trading_execution_runtime_state` row and the
    durable entry identities a reconciliation rebuilds ownership from (#510 E). Neither is an input
    and neither needs to be read by the thread that also runs every Nautilus order callback. The loop
    offers and reads memory here; every statement belongs to the bridge thread and its one connection.
    """

    def __init__(
        self,
        *,
        initial: ExecutionRuntimeState,
        recovery_inputs: tuple[tuple[TradeSignalV1, ...], tuple[OperatorIntentV1, ...]],
    ) -> None:
        self._lock = Lock()
        self._current = initial
        self._pending: ExecutionRuntimeState | None = None
        self._recovery_inputs = recovery_inputs

    @property
    def current(self) -> ExecutionRuntimeState:
        """The last row actually written, which is what the next candidate is compared against."""

        with self._lock:
            return self._current

    def recovery_inputs(self) -> tuple[tuple[TradeSignalV1, ...], tuple[OperatorIntentV1, ...]]:
        """The durable entry identities the next reconciliation rebuilds ownership from."""

        with self._lock:
            return self._recovery_inputs

    def offer(self, candidate: ExecutionRuntimeState) -> None:
        """Hand the loop's freshly computed row to the writer; the newest candidate wins."""

        with self._lock:
            self._pending = candidate

    def start(self, repos: RepositorySession) -> None:
        """Insert the row this generation owns, before the loop can offer anything against it."""

        with repos.transaction():
            repos.trading.put_execution_runtime_state(self._current)

    def write_once(self, repos: RepositorySession) -> None:
        """Write a semantic change immediately, and an unchanged row only on the heartbeat."""

        with self._lock:
            candidate = self._pending
            self._pending = None
            current = self._current
        if candidate is None:
            return
        semantic_change = _semantic_state(candidate) != _semantic_state(current)
        heartbeat_due = candidate.heartbeat_at_ns - current.heartbeat_at_ns >= RUNTIME_HEARTBEAT_INTERVAL_NS
        if not semantic_change and not heartbeat_due:
            return
        with repos.transaction():
            if not repos.trading.update_execution_runtime_state(candidate):
                raise RuntimeError("oi_runtime_generation_lost")
        with self._lock:
            self._current = candidate

    def refresh_recovery_inputs(self, repos: RepositorySession, observed_at_ns: int) -> None:
        inputs = load_recovery_inputs(repos, self.current.account_slot, observed_at_ns)
        with self._lock:
            self._recovery_inputs = inputs


def _semantic_state(state: ExecutionRuntimeState) -> dict[str, Any]:
    values = asdict(state)
    values.pop("heartbeat_at_ns")
    values.pop("updated_at_ns")
    return values


def load_recovery_inputs(
    repos: RepositorySession,
    account_slot: str,
    observed_at_ns: int,
) -> tuple[tuple[TradeSignalV1, ...], tuple[OperatorIntentV1, ...]]:
    """Read the durable entry identities that can still hold Binance exposure."""

    since_ns = max(0, observed_at_ns - _RECOVERY_ENTRY_FACT_WINDOW_NS)
    signal_rows = repos.trading.execution_recovery_signals(
        account_slot=account_slot,
        since_ns=since_ns,
        limit=MAX_EXECUTION_READ_BATCH,
    )
    command_rows = repos.trading.execution_recovery_manual_entries(
        account_slot=account_slot,
        since_ns=since_ns,
        limit=MAX_EXECUTION_READ_BATCH,
    )
    if (
        len(signal_rows) == MAX_EXECUTION_READ_BATCH
        or len(command_rows) == MAX_EXECUTION_READ_BATCH
        or len(signal_rows) + len(command_rows) > MAX_EXECUTION_READ_BATCH
    ):
        raise RuntimeError("oi_runtime_recovery_history_overflow")
    return materialize_trade_signals(signal_rows), materialize_operator_intents(command_rows)


class OiRuntimeDatabaseBridge:
    """The one thread and the one connection that speak PostgreSQL for a running Runtime.

    Inputs, durable audit, the day-start baseline, the account-slot heartbeat and every durable
    current-state read and write happen here. The trading event loop keeps Binance, Nautilus and the
    in-memory picture; on 2026-09-02 it also held a third connection and queried it synchronously
    every 500 ms, on the same thread as every order callback and with no statement timeout (#510 E).
    """

    def __init__(
        self,
        *,
        settings: Any,
        profile: OiRuntimeProfile,
        signals: ExecutionSignalClient,
        audit: AuditSink,
        update_day_start: Callable[[DayStartBaseline], None],
        singleton: AccountSlotSingleton,
        projector: RuntimeStateProjector,
        poll_seconds: float = 0.2,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("oi_runtime_database_poll_invalid")
        self._settings = settings
        self._profile = profile
        self._signals = signals
        self._audit = audit
        self._update_day_start = update_day_start
        self._singleton = singleton
        self._projector = projector
        self._poll_seconds = poll_seconds
        self._stop = Event()
        self._thread: Thread | None = None
        self._lock = Lock()
        self._connected = False
        self._fatal_error: BaseException | None = None
        self._equity: tuple[Decimal, int] | None = None
        self._baseline_day: str | None = None
        self._step_failures: dict[str, str] = {}
        self._appended_since_recovery_read = 0
        self._recovery_read_at_ns = 0

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def fatal_error(self) -> BaseException | None:
        with self._lock:
            return self._fatal_error

    def recovery_inputs(self) -> tuple[tuple[TradeSignalV1, ...], tuple[OperatorIntentV1, ...]]:
        """The durable entry identities the next reconciliation rebuilds ownership from."""

        return self._projector.recovery_inputs()

    def set_equity(self, equity_usd: Decimal, observed_at_ns: int) -> None:
        if equity_usd <= 0 or observed_at_ns <= 0:
            return
        with self._lock:
            self._equity = (equity_usd, observed_at_ns)

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("oi_runtime_database_bridge_already_started")
        self._thread = Thread(target=self._run, name="tracefold-oi-runtime-db", daemon=False)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with open_repositories(
                    self._settings, application_name="tracefold_nautilus_stream", long_lived=True
                ) as repos:
                    repos.conn.execute(f"SET statement_timeout = {_STATEMENT_TIMEOUT_MS}")
                    install_execution_stream_listener(repos.conn, channel=execution_stream_channel())
                    with self._lock:
                        self._connected = True
                    while not self._stop.is_set():
                        self._cycle(repos)
                        if self._stop.is_set():
                            break
                        wait_for_execution_stream_wake(repos.conn, self._poll_seconds)
                    # The composition root offers its `stopped` row on the way out; this connection is
                    # the only one that can still write it.
                    self._step("projection", lambda: self._projector.write_once(repos))
                    break
            except (InterfaceError, OperationalError):
                with self._lock:
                    self._connected = False
                self._stop.wait(self._poll_seconds)
            except BaseException as exc:
                with self._lock:
                    self._connected = False
                    self._fatal_error = exc
                logger.exception("OI Runtime database bridge failed ({})", type(exc).__name__)
                return
        with self._lock:
            self._connected = False

    def _cycle(self, repos: RepositorySession) -> None:
        """Independent steps, so no one of them can silence the others.

        Reading Commands is what lets an operator flatten, so it runs first and no other step's
        failure can delay it; an audit append in the same `try` once stopped it for six hours while a
        position was open (#510 A). Only a lost connection aborts the cycle, because that is the
        session-replacement path in `_run`. A failing current-state step logs once and lets the
        `alive` heartbeat go stale, which is already how every reader decides a Runtime is gone: not
        a new gate.
        """

        # The advisory lock lives on the singleton's own session; the loop reads `acquired` from
        # memory and fails closed. `check` never raises - a dead session is what it reports.
        self._singleton.check()
        self._step(
            "commands",
            lambda: self._signals.poll_commands_once(
                lambda slot, strategy, limit: load_unresolved_operator_intents(repos, slot, strategy, limit),
            ),
        )
        self._step(
            "signals",
            lambda: self._signals.poll_once(
                lambda slot, strategy, limit: load_unresolved_trade_signals(repos, slot, strategy, limit),
            ),
        )
        self._step("audit", lambda: self._flush_audit(repos))
        self._refresh_current_state(repos)
        with self._lock:
            equity = self._equity
        if equity is None:
            return
        equity_usd, observed_at_ns = equity
        utc_day = datetime.fromtimestamp(observed_at_ns / 1_000_000_000, tz=UTC).date().isoformat()
        if utc_day == self._baseline_day:
            return
        baseline = load_or_record_day_start(
            repos=repos,
            factory=self._audit.factory,
            utc_day=utc_day,
            equity_usd=equity_usd,
            recorded_at_ns=observed_at_ns,
        )
        self._update_day_start(baseline)
        self._baseline_day = utc_day

    def _flush_audit(self, repos: RepositorySession) -> None:
        self._appended_since_recovery_read += flush_audit_once(
            repos=repos,
            audit=self._audit,
            signals=self._signals,
        )

    def _refresh_current_state(self, repos: RepositorySession) -> None:
        """Re-read what the loop needs about durable current state, then write what it computed.

        The recovery identities are re-read as soon as this bridge has appended anything, because the
        only way a new identity becomes recoverable is the `order`/`entry` Observation this same step
        just made durable. That makes the set the loop reconciles against fresher than the read it
        replaced, not staler; the periodic floor exists only so a quiet Runtime still refreshes.
        """

        now_ns = time.time_ns()
        recovery_due = now_ns - self._recovery_read_at_ns >= int(self._profile.risk.reconciliation_interval_ns)
        if (self._appended_since_recovery_read or recovery_due) and self._step(
            "recovery",
            lambda: self._projector.refresh_recovery_inputs(repos, now_ns),
        ):
            self._appended_since_recovery_read = 0
            self._recovery_read_at_ns = now_ns
        self._step("projection", lambda: self._projector.write_once(repos))

    def _step(self, name: str, run: Callable[[], object]) -> bool:
        """Run one cycle step, logging a repeating cause once instead of once per cycle (742 times in
        six hours, in production). A psycopg error's first line names the relation and constraint
        without the offending row, so it is stable across rows and is what to deduplicate on.
        """

        try:
            run()
        except (InterfaceError, OperationalError):
            raise
        except Exception as exc:
            reason = f"{type(exc).__name__}: {str(exc).strip().splitlines()[0][:200]}"
            with self._lock:
                changed = self._step_failures.get(name) != reason
                self._step_failures[name] = reason
            if changed:
                logger.exception("OI Runtime database bridge step failed ({})", name)
            return False
        with self._lock:
            recovered = self._step_failures.pop(name, None) is not None
        if recovered:
            logger.info("OI Runtime database bridge step recovered ({})", name)
        return True


def run_nautilus(profile: OiRuntimeProfile) -> None:
    """Refuse to build a node for a disabled profile, and refuse an active one outright.

    A disabled Runtime has no readiness to report: it never opens a session, never projects a row and
    never answers a probe. The readiness value this used to return had exactly one caller, which
    discarded it (#510 E).
    """

    if profile.mode != "disabled":
        raise RuntimeError("oi_runtime_active_profile_requires_composition_root")


def load_unresolved_trade_signals(
    repos: RepositorySession,
    account_slot: str,
    execution_strategy: str,
    limit: int,
) -> tuple[TradeSignalV1, ...]:
    """Materialize Trading-owned rows at the App composition boundary.

    The wall clock is read here, at the composition seam, because "still pending" is a fact about now
    and the storage statement takes it as a bound rather than reading a clock of its own.
    """

    rows = repos.trading.unresolved_trade_signals(
        account_slot=account_slot,
        execution_strategy=execution_strategy,
        now_ns=time.time_ns(),
        limit=limit,
    )
    return materialize_trade_signals(rows)


def load_unresolved_operator_intents(
    repos: RepositorySession,
    account_slot: str,
    execution_strategy: str,
    limit: int,
) -> tuple[OperatorIntentV1, ...]:
    """Materialize authenticated Commands beside Signals at the App boundary."""

    rows = repos.trading.unresolved_operator_intents(
        account_slot=account_slot,
        execution_strategy=execution_strategy,
        now_ns=time.time_ns(),
        limit=limit,
    )
    return materialize_operator_intents(rows)


def execution_stream_channel() -> str:
    """Return the Trading-owned LISTEN wake channel to the PostgreSQL adapter."""

    return EXECUTION_STREAM_NOTIFY_CHANNEL


def load_runtime_control_state(
    repos: RepositorySession,
    account_slot: str,
    *,
    now_ns: int,
) -> RuntimeControlSnapshot:
    """Load this slot's current control row, creating an unpaused one the first time.

    Control belongs to the account slot and survives every deploy: a slot the operator resumed is
    still resumed after a restart, a new image or a risk-config change, and only a Command moves it.
    Command/Observation history is never a startup path.
    """

    with repos.transaction():
        state = repos.trading.ensure_execution_runtime_control_state(account_slot, now_ns=now_ns)
    return RuntimeControlSnapshot(
        entries_paused=state.entries_paused,
        emergency_halted=state.emergency_halted,
        flatten_pending=(),
    )


def flush_audit_once(
    *,
    repos: RepositorySession,
    audit: AuditSink,
    signals: ExecutionSignalClient,
) -> int:
    """Background-only durable append; no Strategy callback can reach this function.

    `flush_once` returns everything that left the queue, durably appended or quarantined, and both
    settle their input: a Signal or Command whose disposition the database refused is disposed of all
    the same, because the Runtime lost the audit fact, not the decision.
    """

    def writer(values: Sequence[ExecutionObservationV1]) -> None:
        prepared = prepare_execution_observations(values)
        try:
            with repos.transaction():
                repos.trading.append_execution_observations(prepared)
        except IntegrityError as exc:
            # A CHECK, unique, foreign key or NOT NULL refusal is a verdict on the batch, not weather:
            # replaying it forever is what blinded the ledger. The sink drops it and records the gap.
            raise AuditAppendRejected(f"{type(exc).__name__}: {exc.diag.constraint_name or exc.sqlstate}") from exc

    flushed = audit.flush_once(writer)
    for value in flushed:
        if value.normalized_kind == "signal_disposition" and value.signal_id is not None:
            signals.mark_durable(value.signal_id)
        if value.normalized_kind == "control_disposition" and value.command_id is not None:
            signals.mark_command_durable(value.command_id)
    return len(flushed)


def load_or_record_day_start(
    *,
    repos: RepositorySession,
    factory: ObservationFactory,
    utc_day: str,
    equity_usd: Decimal,
    recorded_at_ns: int,
) -> DayStartBaseline:
    """Recover the immutable daily baseline before considering new exposure."""

    event_id = factory.day_start_event_id(utc_day)
    stored = repos.trading.execution_observation(event_id)
    if stored is not None:
        return day_start_baseline_from_observation(materialize_execution_observation(stored))
    baseline, observation = factory.day_start_baseline(
        utc_day=utc_day,
        equity_usd=equity_usd,
        recorded_at_ns=recorded_at_ns,
    )
    prepared = prepare_execution_observations((observation,))
    with repos.transaction():
        repos.trading.append_execution_observations(prepared)
    return baseline


__all__ = [
    "RUNTIME_HEARTBEAT_INTERVAL_NS",
    "OiRuntimeDatabaseBridge",
    "RuntimeStateProjector",
    "load_recovery_inputs",
    "run_nautilus",
]
