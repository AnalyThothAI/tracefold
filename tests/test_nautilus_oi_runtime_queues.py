"""Bounded at-least-once Signal and Observation mechanics."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.nautilus_oi_runtime_fixtures import NOW_NS, SignalRows, oi_profile, trade_signal
from tracefold.integrations.nautilus.oi_runtime.audit_sink import (
    AuditSink,
    ObservationFactory,
    day_start_baseline_from_observation,
)
from tracefold.integrations.nautilus.oi_runtime.signal_client import ExecutionSignalClient


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
