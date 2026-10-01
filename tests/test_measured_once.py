from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from tracefold.app.serve_runtime import MeasuredOnce


def test_measurement_single_flight_and_ttl() -> None:
    now = [0.0]
    cache = MeasuredOnce(clock=lambda: now[0])
    started, finish = Event(), Event()
    calls = []

    def measure():
        calls.append(1)
        started.set()
        assert finish.wait(5)
        return {"measured_at_ms": 1000, "calls": len(calls)}

    with ThreadPoolExecutor(max_workers=8) as pool:
        first = pool.submit(cache.get, measure)
        assert started.wait(5)
        others = [pool.submit(cache.get, measure) for _ in range(7)]
        finish.set()
        assert [first.result(), *(future.result() for future in others)] == [{"measured_at_ms": 1000, "calls": 1}] * 8
    now[0] = 29.999
    assert cache.get(measure)["calls"] == 1
    now[0] = 30
    assert cache.get(measure)["calls"] == 2


def test_failed_measurement_is_not_cached_and_unblocks_waiters() -> None:
    cache = MeasuredOnce()
    started, finish = Event(), Event()

    def fail():
        started.set()
        assert finish.wait(5)
        raise ValueError("unavailable")

    with ThreadPoolExecutor(max_workers=2) as pool:
        failed = pool.submit(cache.get, fail)
        assert started.wait(5)
        next_call = pool.submit(cache.get, lambda: {"recovered": True})
        finish.set()
        with pytest.raises(ValueError, match="unavailable"):
            failed.result()
        assert next_call.result() == {"recovered": True}
    assert cache.get(lambda: pytest.fail("already measured")) == {"recovered": True}
