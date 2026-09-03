"""PostgreSQL bridge plus the disabled half of the OI Runtime composition seam."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from threading import Event, Lock, Thread
from typing import Any, Literal

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
from tracefold.integrations.nautilus.oi_runtime.state import RuntimeControlSnapshot
from tracefold.trading import ExecutionObservationV1, OperatorIntentV1, TradeSignalV1
from tracefold.trading.storage.execution_stream import (
    EXECUTION_STREAM_NOTIFY_CHANNEL,
    materialize_execution_observation,
    materialize_operator_intents,
    materialize_trade_signals,
    prepare_execution_observations,
)

# The two `_cycle` steps that read the Runtime's inputs. While either is failing the Runtime is not
# consuming Signals or Commands, which is exactly what `control_plane_ready` already means.
_INPUT_STEPS = ("commands", "signals")


@dataclass(frozen=True, slots=True)
class OiRuntimeReadiness:
    mode: Literal["disabled"]
    runtime_profile_id: str
    runtime_release: str
    alive: Literal[False]
    execution_safe: Literal[False]
    entries_armed: Literal[False]
    entry_block_reason: Literal["disabled"]


class OiRuntimeDatabaseBridge:
    """Own every steady PostgreSQL call outside Nautilus callbacks."""

    def __init__(
        self,
        *,
        settings: Any,
        profile: OiRuntimeProfile,
        signals: ExecutionSignalClient,
        audit: AuditSink,
        update_day_start: Callable[[DayStartBaseline], None],
        poll_seconds: float = 0.2,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("oi_runtime_database_poll_invalid")
        self._settings = settings
        self._profile = profile
        self._signals = signals
        self._audit = audit
        self._update_day_start = update_day_start
        self._poll_seconds = poll_seconds
        self._stop = Event()
        self._thread: Thread | None = None
        self._lock = Lock()
        self._connected = False
        self._fatal_error: BaseException | None = None
        self._equity: tuple[Decimal, int] | None = None
        self._baseline_day: str | None = None
        self._step_failures: dict[str, str] = {}

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def fatal_error(self) -> BaseException | None:
        with self._lock:
            return self._fatal_error

    @property
    def inputs_ready(self) -> bool:
        """False while a Command or Signal read keeps failing, so entries disarm instead of drifting.

        A step that only logs would leave the Runtime `entries_armed` while it is silently consuming
        nothing - armed for exposure it could never be told to unwind. This is the same fact
        `control_plane_ready` has always carried, so it needs no gate of its own: it blocks new
        entries and leaves `execution_safe` alone, because existing exposure is still protected.
        """

        with self._lock:
            return not any(name in self._step_failures for name in _INPUT_STEPS)

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
                with open_repositories(self._settings, application_name="tracefold_nautilus_stream") as repos:
                    install_execution_stream_listener(repos.conn, channel=execution_stream_channel())
                    with self._lock:
                        self._connected = True
                    while not self._stop.is_set():
                        self._cycle(repos)
                        if self._stop.is_set():
                            break
                        wait_for_execution_stream_wake(repos.conn, self._poll_seconds)
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
        """Three independent steps, so no one of them can silence the other two.

        Reading Commands is what lets an operator flatten, and on 2026-09-02 it stopped for six hours
        because the audit append in the same `try` kept raising: the runtime went deaf while holding a
        position (#510 A). Commands are still read first, so a Signal or audit failure cannot even
        delay them, and only a lost connection still aborts the cycle - that is the session-replacement
        path in `_run` and it has to keep working.
        """

        self._step(
            "commands",
            lambda: self._signals.poll_commands_once(
                lambda profile, strategy, limit: load_unresolved_operator_intents(repos, profile, strategy, limit),
            ),
        )
        self._step(
            "signals",
            lambda: self._signals.poll_once(
                lambda profile, strategy, limit: load_unresolved_trade_signals(repos, profile, strategy, limit),
            ),
        )
        self._step("audit", lambda: flush_audit_once(repos=repos, audit=self._audit, signals=self._signals))
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

    def _step(self, name: str, run: Callable[[], object]) -> None:
        """Run one cycle step, and log a repeating cause once instead of once per cycle.

        The production log for the same CheckViolation carried 742 identical tracebacks in six hours.
        The first line of a psycopg error names the relation and constraint without the offending row,
        so it is stable across rows and is the right thing to deduplicate on.
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
            return
        with self._lock:
            recovered = self._step_failures.pop(name, None) is not None
        if recovered:
            logger.info("OI Runtime database bridge step recovered ({})", name)


def run_nautilus(profile: OiRuntimeProfile) -> OiRuntimeReadiness:
    """Return disabled readiness without constructing a node."""

    if profile.mode != "disabled":
        raise RuntimeError("oi_runtime_active_profile_requires_composition_root")
    return OiRuntimeReadiness(
        mode="disabled",
        runtime_profile_id=profile.profile_id,
        runtime_release=profile.runtime_release,
        alive=False,
        execution_safe=False,
        entries_armed=False,
        entry_block_reason="disabled",
    )


def load_unresolved_trade_signals(
    repos: RepositorySession,
    runtime_profile_id: str,
    execution_strategy: str,
    limit: int,
) -> tuple[TradeSignalV1, ...]:
    """Materialize Trading-owned rows at the App composition boundary."""

    rows = repos.trading.unresolved_trade_signals(
        runtime_profile_id=runtime_profile_id,
        execution_strategy=execution_strategy,
        limit=limit,
    )
    return materialize_trade_signals(rows)


def load_unresolved_operator_intents(
    repos: RepositorySession,
    runtime_profile_id: str,
    execution_strategy: str,
    limit: int,
) -> tuple[OperatorIntentV1, ...]:
    """Materialize authenticated Commands beside Signals at the App boundary."""

    rows = repos.trading.unresolved_operator_intents(
        runtime_profile_id=runtime_profile_id,
        execution_strategy=execution_strategy,
        limit=limit,
    )
    return materialize_operator_intents(rows)


def execution_stream_channel() -> str:
    """Return the Trading-owned LISTEN wake channel to the PostgreSQL adapter."""

    return EXECUTION_STREAM_NOTIFY_CHANNEL


def load_runtime_control_state(
    repos: RepositorySession,
    runtime_profile_id: str,
) -> RuntimeControlSnapshot:
    """Load one current row; Command/Observation history is never a startup path."""

    state = repos.trading.execution_runtime_control_state(runtime_profile_id)
    if state is None:
        raise RuntimeError("oi_runtime_control_state_missing")
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


__all__ = ["OiRuntimeDatabaseBridge", "OiRuntimeReadiness", "run_nautilus"]
