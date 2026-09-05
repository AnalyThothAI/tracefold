from __future__ import annotations

import asyncio

import pytest
from psycopg import OperationalError

from tracefold.app.workers import root as workers_module
from tracefold.app.workers.runtime import MARKET_NOTIFICATIONS, CapabilityStates
from tracefold.app.workers.task_contract import WorkerTask, worker_business_tasks
from tracefold.app.workers.wiring.components import _capability_fault_reason
from tracefold.app.workers.wiring.news import run_market_notifications
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
    ("capability", "failure", "reason"),
    [
        ("news_delivery", RuntimeError("handler bug"), "news_delivery:RuntimeError"),
        ("trading_signal_lane", ValueError("signal bug"), "trading_signal_lane:ValueError"),
        (
            "news_editorial",
            ExceptionGroup("message task failed", [BrokerUnavailable("publish failed")]),
            "news_editorial:news_broker_unavailable",
        ),
    ],
)
def test_a_faulted_capability_reports_what_stopped_it(
    capability: str,
    failure: BaseException,
    reason: str,
) -> None:
    assert _capability_fault_reason(capability, failure) == reason


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
            WorkerTask(
                name="trading-signal-lane",
                capability="trading_signal_lane",
                run=raising,
                foundational=False,
            ),
            stop_event=asyncio.Event(),
            capabilities=capabilities,
            on_fault=on_fault,
        )
    )

    assert capabilities.payload()["trading_signal_lane"] == {
        "state": "faulted",
        "reason": "trading_signal_lane:RuntimeError",
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
                WorkerTask(
                    name="news-quotes",
                    capability="news_quotes",
                    run=raising,
                    foundational=False,
                ),
                stop_event=asyncio.Event(),
                capabilities=capabilities,
                on_fault=on_fault,
            )
        )

    assert capabilities.payload() == {}
    assert workers_module._fatal_code(overrun, phase="runtime") == "resource_operation_overrun"


def test_every_news_ingestion_task_is_foundational_and_every_optional_one_owns_its_key() -> None:
    """#553 PR-3. The information entry is not confinable, and a fault always names one capability."""

    # #553 PR-2's market notification loop is the newest optional task, and it is declared beside the
    # Signal lane rather than through `runners()`. Passing one here is what puts its capability key
    # inside the uniqueness assertion below, where a key reused from another loop would be caught.
    tasks = worker_business_tasks(
        news_pipeline=_AllStagesPipeline(),
        signal_lane=None,
        market_notifications=_StubMarketNotifications(),
    )
    by_name = {task.name: task for task in tasks}

    assert {name for name, task in by_name.items() if task.foundational} == {
        "news-receiver",
        "news-recovery",
        "news-deduper",
        "news-janitor",
    }
    assert by_name["market-notifications"].capability == MARKET_NOTIFICATIONS
    assert by_name["market-notifications"].foundational is False
    optional = [task.capability for task in tasks if not task.foundational]
    assert MARKET_NOTIFICATIONS in optional
    assert optional == sorted(set(optional), key=optional.index)
    assert len(optional) == len(set(optional)), "two optional tasks share one capability key"


def test_the_root_refuses_to_confine_a_foundational_task() -> None:
    async def never(_stop: asyncio.Event) -> None:  # pragma: no cover - never awaited
        return None

    async def on_fault() -> None:  # pragma: no cover - never awaited
        return None

    with pytest.raises(RuntimeError, match="workers_foundational_task_must_not_be_confined"):
        asyncio.run(
            workers_module._run_capability_task(
                WorkerTask(name="news-receiver", capability="news_ingestion", run=never, foundational=True),
                stop_event=asyncio.Event(),
                capabilities=CapabilityStates(),
                on_fault=on_fault,
            )
        )


class _StubMarketNotifications:
    """The loop's shape as the task contract uses it: one `advance()`, one startup sweep."""

    async def start(self) -> int:  # pragma: no cover - the task is declared, never run here
        return 0

    async def advance(self) -> None:  # pragma: no cover - the task is declared, never run here
        return None


class _AllStagesPipeline:
    """A News pipeline that declares every stage, so the task contract is checked whole."""

    @staticmethod
    def runners() -> list[tuple[str, object]]:
        return [
            (name, object())
            for name in (
                "news-receiver",
                "news-recovery",
                "news-deduper",
                "news-triage",
                "news-deliverer",
                "news-janitor",
                "news-instruments",
                "news-quotes",
                "news-reactions",
            )
        ]


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


class _CountingMarketLoop:
    """A market loop that records the order the runner drove it in."""

    def __init__(self, *, fail_start: bool = False, fail_after_turns: int | None = None) -> None:
        self.fail_start = fail_start
        self.fail_after_turns = fail_after_turns
        self.calls: list[str] = []

    async def start(self) -> int:
        self.calls.append("start")
        if self.fail_start:
            raise RuntimeError("sweep_failed")
        return 0

    async def advance(self) -> object:
        self.calls.append("advance")
        if self.fail_after_turns is not None and self.calls.count("advance") >= self.fail_after_turns:
            raise RuntimeError("turn_failed")
        return object()


def test_the_market_runner_sweeps_once_then_ticks_until_the_stop_event() -> None:
    """`run_market_notifications` owns the tick, and the startup sweep is not part of it (#553 PR-2)."""

    loop = _CountingMarketLoop()
    stop = asyncio.Event()

    async def drive() -> None:
        runner = asyncio.create_task(
            run_market_notifications(loop, stop_event=stop, poll_seconds=0.01)  # type: ignore[arg-type]
        )
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(runner, timeout=2.0)

    asyncio.run(drive())
    # Swept exactly once, before any turn, and every turn after it is a tick.
    assert loop.calls[0] == "start"
    assert loop.calls.count("start") == 1
    assert loop.calls.count("advance") >= 1


def test_a_failed_startup_sweep_stops_the_task_rather_than_running_without_it() -> None:
    """A sweep that could not adopt the previous process's in-flight cards must not be skipped.

    Running on would leave a row reading `sending` that no process owns: the next turn would see it
    as in flight for ever and the card would never be settled either way.
    """

    loop = _CountingMarketLoop(fail_start=True)
    stop = asyncio.Event()
    with pytest.raises(RuntimeError, match="sweep_failed"):
        asyncio.run(run_market_notifications(loop, stop_event=stop, poll_seconds=0.01))  # type: ignore[arg-type]
    assert loop.calls == ["start"]


def test_an_unexpected_turn_error_is_raised_rather_than_swallowed() -> None:
    """Every business outcome of a send is a durable row, so an exception here is infrastructure.

    Raising is what makes the Workers root record `market_notifications` faulted and stop the task;
    swallowing it would keep a broken loop ticking behind a green capability.
    """

    loop = _CountingMarketLoop(fail_after_turns=2)
    stop = asyncio.Event()
    with pytest.raises(RuntimeError, match="turn_failed"):
        asyncio.run(run_market_notifications(loop, stop_event=stop, poll_seconds=0.001))  # type: ignore[arg-type]
    assert loop.calls == ["start", "advance", "advance"]
