"""The DB thread is the durable inbox and projection for the one Nautilus strategy."""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from psycopg import OperationalError

from tests.trading_v3_fixtures import trade_intent
from tracefold.app.nautilus.database import NautilusDatabaseBridge as _NautilusDatabaseBridge
from tracefold.integrations.nautilus.messages import (
    AdoptIntent,
    BootstrapAccountZeroChanged,
    CloseSubmitted,
    EntryFenceGranted,
    EntryFenceRequested,
    EntryNoSubmitFinalized,
    EntryNoSubmitRequested,
    EntrySubmissionGranted,
    EntrySubmissionRequested,
    IntentReleased,
    OrderOutcomeUnknown,
    PositionClosedObserved,
    PositionFlatConfirmed,
    PositionQuantityChanged,
    ReadinessChanged,
    StopAccepted,
    StopSubmitted,
    strategy_queues,
)
from tracefold.trading import IntentOutcome, TradeIntent, deterministic_client_order_id
from tracefold.trading.quote_authority import ExecutionQuoteRejectionV1, ExecutionQuoteSnapshotV1
from tracefold.trading.storage.intents import EntryFence

NOW_MS = 1_900_000_000_000


class NautilusDatabaseBridge(_NautilusDatabaseBridge):
    """Test constructor with the one binding exercised by this module made explicit."""

    def __init__(self, settings: Any, queues: Any, **kwargs: Any) -> None:
        capability = kwargs.pop("capability_snapshot_sha256", None)
        pending = kwargs.pop("pending_execution_binding", None)
        super().__init__(
            settings,
            queues,
            capability_snapshot_sha256s={"BINANCE_USDM": capability},
            pending_execution_bindings={} if pending is None else {"BINANCE_USDM": pending},
            **kwargs,
        )


def _settings() -> Any:
    return SimpleNamespace(trading=SimpleNamespace(nautilus=SimpleNamespace()))


def _intent() -> TradeIntent:
    return trade_intent(
        case_id="case-1",
        case_manifest_sha256="1" * 64,
        created_at_ms=NOW_MS,
        reference_price=Decimal("10000"),
        target_notional=Decimal("10"),
    )


def _outcome(intent: TradeIntent, **values: object) -> IntentOutcome:
    payload: dict[str, object] = {
        "intent_id": intent.intent_id,
        "execution_state": "PENDING",
        "commissions_by_currency": {},
        "funding_by_currency": {},
        "updated_at_ms": NOW_MS,
    }
    payload.update(values)
    return IntentOutcome.model_validate(payload)


def _accepted_quote(intent: TradeIntent, *, stage: str = "Q1") -> ExecutionQuoteSnapshotV1:
    return ExecutionQuoteSnapshotV1(
        stage=stage,  # type: ignore[arg-type]
        intent_id=intent.intent_id,
        instrument_id=intent.instrument_id,
        side="buy",
        side_price=Decimal("10000"),
        bid=Decimal("9999"),
        ask=Decimal("10000"),
        ts_event_ns=NOW_MS * 1_000_000,
        ts_init_ns=NOW_MS * 1_000_000,
        evaluated_at_ns=NOW_MS * 1_000_000,
        stream_generation=0,
        receive_age_ns=0,
        event_age_ns=0,
        source_latency_ns=0,
        spread_bps=Decimal("1.00005"),
        reference_drift_bps=Decimal(0),
    )


def _fence_request(intent: TradeIntent, quantity: Decimal = Decimal("0.001")) -> EntryFenceRequested:
    return EntryFenceRequested(
        intent_id=intent.intent_id,
        engine_identity="nt-v1",
        quantity=quantity,
        q1_evidence=_accepted_quote(intent),
        requested_at_ms=NOW_MS,
    )


class _Repositories:
    def __init__(self) -> None:
        self.order: list[str] = []
        self.trading = Mock()
        self.trading.binding_execution_runtime.return_value = {
            "control": "PAUSED",
            "capability_snapshot_sha256": None,
        }
        self.trading.active_intent.return_value = None

    @contextmanager
    def transaction(self):
        self.order.append("begin")
        yield
        self.order.append("commit")


def _ready(bridge: NautilusDatabaseBridge, repos: _Repositories) -> None:
    bridge._handle_event(
        repos,
        ReadinessChanged(ready=True, reason="ready", unexpected_exposure=False),
    )


def test_bootstrap_account_zero_is_projected_without_changing_engine_readiness() -> None:
    queues = strategy_queues()
    bridge = NautilusDatabaseBridge(_settings(), queues, now_ms=lambda: NOW_MS)
    repos = _Repositories()
    repos.trading.set_binding_bootstrap_account_zero.return_value = True

    assert bridge._handle_event(
        repos,
        BootstrapAccountZeroChanged(
            verified_at_ms=NOW_MS - 1,
            observed_at_ms=NOW_MS,
        ),
    )

    repos.trading.set_binding_bootstrap_account_zero.assert_called_once_with(
        binding="BINANCE_USDM",
        verified_at_ms=NOW_MS - 1,
        now_ms=NOW_MS,
        expected_capability_snapshot_sha256=None,
    )
    assert bridge.readiness()["engine_ready"] is False


def test_failed_bootstrap_clears_the_proof_before_projecting_unexpected_exposure() -> None:
    queues = strategy_queues()
    bridge = NautilusDatabaseBridge(
        _settings(),
        queues,
        capability_snapshot_sha256="c" * 64,
        now_ms=lambda: NOW_MS,
    )
    repos = _Repositories()
    repos.trading.set_binding_bootstrap_account_zero.return_value = True

    assert bridge._handle_event(
        repos,
        BootstrapAccountZeroChanged(verified_at_ms=None, observed_at_ms=NOW_MS),
    )
    assert bridge._handle_event(
        repos,
        ReadinessChanged(ready=False, reason="external_exposure", unexpected_exposure=True),
    )

    repos.trading.set_binding_bootstrap_account_zero.assert_called_once_with(
        binding="BINANCE_USDM",
        verified_at_ms=None,
        now_ms=NOW_MS,
        expected_capability_snapshot_sha256="c" * 64,
    )
    assert bridge.readiness()["unexpected_exposure"] is True


def test_pending_intent_is_dispatched_once_only_when_control_and_engine_allow_entry() -> None:
    intent = _intent()
    outcome = _outcome(intent)
    queues = strategy_queues()
    bridge = NautilusDatabaseBridge(_settings(), queues, now_ms=lambda: NOW_MS)
    repos = _Repositories()
    repos.trading.active_intent.return_value = (intent, outcome)
    repos.trading.binding_execution_runtime.return_value = {
        "control": "RUNNING",
        "capability_snapshot_sha256": None,
    }
    repos.trading.mark_intent_adopted.return_value = outcome

    bridge._cycle(repos)
    assert queues.commands.empty()

    _ready(bridge, repos)
    bridge._cycle(repos)
    bridge._cycle(repos)

    assert queues.commands.get_nowait() == AdoptIntent(intent=intent, outcome=outcome)
    assert queues.commands.empty()
    repos.trading.append_nautilus_runtime_start.assert_called_once_with(bridge._runtime_start)
    assert repos.trading.set_binding_execution_runtime.call_count == 3


def test_capability_pointer_change_stops_the_old_engine_before_dispatch() -> None:
    queues = strategy_queues()
    bridge = NautilusDatabaseBridge(
        _settings(),
        queues,
        capability_snapshot_sha256="2" * 64,
        now_ms=lambda: NOW_MS,
    )
    repos = _Repositories()
    repos.trading.binding_execution_runtime.return_value = {
        "control": "PAUSED",
        "capability_snapshot_sha256": "3" * 64,
    }

    with pytest.raises(RuntimeError, match="nautilus_capability_snapshot_changed"):
        bridge._cycle(repos)

    repos.trading.binding_execution_runtime.assert_called_once_with(binding="BINANCE_USDM", for_update=True)
    repos.trading.active_intent.assert_not_called()
    repos.trading.set_binding_execution_runtime.assert_not_called()


def test_entry_fence_is_committed_before_the_strategy_receives_permission() -> None:
    intent = _intent()
    quantity = Decimal("0.001")
    queues = strategy_queues()
    bridge = NautilusDatabaseBridge(_settings(), queues, now_ms=lambda: NOW_MS)
    repos = _Repositories()
    fenced = _outcome(
        intent,
        execution_state="IN_FLIGHT",
        execution_phase="ENTRY",
        engine_identity="nt-v1",
        entry_client_order_id=deterministic_client_order_id(intent.intent_id, "entry"),
        entry_fenced_at_ms=NOW_MS,
        submission_fence_version="submission_fence_v1",
        submission_quantity=quantity,
        entry_quote_q1=_accepted_quote(intent),
    )

    def fence(*_args: object, **_kwargs: object) -> EntryFence:
        repos.order.append("fence")
        return EntryFence(disposition="GRANTED", reason="entry_fence_granted", outcome=fenced)

    repos.trading.fence_entry.side_effect = fence
    repos.trading.intent.return_value = intent

    bridge._handle_event(
        repos,
        _fence_request(intent, quantity),
    )

    assert repos.order == ["begin", "fence", "commit"]
    assert queues.commands.get_nowait() == EntryFenceGranted(outcome=fenced)
    repos.trading.intent.assert_not_called()


@pytest.mark.parametrize(
    "fence",
    (
        EntryFence(disposition="UNAVAILABLE", reason="runtime_not_ready"),
        EntryFence(disposition="UNAVAILABLE", reason="intent_not_claimable"),
        EntryFence(disposition="UNAVAILABLE", reason="intent_not_claimable"),
        EntryFence(disposition="UNAVAILABLE", reason="intent_expired"),
    ),
)
def test_an_unavailable_entry_fence_releases_the_intent_and_sends_nothing(fence: EntryFence) -> None:
    """#331: nothing was written, nothing may be sent, and the reason is no longer a bare `None`."""

    intent = _intent()
    quantity = Decimal("0.001")
    queues = strategy_queues()
    bridge = NautilusDatabaseBridge(_settings(), queues, now_ms=lambda: NOW_MS)
    repos = _Repositories()
    repos.trading.fence_entry.return_value = fence

    bridge._handle_event(
        repos,
        _fence_request(intent, quantity),
    )

    assert queues.commands.get_nowait() == IntentReleased(intent_id=intent.intent_id)


def test_a_refused_entry_fence_releases_the_intent_after_a_durable_terminal_rejection() -> None:
    intent = _intent()
    queues = strategy_queues()
    bridge = NautilusDatabaseBridge(_settings(), queues, now_ms=lambda: NOW_MS)
    repos = _Repositories()
    rejected = _outcome(
        intent,
        execution_state="TERMINAL",
        terminal_outcome="REJECTED",
        reason_code="capability_mismatch",
    )
    repos.trading.fence_entry.return_value = EntryFence(
        disposition="REFUSED",
        reason="capability_mismatch",
        outcome=rejected,
    )

    bridge._handle_event(
        repos,
        _fence_request(intent),
    )

    assert queues.commands.get_nowait() == IntentReleased(intent_id=intent.intent_id)


def test_q2_evidence_commits_before_the_strategy_receives_submission_authority() -> None:
    intent = _intent()
    client_order_id = deterministic_client_order_id(intent.intent_id, "entry")
    q2 = _accepted_quote(intent, stage="Q2")
    authorized = _outcome(
        intent,
        execution_state="IN_FLIGHT",
        execution_phase="ENTRY",
        entry_client_order_id=client_order_id,
        entry_fenced_at_ms=NOW_MS,
        submission_fence_version="submission_fence_v1",
        submission_quantity=Decimal("0.001"),
        entry_quote_q1=_accepted_quote(intent),
        entry_quote_q2=q2,
    )
    queues = strategy_queues()
    bridge = NautilusDatabaseBridge(_settings(), queues, now_ms=lambda: NOW_MS)
    repos = _Repositories()
    repos.trading.authorize_entry_submission.return_value = authorized

    assert bridge._handle_event(
        repos,
        EntrySubmissionRequested(
            intent_id=intent.intent_id,
            client_order_id=client_order_id,
            q2_evidence=q2,
        ),
    )

    assert repos.order == ["begin", "commit"]
    assert queues.commands.get_nowait() == EntrySubmissionGranted(outcome=authorized)


def test_q2_no_submit_terminal_commits_before_the_strategy_releases_the_intent() -> None:
    intent = _intent()
    client_order_id = deterministic_client_order_id(intent.intent_id, "entry")
    q2 = ExecutionQuoteRejectionV1(
        stage="Q2",
        reason="quote_receive_stale",
        intent_id=intent.intent_id,
        instrument_id=intent.instrument_id,
        side="buy",
        evaluated_at_ns=NOW_MS * 1_000_000,
    )
    rejected = _outcome(
        intent,
        execution_state="TERMINAL",
        terminal_outcome="REJECTED",
        reason_code="quote_receive_stale",
        entry_client_order_id=client_order_id,
        entry_fenced_at_ms=NOW_MS,
        submission_fence_version="submission_fence_v1",
        submission_quantity=Decimal("0.001"),
        entry_quote_q1=_accepted_quote(intent),
        entry_quote_q2=q2,
    )
    queues = strategy_queues()
    bridge = NautilusDatabaseBridge(_settings(), queues, now_ms=lambda: NOW_MS)
    repos = _Repositories()
    repos.trading.record_fenced_quote_no_submit.return_value = rejected

    assert bridge._handle_event(
        repos,
        EntryNoSubmitRequested(
            intent_id=intent.intent_id,
            client_order_id=client_order_id,
            reason_code="quote_receive_stale",
            q2_evidence=q2,
        ),
    )

    assert repos.order == ["begin", "commit"]
    assert queues.commands.get_nowait() == EntryNoSubmitFinalized(outcome=rejected)


def test_q2_projection_failure_never_grants_submission_authority() -> None:
    intent = _intent()
    client_order_id = deterministic_client_order_id(intent.intent_id, "entry")
    q2 = _accepted_quote(intent, stage="Q2")
    queues = strategy_queues()
    bridge = NautilusDatabaseBridge(_settings(), queues, now_ms=lambda: NOW_MS)
    repos = _Repositories()
    repos.trading.authorize_entry_submission.return_value = None
    repos.trading.intent_outcome.return_value = None

    with pytest.raises(RuntimeError, match="entry_submission_authority_projection_failed"):
        bridge._handle_event(
            repos,
            EntrySubmissionRequested(
                intent_id=intent.intent_id,
                client_order_id=client_order_id,
                q2_evidence=q2,
            ),
        )

    assert queues.commands.empty()


def test_execution_events_write_only_authoritative_identifiers_and_quantities() -> None:
    intent = _intent()
    queues = strategy_queues()
    bridge = NautilusDatabaseBridge(_settings(), queues, now_ms=lambda: NOW_MS + 500)
    repos = _Repositories()
    stop_id = deterministic_client_order_id(intent.intent_id, "stop")
    close_id = deterministic_client_order_id(intent.intent_id, "close")

    bridge._handle_event(
        repos,
        StopSubmitted(
            intent_id=intent.intent_id,
            client_order_id=stop_id,
            generation=0,
            previous_client_order_id=None,
            quantity=Decimal("0.001"),
            submitted_at_ms=NOW_MS + 10,
        ),
    )
    repos.trading.record_stop_submitted.assert_called_once_with(
        intent.intent_id,
        client_order_id=stop_id,
        generation=0,
        previous_client_order_id=None,
        quantity=Decimal("0.001"),
        now_ms=NOW_MS + 10,
    )

    bridge._handle_event(
        repos,
        StopAccepted(
            intent_id=intent.intent_id,
            client_order_id=stop_id,
            venue_order_id="venue-stop-1",
            quantity=Decimal("0.001"),
            trigger_price=Decimal("9800"),
            accepted_at_ms=NOW_MS + 20,
        ),
    )
    repos.trading.record_protected.assert_called_once_with(
        intent.intent_id,
        accepted_client_order_id=stop_id,
        protection_order_id="venue-stop-1",
        protected_quantity=Decimal("0.001"),
        stop_price=Decimal("9800"),
        protected_at_ms=NOW_MS + 20,
        now_ms=NOW_MS + 20,
    )

    bridge._handle_event(
        repos,
        PositionQuantityChanged(
            intent_id=intent.intent_id,
            position_id="position-1",
            actual_quantity=Decimal("0.002"),
            avg_entry_price=Decimal("10100"),
            changed_at_ms=NOW_MS + 25,
        ),
    )
    repos.trading.record_position_changed.assert_called_once_with(
        intent.intent_id,
        position_id="position-1",
        actual_quantity=Decimal("0.002"),
        avg_entry_price=Decimal("10100"),
        now_ms=NOW_MS + 25,
    )

    bridge._handle_event(
        repos,
        CloseSubmitted(
            intent_id=intent.intent_id,
            client_order_id=close_id,
            position_id="position-1",
            quantity=Decimal("0.001"),
            submitted_at_ms=NOW_MS + 30,
        ),
    )
    repos.trading.record_close_submitted.assert_called_once_with(
        intent.intent_id,
        client_order_id=close_id,
        position_id="position-1",
        quantity=Decimal("0.001"),
        submitted_at_ms=NOW_MS + 30,
        now_ms=NOW_MS + 30,
    )


def test_unknown_outcomes_enter_manual_review_and_flat_requires_targeted_zero() -> None:
    intent = _intent()
    queues = strategy_queues()
    bridge = NautilusDatabaseBridge(_settings(), queues, now_ms=lambda: NOW_MS + 500)
    repos = _Repositories()

    bridge._handle_event(
        repos,
        OrderOutcomeUnknown(
            intent_id=intent.intent_id,
            leg="close",
            observed_at_ms=NOW_MS + 40,
        ),
    )
    repos.trading.mark_manual_review.assert_called_once_with(
        intent.intent_id,
        reason_code="close_outcome_unknown",
        now_ms=NOW_MS + 40,
    )

    bridge._handle_event(
        repos,
        PositionClosedObserved(
            intent_id=intent.intent_id,
            instrument_id="SOLUSDT-PERP.BINANCE",
            account_id="BINANCE-001",
            position_id="position-1",
            closing_client_order_id=deterministic_client_order_id(intent.intent_id, "close"),
            local_quantity=Decimal("0"),
            avg_exit_price=Decimal("10050"),
            realized_pnl_amount=None,
            realized_pnl_currency=None,
            commissions_by_currency={"USDT": "0.12"},
            closed_at_ms=NOW_MS + 50,
        ),
    )
    repos.trading.record_position_closed_observed.assert_called_once_with(
        intent.intent_id,
        instrument_id="SOLUSDT-PERP.BINANCE",
        account_id="BINANCE-001",
        position_id="position-1",
        closing_client_order_id=deterministic_client_order_id(intent.intent_id, "close"),
        local_quantity=Decimal("0"),
        avg_exit_price=Decimal("10050"),
        closed_at_ms=NOW_MS + 50,
        realized_pnl_amount=None,
        realized_pnl_currency=None,
        commissions_by_currency={"USDT": "0.12"},
        now_ms=NOW_MS + 50,
        funding_by_currency=None,
    )
    repos.trading.record_closed_flat.assert_not_called()

    bridge._handle_event(
        repos,
        PositionFlatConfirmed(
            intent_id=intent.intent_id,
            position_id="position-1",
            authoritative_quantity=Decimal("0"),
            avg_exit_price=Decimal("10050"),
            realized_pnl_amount=None,
            realized_pnl_currency=None,
            commissions_by_currency={"USDT": "0.12"},
            closed_at_ms=NOW_MS + 50,
            flat_verified_at_ms=NOW_MS + 500,
        ),
    )
    repos.trading.record_closed_flat.assert_called_once_with(
        intent.intent_id,
        position_id="position-1",
        authoritative_quantity=Decimal("0"),
        avg_exit_price=Decimal("10050"),
        closed_at_ms=NOW_MS + 50,
        flat_verified_at_ms=NOW_MS + 500,
        realized_pnl_amount=None,
        realized_pnl_currency=None,
        commissions_by_currency={"USDT": "0.12"},
        now_ms=NOW_MS + 500,
        funding_by_currency=None,
    )


def test_unattributed_unknown_order_fails_readiness_closed_without_inventing_an_intent() -> None:
    queues = strategy_queues()
    bridge = NautilusDatabaseBridge(_settings(), queues, now_ms=lambda: NOW_MS)
    repos = _Repositories()

    bridge._handle_event(
        repos,
        OrderOutcomeUnknown(
            intent_id=None,
            leg=None,
            observed_at_ms=NOW_MS,
        ),
    )

    assert bridge.readiness()["ok"] is False
    assert bridge.readiness()["reason"] == "unattributed_order_outcome_unknown"
    assert bridge.readiness()["unexpected_exposure"] is True
    repos.trading.mark_manual_review.assert_not_called()


def test_projection_event_is_retried_after_a_transient_database_disconnect() -> None:
    intent = _intent()
    queues = strategy_queues()
    bridge = NautilusDatabaseBridge(_settings(), queues, now_ms=lambda: NOW_MS)
    repos = _Repositories()
    repos.trading.binding_execution_runtime.return_value = {
        "control": "RUNNING",
        "capability_snapshot_sha256": None,
    }
    repos.trading.active_intent.return_value = None
    stop_id = deterministic_client_order_id(intent.intent_id, "stop")
    submitted = StopSubmitted(
        intent_id=intent.intent_id,
        client_order_id=stop_id,
        generation=0,
        previous_client_order_id=None,
        quantity=Decimal("0.001"),
        submitted_at_ms=NOW_MS,
    )
    queues.events.put_nowait(submitted)
    projected = _outcome(
        intent,
        execution_state="IN_FLIGHT",
        execution_phase="PROTECTION",
        entry_client_order_id=deterministic_client_order_id(intent.intent_id, "entry"),
        entry_fenced_at_ms=NOW_MS - 10,
        stop_client_order_id=stop_id,
        stop_generation=0,
        stop_submitted_at_ms=NOW_MS,
        actual_quantity=Decimal("0.001"),
    )
    repos.trading.record_stop_submitted.side_effect = [OperationalError("disconnect"), projected]

    with pytest.raises(OperationalError):
        bridge._cycle(repos)
    assert queues.events.empty()

    bridge._cycle(repos)
    assert repos.trading.record_stop_submitted.call_count == 2
    assert bridge._pending_event is None


def test_expiry_projection_failure_blocks_admission_until_the_same_row_updates() -> None:
    intent = _intent()
    queues = strategy_queues()
    bridge = NautilusDatabaseBridge(_settings(), queues, now_ms=lambda: intent.valid_until_ms)
    repos = _Repositories()
    repos.trading.binding_execution_runtime.return_value = {
        "control": "RUNNING",
        "capability_snapshot_sha256": None,
    }
    repos.trading.active_intent.return_value = (intent, _outcome(intent))
    expired = _outcome(
        intent,
        execution_state="TERMINAL",
        terminal_outcome="EXPIRED",
        reason_code="intent_expired",
    )
    repos.trading.expire_unfenced_intent.side_effect = [None, expired]
    bridge._set_db_connected(True)
    _ready(bridge, repos)

    bridge._cycle(repos)
    assert bridge.readiness()["reason"] == "execution_projection_rejected"

    bridge._cycle(repos)
    assert repos.trading.expire_unfenced_intent.call_count == 2
    assert queues.commands.get_nowait() == IntentReleased(intent_id=intent.intent_id)
    assert bridge.readiness()["ok"] is True


def test_successful_expiry_cannot_clear_a_blocked_execution_projection() -> None:
    intent = _intent()
    queues = strategy_queues()
    bridge = NautilusDatabaseBridge(_settings(), queues, now_ms=lambda: intent.valid_until_ms)
    repos = _Repositories()
    repos.trading.binding_execution_runtime.return_value = {
        "control": "RUNNING",
        "capability_snapshot_sha256": None,
    }
    repos.trading.active_intent.return_value = (intent, _outcome(intent))
    repos.trading.expire_unfenced_intent.return_value = _outcome(
        intent,
        execution_state="TERMINAL",
        terminal_outcome="EXPIRED",
        reason_code="intent_expired",
    )
    repos.trading.record_stop_submitted.return_value = None
    repos.trading.intent_outcome.return_value = _outcome(intent)
    submitted = StopSubmitted(
        intent_id=intent.intent_id,
        client_order_id=deterministic_client_order_id(intent.intent_id, "stop"),
        generation=0,
        previous_client_order_id=None,
        quantity=Decimal("0.001"),
        submitted_at_ms=NOW_MS,
    )
    queues.events.put_nowait(submitted)
    bridge._set_db_connected(True)
    _ready(bridge, repos)

    bridge._cycle(repos)

    assert bridge._pending_event == submitted
    assert bridge.readiness()["reason"] == "execution_projection_rejected"
    assert repos.trading.set_binding_execution_runtime.call_args.kwargs["ready"] is False
    assert (
        repos.trading.set_binding_execution_runtime.call_args.kwargs["readiness_reason"]
        == "execution_projection_rejected"
    )


def test_logically_rejected_projection_blocks_admission_and_retries_in_place() -> None:
    intent = _intent()
    queues = strategy_queues()
    bridge = NautilusDatabaseBridge(_settings(), queues, now_ms=lambda: NOW_MS)
    repos = _Repositories()
    repos.trading.binding_execution_runtime.return_value = {
        "control": "RUNNING",
        "capability_snapshot_sha256": None,
    }
    repos.trading.active_intent.return_value = None
    repos.trading.record_stop_submitted.return_value = None
    repos.trading.intent_outcome.return_value = _outcome(
        intent,
        execution_state="IN_FLIGHT",
        execution_phase="PROTECTION",
        entry_client_order_id=deterministic_client_order_id(intent.intent_id, "entry"),
        entry_fenced_at_ms=NOW_MS - 10,
        actual_quantity=Decimal("0.001"),
    )
    submitted = StopSubmitted(
        intent_id=intent.intent_id,
        client_order_id=deterministic_client_order_id(intent.intent_id, "stop"),
        generation=0,
        previous_client_order_id=None,
        quantity=Decimal("0.001"),
        submitted_at_ms=NOW_MS,
    )
    queues.events.put_nowait(submitted)
    bridge._set_db_connected(True)
    _ready(bridge, repos)

    bridge._cycle(repos)
    bridge._cycle(repos)

    assert repos.trading.record_stop_submitted.call_count == 2
    assert bridge.readiness()["ok"] is False
    assert bridge.readiness()["reason"] == "execution_projection_rejected"
    assert bridge._pending_event == submitted
    assert repos.trading.set_binding_execution_runtime.call_count == 2


def test_ambiguous_commit_is_accepted_when_the_same_projection_is_already_durable() -> None:
    intent = _intent()
    queues = strategy_queues()
    bridge = NautilusDatabaseBridge(_settings(), queues, now_ms=lambda: NOW_MS)
    repos = _Repositories()
    repos.trading.binding_execution_runtime.return_value = {
        "control": "RUNNING",
        "capability_snapshot_sha256": None,
    }
    repos.trading.active_intent.return_value = None
    stop_id = deterministic_client_order_id(intent.intent_id, "stop")
    repos.trading.record_stop_submitted.return_value = None
    repos.trading.intent_outcome.return_value = _outcome(
        intent,
        execution_state="IN_FLIGHT",
        execution_phase="PROTECTION",
        entry_client_order_id=deterministic_client_order_id(intent.intent_id, "entry"),
        entry_fenced_at_ms=NOW_MS - 10,
        stop_client_order_id=stop_id,
        stop_generation=0,
        stop_submitted_at_ms=NOW_MS,
        actual_quantity=Decimal("0.001"),
    )
    queues.events.put_nowait(
        StopSubmitted(
            intent_id=intent.intent_id,
            client_order_id=stop_id,
            generation=0,
            previous_client_order_id=None,
            quantity=Decimal("0.001"),
            submitted_at_ms=NOW_MS,
        )
    )
    bridge._set_db_connected(True)
    _ready(bridge, repos)

    bridge._cycle(repos)

    assert bridge._pending_event is None
    assert bridge.readiness()["ok"] is True
    assert bridge.readiness()["reason"] == "ready"


def test_position_change_readback_requires_the_same_authoritative_average() -> None:
    intent = _intent()
    event = PositionQuantityChanged(
        intent_id=intent.intent_id,
        position_id="position-1",
        actual_quantity=Decimal("0.002"),
        avg_entry_price=Decimal("10100"),
        changed_at_ms=NOW_MS,
    )

    assert not NautilusDatabaseBridge._event_is_projected(
        _outcome(
            intent,
            position_id="position-1",
            actual_quantity=Decimal("0.002"),
            avg_entry_price=Decimal("10000"),
        ),
        event,
    )
    assert NautilusDatabaseBridge._event_is_projected(
        _outcome(
            intent,
            position_id="position-1",
            actual_quantity=Decimal("0.002"),
            avg_entry_price=Decimal("10100"),
        ),
        event,
    )


def test_unknown_fee_readback_accepts_a_stronger_durable_snapshot() -> None:
    intent = _intent()
    queues = strategy_queues()
    bridge = NautilusDatabaseBridge(_settings(), queues, now_ms=lambda: NOW_MS)
    repos = _Repositories()
    close_id = deterministic_client_order_id(intent.intent_id, "close")
    event = PositionClosedObserved(
        intent_id=intent.intent_id,
        instrument_id=intent.instrument_id,
        account_id="BINANCE-001",
        position_id="position-1",
        closing_client_order_id=close_id,
        local_quantity=Decimal(0),
        avg_exit_price=Decimal("10050"),
        realized_pnl_amount=Decimal("0.05"),
        realized_pnl_currency="USDT",
        commissions_by_currency=None,
        closed_at_ms=NOW_MS,
    )
    repos.trading.record_position_closed_observed.return_value = None
    repos.trading.intent_outcome.return_value = _outcome(
        intent,
        execution_state="IN_FLIGHT",
        execution_phase="EXIT",
        entry_client_order_id=deterministic_client_order_id(intent.intent_id, "entry"),
        entry_fenced_at_ms=NOW_MS - 100,
        close_client_order_id=close_id,
        close_submitted_at_ms=NOW_MS - 50,
        actual_quantity=Decimal("0.001"),
        avg_exit_price=Decimal("10050"),
        position_id="position-1",
        closed_at_ms=NOW_MS,
        realized_pnl_amount=Decimal("0.05"),
        realized_pnl_currency="USDT",
        commissions_by_currency={"USDT": "0.01"},
    )

    assert bridge._handle_event(repos, event) is True
