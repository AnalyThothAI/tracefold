from __future__ import annotations

import argparse
import asyncio
import threading
import time
from typing import Any


def _ignore_sigterm_and_sleep(delay_seconds: float) -> None:
    import signal
    import time

    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(delay_seconds)


def _return_one() -> int:
    return 1


def _native_news_push_timeout(db: Any) -> None:
    with db.worker_session(
        "test_news_push_bounded_timeout",
        statement_timeout_seconds=0.05,
        transaction_timeout_seconds=0.2,
    ) as repos:
        repos.conn.execute("SELECT pg_sleep(0.2)")


def _news_push_liveness(db: Any) -> int:
    with db.worker_session("test_news_push_recovery") as repos:
        row = repos.conn.execute("SELECT 1 AS ok").fetchone()
        return int(row["ok"])


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument(
        "--mode",
        required=True,
        choices=(
            "inert",
            "child_failure",
            "finite_overrun",
            "finite_never_returns",
            "model_overrun",
            "control_overrun",
            "control_transient_startup",
            "provider_publication",
            "news_bounded_recovery",
        ),
    )
    return parser.parse_args()


def _components(
    workers_module: Any,
    market_providers_module: Any,
    *,
    due_turns: tuple[tuple[Any, float], ...],
) -> Any:
    class _AssetProfileRefresh:
        async def reconcile(self) -> None:
            return None

    class _RadarCurrent:
        async def sample(self) -> None:
            return None

    return workers_module._Components(
        providers=market_providers_module.AssetMarketProviders(),
        asset_profile_refresh=_AssetProfileRefresh(),
        collector=None,
        news=None,
        news_story=None,
        news_brief=None,
        news_push=None,
        macro_source=None,
        macro_turns=(),
        due_turns=due_turns,
        market_poll=None,
        radar_current=_RadarCurrent(),
        projections=(),
        models=(),
        document_model=None,
    )


async def _main() -> None:
    arguments = _arguments()
    if arguments.mode == "news_bounded_recovery":
        from pebble import CONSTS

        # Make the native TERM -> KILL interval longer than the old fixed
        # wrapper grace so the process test deterministically exercises a
        # late, but still classified and recoverable, native CPU timeout.
        CONSTS.term_timeout = 4.5

    from tracefold.app import market_providers as market_providers_module
    from tracefold.app import workers
    from tracefold.platform.config.settings import Settings
    from tracefold.platform.model_candidate import ModelCandidate
    from tracefold.platform.resource import CpuTaskTimeout

    workers._WORKER_INTERNAL_PORT = arguments.port
    workers._HEARTBEAT_SECONDS = 0.1

    if arguments.mode == "control_transient_startup":
        from psycopg import OperationalError

        original_runtime_heartbeat = workers._runtime_heartbeat
        transient_failures = 0
        transient_lock = threading.Lock()

        def transient_runtime_heartbeat(*args: Any, **kwargs: Any) -> None:
            nonlocal transient_failures
            with transient_lock:
                transient_failures += 1
                current = transient_failures
            if current == 1:
                time.sleep(1.25)
            if current <= 4:
                print("CONTROL_TRANSIENT", flush=True)
                raise OperationalError("test transient pooled heartbeat connection")
            original_runtime_heartbeat(*args, **kwargs)

        workers._runtime_heartbeat = transient_runtime_heartbeat

    if arguments.mode == "control_overrun":
        control_never_release = threading.Event()

        def control_never_returns(*_args: Any, **_kwargs: Any) -> None:
            print("CONTROL_STARTED", flush=True)
            control_never_release.wait()

        workers._runtime_heartbeat = control_never_returns

    async def wire_components(**kwargs: Any) -> workers._Components:
        if arguments.mode in {"inert", "control_transient_startup"}:
            return _components(workers, market_providers_module, due_turns=())

        if arguments.mode == "child_failure":

            async def fail() -> bool:
                await asyncio.sleep(0.5)
                print("ABOUT_TO_FAIL", flush=True)
                await asyncio.sleep(0.1)
                raise RuntimeError("test_child_failure")

            return _components(workers, market_providers_module, due_turns=((fail, 1.0),))

        finite = kwargs["finite"]
        if arguments.mode in {"finite_overrun", "finite_never_returns"}:
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

            return _components(workers, market_providers_module, due_turns=((overrun, 1.0),))

        if arguments.mode == "model_overrun":
            model_adapter = kwargs["model_adapter"]
            model_never_release = threading.Event()

            def model_never_returns() -> None:
                print("MODEL_STARTED", flush=True)
                model_never_release.wait()

            class NeverReturningModelCandidate:
                async def peek(self, *, now_ms: int) -> ModelCandidate:
                    return ModelCandidate(
                        kind="test_model",
                        target_key="singleton",
                        due_at_ms=now_ms,
                        stable_order=1,
                    )

                async def execute(self, _candidate: ModelCandidate) -> bool:
                    await model_adapter.run(
                        "model_never_returns",
                        model_never_returns,
                        timeout_seconds=1.0,
                    )
                    return True

            components = _components(workers, market_providers_module, due_turns=())
            components.models = (NeverReturningModelCandidate(),)
            return components

        if arguments.mode == "control_overrun":
            return _components(workers, market_providers_module, due_turns=())

        if arguments.mode == "news_bounded_recovery":
            news_cpu = kwargs["news_cpu"]
            assert news_cpu is not None
            db = kwargs["db"]
            workers._NEWS_STORY_REFRESH_SECONDS = 0.1
            workers._NEWS_PUSH_RECONCILE_SECONDS = 0.1

            class RecoveringNewsStory:
                def __init__(self) -> None:
                    self.calls = 0

                async def sample(self) -> None:
                    self.calls += 1
                    if self.calls == 1:
                        try:
                            await news_cpu.run(
                                "test_news_story_bounded_timeout",
                                _ignore_sigterm_and_sleep,
                                30.0,
                                service_timeout_seconds=0.05,
                            )
                        except CpuTaskTimeout:
                            print("NEWS_STORY_BOUNDED_TIMEOUT", flush=True)
                            return
                    if self.calls == 2:
                        assert (
                            await news_cpu.run(
                                "test_news_story_recovery",
                                _return_one,
                                service_timeout_seconds=1.0,
                            )
                            == 1
                        )
                        print("NEWS_STORY_RECOVERED", flush=True)

            class RecoveringNewsPush:
                def __init__(self) -> None:
                    self.calls = 0

                async def reconcile(self, *, now_ms: int) -> dict[str, int]:
                    del now_ms
                    self.calls += 1
                    if self.calls == 1:
                        return {}
                    if self.calls == 2:
                        try:
                            await db.run_business(
                                "test_news_push_bounded_timeout",
                                _native_news_push_timeout,
                                db,
                                operation_timeout_seconds=0.05,
                            )
                        finally:
                            print("NEWS_PUSH_BOUNDED_TIMEOUT", flush=True)
                    if self.calls == 3:
                        assert (
                            await db.run_business(
                                "test_news_push_recovery",
                                _news_push_liveness,
                                db,
                                operation_timeout_seconds=1.0,
                            )
                            == 1
                        )
                        print("NEWS_PUSH_RECOVERED", flush=True)
                    return {}

                async def close(self) -> None:
                    return None

            components = _components(workers, market_providers_module, due_turns=())
            components.news_story = RecoveringNewsStory()
            components.news_push = RecoveringNewsPush()
            return components

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

        return _components(workers, market_providers_module, due_turns=((provider_publication, 1.0),))

    workers._wire_components = wire_components
    settings = Settings(
        storage={
            "postgres": {
                "serve_dsn": arguments.dsn,
                "workers_dsn": arguments.dsn,
                "migrate_dsn": arguments.dsn,
                "serve_password_file": None,
                "workers_password_file": None,
                "migrate_password_file": None,
            }
        }
    )
    await workers.run_workers(settings)


if __name__ == "__main__":
    asyncio.run(_main())
