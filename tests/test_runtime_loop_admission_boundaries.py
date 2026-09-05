from __future__ import annotations

import asyncio

import pytest
from psycopg import OperationalError

from tracefold.app.workers import root as workers_module
from tracefold.app.workers.runtime import CapabilityStates
from tracefold.app.workers.task_contract import WorkerTask
from tracefold.app.workers.wiring.components import _capability_fault_reason
from tracefold.news.bus import BrokerUnavailable
from tracefold.platform.resource import ResourceCapability, ResourceOperationOverrun


def test_fatal_code_uses_typed_overrun_instead_of_error_text() -> None:
    assert (
        workers_module._fatal_code(
            RuntimeError("resource_operation_overrun:database_business:forged"),
            phase="runtime",
        )
        == "child_failed"
    )
    assert (
        workers_module._fatal_code(
            ResourceOperationOverrun(
                capability=ResourceCapability.FINITE_OPERATION,
                operation_name="typed",
            ),
            phase="runtime",
        )
        == "resource_operation_overrun"
    )


@pytest.mark.parametrize(
    ("task_name", "failure", "reason"),
    [
        ("news-deduper", RuntimeError("handler bug"), "news-deduper:RuntimeError"),
        ("trading-signal-lane", ValueError("signal bug"), "trading-signal-lane:ValueError"),
        (
            "news-triage",
            ExceptionGroup("message task failed", [BrokerUnavailable("publish failed")]),
            "news-triage:news_broker_unavailable",
        ),
    ],
)
def test_a_faulted_task_reports_what_stopped_it(
    task_name: str,
    failure: BaseException,
    reason: str,
) -> None:
    assert _capability_fault_reason(task_name, failure) == reason


def test_a_business_task_program_error_faults_only_its_own_capability() -> None:
    """#553 PR-3. The task stops and says so; the process root never sees the exception."""

    capabilities = CapabilityStates()
    capabilities.running("trading_signal_lane")
    capabilities.running("news_ingestion")
    faults: list[None] = []

    async def raising(_stop: asyncio.Event) -> None:
        raise RuntimeError("lane bug")

    async def on_fault() -> None:
        faults.append(None)

    asyncio.run(
        workers_module._run_capability_task(
            WorkerTask(name="trading-signal-lane", capability="trading_signal_lane", run=raising),
            stop_event=asyncio.Event(),
            capabilities=capabilities,
            on_fault=on_fault,
        )
    )

    assert capabilities.payload()["trading_signal_lane"] == {
        "state": "faulted",
        "reason": "trading-signal-lane:RuntimeError",
    }
    assert capabilities.payload()["news_ingestion"] == {"state": "running", "reason": None}
    assert faults == [None]


def test_a_shared_resource_overrun_inside_a_business_task_is_still_root_fatal() -> None:
    """Confining program errors is not deleting the foundation checks (#553 PR-3)."""

    overrun = ResourceOperationOverrun(
        capability=ResourceCapability.FINITE_OPERATION,
        operation_name="never_returns",
    )
    capabilities = CapabilityStates()

    async def raising(_stop: asyncio.Event) -> None:
        raise overrun

    async def on_fault() -> None:
        raise AssertionError("an overrun must not be confined to a capability")

    with pytest.raises(ResourceOperationOverrun):
        asyncio.run(
            workers_module._run_capability_task(
                WorkerTask(name="news-deduper", capability="news_ingestion", run=raising),
                stop_event=asyncio.Event(),
                capabilities=capabilities,
                on_fault=on_fault,
            )
        )

    assert capabilities.payload() == {}
    assert workers_module._fatal_code(overrun, phase="runtime") == "resource_operation_overrun"


def test_control_loop_retries_a_transient_pooled_heartbeat_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    waits: list[float] = []

    async def heartbeat(**_kwargs: object) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalError("server closed the pooled connection")
        stop_event.set()
        return 2_000

    async def wait(_stop_event: asyncio.Event, seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(workers_module, "_control_liveness_and_heartbeat", heartbeat)
    monkeypatch.setattr(workers_module, "_wait_or_stop", wait)
    stop_event = asyncio.Event()
    probe_state = workers_module._ProbeState(
        runtime_id="runtime-control-retry",
        runtime_version="v2",
        started_at_ms=1_000,
        clock_ms=lambda: 2_000,
    )

    asyncio.run(
        workers_module._run_control(
            db=object(),  # type: ignore[arg-type]
            lock_conn=object(),
            runtime_id=probe_state.runtime_id,
            probe_state=probe_state,
            stop_event=stop_event,
        )
    )

    assert calls == 2
    assert waits == [workers_module._CONTROL_RETRY_SECONDS, workers_module._HEARTBEAT_SECONDS]
    assert probe_state.heartbeat_at_ms == 2_000


def test_control_typed_overrun_reaches_the_root_without_control_wrapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overrun = ResourceOperationOverrun(
        capability=ResourceCapability.DATABASE_CONTROL,
        operation_name="workers_runtime_heartbeat",
    )

    async def heartbeat(**_kwargs: object) -> int:
        raise overrun

    monkeypatch.setattr(workers_module, "_control_liveness_and_heartbeat", heartbeat)
    probe_state = workers_module._ProbeState(
        runtime_id="runtime-control-overrun",
        runtime_version="v2",
        started_at_ms=1_000,
        clock_ms=lambda: 2_000,
    )

    with pytest.raises(ResourceOperationOverrun) as caught:
        asyncio.run(
            workers_module._run_control(
                db=object(),  # type: ignore[arg-type]
                lock_conn=object(),
                runtime_id=probe_state.runtime_id,
                probe_state=probe_state,
                stop_event=asyncio.Event(),
            )
        )

    assert caught.value is overrun
    assert workers_module._fatal_code(caught.value, phase="runtime") == "resource_operation_overrun"


def test_persistent_transient_heartbeat_failures_degrade_readiness_until_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def heartbeat(**_kwargs: object) -> int:
        raise OperationalError("server keeps closing the pooled connection")

    async def scenario() -> None:
        stop_event = asyncio.Event()
        probe_state = workers_module._ProbeState(
            runtime_id="runtime-control-watchdog",
            runtime_version="v2",
            started_at_ms=1_000,
            clock_ms=lambda: 2_000,
            lifecycle_state="running",
            heartbeat_at_ms=1_000,
            ready=True,
            unavailable_reason="",
        )
        control = asyncio.create_task(
            workers_module._run_control(
                db=object(),  # type: ignore[arg-type]
                lock_conn=object(),
                runtime_id=probe_state.runtime_id,
                probe_state=probe_state,
                stop_event=stop_event,
            )
        )
        await asyncio.sleep(0.05)
        assert not control.done()
        assert probe_state.payload()["unavailable_reason"] == "runtime_heartbeat_stale"
        stop_event.set()
        await control

    monkeypatch.setattr(workers_module, "_control_liveness_and_heartbeat", heartbeat)
    monkeypatch.setattr(workers_module, "_CONTROL_RETRY_SECONDS", 0.05)
    monkeypatch.setattr(workers_module, "_CONTROL_HEARTBEAT_STALE_SECONDS", 0.01)

    asyncio.run(scenario())
