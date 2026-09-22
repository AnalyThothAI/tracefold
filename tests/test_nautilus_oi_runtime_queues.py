"""The Runtime's two in-memory seams: the bounded input queue and the outbound journal."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.nautilus_oi_runtime_fixtures import NOW_NS, oi_profile, open_plan, operator_intent, rows, trade_signal
from tracefold.integrations.nautilus.oi_runtime.journal import (
    ExecutionJournal,
    ObservationFactory,
    PlanReceipt,
    day_start_baseline_from_observation,
)
from tracefold.integrations.nautilus.oi_runtime.signal_client import ExecutionSignalClient


def _client(**bounds: int) -> ExecutionSignalClient:
    return ExecutionSignalClient(account_slot=oi_profile().account_slot, execution_strategy="oi_nautilus_v1", **bounds)


def _factory() -> ObservationFactory:
    return ObservationFactory(account_slot=oi_profile().account_slot, execution_strategy="oi_nautilus_v1")


def _observation(index: int) -> object:
    return _factory().create(
        normalized_kind="order",
        occurred_at_ns=NOW_NS + index,
        observed_at_ns=NOW_NS + index,
        summary={"leg": "entry", "status": "submitted"},
        event_identity=f"row-{index}",
    )


# -- the input queue -------------------------------------------------------------------------------


def test_signal_queue_is_count_and_byte_bounded_without_a_silent_pending_claim() -> None:
    first = trade_signal(signal_id="1" * 64)
    second = trade_signal(signal_id="2" * 64)
    client = _client(max_count=2, max_bytes=len(first.model_dump_json().encode()))

    assert client.poll_once(rows(first, second)) == 1
    assert client.next_nowait() == first
    assert client.next_nowait() is None
    # Still pending until its verdict is durable, so the next indexed poll cannot enqueue it again.
    assert client.poll_once(rows(first)) == 0
    client.mark_durable(first.signal_id)
    assert client.poll_once(rows(first)) == 1


def test_a_released_signal_is_offered_again_and_a_foreign_verdict_settles_nothing() -> None:
    signal = trade_signal()
    client = _client()
    client.poll_once(rows(signal))
    assert client.next_nowait() == signal
    client.release(signal.signal_id)
    assert client.poll_once(rows(signal)) == 1
    # A verdict for a Signal an earlier generation polled is not this client's to settle.
    client.mark_durable("9" * 64)
    client.mark_command_durable("9" * 64)


def test_commands_are_admitted_before_signals_into_the_shared_bound() -> None:
    signal = trade_signal()
    command = operator_intent()
    client = _client(max_count=1)

    assert client.poll_commands_once(rows(command)) == 1
    assert client.poll_once(rows(signal)) == 0
    assert client.next_command_nowait() == command
    assert client.next_nowait() is None


def test_a_command_scan_evicts_a_buffered_signal_instead_of_being_starved() -> None:
    first = trade_signal(signal_id="1" * 64)
    second = trade_signal(signal_id="2" * 64)
    command = operator_intent(command_id="3" * 64)
    client = _client(max_count=2)
    assert client.poll_once(rows(first, second)) == 2

    assert client.poll_commands_once(rows(command)) == 1

    assert client.queued_command_count == 1
    assert client.command_scan_complete is False
    assert client.poll_once(rows(first, second)) == 0


def test_a_failed_command_scan_closes_the_signal_gate() -> None:
    signal = trade_signal()
    client = _client(max_count=2)
    assert client.poll_once(rows(signal)) == 1

    def unavailable(_slot: str, _strategy: str, _limit: int) -> tuple[()]:
        raise RuntimeError("command-reader-unavailable")

    with pytest.raises(RuntimeError, match="command-reader-unavailable"):
        client.poll_commands_once(unavailable)
    assert client.command_scan_complete is False
    assert client.poll_once(rows(signal)) == 0


# -- the journal -----------------------------------------------------------------------------------


def test_the_journal_is_fifo_deduplicates_an_identical_offer_and_refuses_past_its_bound() -> None:
    journal = ExecutionJournal(factory=_factory(), max_rows=2)
    first, second, third = (_observation(index) for index in range(3))

    assert journal.offer(first)
    assert journal.offer(first)
    assert journal.offer(second)
    assert not journal.offer(third)
    assert [row.value for row in journal.due(0.0)] == [first, second]


def test_a_failing_row_waits_out_its_backoff_while_every_row_behind_it_keeps_flowing() -> None:
    journal = ExecutionJournal(factory=_factory())
    first, second = _observation(1), _observation(2)
    journal.offer(first)
    journal.offer(second)

    head, behind = journal.due(100.0)
    journal.retry_later(head, 100.0)
    journal.written(behind, behind.value)

    assert journal.due(100.0) == ()
    [retried] = journal.due(100.0 + 30.0)
    assert retried.value is first
    for _ in range(20):
        journal.retry_later(retried, 200.0)
    assert retried.not_before == 230.0


def test_a_newer_plan_transition_replaces_a_queued_one_and_a_terminal_one_is_never_undone() -> None:
    journal = ExecutionJournal(factory=_factory())
    plan = open_plan(opened_at_ns=None)
    opened = plan.opened(opened_at_ns=NOW_NS, now_ns=NOW_NS)
    closed = opened.closed(reason="stop_filled", terminal_at_ns=NOW_NS + 1, now_ns=NOW_NS + 1)

    journal.offer_plan(opened)
    journal.offer_plan(closed)
    journal.offer_plan(opened)

    [row] = journal.due(0.0)
    assert row.value == closed


def test_a_plan_transition_arriving_while_the_bridge_writes_the_older_one_is_still_written() -> None:
    journal = ExecutionJournal(factory=_factory())
    plan = open_plan(opened_at_ns=None)
    opened = plan.opened(opened_at_ns=NOW_NS, now_ns=NOW_NS)
    closed = opened.closed(reason="time_exit", terminal_at_ns=NOW_NS + 1, now_ns=NOW_NS + 1)
    journal.offer_plan(opened)
    [row] = journal.due(0.0)
    in_flight = row.value

    journal.offer_plan(closed)
    journal.written(row, in_flight)

    [still_queued] = journal.due(0.0)
    assert still_queued.value == closed


def test_one_entry_plan_at_a_time_and_only_a_receipt_releases_the_next() -> None:
    journal = ExecutionJournal(factory=_factory())
    first = open_plan(entry_id="1" * 64, opened_at_ns=None)
    second = open_plan(entry_id="2" * 64, opened_at_ns=None)

    assert journal.prepare(first)
    assert not journal.prepare(second)
    assert journal.take_receipt() is None
    journal.settle_prepare(PlanReceipt(first, committed=True))
    assert journal.pending_prepare() is None
    assert not journal.prepare(second)
    assert journal.take_receipt() == PlanReceipt(first, committed=True)
    assert journal.prepare(second)
    with pytest.raises(RuntimeError, match="trade_plan_prepare_identity_lost"):
        journal.settle_prepare(PlanReceipt(first, committed=True))


def test_the_day_start_baseline_has_a_fixed_identity_and_round_trips_its_exact_equity() -> None:
    factory = _factory()
    equity = Decimal("1234.567890123456")
    baseline, observation = factory.day_start_baseline(utc_day="2030-03-17", equity_usd=equity, recorded_at_ns=NOW_NS)

    assert observation.event_id == factory.day_start_event_id("2030-03-17") == baseline.event_id
    restored = day_start_baseline_from_observation(observation)
    assert restored.equity_usd == equity and restored.utc_day == "2030-03-17"
    with pytest.raises(ValueError, match="oi_runtime_day_start_equity_precision_invalid"):
        factory.day_start_baseline(utc_day="2030-03-17", equity_usd=Decimal(0), recorded_at_ns=NOW_NS)
