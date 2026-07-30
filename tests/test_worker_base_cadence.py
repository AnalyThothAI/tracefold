from __future__ import annotations

from tracefold.platform.workers.worker_base import _successful_iteration_delay


def test_successful_worker_cadence_targets_start_to_start_interval() -> None:
    assert _successful_iteration_delay(interval_seconds=10, duration_seconds=2.5) == 7.5


def test_overrun_worker_catch_up_keeps_a_bounded_non_busy_wait() -> None:
    assert _successful_iteration_delay(interval_seconds=10, duration_seconds=12) == 1
    assert _successful_iteration_delay(interval_seconds=0.25, duration_seconds=2) == 0.25
