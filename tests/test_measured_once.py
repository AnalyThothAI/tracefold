from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from tracefold.app.http.exceptions import ApiUnavailable
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


def test_failed_round_is_shared_then_recovery_is_measured() -> None:
    now = [0.0]
    cache = MeasuredOnce(clock=lambda: now[0])
    started, finish = Event(), Event()
    calls = []

    def fail():
        calls.append(1)
        started.set()
        assert finish.wait(5)
        raise ValueError("unavailable")

    with ThreadPoolExecutor(max_workers=8) as pool:
        failed = pool.submit(cache.get, fail)
        assert started.wait(5)
        followers = [pool.submit(cache.get, fail) for _ in range(7)]
        finish.set()
        with pytest.raises(ApiUnavailable):
            failed.result()
        for follower in followers:
            with pytest.raises(ApiUnavailable):
                follower.result()
    assert calls == [1]
    with pytest.raises(ApiUnavailable):
        cache.get(lambda: pytest.fail("negative cache must not measure"))
    now[0] = 1.001
    assert cache.get(lambda: {"recovered": True}) == {"recovered": True}
    assert cache.get(lambda: pytest.fail("already measured")) == {"recovered": True}


def test_hung_leader_does_not_hold_followers() -> None:
    cache = MeasuredOnce(wait_s=0.05)
    started, finish = Event(), Event()

    def measure():
        started.set()
        finish.wait(5)
        return {"ok": True}

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(cache.get, measure)
        assert started.wait(5)
        try:
            with pytest.raises(ApiUnavailable):
                pool.submit(cache.get, measure).result(timeout=0.5)
        finally:
            finish.set()
        assert leader.result() == {"ok": True}


def test_refresh_returns_bounded_stale_value_immediately() -> None:
    now = [0.0]
    cache = MeasuredOnce(clock=lambda: now[0], wait_s=0.05)
    old = cache.get(lambda: {"measured_at_ms": 123})
    now[0] = 30
    started, finish = Event(), Event()

    def refresh():
        started.set()
        finish.wait(5)
        return {"measured_at_ms": 456}

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(cache.get, refresh)
        assert started.wait(5)
        try:
            assert pool.submit(cache.get, refresh).result(timeout=0.5) == old
            now[0] = 60.001
            with pytest.raises(ApiUnavailable):
                pool.submit(cache.get, refresh).result(timeout=0.5)
        finally:
            finish.set()
        assert leader.result() == {"measured_at_ms": 456}


def test_a_measurement_that_finishes_after_the_maximum_age_is_not_cached():
    now = [0.0]
    cache = MeasuredOnce(clock=lambda: now[0])

    def slow():
        now[0] = 61.0
        return {"stale": True}

    with pytest.raises(ApiUnavailable, match="service_busy"):
        cache.get(slow)
    now[0] = 63.0
    assert cache.get(lambda: {"fresh": True}) == {"fresh": True}
