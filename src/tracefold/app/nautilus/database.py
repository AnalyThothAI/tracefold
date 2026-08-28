"""One PostgreSQL thread bridging durable intents to the Nautilus thread."""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from decimal import Decimal
from queue import Empty
from threading import Event, Lock, Thread
from typing import Any

from loguru import logger
from psycopg import InterfaceError, OperationalError

from tracefold.app.repository_session import repositories
from tracefold.integrations.nautilus.messages import (
    AdoptIntent,
    CloseSubmitted,
    EntryFenceGranted,
    EntryFenceRequested,
    EntryFilled,
    EntryRejected,
    IntentRefused,
    IntentReleased,
    OrderOutcomeUnknown,
    PositionClosedObserved,
    PositionFlatConfirmed,
    PositionQuantityChanged,
    ReadinessChanged,
    StopAccepted,
    StopSubmitted,
    StrategyEvent,
    StrategyQueues,
)
from tracefold.platform.config.models import Settings

_RepositoryFactory = Callable[..., AbstractContextManager[Any]]
NAUTILUS_POLL_SECONDS = 1.0


class NautilusDatabaseBridge:
    """Own the sole DB connection and the two bounded thread queues."""

    def __init__(
        self,
        settings: Settings,
        queues: StrategyQueues,
        *,
        capability_snapshot_sha256: str | None = None,
        repository_factory: _RepositoryFactory = repositories,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._settings = settings
        self._queues = queues
        self._capability_snapshot_sha256 = capability_snapshot_sha256
        self._repository_factory = repository_factory
        self._now_ms = now_ms or (lambda: int(time.time() * 1_000))
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._lock = Lock()
        self._db_connected = False
        self._engine_ready = False
        self._readiness_reason = "engine_starting"
        self._unexpected_exposure = False
        self._event_projection_healthy = True
        self._expiry_projection_healthy = True
        self._heartbeat_at_ms: int | None = None
        self._error: BaseException | None = None
        self._dispatched_intent_id: str | None = None
        self._pending_event: StrategyEvent | None = None

    @property
    def error(self) -> BaseException | None:
        with self._lock:
            return self._error

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("nautilus_database_bridge_already_started")
        self._thread = Thread(target=self._run, name="tracefold-nautilus-db")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def readiness(self) -> dict[str, Any]:
        with self._lock:
            heartbeat_at_ms = self._heartbeat_at_ms
            heartbeat_current = heartbeat_at_ms is not None and max(0, self._now_ms() - heartbeat_at_ms) <= int(
                NAUTILUS_POLL_SECONDS * 3_000
            )
            ok = bool(
                self._error is None
                and self._db_connected
                and self._engine_ready
                and self._projection_healthy
                and not self._unexpected_exposure
                and heartbeat_current
            )
            if self._error is not None:
                reason = "database_bridge_failed"
            elif self._unexpected_exposure:
                reason = self._readiness_reason
            elif not self._db_connected:
                reason = "database_unavailable"
            elif not self._projection_healthy:
                reason = "execution_projection_rejected"
            elif not self._engine_ready:
                reason = self._readiness_reason
            elif not heartbeat_current:
                reason = "database_heartbeat_stale"
            else:
                reason = "ready"
            return {
                "ok": ok,
                "reason": reason,
                "heartbeat_at_ms": heartbeat_at_ms,
                "db_connected": self._db_connected,
                "engine_ready": self._engine_ready,
                "unexpected_exposure": self._unexpected_exposure,
            }

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                with self._repository_factory(self._settings, role="nautilus") as repos:
                    self._set_db_connected(True)
                    while not self._stop_event.is_set():
                        self._cycle(repos)
                        self._stop_event.wait(NAUTILUS_POLL_SECONDS)
            except (InterfaceError, OperationalError) as exc:
                self._set_db_connected(False)
                logger.warning("Nautilus database unavailable; retrying ({})", type(exc).__name__)
                self._stop_event.wait(NAUTILUS_POLL_SECONDS)
            except BaseException as exc:
                with self._lock:
                    self._error = exc
                    self._db_connected = False
                logger.exception("Nautilus database bridge failed")
                return
        self._set_db_connected(False)

    def _cycle(self, repos: Any) -> None:
        projection_blocked = False
        if self._pending_event is not None:
            if self._handle_event(repos, self._pending_event):
                self._pending_event = None
            else:
                projection_blocked = True
        if not projection_blocked:
            for _ in range(self._queues.events.maxsize):
                try:
                    event = self._queues.events.get_nowait()
                except Empty:
                    break
                self._pending_event = event
                if not self._handle_event(repos, event):
                    projection_blocked = True
                    break
                self._pending_event = None

        now_ms = self._now_ms()
        command: AdoptIntent | IntentReleased | None = None
        with repos.transaction():
            runtime = repos.trading.nautilus_runtime_state(for_update=True)
            if runtime is None or runtime.get("active_capability_snapshot_sha256") != self._capability_snapshot_sha256:
                raise RuntimeError("nautilus_capability_snapshot_changed")
            active = repos.trading.active_intent()
            if active is None:
                self._dispatched_intent_id = None
            else:
                intent, outcome = active
                if outcome.execution_state == "PENDING" and intent.valid_until_ms <= now_ms:
                    expired = repos.trading.expire_unfenced_intent(intent.intent_id, now_ms=now_ms)
                    with self._lock:
                        self._expiry_projection_healthy = expired is not None
                    if expired is not None:
                        command = IntentReleased(intent_id=intent.intent_id)
                    self._dispatched_intent_id = None
                elif self._should_dispatch(runtime, outcome) and self._dispatched_intent_id != intent.intent_id:
                    command = AdoptIntent(intent=intent, outcome=outcome)
                    self._dispatched_intent_id = intent.intent_id
            repos.trading.set_nautilus_runtime(
                heartbeat_at_ms=now_ms,
                ready=self._engine_ready and self._projection_healthy,
                readiness_reason=(
                    self._readiness_reason if self._projection_healthy else "execution_projection_rejected"
                ),
                unexpected_exposure=self._unexpected_exposure,
                now_ms=now_ms,
            )
        with self._lock:
            self._heartbeat_at_ms = now_ms
        if command is not None:
            self._queues.commands.put_nowait(command)

    def _should_dispatch(self, runtime: dict[str, Any] | None, outcome: Any) -> bool:
        if outcome.execution_state != "PENDING":
            return True
        return bool(
            runtime is not None
            and runtime.get("control") == "RUNNING"
            and self._engine_ready
            and self._projection_healthy
            and not self._unexpected_exposure
        )

    def _handle_event(self, repos: Any, event: StrategyEvent) -> bool:
        if isinstance(event, ReadinessChanged):
            with self._lock:
                self._engine_ready = event.ready
                self._readiness_reason = event.reason
                self._unexpected_exposure = event.unexpected_exposure
            return True

        if isinstance(event, EntryFenceRequested):
            with repos.transaction():
                outcome = repos.trading.fence_entry(
                    event.intent_id,
                    engine_identity=event.engine_identity,
                    now_ms=self._now_ms(),
                )
            if outcome is None:
                self._dispatched_intent_id = None
                self._queues.commands.put_nowait(IntentReleased(intent_id=event.intent_id))
                return True
            if outcome.execution_state == "TERMINAL":
                self._dispatched_intent_id = None
                self._queues.commands.put_nowait(IntentReleased(intent_id=event.intent_id))
                return True
            self._queues.commands.put_nowait(EntryFenceGranted(outcome=outcome, quantity=event.quantity))
            return True

        if isinstance(event, OrderOutcomeUnknown) and event.intent_id is None:
            with self._lock:
                self._engine_ready = False
                self._readiness_reason = "unattributed_order_outcome_unknown"
                self._unexpected_exposure = True
            return True

        with repos.transaction():
            outcome = self._write_execution_event(repos.trading, event)
            if outcome is None:
                current = repos.trading.intent_outcome(event.intent_id)
                if current is not None and self._event_is_projected(current, event):
                    outcome = current
        with self._lock:
            self._event_projection_healthy = outcome is not None
        return outcome is not None

    def _write_execution_event(self, trading: Any, event: StrategyEvent) -> Any:
        if isinstance(event, IntentRefused):
            if event.reason_code == "intent_expired":
                outcome = trading.expire_unfenced_intent(event.intent_id, now_ms=self._now_ms())
            else:
                outcome = trading.record_rejected_without_exposure(
                    event.intent_id,
                    reason_code=event.reason_code,
                    authoritative_quantity=Decimal(0),
                    entry_client_order_id=None,
                    now_ms=self._now_ms(),
                )
        elif isinstance(event, EntryFilled):
            outcome = trading.record_entry_fill(
                event.intent_id,
                actual_quantity=event.actual_quantity,
                avg_entry_price=event.avg_entry_price,
                position_id=event.position_id,
                opened_at_ms=event.opened_at_ms,
                now_ms=event.opened_at_ms,
            )
        elif isinstance(event, EntryRejected):
            outcome = trading.record_rejected_without_exposure(
                event.intent_id,
                reason_code=event.reason_code,
                authoritative_quantity=Decimal(0),
                entry_client_order_id=event.client_order_id,
                now_ms=event.observed_at_ms,
            )
        elif isinstance(event, StopSubmitted):
            if event.generation == 0:
                outcome = trading.record_stop_submitted(
                    event.intent_id,
                    client_order_id=event.client_order_id,
                    generation=event.generation,
                    previous_client_order_id=event.previous_client_order_id,
                    quantity=event.quantity,
                    now_ms=event.submitted_at_ms,
                )
            else:
                if event.previous_client_order_id is None:
                    raise ValueError("nautilus_replacement_stop_previous_id_missing")
                outcome = trading.prepare_stop_replacement(
                    event.intent_id,
                    canceled_client_order_id=event.previous_client_order_id,
                    submitted_client_order_id=event.client_order_id,
                    generation=event.generation,
                    quantity=event.quantity,
                    now_ms=event.submitted_at_ms,
                )
        elif isinstance(event, StopAccepted):
            outcome = trading.record_protected(
                event.intent_id,
                accepted_client_order_id=event.client_order_id,
                protection_order_id=event.venue_order_id,
                protected_quantity=event.quantity,
                stop_price=event.trigger_price,
                protected_at_ms=event.accepted_at_ms,
                now_ms=event.accepted_at_ms,
            )
        elif isinstance(event, PositionQuantityChanged):
            outcome = trading.record_position_changed(
                event.intent_id,
                position_id=event.position_id,
                actual_quantity=event.actual_quantity,
                avg_entry_price=event.avg_entry_price,
                now_ms=event.changed_at_ms,
            )
        elif isinstance(event, CloseSubmitted):
            outcome = trading.record_close_submitted(
                event.intent_id,
                client_order_id=event.client_order_id,
                position_id=event.position_id,
                quantity=event.quantity,
                submitted_at_ms=event.submitted_at_ms,
                now_ms=event.submitted_at_ms,
            )
        elif isinstance(event, PositionClosedObserved):
            outcome = trading.record_position_closed_observed(
                event.intent_id,
                instrument_id=event.instrument_id,
                account_id=event.account_id,
                position_id=event.position_id,
                closing_client_order_id=event.closing_client_order_id,
                local_quantity=event.local_quantity,
                avg_exit_price=event.avg_exit_price,
                closed_at_ms=event.closed_at_ms,
                realized_pnl_amount=event.realized_pnl_amount,
                realized_pnl_currency=event.realized_pnl_currency,
                commissions_by_currency=event.commissions_by_currency,
                now_ms=event.closed_at_ms,
            )
        elif isinstance(event, PositionFlatConfirmed):
            outcome = trading.record_closed_flat(
                event.intent_id,
                position_id=event.position_id,
                authoritative_quantity=event.authoritative_quantity,
                avg_exit_price=event.avg_exit_price,
                closed_at_ms=event.closed_at_ms,
                flat_verified_at_ms=event.flat_verified_at_ms,
                realized_pnl_amount=event.realized_pnl_amount,
                realized_pnl_currency=event.realized_pnl_currency,
                commissions_by_currency=event.commissions_by_currency,
                now_ms=event.flat_verified_at_ms,
            )
        elif isinstance(event, OrderOutcomeUnknown):
            if event.leg is None or event.intent_id is None:
                raise RuntimeError("nautilus_unknown_outcome_identity_missing")
            reason = {
                "entry": "entry_outcome_unknown",
                "stop": "protection_unproven",
                "close": "close_outcome_unknown",
            }[event.leg]
            outcome = trading.mark_manual_review(
                event.intent_id,
                reason_code=reason,
                now_ms=event.observed_at_ms,
            )
        else:
            raise TypeError(f"unsupported_nautilus_event:{type(event).__name__}")
        return outcome

    @staticmethod
    def _event_is_projected(outcome: Any, event: StrategyEvent) -> bool:
        """Resolve an ambiguous commit without accepting a different transition."""

        if isinstance(event, IntentRefused):
            terminal = "EXPIRED" if event.reason_code == "intent_expired" else "REJECTED"
            return bool(outcome.terminal_outcome == terminal and outcome.reason_code == event.reason_code)
        if isinstance(event, EntryFilled):
            return bool(
                outcome.actual_quantity == event.actual_quantity
                and outcome.avg_entry_price == event.avg_entry_price
                and outcome.position_id == event.position_id
                and outcome.opened_at_ms == event.opened_at_ms
            )
        if isinstance(event, EntryRejected):
            return bool(
                outcome.terminal_outcome == "REJECTED"
                and outcome.reason_code == event.reason_code
                and outcome.entry_client_order_id == event.client_order_id
            )
        if isinstance(event, StopSubmitted):
            return bool(
                outcome.stop_client_order_id == event.client_order_id
                and outcome.stop_generation == event.generation
                and outcome.stop_submitted_at_ms == event.submitted_at_ms
                and outcome.actual_quantity == event.quantity
            )
        if isinstance(event, StopAccepted):
            return bool(
                outcome.stop_client_order_id == event.client_order_id
                and outcome.protection_order_id == event.venue_order_id
                and outcome.protected_quantity == event.quantity
                and outcome.stop_price == event.trigger_price
                and outcome.protected_at_ms == event.accepted_at_ms
            )
        if isinstance(event, PositionQuantityChanged):
            return bool(
                outcome.position_id == event.position_id
                and outcome.actual_quantity == event.actual_quantity
                and outcome.avg_entry_price == event.avg_entry_price
            )
        if isinstance(event, CloseSubmitted):
            return bool(
                outcome.close_client_order_id == event.client_order_id
                and outcome.close_submitted_at_ms == event.submitted_at_ms
                and outcome.position_id == event.position_id
                and outcome.actual_quantity == event.quantity
            )
        if isinstance(event, PositionClosedObserved):
            return bool(
                outcome.execution_phase == "EXIT"
                and outcome.position_id == event.position_id
                and event.closing_client_order_id in {outcome.stop_client_order_id, outcome.close_client_order_id}
                and outcome.avg_exit_price == event.avg_exit_price
                and outcome.closed_at_ms == event.closed_at_ms
                and outcome.realized_pnl_amount == event.realized_pnl_amount
                and outcome.realized_pnl_currency == event.realized_pnl_currency
                and (
                    event.commissions_by_currency is None
                    or outcome.commissions_by_currency == event.commissions_by_currency
                )
            )
        if isinstance(event, PositionFlatConfirmed):
            return bool(
                outcome.execution_state == "TERMINAL"
                and outcome.terminal_outcome == "CLOSED_FLAT"
                and outcome.position_id == event.position_id
                and outcome.avg_exit_price == event.avg_exit_price
                and outcome.closed_at_ms == event.closed_at_ms
                and outcome.flat_verified_at_ms == event.flat_verified_at_ms
                and outcome.realized_pnl_amount == event.realized_pnl_amount
                and outcome.realized_pnl_currency == event.realized_pnl_currency
                and (
                    event.commissions_by_currency is None
                    or outcome.commissions_by_currency == event.commissions_by_currency
                )
            )
        if isinstance(event, OrderOutcomeUnknown):
            reason = {
                "entry": "entry_outcome_unknown",
                "stop": "protection_unproven",
                "close": "close_outcome_unknown",
            }[event.leg]
            return bool(outcome.execution_state == "MANUAL_REVIEW" and outcome.reason_code == reason)
        return False

    def _set_db_connected(self, value: bool) -> None:
        with self._lock:
            self._db_connected = value

    @property
    def _projection_healthy(self) -> bool:
        return self._event_projection_healthy and self._expiry_projection_healthy


__all__ = ["NAUTILUS_POLL_SECONDS", "NautilusDatabaseBridge"]
