from __future__ import annotations

import asyncio
import time
from concurrent.futures import Future
from contextlib import suppress
from threading import Event, Lock
from typing import Any

import pytest

from tracefold.app import worker_database as database_module
from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers.capabilities import FiniteOperations
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.resource import (
    ResourceAdmissionTimeout,
    ResourceCapability,
    ResourceOperationOverrun,
    await_concurrent_future,
)


async def _rendered_once_active_permits_settle(
    telemetry: TelemetryRegistry,
    capability_label: str,
    *,
    timeout_seconds: float = 2.0,
) -> str:
    """Render the metrics once the accounting the caller is about to assert on has actually landed.

    `change_resource_active(..., -1)` runs inside each future's done-callback, and asyncio schedules
    those with `call_soon`. `await asyncio.sleep(0)` yields exactly one loop iteration, which is a
    guess about how many turns the callback queue needs rather than a barrier for it. The guess held
    on an idle machine — eighty local runs, idle and under load, all read `0.0` — and lost once on a
    CI runner, on a commit that touched only the Makefile and a deploy test. Re-running that exact
    commit passed, which is what makes it a race rather than a regression.

    Waiting for the condition keeps the assertion exactly as strong. A gauge that never returns to
    zero still renders non-zero when the deadline expires, and the caller still fails on it.
    """

    settled = f'tracefold_worker_resource_active{{capability="{capability_label}"}} 0.0'
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while True:
        rendered = telemetry.render_prometheus_text()
        if settled in rendered or loop.time() >= deadline:
            return rendered
        await asyncio.sleep(0.01)


def _sleep_and_return(delay_seconds: float, value: int) -> int:
    time.sleep(delay_seconds)
    return value


class _ExpectedNativeFailure(RuntimeError):
    pass


def _delayed_native_failure(delay_seconds: float) -> None:
    time.sleep(delay_seconds)
    raise _ExpectedNativeFailure("native_failure")


@pytest.mark.parametrize("capability_type", [FiniteOperations])
def test_native_completion_finishes_before_thread_wrapper_watchdog(capability_type: type[Any]) -> None:
    async def scenario() -> None:
        capability = capability_type()
        try:
            with pytest.raises(_ExpectedNativeFailure, match="native_failure"):
                await capability.run(
                    "native_failure",
                    _delayed_native_failure,
                    0.05,
                    timeout_seconds=0.01,
                )
        finally:
            capability.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["result", "error"])
def test_completed_native_future_wins_and_cancels_its_delayed_wrapper(outcome: str) -> None:
    async def scenario() -> None:
        underlying: Future[int] = Future()
        wrapped: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        if outcome == "result":
            underlying.set_result(7)
            assert (
                await await_concurrent_future(
                    underlying,
                    wrapped,
                    timeout_seconds=0.001,
                    capability=ResourceCapability.FINITE_OPERATION,
                    operation_name="test",
                )
                == 7
            )
        else:
            underlying.set_exception(_ExpectedNativeFailure("native_failure"))
            with pytest.raises(_ExpectedNativeFailure, match="native_failure"):
                await await_concurrent_future(
                    underlying,
                    wrapped,
                    timeout_seconds=0.001,
                    capability=ResourceCapability.FINITE_OPERATION,
                    operation_name="test",
                )
        assert wrapped.cancelled()

    asyncio.run(scenario())


def test_finite_permits_follow_underlying_futures_after_callers_time_out() -> None:
    async def scenario() -> str:
        telemetry = TelemetryRegistry()
        capability = FiniteOperations(telemetry=telemetry)
        release = Event()
        all_started = Event()
        submitted = 0
        submitted_lock = Lock()

        def blocking() -> str:
            nonlocal submitted
            with submitted_lock:
                submitted += 1
                if submitted == 3:
                    all_started.set()
            release.wait(timeout=2.0)
            return "done"

        try:
            first: list[asyncio.Task[str]] = [
                asyncio.create_task(
                    capability.run(
                        f"blocking_{index}",
                        blocking,
                        timeout_seconds=0.01,
                    )
                )
                for index in range(3)
            ]
            results = await asyncio.gather(*first, return_exceptions=True)
            assert all(isinstance(result, ResourceOperationOverrun) for result in results)
            assert all_started.wait(timeout=1.0)

            waiting = asyncio.create_task(capability.run("must_wait", blocking, timeout_seconds=0.5))
            await asyncio.sleep(0.05)
            assert not waiting.done()
            assert submitted == 3
            waiting.cancel()
            with suppress(asyncio.CancelledError):
                await waiting

            release.set()
            assert await capability.drain(timeout_seconds=1.0)
            await asyncio.sleep(0)
            assert (
                await capability.run(
                    "after_release",
                    lambda: "released",
                    timeout_seconds=0.5,
                )
                == "released"
            )
            return await _rendered_once_active_permits_settle(telemetry, "finite_operation")
        finally:
            release.set()
            capability.close()

    metrics = asyncio.run(scenario())
    assert 'tracefold_worker_resource_active{capability="finite_operation"} 0.0' in metrics
    assert "tracefold_worker_resource_admission_seconds_count" in metrics
    assert "tracefold_worker_resource_service_seconds_count" in metrics


def test_finite_awaits_durable_fence_before_submitting_thread_work() -> None:
    async def scenario() -> None:
        capability = FiniteOperations()
        events: list[str] = []

        async def persist_fence() -> None:
            events.append("fenced")

        def operation() -> str:
            assert events == ["fenced"]
            events.append("submitted")
            return "done"

        try:
            assert (
                await capability.run(
                    "fenced_operation",
                    operation,
                    timeout_seconds=0.5,
                    before_submit=persist_fence,
                )
                == "done"
            )
            assert events == ["fenced", "submitted"]
        finally:
            capability.close()

    asyncio.run(scenario())


def test_story_projection_diagnostics_are_bounded_labelled_gauges() -> None:
    telemetry = TelemetryRegistry()

    telemetry.set_news_story_projection_value("candidate_pair_count", 7)
    telemetry.set_news_story_projection_value("event_family_general", 3)

    rendered = telemetry.render_prometheus_text()
    assert 'tracefold_news_story_projection_value{measure="candidate_pair_count"} 7.0' in rendered
    assert 'tracefold_news_story_projection_value{measure="event_family_general"} 3.0' in rendered


def test_external_data_metrics_use_bounded_labels_and_a_live_success_age(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tracefold.platform.observability.telemetry.time.time", lambda: 130.0)
    telemetry = TelemetryRegistry()

    telemetry.record_external_data_turn(
        "quote_snapshot",
        "success",
        0.25,
        target_count=7,
        source_count=2,
        timestamp=123.0,
    )
    telemetry.record_external_data_turn("quote_snapshot", "error", 0.5)
    telemetry.record_external_data_provider_call("quote_snapshot", "binance_spot", "success", 0.1)
    telemetry.record_external_data_skipped("event_reaction", "coalesced")

    rendered = telemetry.render_prometheus_text()
    assert 'tracefold_external_data_turn_total{name="quote_snapshot",outcome="success"} 1.0' in rendered
    assert 'tracefold_external_data_turn_total{name="quote_snapshot",outcome="error"} 1.0' in rendered
    assert 'tracefold_external_data_target_count{name="quote_snapshot"} 7.0' in rendered
    assert 'tracefold_external_data_source_count{name="quote_snapshot"} 2.0' in rendered
    assert 'tracefold_external_data_last_success_age_seconds{name="quote_snapshot"} 7.0' in rendered
    assert (
        'tracefold_external_data_provider_call_total{name="quote_snapshot",outcome="success",source="binance_spot"}'
        " 1.0" in rendered
    )
    assert (
        'tracefold_external_data_skipped_or_coalesced_total{name="event_reaction",reason="coalesced"} 1.0' in rendered
    )

    with pytest.raises(ValueError, match="external_data_name_invalid"):
        telemetry.record_external_data_turn("BTCUSDT", "success", 0.1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="external_data_source_invalid"):
        telemetry.record_external_data_provider_call(
            "quote_snapshot",
            "https://provider.example/BTCUSDT",
            "success",
            0.1,
        )  # type: ignore[arg-type]


def test_news_durable_event_metrics_use_only_bounded_labels() -> None:
    telemetry = TelemetryRegistry()

    telemetry.set_news_handoff_state("event", pending=2, oldest_age_seconds=5.0, expired=3)
    telemetry.record_news_handoff_repair("event", "published")
    telemetry.record_news_rabbitmq_consumer_fatal("news.raw", "handler")
    telemetry.set_news_opennews_incident(
        provider="opennews",
        cause="broker_unavailable",
        count=1,
        oldest_age_seconds=9.0,
    )
    telemetry.record_news_opennews_recovery_turn(
        "budget",
        provider_calls=4,
        published_messages=7,
        exhausted_budget="provider_calls",
    )

    rendered = telemetry.render_prometheus_text()
    assert 'tracefold_news_handoff_pending{stage="event"} 2.0' in rendered
    assert 'tracefold_news_handoff_expired_total{stage="event"} 3.0' in rendered
    assert 'tracefold_news_handoff_repair_total{outcome="published",stage="event"} 1.0' in rendered
    assert 'tracefold_news_rabbitmq_consumer_fatal_total{queue="news.raw",reason_class="handler"} 1.0' in rendered
    assert 'tracefold_news_opennews_incident_open{cause="broker_unavailable",provider="opennews"} 1.0' in rendered
    assert 'tracefold_news_opennews_recovery_turn_total{outcome="budget"} 1.0' in rendered
    assert "tracefold_news_opennews_recovery_provider_calls_total 4.0" in rendered
    assert "tracefold_news_opennews_recovery_published_messages_total 7.0" in rendered
    assert 'tracefold_news_opennews_recovery_budget_exhaustion_total{budget="provider_calls"} 1.0' in rendered

    with pytest.raises(ValueError, match="news_handoff_stage_invalid"):
        telemetry.set_news_handoff_state("BTC", pending=1, oldest_age_seconds=1, expired=0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="news_recovery_budget_invalid"):
        telemetry.record_news_opennews_recovery_turn(
            "budget",
            provider_calls=0,
            published_messages=0,
            exhausted_budget="incident-42",  # type: ignore[arg-type]
        )


def test_finite_does_not_submit_when_durable_fence_fails() -> None:
    class FenceFailure(RuntimeError):
        pass

    async def scenario() -> None:
        capability = FiniteOperations()
        submitted = False

        async def reject_fence() -> None:
            raise FenceFailure("fence_failed")

        def operation() -> None:
            nonlocal submitted
            submitted = True

        try:
            with pytest.raises(FenceFailure, match="fence_failed"):
                await capability.run(
                    "rejected_fenced_operation",
                    operation,
                    timeout_seconds=0.5,
                    before_submit=reject_fence,
                )
            assert submitted is False
            assert (
                await capability.run(
                    "after_rejected_fence",
                    lambda: "available",
                    timeout_seconds=0.5,
                )
                == "available"
            )
        finally:
            capability.close()

    asyncio.run(scenario())


def test_database_has_two_business_slots_and_an_independent_control_slot() -> None:
    async def scenario() -> None:
        database = WorkerDatabase(worker_pool=object(), telemetry=None)
        release = Event()
        both_started = Event()
        submitted = 0
        submitted_lock = Lock()

        def blocking() -> int:
            nonlocal submitted
            with submitted_lock:
                submitted += 1
                if submitted == 2:
                    both_started.set()
            release.wait()
            return 1

        try:
            first: list[asyncio.Task[int]] = [
                asyncio.create_task(
                    database.run_business(
                        f"business_{index}",
                        blocking,
                        operation_timeout_seconds=0.01,
                    )
                )
                for index in range(2)
            ]
            results = await asyncio.gather(*first, return_exceptions=True)
            assert all(isinstance(result, ResourceOperationOverrun) for result in results)
            assert both_started.wait(timeout=1.0)
            assert (
                await database.run_control(
                    "control",
                    lambda: 7,
                    operation_timeout_seconds=0.5,
                )
                == 7
            )

            waiting: asyncio.Task[int] = asyncio.create_task(
                database.run_business(
                    "third_business",
                    blocking,
                    operation_timeout_seconds=0.5,
                )
            )
            await asyncio.sleep(0.05)
            assert not waiting.done()
            assert submitted == 2
            waiting.cancel()
            with suppress(asyncio.CancelledError):
                await waiting

            release.set()
            assert await database.drain_business(timeout_seconds=1.0)
            await asyncio.sleep(0)
            assert (
                await database.run_business(
                    "after_release",
                    lambda: 9,
                    operation_timeout_seconds=0.5,
                )
                == 9
            )
        finally:
            release.set()
            database.close_executors()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fact_operation",
    ("gmgn_event_publish", "opennews_live_publish", "macro_publish_success"),
)
def test_coincident_heavy_database_operations_do_not_jointly_fill_both_business_slots(
    fact_operation: str,
) -> None:
    async def scenario() -> None:
        database = WorkerDatabase(worker_pool=object(), telemetry=None)
        heavy = database.heavy_business()
        release_first = Event()
        first_submitted = asyncio.Event()
        second_submitted = asyncio.Event()

        def first_heavy() -> str:
            release_first.wait(timeout=2.0)
            return "brief"

        def second_heavy() -> str:
            return "story"

        try:
            first = asyncio.create_task(
                heavy.run_business(
                    "brief_load",
                    first_heavy,
                    operation_timeout_seconds=0.5,
                    on_submitted=first_submitted.set,
                )
            )
            await asyncio.wait_for(first_submitted.wait(), timeout=1.0)
            second = asyncio.create_task(
                heavy.run_business(
                    "story_load",
                    second_heavy,
                    operation_timeout_seconds=0.5,
                    on_submitted=second_submitted.set,
                )
            )
            await asyncio.sleep(0.05)
            assert not second_submitted.is_set()

            fact = asyncio.create_task(
                database.run_business(
                    fact_operation,
                    lambda: "fact-written",
                    operation_timeout_seconds=0.5,
                )
            )
            assert await asyncio.wait_for(fact, timeout=0.2) == "fact-written"

            release_first.set()
            assert await first == "brief"
            assert await second == "story"
            assert second_submitted.is_set()
        finally:
            release_first.set()
            await database.drain_business(timeout_seconds=1.0)
            database.close_executors()

    asyncio.run(scenario())


def test_hung_heavy_database_operation_keeps_bulkhead_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        database = WorkerDatabase(worker_pool=object(), telemetry=None)
        heavy = database.heavy_business()
        release = Event()
        first_started = Event()
        second_started = Event()

        def first_heavy() -> None:
            first_started.set()
            release.wait(timeout=2.0)

        def second_heavy() -> None:
            second_started.set()
            release.wait(timeout=2.0)

        try:
            first = asyncio.create_task(
                heavy.run_business(
                    "brief_load",
                    first_heavy,
                    operation_timeout_seconds=0.001,
                )
            )
            with pytest.raises(ResourceOperationOverrun) as raised:
                await first
            assert raised.value.capability is ResourceCapability.DATABASE_BUSINESS
            assert first_started.wait(timeout=1.0)

            second = asyncio.create_task(
                heavy.run_business(
                    "story_load",
                    second_heavy,
                    operation_timeout_seconds=0.5,
                )
            )
            with pytest.raises(ResourceAdmissionTimeout):
                await second
            assert not second_started.is_set()

            assert (
                await database.run_business(
                    "gmgn_event_publish",
                    lambda: "fact-written",
                    operation_timeout_seconds=0.5,
                )
                == "fact-written"
            )
        finally:
            release.set()
            await database.drain_business(timeout_seconds=1.0)
            database.close_executors()

    monkeypatch.setattr(
        database_module,
        "_WORKER_BUSINESS_OPERATION_COMPLETION_GRACE_SECONDS",
        0.001,
    )
    monkeypatch.setattr(
        database_module,
        "_WORKER_HEAVY_ADMISSION_TIMEOUT_SECONDS",
        0.05,
    )
    asyncio.run(scenario())


def test_database_native_transaction_timeout_precedes_outer_overrun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NativeTransactionTimeout(RuntimeError):
        pass

    async def scenario() -> None:
        database = WorkerDatabase(worker_pool=object(), telemetry=None)

        def native_timeout() -> None:
            time.sleep(0.02)
            raise _NativeTransactionTimeout("native transaction timeout")

        try:
            with pytest.raises(_NativeTransactionTimeout, match="native transaction timeout"):
                await database.run_business(
                    "native_timeout_precedes_outer",
                    native_timeout,
                    operation_timeout_seconds=0.01,
                )
        finally:
            database.close_executors()

    monkeypatch.setattr(
        database_module,
        "_WORKER_BUSINESS_OPERATION_COMPLETION_GRACE_SECONDS",
        0.02,
    )
    asyncio.run(scenario())


def test_database_outer_grace_exceeds_worker_transaction_timeout_margin() -> None:
    assert (
        database_module._WORKER_BUSINESS_OPERATION_COMPLETION_GRACE_SECONDS
        > database_module._WORKER_TRANSACTION_TIMEOUT_MARGIN_SECONDS
    )
    assert (
        database_module._WORKER_CONTROL_OPERATION_COMPLETION_GRACE_SECONDS
        > database_module._WORKER_TRANSACTION_TIMEOUT_MARGIN_SECONDS
    )


def test_late_wrapped_failure_after_overrun_is_retrieved() -> None:
    async def scenario() -> list[dict[str, object]]:
        loop = asyncio.get_running_loop()
        underlying: Future[int] = Future()
        wrapped = asyncio.wrap_future(underlying)
        unhandled: list[dict[str, object]] = []
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))

        with pytest.raises(ResourceOperationOverrun, match="late_native_failure"):
            await await_concurrent_future(
                underlying,
                wrapped,
                timeout_seconds=0.001,
                capability=ResourceCapability.FINITE_OPERATION,
                operation_name="late_native_failure",
            )
        underlying.set_exception(_ExpectedNativeFailure("late native failure"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        del wrapped
        await asyncio.sleep(0)
        return unhandled

    assert asyncio.run(scenario()) == []


def test_late_wrapped_failure_after_caller_cancellation_is_retrieved() -> None:
    async def scenario() -> list[dict[str, object]]:
        loop = asyncio.get_running_loop()
        underlying: Future[int] = Future()
        wrapped = asyncio.wrap_future(underlying)
        unhandled: list[dict[str, object]] = []
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        waiting = asyncio.create_task(
            await_concurrent_future(
                underlying,
                wrapped,
                timeout_seconds=1.0,
                capability=ResourceCapability.FINITE_OPERATION,
                operation_name="cancelled_caller",
            )
        )

        await asyncio.sleep(0)
        waiting.cancel()
        with suppress(asyncio.CancelledError):
            await waiting
        underlying.set_exception(_ExpectedNativeFailure("late native failure"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        del wrapped
        await asyncio.sleep(0)
        return unhandled

    assert asyncio.run(scenario()) == []


# ---------------------------------------------------------------------------- #88 price loop composition
def _settings(**venues: Any):
    from tracefold.platform.config.models import Settings

    return Settings(ws_token="wiring-token", news={"enabled": True, "venues": venues or {}})


def test_price_loops_are_wired_per_source_and_follow_the_existing_venue_switches() -> None:
    """The composition root is the only place these adapters are chosen; nothing here calls a venue."""

    from tracefold.app.workers.wiring.market_review import _event_reaction_loop, _quote_snapshot_loop

    settings = _settings()
    quotes = _quote_snapshot_loop(settings, db=_UncalledDatabasePort(), watchlist=["BTC"])
    reactions = _event_reaction_loop(settings, db=_UncalledDatabasePort())
    assert quotes is not None and reactions is not None

    # One adapter per provider source, including a HIP-3 dex nobody wired by hand.
    assert quotes.fetcher_for("binance.spot") is not None
    assert quotes.fetcher_for("binance.perp") is not None
    assert quotes.fetcher_for("hl.perp") is not None
    assert quotes.fetcher_for("hl.brandnewdex") is not None
    assert quotes.fetcher_for("us.listed") is None  # a reference tier is never a price source

    # The wide day endpoint exists for exactly the venues that publish the day change separately (#109).
    assert quotes.day_fetcher_for("binance.spot") is not None
    assert quotes.day_fetcher_for("binance.perp") is not None
    assert quotes.day_fetcher_for("hl.perp") is None  # one Hyperliquid request already carries prevDayPx
    assert quotes.day_fetcher_for("hl.brandnewdex") is None
    assert quotes.day_fetcher_for("us.listed") is None

    assert reactions.fetcher_for("binance.perp") is not None
    assert reactions.fetcher_for("hl.spot") is not None
    assert reactions.fetcher_for("us.listed") is None


def test_a_disabled_venue_removes_its_adapter_rather_than_failing_the_turn() -> None:
    from tracefold.app.workers.wiring.market_review import _event_reaction_loop, _quote_snapshot_loop

    binance_only = _settings(binance=True, hyperliquid=False)
    quotes = _quote_snapshot_loop(binance_only, db=_UncalledDatabasePort(), watchlist=[])
    assert quotes is not None
    assert quotes.fetcher_for("binance.perp") is not None
    assert quotes.fetcher_for("hl.perp") is None

    hyperliquid_only = _settings(binance=False, hyperliquid=True)
    partial = _quote_snapshot_loop(hyperliquid_only, db=_UncalledDatabasePort(), watchlist=[])
    assert partial is not None
    assert partial.day_fetcher_for("binance.perp") is None  # the venue switch reaches both factories

    off = _settings(enabled=False)
    assert _quote_snapshot_loop(off, db=_UncalledDatabasePort(), watchlist=[]) is None
    assert _event_reaction_loop(off, db=_UncalledDatabasePort()) is None


class _UncalledDatabasePort:
    """Sentinel: these adapter-selection tests never execute database work."""
