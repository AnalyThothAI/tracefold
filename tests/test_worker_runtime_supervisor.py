import asyncio

from tracefold.app.worker_manifest import worker_names
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


def test_steady_worker_topology_is_exact_and_code_owned():
    assert worker_names() == (
        "collector",
        "market_tick_stream",
        "market_tick_poll",
        "event_anchor_capture",
        "resolution_refresh",
        "macro_intraday_market",
        "macro_settlements",
        "macro_economic_releases",
        "macro_official_state",
        "macro_official_documents",
        "news_ingest",
        "asset_profile_refresh",
        "token_image_mirror",
        "steady_projection_coordinator",
        "model_generation_coordinator",
    )


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
        await supervisor.stop()

        assert published
        assert published[0]["collector"]["effective_status"] == "running"

    asyncio.run(scenario())
