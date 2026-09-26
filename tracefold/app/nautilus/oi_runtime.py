"""The PostgreSQL bridge and current-state writer for the OI Runtime.

The bridge and the current-state writer use separate fixed connections. Nothing they do is fatal to the
process (#680 RC1): a statement that fails is logged once per cause and retried, a lost session is
replaced after a bounded backoff, and a journal row the database refuses on integrity grounds is
dropped and logged rather than replayed. The account-slot lock is the one fact that stops the process,
and it lives on its own session.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from threading import Event, Lock, Thread
from typing import Any

from loguru import logger
from psycopg import InterfaceError, OperationalError
from psycopg.errors import DataError, IntegrityError, RaiseException

from tracefold.app.repository_session import RepositorySession
from tracefold.app.repository_session import repositories as open_repositories
from tracefold.integrations.nautilus.oi_runtime.config import OiRuntimeProfile
from tracefold.integrations.nautilus.oi_runtime.journal import (
    EntryValidityReceipt,
    ExecutionJournal,
    JournalRow,
    ObservationFactory,
    PlanReceipt,
    day_start_baseline_from_observation,
)
from tracefold.integrations.nautilus.oi_runtime.risk import DayStartBaseline
from tracefold.integrations.nautilus.oi_runtime.signal_client import ExecutionSignalClient
from tracefold.integrations.nautilus.oi_runtime.singleton import AccountSlotSingleton
from tracefold.integrations.nautilus.oi_runtime.strategy import OpenPlan, RuntimeControlSnapshot, RuntimeInputs
from tracefold.trading.execution_contracts import (
    ExecutionObservationV1,
    OperatorIntentV1,
    TradeSignalV3,
)
from tracefold.trading.storage.execution_stream import (
    ExecutionRuntimeState,
    materialize_execution_observation,
    materialize_operator_intents,
    materialize_trade_signals,
    prepare_execution_observations,
)
from tracefold.trading.storage.trade_plans import (
    MAX_OPEN_TRADE_PLANS,
    prepare_trade_plan,
    prepare_trade_plan_update,
)
from tracefold.trading.trade_plan import PlanOrderBinding, TradePlan

# How often the current row is rewritten when nothing about it changed. It is well inside the public
# five-second stale budget, so a Runtime that stops projecting reads as stale rather than as healthy.
RUNTIME_HEARTBEAT_INTERVAL_NS = 500_000_000
# Reading Commands on this connection is how an operator flattens. A statement that has not finished
# in five seconds is broken, not slow: PostgreSQL cancels it and the step retries.
_STATEMENT_TIMEOUT_MS = 5_000
_RECONNECT_BACKOFF_SECONDS = (0.2, 0.5, 1.0, 2.0, 5.0)
# The database's verdict on a row, as opposed to weather: no retry can change it.
_ROW_REFUSALS = (IntegrityError, DataError, RaiseException, ValueError, RuntimeError)


class RuntimeStateProjector:
    """Newest event-loop candidate and the last state actually committed by its writer."""

    def __init__(self, *, initial: ExecutionRuntimeState) -> None:
        self._lock = Lock()
        self._current = initial
        self._pending: ExecutionRuntimeState | None = None

    @property
    def current(self) -> ExecutionRuntimeState:
        """The last row actually written, which is what the next candidate is compared against."""

        with self._lock:
            return self._current

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
            current = self._current
        if candidate is None:
            return
        semantic_change = _semantic_state(candidate) != _semantic_state(current)
        heartbeat_due = candidate.heartbeat_at_ns - current.heartbeat_at_ns >= RUNTIME_HEARTBEAT_INTERVAL_NS
        if not semantic_change and not heartbeat_due:
            return
        with repos.transaction():
            written = repos.trading.update_execution_runtime_state(candidate)
        if not written:
            raise RuntimeError(f"oi_runtime_generation_fenced:{candidate.account_slot}")
        with self._lock:
            self._current = candidate
            if self._pending is candidate:
                self._pending = None


class RuntimeStateWriter:
    """The only current-state writer; it never reads Nautilus or shares the journal connection."""

    def __init__(self, *, settings: Any, projector: RuntimeStateProjector, poll_seconds: float = 0.2) -> None:
        self._settings = settings
        self._projector = projector
        self._poll_seconds = poll_seconds
        self._stop = Event()
        self._thread: Thread | None = None
        self._lock = Lock()
        self._initialized = False
        self._connected = False
        self._failure: str | None = None
        self._last_written_at_ns: int | None = None

    @property
    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "connected": self._connected,
                "failure": self._failure,
                "last_written_at_ns": self._last_written_at_ns,
            }

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("oi_runtime_state_writer_already_started")
        self._thread = Thread(target=self._run, name="tracefold-oi-runtime-state", daemon=False)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                raise RuntimeError("oi_runtime_state_writer_shutdown_timeout")

    def _run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                with open_repositories(
                    self._settings, application_name="tracefold_nautilus_state", long_lived=True
                ) as repos:
                    repos.conn.execute("SET statement_timeout = 1000")
                    with self._lock:
                        self._connected = True
                    if not self._initialized:
                        self._projector.start(repos)
                        self._initialized = True
                        with self._lock:
                            self._last_written_at_ns = self._projector.current.heartbeat_at_ns
                            self._failure = None
                    failures = 0
                    while not self._stop.is_set():
                        self._projector.write_once(repos)
                        with self._lock:
                            self._last_written_at_ns = self._projector.current.heartbeat_at_ns
                            self._failure = None
                        self._stop.wait(self._poll_seconds)
                    # The root offers its stopped row before asking us to finish.
                    self._projector.write_once(repos)
                    break
            except Exception as exc:
                reason = type(exc).__name__
                with self._lock:
                    changed = self._failure != reason
                    self._failure = reason
                    self._connected = False
                if changed:
                    logger.opt(exception=exc).error("OI Runtime current-state write failed")
                if reason == "RuntimeError" and str(exc).startswith("oi_runtime_generation_fenced:"):
                    break
                delay = _RECONNECT_BACKOFF_SECONDS[min(failures, len(_RECONNECT_BACKOFF_SECONDS) - 1)]
                failures += 1
                self._stop.wait(delay)
        with self._lock:
            self._connected = False


def _semantic_state(state: ExecutionRuntimeState) -> dict[str, Any]:
    values = asdict(state)
    values.pop("heartbeat_at_ns")
    values.pop("updated_at_ns")
    return values


def load_runtime_inputs(
    repos: RepositorySession,
    profile: OiRuntimeProfile,
    *,
    now_ns: int,
) -> RuntimeInputs:
    """Everything a generation reads before its Strategy starts: control, open plans, stop-outs.

    Control belongs to the account slot and outlives this process: a slot the operator resumed is
    still resumed after a restart, a new image or a risk-config change (#520 PR-A). Open plans are the
    intent Nautilus' reconciled Cache is matched against; they carry no execution state.
    """

    with repos.transaction():
        control = repos.trading.ensure_execution_runtime_control_state(profile.account_slot, now_ns=now_ns)
    rows = repos.trading.open_trade_plans(
        account_slot=profile.account_slot,
        limit=MAX_OPEN_TRADE_PLANS,
    )
    materialized: list[OpenPlan] = []
    for row in rows:
        plan = TradePlan.model_validate({key: value for key, value in row.items() if key != "disposition_pending"})
        signal: TradeSignalV3 | None = None
        checked = False
        if plan.source == "signal" and plan.status == "prepared":
            stored = repos.trading.trade_signal(plan.entry_id)
            if stored is not None and stored[1].get("signal_version") == "trade_signal_v3":
                signal = TradeSignalV3.model_validate_json(json.dumps(stored[1] | {"seq": stored[0]}))
            checked = repos.trading.latest_entry_validity_check(plan.entry_id) is not None
        materialized.append(
            OpenPlan(
                plan=plan,
                disposition_pending=bool(row["disposition_pending"]),
                signal=signal,
                final_check_started=checked,
            )
        )
    open_plans = tuple(materialized)
    bindings: dict[str, PlanOrderBinding] = {}
    after_seq = 0
    while open_plans:
        binding_rows = repos.trading.trade_plan_order_bindings(
            account_slot=profile.account_slot,
            entry_ids=tuple(value.plan.entry_id for value in open_plans),
            after_seq=after_seq,
            observed_before_ns=now_ns,
            limit=256,
        )
        for binding_row in binding_rows:
            after_seq = int(binding_row["seq"])
            binding = PlanOrderBinding.model_validate(
                {key: value for key, value in binding_row.items() if key != "seq"}
            )
            previous = bindings.get(binding.client_order_id)
            if previous is not None and previous != binding:
                raise ValueError("plan_order_identity_conflict")
            bindings[binding.client_order_id] = binding
        if len(binding_rows) < 256:
            break
    stop_exits = repos.trading.recent_stop_exits(
        account_slot=profile.account_slot,
        since_ns=now_ns - profile.risk.post_stop_cooldown_ns,
    )
    return RuntimeInputs(
        control=RuntimeControlSnapshot(
            entries_paused=control.entries_paused,
            emergency_halted=control.emergency_halted,
        ),
        open_plans=open_plans,
        order_bindings=tuple(bindings.values()),
        stop_exits=stop_exits,
    )


def commit_entry_plan(repos: RepositorySession, plan: TradePlan) -> PlanReceipt:
    """Return only after commit. Only the exact prepared plan authorizes its entry order."""

    values = prepare_trade_plan(plan)
    with repos.transaction():
        scoped = repos.trading.trade_plan_for_scope(
            account_slot=plan.account_slot,
            entry_scope_id=plan.entry_scope_id,
        )
        if scoped is not None and scoped["entry_id"] != plan.entry_id:
            return PlanReceipt(plan, committed=False, reason="entry_scope_already_used")
        repos.trading.insert_trade_plan(values)
        stored = repos.trading.trade_plan(plan.entry_id)
    if stored is None:
        return PlanReceipt(plan, committed=False, reason="trade_plan_commit_missing")
    if TradePlan.model_validate(stored) != plan:
        return PlanReceipt(plan, committed=False, reason="trade_plan_conflict")
    return PlanReceipt(plan, committed=True)


def write_journal_row(repos: RepositorySession, value: ExecutionObservationV1 | TradePlan) -> None:
    """One row, one transaction."""

    if isinstance(value, TradePlan):
        prepared_plan = prepare_trade_plan_update(value)
        with repos.transaction():
            written = repos.trading.update_trade_plan(prepared_plan)
            if not written:
                stored = repos.trading.trade_plan(value.entry_id)
                if stored is None:
                    raise ValueError("trade_plan_transition_missing")
                current = TradePlan.model_validate(stored)
                if current != value and not (
                    current.entry_client_order_id == value.entry_client_order_id
                    and current.updated_at_ns > value.updated_at_ns
                    and current.status == "closed"
                    and value.status != "closed"
                ):
                    raise ValueError("trade_plan_transition_conflict")
        return
    prepared = prepare_execution_observations((value,))
    with repos.transaction():
        repos.trading.append_execution_observations(prepared)


class OiRuntimeDatabaseBridge:
    """Commands, durable preparation, journal and signals; current-state has its own writer."""

    def __init__(
        self,
        *,
        settings: Any,
        profile: OiRuntimeProfile,
        signals: ExecutionSignalClient,
        journal: ExecutionJournal,
        update_day_start: Callable[[DayStartBaseline], None],
        singleton: AccountSlotSingleton,
        poll_seconds: float = 0.2,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("oi_runtime_database_poll_invalid")
        self._settings = settings
        self._profile = profile
        self._signals = signals
        self._journal = journal
        self._update_day_start = update_day_start
        self._singleton = singleton
        self._poll_seconds = poll_seconds
        self._stop = Event()
        self._thread: Thread | None = None
        self._lock = Lock()
        self._connected = False
        self._equity: tuple[Decimal, int] | None = None
        self._baseline_day: str | None = None
        self._step_failures: dict[str, str] = {}
        self._step_duration_ms: dict[str, float] = {}

    @property
    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "connected": self._connected,
                "step_failures": dict(self._step_failures),
                "step_duration_ms": dict(self._step_duration_ms),
            }

    def set_equity(self, equity_usd: Decimal | None, observed_at_ns: int) -> None:
        if equity_usd is None or equity_usd <= 0 or observed_at_ns <= 0:
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
            if self._thread.is_alive():
                raise RuntimeError("oi_runtime_database_bridge_shutdown_timeout")

    def _run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                with open_repositories(
                    self._settings, application_name="tracefold_nautilus_stream", long_lived=True
                ) as repos:
                    repos.conn.execute(f"SET statement_timeout = {_STATEMENT_TIMEOUT_MS}")
                    with self._lock:
                        self._connected = True
                    if failures:
                        logger.info("OI Runtime database bridge reconnected")
                    failures = 0
                    while not self._stop.is_set():
                        self._cycle(repos)
                        if self._stop.is_set():
                            break
                        self._stop.wait(self._poll_seconds)
                    self._step("journal", lambda: self._flush_journal(repos))
                    break
            except Exception as exc:
                # A lost session, a failed connect, or anything else the cycle did not contain: the
                # bridge waits, reconnects and carries on. It is never why the Runtime stops.
                with self._lock:
                    self._connected = False
                delay = _RECONNECT_BACKOFF_SECONDS[min(failures, len(_RECONNECT_BACKOFF_SECONDS) - 1)]
                if failures == 0:
                    logger.warning("OI Runtime database bridge lost its session ({}); reconnecting", type(exc).__name__)
                failures += 1
                self._stop.wait(delay)
        with self._lock:
            self._connected = False

    def _cycle(self, repos: RepositorySession) -> None:
        """Independent steps, so no one of them can silence the others.

        Reading Commands is what lets an operator flatten, so it runs first and no other step's
        failure can delay it (#510 A). Only a lost connection aborts the cycle, because that is the
        session-replacement path in `_run`.
        """

        # The advisory lock lives on the singleton's own session; the loop reads `acquired` from
        # memory and exits when it is gone. `check` never raises - a dead session is what it reports.
        self._singleton.check()
        self._step(
            "commands",
            lambda: self._signals.poll_commands_once(
                lambda slot, strategy, limit: load_unresolved_operator_intents(repos, slot, strategy, limit),
            ),
        )
        self._step("entry_plan", lambda: self._commit_entry_plan(repos))
        self._step("entry_validity", lambda: self._check_entry_validity(repos))
        self._step("journal", lambda: self._flush_journal(repos))
        self._step(
            "signals",
            lambda: self._signals.poll_once(
                lambda slot, strategy, limit: load_unresolved_trade_signals(repos, slot, strategy, limit),
            ),
        )
        self._step("day_start", lambda: self._refresh_day_start(repos))

    def _commit_entry_plan(self, repos: RepositorySession) -> None:
        plan = self._journal.pending_prepare()
        if plan is None:
            return
        try:
            receipt = commit_entry_plan(repos, plan)
        except _ROW_REFUSALS as exc:
            logger.error("OI Runtime entry plan refused ({}): {}", plan.entry_id, type(exc).__name__)
            scoped = repos.trading.trade_plan_for_scope(
                account_slot=plan.account_slot,
                entry_scope_id=plan.entry_scope_id,
            )
            receipt = PlanReceipt(
                plan,
                committed=False,
                reason="entry_scope_already_used" if scoped is not None else "trade_plan_rejected",
            )
        self._journal.settle_prepare(receipt)

    def _check_entry_validity(self, repos: RepositorySession) -> None:
        plan = self._journal.pending_entry_validity()
        if plan is None:
            return
        checked_at_ns = time.time_ns()
        with repos.transaction():
            allowed, reason = repos.trading.validate_signal_entry(
                entry_id=plan.entry_id,
                now_ns=checked_at_ns,
            )
        self._journal.settle_entry_validity(
            EntryValidityReceipt(
                entry_id=plan.entry_id,
                allowed=allowed,
                reason=reason,
                checked_at_ns=checked_at_ns,
            )
        )

    def _flush_journal(self, repos: RepositorySession) -> None:
        """Bound each cycle and retain rejected Plan transitions for a later storage verdict."""

        deadline = time.monotonic() + 0.1
        for row in self._journal.due(time.monotonic(), limit=32):
            if time.monotonic() >= deadline:
                break
            value = row.value
            try:
                write_journal_row(repos, value)
            except (InterfaceError, OperationalError):
                self._journal.retry_later(row, time.monotonic())
                raise
            except _ROW_REFUSALS as exc:
                if (
                    isinstance(value, TradePlan)
                    or value.normalized_kind == "fill"
                    or value.summary.get("binding_version") == "plan_order_v1"
                ):
                    logger.error(
                        "OI Runtime critical evidence not durable ({}): {}",
                        row.key,
                        type(exc).__name__,
                    )
                    self._journal.retry_later(row, time.monotonic())
                    continue
                logger.error(
                    "OI Runtime journal observation refused and dropped ({} {}): {}",
                    _row_kind(row),
                    row.key,
                    f"{type(exc).__name__}: {(str(exc).strip().splitlines() or [''])[0][:200]}",
                )
                self._settled(value)
                self._journal.written(row, value)
                continue
            except Exception as exc:
                logger.warning(
                    "OI Runtime journal row deferred ({} {}): {}", _row_kind(row), row.key, type(exc).__name__
                )
                self._journal.retry_later(row, time.monotonic())
                continue
            self._settled(value)
            self._journal.written(row, value)
            if time.monotonic() >= deadline:
                break

    def _settled(self, value: ExecutionObservationV1 | TradePlan) -> None:
        """A written verdict releases its input from this process's in-flight claim."""

        if not isinstance(value, ExecutionObservationV1):
            return
        if value.normalized_kind == "signal_disposition" and value.signal_id is not None:
            self._signals.mark_durable(value.signal_id)
        if value.normalized_kind == "control_disposition" and value.command_id is not None:
            self._signals.mark_command_durable(value.command_id)

    def _refresh_day_start(self, repos: RepositorySession) -> None:
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
            factory=self._journal.factory,
            utc_day=utc_day,
            equity_usd=equity_usd,
            recorded_at_ns=observed_at_ns,
        )
        self._update_day_start(baseline)
        self._baseline_day = utc_day

    def _step(self, name: str, run: Callable[[], object]) -> bool:
        """Run one cycle step, logging a repeating cause once instead of once per cycle."""

        started = time.monotonic()
        try:
            run()
        except (InterfaceError, OperationalError):
            raise
        except Exception as exc:
            reason = f"{type(exc).__name__}: {(str(exc).strip().splitlines() or [''])[0][:200]}"
            with self._lock:
                changed = self._step_failures.get(name) != reason
                self._step_failures[name] = reason
            if changed:
                logger.exception("OI Runtime database bridge step failed ({})", name)
            return False
        finally:
            with self._lock:
                self._step_duration_ms[name] = round((time.monotonic() - started) * 1000, 3)
        with self._lock:
            recovered = self._step_failures.pop(name, None) is not None
        if recovered:
            logger.info("OI Runtime database bridge step recovered ({})", name)
        return True


def _row_kind(row: JournalRow) -> str:
    value = row.value
    return value.normalized_kind if isinstance(value, ExecutionObservationV1) else f"plan:{value.status}"


def load_unresolved_trade_signals(
    repos: RepositorySession,
    account_slot: str,
    execution_strategy: str,
    limit: int,
) -> tuple[TradeSignalV3, ...]:
    """Materialize Trading-owned rows at the App composition boundary."""

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
    "commit_entry_plan",
    "load_or_record_day_start",
    "load_runtime_inputs",
    "load_unresolved_operator_intents",
    "load_unresolved_trade_signals",
    "write_journal_row",
]
