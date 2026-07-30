from __future__ import annotations

from tracefold.platform.workers.worker_base import _successful_iteration_delay


def test_successful_worker_cadence_targets_start_to_start_interval() -> None:
    assert _successful_iteration_delay(interval_seconds=10, duration_seconds=2.5) == 7.5


def test_overrun_worker_skips_missed_ticks_and_waits_for_the_next_cadence_boundary() -> None:
    assert _successful_iteration_delay(interval_seconds=10, duration_seconds=12) == 8
    assert _successful_iteration_delay(interval_seconds=10, duration_seconds=20) == 10
    assert _successful_iteration_delay(interval_seconds=0.25, duration_seconds=2) == 0.25
