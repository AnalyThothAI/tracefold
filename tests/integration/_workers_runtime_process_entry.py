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
            "child_failure",
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
            "trading_bindings",
            "trading_missing_authority",
            "trading_wiring_fault",
        ),
    )
    return parser.parse_args()


class _TurnPipeline:
    """Carry the test turns through the runtime's one business-task slot."""

    def __init__(self, turns: tuple[tuple[Any, float], ...]) -> None:
        self._turns = turns

    def runners(self) -> list[tuple[str, Any]]:
        return [
            (f"test-turn-{index}", _turn_runner(turn, idle_seconds))
            for index, (turn, idle_seconds) in enumerate(self._turns)
        ]

    async def drain(self) -> None:
        return None

    async def close(self) -> None:
        return None


class _ManifestBarrierPipeline(_TurnPipeline):
    def __init__(self, *, dsn: str, release_gate: Path) -> None:
        super().__init__(())
        self._dsn = dsn
        self._release_gate = release_gate

    async def register_runtime_manifest(self) -> None:
        Path(f"{self._release_gate}.entered").touch()
        while not self._release_gate.exists():
            await asyncio.sleep(0.01)

        from psycopg import connect
        from psycopg.rows import dict_row

        from tracefold.app.repository_session import repositories_for_connection

        conn = connect(self._dsn, row_factory=dict_row)
        try:
            repositories = repositories_for_connection(conn)
            with repositories.transaction():
                repositories.news.register_agent_runtime_manifest(
                    manifest_sha=RUNTIME_MANIFEST_BARRIER_SHA,
                    stable_bundle_sha="b" * 64,
                    envelope_sha256="e" * 64,
                    artifact_schema_version="news_program_strategy_artifact_v1",
                    program_version="news_semantic_program_v5",
                    program_sha256="f" * 64,
                    candidate_shas=(),
                    image_digest="sha256:" + "c" * 64,
                    runtime_revision="git:test-manifest-barrier",
                    now_ms=int(time.time() * 1_000),
                )
        finally:
            conn.close()


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
    due_turns: tuple[tuple[Any, float], ...],
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

        if arguments.mode == "child_failure":

            async def fail() -> bool:
                await asyncio.sleep(0.5)
                print("ABOUT_TO_FAIL", flush=True)
                await asyncio.sleep(0.1)
                raise RuntimeError("test_child_failure")

            return _components(workers, due_turns=((fail, 1.0),))

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

            return _components(workers, due_turns=((overrun, 1.0),))

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

        return _components(workers, due_turns=((provider_publication, 1.0),))

    if arguments.mode in {
        "manifest_barrier",
        "trading_bindings",
        "trading_missing_authority",
        "trading_wiring_fault",
    }:
        from tracefold.app.workers.wiring import trading as trading_wiring

        async def empty_catalog() -> tuple[()]:
            return ()

        trading_wiring.fetch_binance_usdm_catalog = empty_catalog
        trading_wiring.fetch_hyperliquid_perp_catalog = empty_catalog

    if arguments.mode == "manifest_barrier":
        release_gate = Path(os.environ["TRACEFOLD_TEST_MANIFEST_GATE"])

        async def wire_news_pipeline(**_kwargs: Any) -> tuple[None, _ManifestBarrierPipeline]:
            return None, _ManifestBarrierPipeline(dsn=arguments.dsn, release_gate=release_gate)

        workers_wiring._wire_news_pipeline = wire_news_pipeline
    elif arguments.mode in {"trading_bindings", "trading_missing_authority", "trading_wiring_fault"}:
        if arguments.mode == "trading_wiring_fault":

            def fail_trading_wiring(**_kwargs: Any) -> None:
                raise RuntimeError("test_trading_wiring_fault")

            workers_wiring._wire_capital_lane = fail_trading_wiring
    else:
        workers._wire_components = wire_components
    trading_process = arguments.mode in {
        "trading_bindings",
        "trading_missing_authority",
        "trading_wiring_fault",
    }
    binding_variant = os.environ.get("TRACEFOLD_TEST_BINDING_VARIANT", "none")
    settings = Settings(
        news={"enabled": arguments.mode == "manifest_barrier"},
        trading={
            "enabled": trading_process,
            "bindings": {
                "hyperliquid_perp": {
                    "account_address": "0x" + "22" * 20 if binding_variant in {"dual", "invalid"} else None,
                }
            },
        },
        storage={
            "postgres": {
                "serve_dsn": arguments.dsn,
                "workers_dsn": arguments.dsn,
                "migrate_dsn": arguments.dsn,
                "serve_password_file": None,
                "workers_password_file": None,
                "migrate_password_file": None,
            }
        },
    )
    if trading_process:
        settings.set_config_dir(Path(os.environ["TRACEFOLD_TEST_CONFIG_DIR"]))
    if arguments.mode == "trading_missing_authority":
        from psycopg import connect

        connection = connect(arguments.dsn)
        try:
            connection.execute("DELETE FROM trading_runtime_state WHERE id = 1")
            connection.commit()
        finally:
            connection.close()
    await workers.run_workers(settings)


if __name__ == "__main__":
    asyncio.run(_main())
