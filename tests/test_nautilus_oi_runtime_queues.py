"""Bounded at-least-once Signal and Observation mechanics."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.nautilus_oi_runtime_fixtures import (
    NOW_NS,
    CommandRows,
    SignalRows,
    oi_profile,
    operator_intent,
    trade_signal,
)
from tracefold.integrations.nautilus.oi_runtime.audit_sink import (
    AuditSink,
    ObservationFactory,
    day_start_baseline_from_observation,
)
from tracefold.integrations.nautilus.oi_runtime.signal_client import (
    ExecutionSignalClient,
    poll_execution_inputs_once,
)


def _factory() -> ObservationFactory:
    profile = oi_profile()
    return ObservationFactory(
        runtime_profile_id=profile.profile_id,
        runtime_release=profile.runtime_release,
        execution_strategy="oi_nautilus_v1",
    )


def test_signal_queue_is_count_and_byte_bounded_without_silent_pending_claim() -> None:
    first = trade_signal(signal_id="1" * 64)
    second = trade_signal(signal_id="2" * 64)
    one_size = len(first.model_dump_json().encode())
    client = ExecutionSignalClient(
        runtime_profile_id=oi_profile().profile_id,
        execution_strategy="oi_nautilus_v1",
        max_count=2,
        max_bytes=one_size,
    )

    admitted = client.poll_once(SignalRows(first, second))

    assert admitted == 1
    assert client.queued_count == 1
    assert client.pending_ids == {first.signal_id}
    assert second.signal_id not in client.pending_ids
    assert client.next_nowait() == first
    assert client.queued_count == 0
    assert client.pending_ids == {first.signal_id}
    client.mark_durable(first.signal_id)
    assert client.pending_ids == set()


def test_signal_pending_set_prevents_duplicate_enqueue_until_disposition_is_durable() -> None:
    signal = trade_signal()
    rows = SignalRows(signal)
    client = ExecutionSignalClient(
        runtime_profile_id=oi_profile().profile_id,
        execution_strategy="oi_nautilus_v1",
    )

    assert client.poll_once(rows) == 1
    assert client.next_nowait() == signal
    assert client.poll_once(rows) == 0
    client.mark_durable(signal.signal_id)
    assert client.poll_once(rows) == 1


def test_signal_can_be_retried_when_audit_backpressure_prevents_final_disposition() -> None:
    signal = trade_signal()
    client = ExecutionSignalClient(
        runtime_profile_id=oi_profile().profile_id,
        execution_strategy="oi_nautilus_v1",
    )
    client.poll_once(SignalRows(signal))
    assert client.next_nowait() == signal

    client.retry(signal)

    assert client.next_nowait() == signal
    assert client.pending_ids == {signal.signal_id}


def test_signal_client_consumes_commands_in_the_same_total_count_and_byte_bound() -> None:
    signal = trade_signal()
    command = operator_intent()
    client = ExecutionSignalClient(
        runtime_profile_id=oi_profile().profile_id,
        execution_strategy="oi_nautilus_v1",
        max_count=1,
        max_bytes=1_048_576,
    )

    assert client.poll_commands_once(CommandRows(command)) == 1
    assert client.poll_once(SignalRows(signal)) == 0
    assert client.next_command_nowait() == command
    assert client.pending_command_ids == {command.command_id}
    client.retry_command(command)
    assert client.next_command_nowait() == command
    client.mark_command_durable(command.command_id)
    assert client.pending_command_ids == set()


def test_poll_admits_operator_commands_before_signals_into_the_shared_bound() -> None:
    signal = trade_signal()
    command = operator_intent()
    client = ExecutionSignalClient(
        runtime_profile_id=oi_profile().profile_id,
        execution_strategy="oi_nautilus_v1",
        max_count=1,
    )

    admitted = poll_execution_inputs_once(
        client=client,
        reader=SignalRows(signal),
        command_reader=CommandRows(command),
    )

    assert admitted == (1, 0)
    assert client.next_command_nowait() == command
    assert client.next_nowait() is None


def test_signal_retry_cannot_overfill_the_shared_command_and_signal_bound() -> None:
    signal = trade_signal()
    command = operator_intent()
    client = ExecutionSignalClient(
        runtime_profile_id=oi_profile().profile_id,
        execution_strategy="oi_nautilus_v1",
        max_count=1,
    )
    assert client.poll_once(SignalRows(signal)) == 1
    assert client.next_nowait() == signal
    assert client.poll_commands_once(CommandRows(command)) == 1

    with pytest.raises(RuntimeError, match="oi_runtime_signal_retry_overflow"):
        client.retry(signal)

    assert client.queued_count == 1


def test_durable_command_scan_evicts_one_buffered_signal_instead_of_being_starved() -> None:
    first = trade_signal(signal_id="1" * 64)
    second = trade_signal(signal_id="2" * 64)
    command = operator_intent(command_id="3" * 64)
    client = ExecutionSignalClient(
        runtime_profile_id=oi_profile().profile_id,
        execution_strategy="oi_nautilus_v1",
        max_count=2,
    )
    assert client.poll_once(SignalRows(first, second)) == 2

    assert client.poll_commands_once(CommandRows(command)) == 1

    assert client.queued_count == 2
    assert client.queued_command_count == 1
    assert client.command_scan_complete is False
    assert client.pending_ids == {first.signal_id}
    assert client.pending_command_ids == {command.command_id}
    assert client.poll_once(SignalRows(first, second)) == 0


def test_durable_command_scan_can_reclaim_signal_bytes_without_losing_database_truth() -> None:
    first = trade_signal(signal_id="1" * 64)
    second = trade_signal(signal_id="2" * 64)
    command = operator_intent(command_id="3" * 64)
    signal_bytes = len(first.model_dump_json().encode()) + len(second.model_dump_json().encode())
    command_bytes = len(command.model_dump_json().encode())
    client = ExecutionSignalClient(
        runtime_profile_id=oi_profile().profile_id,
        execution_strategy="oi_nautilus_v1",
        max_count=3,
        max_bytes=max(signal_bytes, command_bytes),
    )
    assert client.poll_once(SignalRows(first, second)) == 2

    assert client.poll_commands_once(CommandRows(command)) == 1

    assert client.pending_command_ids == {command.command_id}
    assert client.queued_bytes <= max(signal_bytes, command_bytes)
    assert len(client.pending_ids) < 2


def test_failed_command_scan_closes_the_signal_gate() -> None:
    signal = trade_signal()
    client = ExecutionSignalClient(
        runtime_profile_id=oi_profile().profile_id,
        execution_strategy="oi_nautilus_v1",
        max_count=2,
    )
    assert client.poll_once(SignalRows(signal)) == 1

    def unavailable(_profile: str, _strategy: str, _limit: int) -> tuple[()]:
        raise RuntimeError("command-reader-unavailable")

    with pytest.raises(RuntimeError, match="command-reader-unavailable"):
        client.poll_commands_once(unavailable)

    assert client.command_scan_complete is False
    assert client.poll_once(SignalRows(signal)) == 0


def test_audit_flush_failure_keeps_batch_and_blocks_exposure_until_success() -> None:
    factory = _factory()
    value = factory.create(
        normalized_kind="readiness",
        occurred_at_ns=NOW_NS,
        observed_at_ns=NOW_NS,
        summary={"ready": False},
        payload={"ready": False},
    )
    sink = AuditSink(factory=factory, max_count=2, max_bytes=20_000)
    assert sink.offer(value) is True

    def fail(_values: object) -> None:
        raise RuntimeError("append-failed")

    with pytest.raises(RuntimeError, match="append-failed"):
        sink.flush_once(fail)
    assert sink.queued_count == 1
    assert sink.healthy is False
    assert sink.can_accept_exposure() is False

    written: list[object] = []
    assert sink.flush_once(written.extend) == (value,)
    assert sink.queued_count == 0
    assert sink.healthy is True
    assert sink.can_accept_exposure() is False


def test_audit_identity_conflict_stays_unhealthy_until_gap_is_durable() -> None:
    factory = _factory()
    event_id = "f" * 64
    first = factory.create(
        normalized_kind="readiness",
        occurred_at_ns=NOW_NS,
        observed_at_ns=NOW_NS,
        payload={"version": 1},
        fixed_event_id=event_id,
    )
    conflicting = factory.create(
        normalized_kind="readiness",
        occurred_at_ns=NOW_NS + 1,
        observed_at_ns=NOW_NS + 1,
        payload={"version": 2},
        fixed_event_id=event_id,
    )
    sink = AuditSink(factory=factory, max_count=1, max_bytes=20_000)

    assert sink.offer(first) is True
    assert sink.offer(conflicting) is False
    assert sink.failure_reason == "audit_identity_conflict"
    written: list[object] = []
    assert sink.flush_once(written.extend) == (first,)
    assert sink.healthy is False
    gap_batch = sink.flush_once(written.extend)

    assert len(gap_batch) == 1
    gap = gap_batch[0]
    assert gap.normalized_kind == "audit_gap"
    assert gap.summary == {"cause": "audit_identity_conflict", "conflict_count": 1}
    assert sink.healthy is True


def test_audit_overflow_stays_unhealthy_until_a_durable_gap_is_written() -> None:
    factory = _factory()
    sink = AuditSink(factory=factory, max_count=2, max_bytes=20_000)
    values = tuple(
        factory.create(
            normalized_kind="readiness",
            occurred_at_ns=NOW_NS + index,
            observed_at_ns=NOW_NS + index,
            summary={"index": index},
            payload={"index": index},
        )
        for index in range(5)
    )

    assert sink.offer(values[0]) is True
    assert sink.offer(values[1]) is True
    assert sink.offer(values[2]) is False
    assert sink.failure_reason == "audit_queue_overflow"

    written: list[object] = []
    assert sink.flush_once(written.extend) == values[:2]
    assert sink.healthy is False
    assert sink.offer(values[3]) is True
    assert sink.offer(values[4]) is False
    gap_batch = sink.flush_once(written.extend)

    assert len(gap_batch) == 2
    gap = gap_batch[0]
    assert gap.normalized_kind == "audit_gap"
    assert gap.summary == {"cause": "audit_queue_overflow", "dropped_count": 1}
    assert sink.healthy is False
    next_gap_batch = sink.flush_once(written.extend)
    assert len(next_gap_batch) == 1
    assert next_gap_batch[0].normalized_kind == "audit_gap"
    assert next_gap_batch[0].summary == {"cause": "audit_queue_overflow", "dropped_count": 1}
    assert sink.healthy is True


def test_audit_gap_identity_cannot_collide_across_process_restart_clocks() -> None:
    factory = _factory()

    def overflow_gap(offset: int) -> object:
        sink = AuditSink(factory=factory, max_count=1, max_bytes=20_000)
        first = factory.create(
            normalized_kind="readiness",
            occurred_at_ns=NOW_NS + offset,
            observed_at_ns=NOW_NS + offset,
            payload={"offset": offset},
        )
        dropped = factory.create(
            normalized_kind="readiness",
            occurred_at_ns=NOW_NS + offset + 1,
            observed_at_ns=NOW_NS + offset + 1,
            payload={"dropped": offset},
        )
        assert sink.offer(first) is True
        assert sink.offer(dropped) is False
        sink.flush_once(lambda _values: None)
        return sink.flush_once(lambda _values: None)[0]

    first_gap = overflow_gap(0)
    restarted_gap = overflow_gap(100)

    assert first_gap.event_id != restarted_gap.event_id


def test_day_start_baseline_has_stable_identity_and_exact_restart_value() -> None:
    factory = _factory()
    first, observation = factory.day_start_baseline(
        utc_day="2030-03-17",
        equity_usd=Decimal("1000.123456"),
        recorded_at_ns=NOW_NS,
    )
    restarted = day_start_baseline_from_observation(observation)

    assert first == restarted
    assert factory.day_start_event_id("2030-03-17") == observation.event_id
    assert factory.day_start_event_id("2030-03-18") != observation.event_id
