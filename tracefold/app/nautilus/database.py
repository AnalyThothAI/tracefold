"""One PostgreSQL thread bridging durable intents to the Nautilus thread."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from decimal import Decimal
from queue import Empty
from threading import Event, Lock, Thread
from typing import Any, cast
from uuid import uuid4

from loguru import logger
from psycopg import InterfaceError, OperationalError

from tracefold.app.repository_session import repositories
from tracefold.integrations.nautilus.config import (
    NAUTILUS_RELEASE,
    installed_nautilus_wheel_identity,
)
from tracefold.integrations.nautilus.messages import (
    AdoptIntent,
    BootstrapAccountZeroChanged,
    CloseSubmitted,
    EntryAccepted,
    EntryFenceGranted,
    EntryFenceRequested,
    EntryFilled,
    EntryNoSubmitFinalized,
    EntryNoSubmitRequested,
    EntryPreflightRejected,
    EntryRejected,
    EntrySubmissionGranted,
    EntrySubmissionRequested,
    EntrySubmitted,
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
from tracefold.platform.runtime_identity import runtime_identity
from tracefold.trading import (
    ActiveIntentValues,
    ExecutionBindingV1,
    ExecutionQuoteSnapshotV1,
    NautilusRuntimeStartV1,
    VenueBinding,
    materialize_active_intent,
    materialize_entry_fence,
    materialize_intent_outcome,
    validate_close_submission_identity,
    validate_stop_submission_identity,
)

_RepositoryFactory = Callable[..., AbstractContextManager[Any]]
NAUTILUS_POLL_SECONDS = 1.0


class NautilusDatabaseBridge:
    """Own the sole DB connection and the two bounded thread queues."""

    def __init__(
        self,
        settings: Settings,
        queues: StrategyQueues,
        *,
        capability_snapshot_sha256s: Mapping[VenueBinding, str | None],
        pending_execution_bindings: Mapping[VenueBinding, ExecutionBindingV1] | None = None,
        repository_factory: _RepositoryFactory = repositories,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._settings = settings
        self._queues = queues
        self._capability_snapshot_sha256s = dict(capability_snapshot_sha256s)
        self._pending_execution_bindings = dict(pending_execution_bindings or {})
        self._repository_factory = repository_factory
        self._now_ms = now_ms or (lambda: int(time.time() * 1_000))
        identity = runtime_identity()
        self._runtime_start = NautilusRuntimeStartV1(
            runtime_id=uuid4(),
            runtime_revision=identity.runtime_revision,
            image_digest=identity.image_digest,
            nautilus_version=NAUTILUS_RELEASE.version,
            nautilus_source_git_commit=NAUTILUS_RELEASE.git_commit,
            nautilus_wheel_identity=installed_nautilus_wheel_identity(),
            started_at_ms=self._now_ms(),
        )
        self._runtime_start_recorded = False
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
        if not self._runtime_start_recorded:
            with repos.transaction():
                repos.trading.append_nautilus_runtime_start(self._runtime_start)
            self._runtime_start_recorded = True
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
        adopt_values: ActiveIntentValues | None = None
        released_intent_id: str | None = None
        with repos.transaction():
            runtimes: dict[VenueBinding, dict[str, Any]] = {}
            for binding, capability_sha256 in self._capability_snapshot_sha256s.items():
                runtime = repos.trading.binding_execution_runtime(binding=binding, for_update=True)
                if runtime is None or runtime.get("capability_snapshot_sha256") != capability_sha256:
                    raise RuntimeError(f"nautilus_capability_snapshot_changed:{binding}")
                runtimes[binding] = runtime
            active_values = repos.trading.active_intent_values()
            if active_values is None:
                self._dispatched_intent_id = None
            else:
                intent_values, outcome_values = active_values
                intent_id = str(intent_values["intent_id"])
                execution_state = str(outcome_values["execution_state"])
                if execution_state == "PENDING" and int(intent_values["valid_until_ms"]) <= now_ms:
                    expired_values = repos.trading.expire_unfenced_intent_values(intent_id, now_ms=now_ms)
                    with self._lock:
                        self._expiry_projection_healthy = expired_values is not None
                    if expired_values is not None:
                        released_intent_id = intent_id
                    self._dispatched_intent_id = None
                elif (
                    self._should_dispatch(runtimes.get(intent_values["binding"]), execution_state)
                    and self._dispatched_intent_id != intent_id
                ):
                    if execution_state == "PENDING":
                        adopted_values = repos.trading.mark_intent_adopted_values(intent_id, now_ms=now_ms)
                        if adopted_values is None:
                            raise RuntimeError("nautilus_intent_adoption_projection_failed")
                        outcome_values = adopted_values
                    adopt_values = intent_values, outcome_values
                    self._dispatched_intent_id = intent_id
            for binding, capability_sha256 in self._capability_snapshot_sha256s.items():
                repos.trading.set_binding_execution_runtime(
                    binding=binding,
                    expected_capability_snapshot_sha256=capability_sha256,
                    heartbeat_at_ms=now_ms,
                    ready=self._engine_ready and self._projection_healthy,
                    readiness_reason=(
                        self._readiness_reason if self._projection_healthy else "execution_projection_rejected"
                    ),
                    unexpected_exposure=self._unexpected_exposure,
                    now_ms=now_ms,
                )
            if self._engine_ready and self._projection_healthy:
                for binding, execution_binding in tuple(self._pending_execution_bindings.items()):
                    if not repos.trading.append_and_activate_execution_binding(execution_binding):
                        raise RuntimeError(f"nautilus_execution_binding_activation_failed:{binding}")
                    del self._pending_execution_bindings[binding]
        with self._lock:
            self._heartbeat_at_ms = now_ms
        command: AdoptIntent | IntentReleased | None = None
        if adopt_values is not None:
            intent, outcome = materialize_active_intent(adopt_values)
            command = AdoptIntent(intent=intent, outcome=outcome)
        elif released_intent_id is not None:
            command = IntentReleased(intent_id=released_intent_id)
        if command is not None:
            self._queues.commands.put_nowait(command)

    def _should_dispatch(self, runtime: dict[str, Any] | None, execution_state: str) -> bool:
        if execution_state != "PENDING":
            return True
        return bool(
            runtime is not None
            and runtime.get("control") == "RUNNING"
            and self._engine_ready
            and self._projection_healthy
            and not self._unexpected_exposure
        )

    def _handle_event(self, repos: Any, event: StrategyEvent) -> bool:
        if isinstance(event, BootstrapAccountZeroChanged):
            with repos.transaction():
                projected = []
                for binding, capability_sha256 in self._capability_snapshot_sha256s.items():
                    updated = bool(
                        repos.trading.set_binding_account_reconciliation(
                            binding=binding,
                            verified_at_ms=event.verified_at_ms,
                            now_ms=event.observed_at_ms,
                            expected_capability_snapshot_sha256=capability_sha256,
                        )
                    )
                    projected.append(updated)
                    if updated and event.verified_at_ms is not None and capability_sha256 is None:
                        repos.trading.activate_latest_bootstrap_capability(
                            binding=binding,
                            now_ms=event.observed_at_ms,
                        )
                return all(projected)

        if isinstance(event, ReadinessChanged):
            with self._lock:
                self._engine_ready = event.ready
                self._readiness_reason = event.reason
                self._unexpected_exposure = event.unexpected_exposure
            return True

        if isinstance(event, EntryFenceRequested):
            now_ms = self._now_ms()
            with repos.transaction():
                blacklist_state = repos.trading.blacklist_snapshot_rows(now_ms=now_ms, materialize_expiry=True)
            prepared = repos.trading.prepare_entry_fence(
                event.intent_id,
                submission_quantity=event.quantity,
                q1_evidence=event.q1_evidence,
                blacklist_state=blacklist_state,
                requested_at_ms=event.requested_at_ms,
                now_ms=now_ms,
            )
            with repos.transaction():
                written = repos.trading.fence_entry(
                    prepared,
                    engine_identity=event.engine_identity,
                    submission_quantity=event.quantity,
                    requested_at_ms=event.requested_at_ms,
                    now_ms=now_ms,
                )
            fence = materialize_entry_fence(written)
            # Three dispositions, three different facts (#331). `GRANTED` is the only one that may
            # precede a provider entry, and it is committed before this line runs. `REFUSED` wrote a
            # terminal rejection at zero exposure; `UNAVAILABLE` wrote nothing at all and names why —
            # a stale dispatch, an unready runtime, or an expired TTL.
            # The old `None` carried every one of those, so an engine held back by readiness looked
            # exactly like one with nothing to do.
            if not fence.granted or fence.outcome is None:
                if fence.disposition == "UNAVAILABLE":
                    logger.info("entry fence unavailable intent_id={} reason={}", event.intent_id, fence.reason)
                self._dispatched_intent_id = None
                self._queues.commands.put_nowait(IntentReleased(intent_id=event.intent_id))
                return True
            self._queues.commands.put_nowait(EntryFenceGranted(outcome=fence.outcome))
            return True

        if isinstance(event, EntrySubmissionRequested):
            q2_payload_json = repos.trading.prepare_quote_audit_json(
                event.intent_id,
                event.q2_evidence,
                stage="Q2",
                accepted=True,
            )
            if q2_payload_json is None:
                raise RuntimeError("entry_submission_authority_projection_failed")
            with repos.transaction():
                outcome_values = repos.trading.authorize_entry_submission_values(
                    event.intent_id,
                    entry_client_order_id=event.client_order_id,
                    q2_payload_json=q2_payload_json,
                    now_ms=self._now_ms(),
                )
                if outcome_values is None:
                    outcome_values = repos.trading.intent_outcome_values(event.intent_id)
            outcome = None if outcome_values is None else materialize_intent_outcome(outcome_values)
            if outcome is None or outcome.entry_quote_q2 != event.q2_evidence:
                raise RuntimeError("entry_submission_authority_projection_failed")
            self._queues.commands.put_nowait(EntrySubmissionGranted(outcome=outcome))
            return True

        if isinstance(event, EntryNoSubmitRequested):
            q2_payload_json = repos.trading.prepare_quote_audit_json(
                event.intent_id,
                event.q2_evidence,
                stage="Q2",
                accepted=False,
                reason=event.reason_code,
            )
            if q2_payload_json is None:
                raise RuntimeError("entry_no_submit_projection_failed")
            with repos.transaction():
                outcome_values = repos.trading.record_fenced_quote_no_submit_values(
                    event.intent_id,
                    entry_client_order_id=event.client_order_id,
                    reason_code=event.reason_code,
                    q2_payload_json=q2_payload_json,
                    now_ms=self._now_ms(),
                )
                if outcome_values is None:
                    outcome_values = repos.trading.intent_outcome_values(event.intent_id)
            outcome = None if outcome_values is None else materialize_intent_outcome(outcome_values)
            if outcome is None or outcome.entry_quote_q2 != event.q2_evidence or outcome.terminal_outcome != "REJECTED":
                raise RuntimeError("entry_no_submit_projection_failed")
            self._queues.commands.put_nowait(EntryNoSubmitFinalized(outcome=outcome))
            return True

        if isinstance(event, OrderOutcomeUnknown) and event.intent_id is None:
            with self._lock:
                self._engine_ready = False
                self._readiness_reason = "unattributed_order_outcome_unknown"
                self._unexpected_exposure = True
            return True

        quote_payload_json: str | None = None
        commissions_json: str | None = None
        funding_json: str | None = None
        if isinstance(event, EntryPreflightRejected):
            q1_accepted = isinstance(event.q1_evidence, ExecutionQuoteSnapshotV1)
            quote_payload_json = repos.trading.prepare_quote_audit_json(
                event.intent_id,
                event.q1_evidence,
                stage="Q1",
                accepted=q1_accepted,
                reason=None if q1_accepted else event.reason_code,
            )
        elif isinstance(event, StopSubmitted):
            validate_stop_submission_identity(
                event.intent_id,
                client_order_id=event.client_order_id,
                generation=event.generation,
                previous_client_order_id=event.previous_client_order_id,
            )
        elif isinstance(event, CloseSubmitted):
            validate_close_submission_identity(event.intent_id, client_order_id=event.client_order_id)
        elif isinstance(event, PositionClosedObserved | PositionFlatConfirmed):
            if isinstance(event, PositionClosedObserved) and (not event.instrument_id or not event.account_id):
                raise ValueError("close_observation_scope_invalid")
            commissions_json = repos.trading.prepare_currency_amounts_json(event.commissions_by_currency)
            funding_json = repos.trading.prepare_currency_amounts_json(event.funding_by_currency)
        with repos.transaction():
            outcome_values = self._write_execution_event_values(
                repos.trading,
                event,
                quote_payload_json=quote_payload_json,
                commissions_json=commissions_json,
                funding_json=funding_json,
            )
            wrote = outcome_values is not None
            if outcome_values is None:
                outcome_values = repos.trading.intent_outcome_values(event.intent_id)
        outcome = None if outcome_values is None else materialize_intent_outcome(outcome_values)
        if not wrote and outcome is not None and not self._event_is_projected(outcome, event):
            outcome = None
        with self._lock:
            self._event_projection_healthy = outcome is not None
        return outcome is not None

    def _write_execution_event_values(
        self,
        trading: Any,
        event: StrategyEvent,
        *,
        quote_payload_json: str | None,
        commissions_json: str | None,
        funding_json: str | None,
    ) -> dict[str, Any] | None:
        if isinstance(event, IntentRefused):
            if event.reason_code == "intent_expired":
                outcome = trading.expire_unfenced_intent_values(event.intent_id, now_ms=self._now_ms())
            else:
                outcome = trading.record_rejected_without_exposure_values(
                    event.intent_id,
                    reason_code=event.reason_code,
                    authoritative_quantity=Decimal(0),
                    entry_client_order_id=None,
                    now_ms=self._now_ms(),
                )
        elif isinstance(event, EntryPreflightRejected):
            if quote_payload_json is None:
                return None
            outcome = trading.record_entry_preflight_no_submit_values(
                event.intent_id,
                reason_code=event.reason_code,
                q1_payload_json=quote_payload_json,
                now_ms=self._now_ms(),
            )
        elif isinstance(event, EntrySubmitted):
            outcome = trading.record_entry_submitted_values(
                event.intent_id,
                entry_client_order_id=event.client_order_id,
                submitted_at_ms=event.submitted_at_ms,
            )
        elif isinstance(event, EntryAccepted):
            outcome = trading.record_entry_accepted_values(
                event.intent_id,
                entry_client_order_id=event.client_order_id,
                accepted_at_ms=event.accepted_at_ms,
            )
        elif isinstance(event, EntryFilled):
            outcome = trading.record_entry_fill_values(
                event.intent_id,
                actual_quantity=event.actual_quantity,
                avg_entry_price=event.avg_entry_price,
                position_id=event.position_id,
                opened_at_ms=event.opened_at_ms,
                now_ms=event.opened_at_ms,
            )
        elif isinstance(event, EntryRejected):
            outcome = trading.record_rejected_without_exposure_values(
                event.intent_id,
                reason_code=event.reason_code,
                authoritative_quantity=Decimal(0),
                entry_client_order_id=event.client_order_id,
                now_ms=event.observed_at_ms,
            )
        elif isinstance(event, StopSubmitted):
            if event.generation == 0:
                outcome = trading.record_stop_submitted_values(
                    event.intent_id,
                    client_order_id=event.client_order_id,
                    generation=event.generation,
                    quantity=event.quantity,
                    now_ms=event.submitted_at_ms,
                )
            else:
                if event.previous_client_order_id is None:
                    raise ValueError("nautilus_replacement_stop_previous_id_missing")
                outcome = trading.prepare_stop_replacement_values(
                    event.intent_id,
                    canceled_client_order_id=event.previous_client_order_id,
                    submitted_client_order_id=event.client_order_id,
                    generation=event.generation,
                    quantity=event.quantity,
                    now_ms=event.submitted_at_ms,
                )
        elif isinstance(event, StopAccepted):
            outcome = trading.record_protected_values(
                event.intent_id,
                accepted_client_order_id=event.client_order_id,
                protection_order_id=event.venue_order_id,
                protected_quantity=event.quantity,
                stop_price=event.trigger_price,
                protected_at_ms=event.accepted_at_ms,
                now_ms=event.accepted_at_ms,
            )
        elif isinstance(event, PositionQuantityChanged):
            outcome = trading.record_position_changed_values(
                event.intent_id,
                position_id=event.position_id,
                actual_quantity=event.actual_quantity,
                avg_entry_price=event.avg_entry_price,
                now_ms=event.changed_at_ms,
            )
        elif isinstance(event, CloseSubmitted):
            outcome = trading.record_close_submitted_values(
                event.intent_id,
                client_order_id=event.client_order_id,
                position_id=event.position_id,
                quantity=event.quantity,
                submitted_at_ms=event.submitted_at_ms,
                now_ms=event.submitted_at_ms,
            )
        elif isinstance(event, PositionClosedObserved):
            outcome = trading.record_position_closed_observed_values(
                event.intent_id,
                instrument_id=event.instrument_id,
                position_id=event.position_id,
                closing_client_order_id=event.closing_client_order_id,
                local_quantity=event.local_quantity,
                avg_exit_price=event.avg_exit_price,
                closed_at_ms=event.closed_at_ms,
                realized_pnl_amount=event.realized_pnl_amount,
                realized_pnl_currency=event.realized_pnl_currency,
                commissions_json=commissions_json,
                now_ms=event.closed_at_ms,
                funding_json=funding_json,
            )
        elif isinstance(event, PositionFlatConfirmed):
            outcome = trading.record_closed_flat_values(
                event.intent_id,
                position_id=event.position_id,
                authoritative_quantity=event.authoritative_quantity,
                avg_exit_price=event.avg_exit_price,
                closed_at_ms=event.closed_at_ms,
                flat_verified_at_ms=event.flat_verified_at_ms,
                realized_pnl_amount=event.realized_pnl_amount,
                realized_pnl_currency=event.realized_pnl_currency,
                commissions_json=commissions_json,
                now_ms=event.flat_verified_at_ms,
                funding_json=funding_json,
            )
        elif isinstance(event, OrderOutcomeUnknown):
            if event.leg is None or event.intent_id is None:
                raise RuntimeError("nautilus_unknown_outcome_identity_missing")
            reason = {
                "entry": "entry_outcome_unknown",
                "stop": "protection_unproven",
                "close": "close_outcome_unknown",
            }[event.leg]
            outcome = trading.mark_manual_review_values(
                event.intent_id,
                reason_code=reason,
                now_ms=event.observed_at_ms,
            )
        else:
            raise TypeError(f"unsupported_nautilus_event:{type(event).__name__}")
        return cast(dict[str, Any] | None, outcome)

    @staticmethod
    def _event_is_projected(outcome: Any, event: StrategyEvent) -> bool:
        """Resolve an ambiguous commit without accepting a different transition."""

        if isinstance(event, IntentRefused):
            terminal = "EXPIRED" if event.reason_code == "intent_expired" else "REJECTED"
            return bool(outcome.terminal_outcome == terminal and outcome.reason_code == event.reason_code)
        if isinstance(event, EntryPreflightRejected):
            return bool(
                outcome.terminal_outcome == "REJECTED"
                and outcome.reason_code == event.reason_code
                and outcome.entry_quote_q1 == event.q1_evidence
            )
        if isinstance(event, EntrySubmitted):
            return bool(
                outcome.entry_client_order_id == event.client_order_id
                and outcome.entry_submitted_at_ms == event.submitted_at_ms
            )
        if isinstance(event, EntryAccepted):
            return bool(
                outcome.entry_client_order_id == event.client_order_id
                and outcome.entry_accepted_at_ms == event.accepted_at_ms
            )
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
                and (event.funding_by_currency is None or outcome.funding_by_currency == event.funding_by_currency)
            )
        if isinstance(event, PositionFlatConfirmed):
            return bool(
                outcome.execution_state in {"TERMINAL", "MANUAL_REVIEW"}
                and (
                    (outcome.execution_state == "TERMINAL" and outcome.terminal_outcome == "CLOSED_FLAT")
                    or (outcome.execution_state == "MANUAL_REVIEW" and outcome.reason_code == "settlement_unproven")
                )
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
                and (event.funding_by_currency is None or outcome.funding_by_currency == event.funding_by_currency)
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
