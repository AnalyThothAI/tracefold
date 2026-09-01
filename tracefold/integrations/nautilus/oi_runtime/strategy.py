"""Thin Nautilus lifecycle and callback router for the OI Runtime."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any, Literal

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.identifiers import ClientId, PositionId
from nautilus_trader.trading.strategy import Strategy

from tracefold.trading import ExecutionAccountSnapshot, OperatorIntentV1

from .account_projection import RuntimeAccountProjector
from .audit_sink import AuditSink
from .config import OiRuntimeProfile
from .entry import EntryCoordinator
from .exit import ExitCoordinator
from .observations import AuditBackpressure, RuntimeObservationWriter
from .protection import ProtectionCoordinator
from .recovery import RecoveryCoordinator
from .risk import DayStartBaseline
from .signal_client import ExecutionSignalClient
from .state import (
    ExecutionState,
    PrivateReconciliationReason,
    RuntimeControlSnapshot,
    RuntimeEntryRequest,
    RuntimeExecutionState,
    RuntimeReadiness,
    RuntimeReadinessSnapshot,
    RuntimeReconciliationSnapshot,
    order_for_event,
)

_CALLBACK_BATCH = 16
_PUMP_INTERVAL_MS = 100
_AMBIGUOUS_REASONS = ("-1007", "503", "timeout", "timed out", "response unknown")


def oi_strategy_config(profile: OiRuntimeProfile) -> StrategyConfig:
    claims = sorted((route.instrument_id for route in profile.routes), key=lambda item: item.value)
    tag = hashlib.sha256(profile.profile_id.encode()).hexdigest()[:3].upper()
    return StrategyConfig(
        strategy_id="OI-RUNTIME",
        order_id_tag=tag,
        oms_type="NETTING",
        external_order_claims=claims,
    )


class OiNautilusStrategy(Strategy):
    """Route callbacks to concrete owners; never synchronously call PostgreSQL."""

    def __init__(
        self,
        *,
        profile: OiRuntimeProfile,
        signals: ExecutionSignalClient,
        audit: AuditSink,
        readiness: RuntimeReadiness,
        singleton_ready: Any,
        control_plane_ready: Callable[[], bool],
        day_start: DayStartBaseline | None,
        request_reconciliation: Callable[[PrivateReconciliationReason], None],
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
        self._control_plane_ready = control_plane_ready
        self._day_start = day_start
        self._day_start_lock = Lock()
        self._request_reconciliation = request_reconciliation
        factory = audit.factory
        if (
            factory.runtime_profile_id != profile.profile_id
            or factory.runtime_release != profile.runtime_release
            or factory.execution_strategy != "oi_nautilus_v1"
        ):
            raise ValueError("oi_runtime_audit_identity_invalid")
        self._runtime = RuntimeExecutionState.from_control_snapshot(initial_control_state)
        self._account_projector = RuntimeAccountProjector(engine=self, profile=profile, state=self._runtime)
        self._observation_writer = RuntimeObservationWriter(
            audit=audit,
            signals=signals,
            state=self._runtime,
            timestamp_ns=lambda: int(self.clock.timestamp_ns()),
        )
        self._exits = ExitCoordinator(
            engine=self,
            profile=profile,
            state=self._runtime,
            observations=self._observation_writer,
            request_reconciliation=request_reconciliation,
            halt_for_unexpected_exposure=readiness.halt_for_unexpected_exposure,
        )
        self._protection = ProtectionCoordinator(
            engine=self,
            profile=profile,
            state=self._runtime,
            readiness=readiness,
            observations=self._observation_writer,
            exits=self._exits,
            request_reconciliation=request_reconciliation,
        )
        self._recovery = RecoveryCoordinator(
            engine=self,
            profile=profile,
            state=self._runtime,
            readiness=readiness,
            protection=self._protection,
            exits=self._exits,
            request_reconciliation=request_reconciliation,
        )
        self._entry = EntryCoordinator(
            engine=self,
            profile=profile,
            state=self._runtime,
            readiness=readiness,
            observations=self._observation_writer,
            current_day_start=self._current_day_start,
            readiness_snapshot=self.readiness,
            verify_owned_exposure=self._recovery.verify_owned_exposure,
            request_reconciliation=request_reconciliation,
        )

    def on_start(self) -> None:
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
        for _ in range(_CALLBACK_BATCH):
            command = self._signals.next_command_nowait()
            if command is None:
                break
            try:
                self._route_command(command)
            except AuditBackpressure:
                break
        self._exits.advance_pending()
        if self._signals.queued_command_count == 0 and self._signals.command_scan_complete:
            for _ in range(_CALLBACK_BATCH):
                signal = self._signals.next_nowait()
                if signal is None:
                    break
                try:
                    self._entry.handle_signal(signal)
                except AuditBackpressure:
                    break
        self._entry.query_aged()
        self._exits.retry_failed()
        self._recovery.verify_owned_exposure()

    def readiness(self) -> RuntimeReadinessSnapshot:
        return self._readiness.snapshot(
            singleton_ready=bool(self._singleton_ready()),
            portfolio_ready=bool(self.portfolio.initialized),
            control_plane_ready=bool(self._control_plane_ready()),
            audit_ready=self._audit.can_accept_exposure(),
            day_start_ready=self._current_day_start() is not None,
            entries_paused=self._runtime.entries_paused,
            emergency_halted=self._runtime.emergency_halted,
        )

    def control_state(self) -> RuntimeControlSnapshot:
        return self._runtime.control_snapshot()

    def account_snapshot(self, *, projected_at_ns: int) -> ExecutionAccountSnapshot:
        return self._account_projector.snapshot(
            baseline=self._current_day_start(),
            projected_at_ns=projected_at_ns,
        )

    def protection_status(
        self,
        *,
        positions_count: int,
        unexpected_exposure: bool,
    ) -> Literal["not_applicable", "protected", "pending", "unprotected", "unknown"]:
        return self._protection.status(
            positions_count=positions_count,
            unexpected_exposure=unexpected_exposure,
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

    def reconcile_runtime(self, snapshot: RuntimeReconciliationSnapshot) -> bool:
        return self._recovery.reconcile(snapshot)

    def flatten_position(self, position_id: PositionId) -> None:
        self._exits.flatten(position_id)

    def _route_command(self, command: OperatorIntentV1) -> None:
        now_ns = int(self.clock.timestamp_ns())
        if (
            command.command_id in self._runtime.disposed_command_ids
            or command.command_id in self._runtime.pending_flatten
        ):
            return
        if command.target_profile_id != self._profile.profile_id:
            self._observation_writer.dispose_command(command, "rejected", "profile_mismatch")
            return
        if command.expires_at_ns <= now_ns:
            self._observation_writer.dispose_command(command, "rejected", "expired")
            return
        if command.action == "pause_entries":
            self._runtime.entries_paused = True
            self._observation_writer.dispose_command(command, "accepted", "entries_paused")
            return
        if command.action == "resume_entries":
            if self._runtime.emergency_halted:
                self._observation_writer.dispose_command(command, "rejected", "emergency_halt_sticky")
                return
            self._runtime.entries_paused = False
            self._observation_writer.dispose_command(command, "accepted", "entries_resumed")
            return
        if command.action == "emergency_halt":
            self._runtime.entries_paused = True
            self._runtime.emergency_halted = True
            self._observation_writer.dispose_command(command, "accepted", "emergency_halted")
            return
        if command.action == "manual_entry":
            self._entry.handle(RuntimeEntryRequest.from_manual_command(command))
            return
        if command.action != "flatten" or command.scope != "account":
            self._observation_writer.dispose_command(command, "rejected", "flatten_scope_unsupported")
            return
        self._exits.start_flatten(command)

    def on_position_opened(self, event: Any) -> None:
        self._protection.position_opened(event)

    def on_position_changed(self, event: Any) -> None:
        self._protection.position_changed(event)

    def on_position_closed(self, event: Any) -> None:
        self._protection.position_closed(event)

    def on_order_canceled(self, event: Any) -> None:
        routed = self._runtime.state_for_order(event.client_order_id)
        if routed is None:
            return
        state, leg = routed
        self._observation_writer.native_order_event(state, leg, "canceled", event)
        self._route_known_terminal(state, event.client_order_id, leg)

    def on_order_accepted(self, event: Any) -> None:
        routed = self._runtime.state_for_order(event.client_order_id)
        if routed is None:
            return
        state, leg = routed
        if leg == "entry":
            self._entry.accepted(state)
        elif leg == "protection":
            self._protection.accept_pending(state, event.client_order_id)
        self._observation_writer.native_order_event(state, leg, "accepted", event)

    def on_order_filled(self, event: Any) -> None:
        routed = self._runtime.state_for_order(event.client_order_id)
        if routed is None:
            return
        state, leg = routed
        if leg == "entry":
            self._entry.accepted(state)
        self._observation_writer.fill(state, leg, event)

    def on_order_rejected(self, event: Any) -> None:
        self._route_rejected(event, "rejected")

    def on_order_denied(self, event: Any) -> None:
        self._route_rejected(event, "denied")

    def on_order_expired(self, event: Any) -> None:
        self._route_rejected(event, "expired")

    def _route_rejected(self, event: Any, status: str) -> None:
        routed = self._runtime.state_for_order(event.client_order_id)
        if routed is None:
            return
        state, leg = routed
        reason = str(getattr(event, "reason", "")).lower()
        order = order_for_event(state, event.client_order_id, leg)
        if status == "rejected" and order is not None and any(token in reason for token in _AMBIGUOUS_REASONS):
            self._request_reconciliation("protection_ambiguity" if leg == "protection" else "unknown_outcome")
            if leg == "entry":
                self._entry.mark_unknown(state)
            self.query_order(order, client_id=ClientId("BINANCE"))
            self._observation_writer.order(state, order, leg, "unknown_query_first")
            if (
                leg == "protection"
                and event.client_order_id not in state.retiring_stop_orders
                and state.position_quantity > 0
                and state.position_id is not None
            ):
                self._exits.flatten(state.position_id)
            return
        self._route_known_terminal(state, event.client_order_id, leg)
        self._observation_writer.rejected_order_event(state, leg, status, event)

    def _route_known_terminal(self, state: ExecutionState, client_order_id: Any, leg: str) -> None:
        if leg == "entry":
            self._entry.known_terminal(state)
        elif leg == "exit":
            self._exits.known_terminal(state, client_order_id)
        elif leg == "protection":
            self._protection.known_terminal(state, client_order_id)


__all__ = ["OiNautilusStrategy", "oi_strategy_config"]
