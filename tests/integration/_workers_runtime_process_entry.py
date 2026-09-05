from __future__ import annotations

import argparse
import asyncio
import os
import threading
import time
from pathlib import Path
from typing import Any

RUNTIME_MANIFEST_BARRIER_SHA = "a" * 64


def _ignore_sigterm_and_sleep(delay_seconds: float) -> None:
    import signal
    import time

    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(delay_seconds)


def _return_one() -> int:
    return 1


def _native_transaction_timeout(db: Any) -> None:
    with db.worker_session(
        "test_native_transaction_timeout",
        statement_timeout_seconds=1.0,
    ) as repos:
        repos.conn.execute("SELECT 1")
        time.sleep(0.12)
        repos.conn.execute("SELECT 1")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--graceful-timeout-seconds", type=float)
    parser.add_argument(
        "--mode",
        required=True,
        choices=(
            "inert",
            "manifest_barrier",
            "manifest_registration_fault",
            "optional_task_fault",
            "ingestion_task_fault",
            "trading_lane_fault",
            "schema_mismatch",
            "finite_overrun",
            "finite_never_returns",
            "finite_never_returns_failed_transition_once",
            "control_overrun",
            "control_native_timeout",
            "control_transient_startup",
            "control_transient_startup_persistent",
            "control_transient_runtime",
            "shutdown_stopping_control_never_returns",
            "provider_publication",
            "trading_enabled",
            "trading_execution_requested",
            "trading_wiring_fault",
        ),
    )
    return parser.parse_args()


class _TurnPipeline:
    """Carry the test turns through the runtime's business-task slots.

    Turns run under real News task names because the Workers task contract maps a task name onto the
    capability it answers for, and refuses a name it does not know. `news-deduper` is the admission
    task: the one whose continued fact writes are the point of every confinement test here.
    """

    def __init__(self, turns: tuple[tuple[str, Any, float], ...]) -> None:
        self._turns = turns

    @property
    def runtime_manifest_sha(self) -> str | None:
        return None

    async def register_runtime_manifest(self) -> None:
        return None

    def runners(self) -> list[tuple[str, Any]]:
        return [(name, _turn_runner(turn, idle_seconds)) for name, turn, idle_seconds in self._turns]

    def disable_editorial(self) -> None:
        return None

    async def drain(self) -> None:
        return None

    async def close(self) -> None:
        return None


class _RegistrationFaultPipeline(_TurnPipeline):
    """A pipeline whose editorial Program manifest cannot be registered."""

    async def register_runtime_manifest(self) -> None:
        print("MANIFEST_REGISTRATION_ABOUT_TO_FAIL", flush=True)
        raise RuntimeError("test_manifest_registration_fault")


class _ManifestBarrierPipeline(_TurnPipeline):
    def __init__(self, *, dsn: str, release_gate: Path) -> None:
        super().__init__(())
        self._dsn = dsn
        self._release_gate = release_gate

    @property
    def runtime_manifest_sha(self) -> str:
        return RUNTIME_MANIFEST_BARRIER_SHA

    async def register_runtime_manifest(self) -> None:
        Path(f"{self._release_gate}.entered").touch()
        while not self._release_gate.exists():
            await asyncio.sleep(0.01)

        from psycopg import connect
        from psycopg.rows import dict_row

        from tracefold.app.repository_session import repositories_for_connection
        from tracefold.news.program.runtime import PROGRAM_VERSION

        conn = connect(self._dsn, row_factory=dict_row)
        try:
            repositories = repositories_for_connection(conn)
            with repositories.transaction():
                repositories.news.register_agent_runtime_manifest(
                    manifest_sha=RUNTIME_MANIFEST_BARRIER_SHA,
                    stable_bundle_sha="b" * 64,
                    envelope_sha256="e" * 64,
                    artifact_schema_version="news_program_strategy_artifact_v1",
                    program_version=PROGRAM_VERSION,
                    program_sha256="f" * 64,
                    candidate_shas=(),
                    image_digest="sha256:" + "c" * 64,
                    runtime_revision="git:test-manifest-barrier",
                    now_ms=int(time.time() * 1_000),
                )
        finally:
            conn.close()


def _fact_writer(db: Any) -> Any:
    """A business task that keeps committing real facts, so a confined fault can be measured."""

    def insert() -> None:
        with db.worker_session("test_fact_write") as repos, repos.transaction():
            repos.conn.execute("INSERT INTO worker_runtime_test_facts(id) VALUES (DEFAULT)")

    async def turn() -> bool:
        await db.run_business("test_fact_write", insert, operation_timeout_seconds=2.0)
        return True

    return turn


def _backlog_consumer(db: Any, *, resume_gate: Path) -> Any:
    """The optional task standing in for #553 PR-2's market notification loop.

    Its first run raises, which must stop this task alone. Its PostgreSQL backlog keeps growing while
    it is stopped, and the operator restart -- the only recovery this PR offers -- drains it.
    """

    def drain() -> int:
        with db.worker_session("test_backlog_drain") as repos, repos.transaction():
            row = repos.conn.execute(
                "UPDATE worker_runtime_test_backlog SET processed = true WHERE NOT processed RETURNING id"
            ).fetchall()
            return len(row)

    def enqueue() -> None:
        with db.worker_session("test_backlog_enqueue") as repos, repos.transaction():
            repos.conn.execute("INSERT INTO worker_runtime_test_backlog(id) VALUES (DEFAULT)")

    async def turn() -> bool:
        await db.run_business("test_backlog_enqueue", enqueue, operation_timeout_seconds=2.0)
        if not resume_gate.exists():
            print("OPTIONAL_TASK_ABOUT_TO_FAIL", flush=True)
            raise RuntimeError("test_optional_task_fault")
        drained = await db.run_business("test_backlog_drain", drain, operation_timeout_seconds=2.0)
        print(f"BACKLOG_DRAINED {drained}", flush=True)
        return True

    return turn


def _declare_news_capabilities(
    capabilities: Any,
    *,
    quotes: bool = False,
    delivery: str = "disabled",
) -> None:
    """Stand in for what the real News composition declares about the tasks it just wired."""

    from tracefold.app.workers.runtime import (
        NEWS_DELIVERY,
        NEWS_EDITORIAL,
        NEWS_INGESTION,
        NEWS_QUOTES,
    )

    capabilities.running(NEWS_INGESTION)
    capabilities.running(NEWS_EDITORIAL)
    capabilities.declare(
        NEWS_DELIVERY,
        delivery,
        reason=(
            "news_item_push_telegram_bot_token_unavailable"
            if delivery == "unavailable"
            else "news_item_push_not_requested"
        ),
    )
    if quotes:
        capabilities.running(NEWS_QUOTES)
    else:
        capabilities.disabled(NEWS_QUOTES, "news_quotes_not_configured")


def _idle_turn() -> Any:
    """A task that runs and does nothing, so only its presence is under test."""

    async def turn() -> bool:
        return False

    return turn


def _turn_runner(turn: Any, idle_seconds: float) -> Any:
    async def run(stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            progressed = await turn()
            delay = 0.25 if progressed else float(idle_seconds)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except TimeoutError:
                continue

    return run


def _components(
    workers_module: Any,
    *,
    due_turns: tuple[tuple[str, Any, float], ...],
) -> Any:
    return workers_module._Components(
        news_pipeline=_TurnPipeline(due_turns),
        news_bus=None,
    )


async def _main() -> None:
    arguments = _arguments()
    from tracefold.app.workers import root as workers
    from tracefold.app.workers.wiring import components as workers_wiring
    from tracefold.platform.config.models import Settings

    workers._WORKER_INTERNAL_PORT = arguments.port
    workers._HEARTBEAT_SECONDS = 0.1
    if arguments.graceful_timeout_seconds is not None:
        workers.GRACEFUL_DRAIN_TIMEOUT_SECONDS = arguments.graceful_timeout_seconds

    if arguments.mode == "finite_never_returns_failed_transition_once":
        from psycopg import OperationalError

        original_runtime_transition = workers._runtime_transition
        workers.GRACEFUL_DRAIN_TIMEOUT_SECONDS = 1.0
        workers._CONTROL_RETRY_SECONDS = 0.05
        failed_transition_attempts = 0

        def transient_failed_transition(
            db: Any,
            runtime_id: str,
            lifecycle_state: Any,
            fatal_code: Any,
        ) -> None:
            nonlocal failed_transition_attempts
            if lifecycle_state == "failed" and failed_transition_attempts == 0:
                failed_transition_attempts += 1
                print("FAILED_TRANSITION_TRANSIENT", flush=True)
                raise OperationalError("test transient fatal transition")
            original_runtime_transition(db, runtime_id, lifecycle_state, fatal_code)

        workers._runtime_transition = transient_failed_transition

    if arguments.mode in {
        "control_transient_startup",
        "control_transient_startup_persistent",
    }:
        from psycopg import OperationalError

        original_runtime_heartbeat = workers._runtime_heartbeat
        transient_failures = 0
        transient_lock = threading.Lock()
        persistent = arguments.mode == "control_transient_startup_persistent"
        if persistent:
            workers._CONTROL_RETRY_SECONDS = 0.05

        def transient_runtime_heartbeat(*args: Any, **kwargs: Any) -> None:
            nonlocal transient_failures
            with transient_lock:
                transient_failures += 1
                current = transient_failures
            if current == 1 and not persistent:
                time.sleep(1.25)
            if persistent or current <= 4:
                marker = "CONTROL_TRANSIENT_PERSISTENT" if persistent else "CONTROL_TRANSIENT"
                print(marker, flush=True)
                raise OperationalError("test transient pooled heartbeat connection")
            original_runtime_heartbeat(*args, **kwargs)

        workers._runtime_heartbeat = transient_runtime_heartbeat

    if arguments.mode == "control_overrun":
        control_never_release = threading.Event()

        def control_never_returns(*_args: Any, **_kwargs: Any) -> None:
            print("CONTROL_STARTED", flush=True)
            control_never_release.wait()

        workers._runtime_heartbeat = control_never_returns

    if arguments.mode == "shutdown_stopping_control_never_returns":
        original_runtime_transition = workers._runtime_transition
        stopping_never_release = threading.Event()
        workers.GRACEFUL_DRAIN_TIMEOUT_SECONDS = 1.0

        def shutdown_runtime_transition(
            db: Any,
            runtime_id: str,
            lifecycle_state: Any,
            fatal_code: Any,
        ) -> None:
            if lifecycle_state == "stopping":
                print("SHUTDOWN_STOPPING_CONTROL_STARTED", flush=True)
                stopping_never_release.wait()
            original_runtime_transition(db, runtime_id, lifecycle_state, fatal_code)

        workers._runtime_transition = shutdown_runtime_transition

    if arguments.mode == "control_native_timeout":
        from tracefold.app import worker_database as database_module

        database_module._WORKER_CONTROL_OPERATION_COMPLETION_GRACE_SECONDS = 0.1
        database_module._WORKER_TRANSACTION_TIMEOUT_MARGIN_SECONDS = -0.9
        original_runtime_heartbeat = workers._runtime_heartbeat
        native_timeout_calls = 0
        native_timeout_lock = threading.Lock()
        workers._CONTROL_HEARTBEAT_STALE_SECONDS = 0.25
        workers._CONTROL_RETRY_SECONDS = 0.02

        def native_timeout_runtime_heartbeat(
            db: Any,
            runtime_id: str,
            heartbeat_at_ms: int,
        ) -> None:
            nonlocal native_timeout_calls
            with native_timeout_lock:
                native_timeout_calls += 1
                current = native_timeout_calls
            if current <= 4:
                try:
                    _native_transaction_timeout(db)
                except Exception:
                    print(f"CONTROL_NATIVE_TIMEOUT_{current}", flush=True)
                    raise
                raise AssertionError("native transaction timeout missing")
            print("CONTROL_NATIVE_TIMEOUT_RECOVERED", flush=True)
            original_runtime_heartbeat(db, runtime_id, heartbeat_at_ms)

        workers._runtime_heartbeat = native_timeout_runtime_heartbeat

    if arguments.mode == "control_transient_runtime":
        from psycopg import OperationalError

        original_runtime_heartbeat = workers._runtime_heartbeat
        heartbeat_calls = 0
        heartbeat_lock = threading.Lock()
        workers._CONTROL_HEARTBEAT_STALE_SECONDS = 0.25
        workers._CONTROL_RETRY_SECONDS = 0.05

        def transient_runtime_heartbeat(*args: Any, **kwargs: Any) -> None:
            nonlocal heartbeat_calls
            with heartbeat_lock:
                heartbeat_calls += 1
                current = heartbeat_calls
            if 6 <= current <= 25:
                if current == 6:
                    print("CONTROL_RUNTIME_TRANSIENT_BEGIN", flush=True)
                raise OperationalError("test transient runtime heartbeat connection")
            if current == 26:
                print("CONTROL_RUNTIME_TRANSIENT_RECOVERED", flush=True)
            original_runtime_heartbeat(*args, **kwargs)

        workers._runtime_heartbeat = transient_runtime_heartbeat

    async def wire_components(**kwargs: Any) -> workers._Components:
        if arguments.mode in {
            "inert",
            "control_transient_startup",
            "control_transient_startup_persistent",
            "control_transient_runtime",
            "control_native_timeout",
            "shutdown_stopping_control_never_returns",
        }:
            return _components(workers, due_turns=())

        finite = kwargs["finite"]
        if arguments.mode in {
            "finite_overrun",
            "finite_never_returns",
            "finite_never_returns_failed_transition_once",
        }:
            never_release = threading.Event()

            def never_returns() -> None:
                print("FINITE_STARTED", flush=True)
                never_release.wait()

            async def overrun() -> bool:
                await finite.run(
                    "never_returns",
                    never_returns,
                    timeout_seconds=1.0 if arguments.mode == "finite_overrun" else 3_600.0,
                )
                return True

            return _components(workers, due_turns=(("news-deduper", overrun, 1.0),))

        if arguments.mode == "control_overrun":
            return _components(workers, due_turns=())

        db = kwargs["db"]
        published = False

        def publish() -> None:
            with db.worker_session("test_provider_publication") as repos, repos.transaction():
                repos.conn.execute("INSERT INTO worker_runtime_test_publications(id) VALUES (1)")

        async def provider_publication() -> bool:
            nonlocal published
            if published:
                return False
            await finite.run(
                "provider_completed",
                lambda: None,
                timeout_seconds=1.0,
            )
            print("PROVIDER_DONE", flush=True)
            await asyncio.sleep(2.0)
            await db.run_business(
                "provider_publication",
                publish,
                operation_timeout_seconds=2.0,
            )
            published = True
            return True

        return _components(workers, due_turns=(("news-deduper", provider_publication, 1.0),))

    if arguments.mode == "manifest_barrier":
        release_gate = Path(os.environ["TRACEFOLD_TEST_MANIFEST_GATE"])

        async def wire_news_pipeline(**_kwargs: Any) -> tuple[None, _ManifestBarrierPipeline, None]:
            # Three-tuple since #553 PR-2: bus, pipeline, market notification loop. These stubs
            # compose no market loop, so the optional task is simply not declared.
            return None, _ManifestBarrierPipeline(dsn=arguments.dsn, release_gate=release_gate), None

        workers_wiring._wire_news_pipeline = wire_news_pipeline
    elif arguments.mode == "manifest_registration_fault":
        # The real `_wire_components` runs: what fails is the editorial Program registration, and the
        # fact-writing ingestion task beside it must keep committing.
        async def wire_registration_fault(**kwargs: Any) -> tuple[None, _RegistrationFaultPipeline, None]:
            _declare_news_capabilities(kwargs["capabilities"])
            return None, _RegistrationFaultPipeline((("news-deduper", _fact_writer(kwargs["db"]), 1.0),)), None

        workers_wiring._wire_news_pipeline = wire_registration_fault
    elif arguments.mode == "optional_task_fault":
        resume_gate = Path(os.environ["TRACEFOLD_TEST_RESUME_GATE"])

        async def wire_optional_task_fault(**kwargs: Any) -> tuple[None, _TurnPipeline, None]:
            db = kwargs["db"]
            _declare_news_capabilities(kwargs["capabilities"], quotes=True)
            return (
                None,
                _TurnPipeline(
                    (
                        ("news-deduper", _fact_writer(db), 1.0),
                        # `news-quotes` is an optional capability task with a capability all to itself,
                        # which is the shape #553 PR-2's market notification loop registers as.
                        ("news-quotes", _backlog_consumer(db, resume_gate=resume_gate), 1.0),
                    )
                ),
                None,
            )

        workers_wiring._wire_news_pipeline = wire_optional_task_fault
    elif arguments.mode == "ingestion_task_fault":
        # Reception and admission are the information entry, not a capability to switch off. A program
        # error there must still end the process, so the container restart that has always healed it
        # keeps happening instead of a permanent outage behind a 200 /readyz.
        async def wire_ingestion_fault(**kwargs: Any) -> tuple[None, _TurnPipeline, None]:
            _declare_news_capabilities(kwargs["capabilities"])

            async def fail() -> bool:
                # After readiness, so this is a receiver crashing in service rather than a startup
                # refusal: the case where confinement would have replaced a restart with an outage.
                await asyncio.sleep(0.5)
                print("INGESTION_ABOUT_TO_FAIL", flush=True)
                raise RuntimeError("test_ingestion_task_fault")

            return None, _TurnPipeline((("news-receiver", fail, 1.0),)), None

        workers_wiring._wire_news_pipeline = wire_ingestion_fault
    elif arguments.mode == "trading_lane_fault":
        from tracefold.trading.signal_lane import SignalLane

        async def wire_trading_lane_fault(**kwargs: Any) -> tuple[None, _TurnPipeline, None]:
            # A Deliverer task runs beside an `unavailable` sender on purpose: it settles those
            # Events `delivery_unavailable` rather than dropping them, so declaring the task must not
            # overwrite what composition recorded about the sender it could not build.
            _declare_news_capabilities(kwargs["capabilities"], delivery="unavailable")
            return (
                None,
                _TurnPipeline(
                    (
                        ("news-deduper", _fact_writer(kwargs["db"]), 1.0),
                        ("news-deliverer", _idle_turn(), 1.0),
                    )
                ),
                None,
            )

        async def failing_advance(_self: Any) -> None:
            print("TRADING_LANE_ABOUT_TO_FAIL", flush=True)
            raise RuntimeError("test_trading_lane_fault")

        workers_wiring._wire_news_pipeline = wire_trading_lane_fault
        SignalLane.advance = failing_advance  # type: ignore[method-assign]
    elif arguments.mode == "schema_mismatch":
        workers.latest_migration_version = lambda: "00000000_0000"
    elif arguments.mode in {
        "trading_enabled",
        "trading_execution_requested",
        "trading_wiring_fault",
    }:
        if arguments.mode == "trading_wiring_fault":

            def fail_trading_wiring(**_kwargs: Any) -> None:
                raise RuntimeError("test_trading_wiring_fault")

            workers_wiring._wire_signal_lane = fail_trading_wiring
    else:
        workers._wire_components = wire_components
    trading_process = arguments.mode in {
        "trading_enabled",
        "trading_execution_requested",
        "trading_wiring_fault",
        "trading_lane_fault",
    }
    news_process = arguments.mode in {
        "manifest_barrier",
        "manifest_registration_fault",
        "optional_task_fault",
        "ingestion_task_fault",
        "trading_lane_fault",
    }
    settings = Settings(
        news={"enabled": news_process},
        trading={
            "enabled": trading_process,
            "execution": {"mode": "paper" if arguments.mode == "trading_execution_requested" else "disabled"},
        },
        storage={"postgres": {"dsn": arguments.dsn, "password_file": None}},
    )
    if trading_process:
        settings.set_config_dir(Path(os.environ["TRACEFOLD_TEST_CONFIG_DIR"]))
    await workers.run_workers(settings)


if __name__ == "__main__":
    asyncio.run(_main())
