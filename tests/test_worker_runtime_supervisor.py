import asyncio
from unittest.mock import patch

from tracefold.app.worker_manifest import all_worker_manifests, worker_names
from tracefold.app.worker_runtime_supervisor import WorkerRuntimeSupervisor


class _FakeWorker:
    def __init__(self, name: str, started: list[str], stopped: list[str]) -> None:
        self.name = name
        self.started = started
        self.stopped = stopped
        self._stop = asyncio.Event()

    async def run(self) -> None:
        self.started.append(self.name)
        await self._stop.wait()

    async def stop(self) -> None:
        self.stopped.append(self.name)
        self._stop.set()

    async def aclose(self) -> None:
        return None

    def status_payload(self) -> dict:
        return {"effective_status": "running"}


def test_supervisor_starts_exact_topology_without_phases_or_iteration_groups():
    async def scenario() -> None:
        started: list[str] = []
        stopped: list[str] = []
        workers = {
            "collector": _FakeWorker("collector", started, stopped),
            "market_tick_stream": _FakeWorker("market_tick_stream", started, stopped),
        }
        supervisor = WorkerRuntimeSupervisor(workers=workers)

        await supervisor.start()
        await asyncio.sleep(0)
        assert started == ["collector", "market_tick_stream"]
        assert not hasattr(supervisor, "startup_phase_delays_seconds")
        assert not hasattr(supervisor, "iteration_gates")

        await supervisor.stop()
        assert stopped == ["collector", "market_tick_stream"]

    asyncio.run(scenario())


def test_steady_worker_topology_has_one_manifest_authority():
    manifests = all_worker_manifests()

    assert worker_names() == tuple(item.name for item in manifests)
    assert len(worker_names()) == len(set(worker_names()))
    assert all(item.name for item in manifests)


def test_supervisor_publishes_status_without_owning_database_connections():
    async def scenario() -> None:
        published: list[dict[str, dict]] = []
        started: list[str] = []
        stopped: list[str] = []
        worker = _FakeWorker("collector", started, stopped)

        async def status_sink(payload: dict[str, dict]) -> None:
            published.append(payload)

        supervisor = WorkerRuntimeSupervisor(
            workers={"collector": worker},
            status_sink=status_sink,
            heartbeat_interval_seconds=60,
        )
        await supervisor.start()
        assert supervisor.readiness()["ok"] is True
        await supervisor.stop()

        assert published
        assert published[0]["collector"]["effective_status"] == "running"

    asyncio.run(scenario())


def test_supervisor_retries_failed_heartbeat_and_recovers_readiness():
    async def scenario() -> None:
        calls = 0
        started: list[str] = []
        stopped: list[str] = []

        async def status_sink(_payload: dict[str, dict]) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("database unavailable")

        supervisor = WorkerRuntimeSupervisor(
            workers={
                "collector": _FakeWorker(
                    "collector",
                    started,
                    stopped,
                )
            },
            status_sink=status_sink,
            heartbeat_interval_seconds=60,
        )
        with patch(
            "tracefold.app.worker_runtime_supervisor._CONTROL_RETRY_SECONDS",
            (0.001, 0.005, 0.015),
        ):
            await supervisor.start()
            assert supervisor.readiness()["ok"] is False
            await asyncio.sleep(0.01)
            assert calls >= 2
            assert supervisor.readiness()["ok"] is True
            last_success = supervisor._last_control_success_monotonic
            assert last_success is not None
            stale = supervisor.readiness(
                now_monotonic=last_success + 15.001,
            )
            assert stale["ok"] is False
            assert stale["reason"] == "heartbeat_stale"
            await supervisor.stop()

    asyncio.run(scenario())


def test_supervisor_terminates_runtime_after_persisted_heartbeat_is_stale():
    async def scenario() -> None:
        started: list[str] = []
        stopped: list[str] = []
        exits: list[str] = []
        exited = asyncio.Event()

        async def status_sink(_payload: dict[str, dict]) -> None:
            raise RuntimeError("database unavailable")

        def fatal_exit(reason: str) -> None:
            exits.append(reason)
            exited.set()

        supervisor = WorkerRuntimeSupervisor(
            workers={
                "collector": _FakeWorker(
                    "collector",
                    started,
                    stopped,
                )
            },
            status_sink=status_sink,
            heartbeat_interval_seconds=60,
            fatal_exit=fatal_exit,
        )
        with (
            patch(
                "tracefold.app.worker_runtime_supervisor._CONTROL_RETRY_SECONDS",
                (0.001, 0.001, 0.001),
            ),
            patch(
                "tracefold.app.worker_runtime_supervisor._CONTROL_STALE_SECONDS",
                0.002,
            ),
        ):
            await supervisor.start()
            await asyncio.wait_for(exited.wait(), timeout=1)

        assert exits == ["RuntimeError: database unavailable"]
        assert supervisor.readiness()["reason"] == "control_loop_stopped"
        assert await supervisor.request_stop() == []
        assert await supervisor.finish(publish_status=False) == []

    asyncio.run(scenario())
