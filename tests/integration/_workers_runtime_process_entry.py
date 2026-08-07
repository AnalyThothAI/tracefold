from __future__ import annotations

import argparse
import asyncio
import threading
from typing import Any


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
            "provider_publication",
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

    return workers_module._Components(
        providers=market_providers_module.AssetMarketProviders(),
        asset_profile_refresh=_AssetProfileRefresh(),
        collector=None,
        news=None,
        news_story=None,
        news_brief=None,
        news_title_translation=None,
        news_push=None,
        macro_source=None,
        macro_turns=(),
        due_turns=due_turns,
        market_poll=None,
        projections=(),
        models=(),
        document_model=None,
    )


async def _main() -> None:
    from tracefold.app import market_providers as market_providers_module
    from tracefold.app import workers
    from tracefold.platform.config.settings import Settings
    from tracefold.platform.model_candidate import ModelCandidate

    arguments = _arguments()
    workers._WORKER_INTERNAL_PORT = arguments.port
    workers._HEARTBEAT_SECONDS = 0.1

    if arguments.mode == "control_overrun":
        control_never_release = threading.Event()

        def control_never_returns(*_args: Any, **_kwargs: Any) -> None:
            print("CONTROL_STARTED", flush=True)
            control_never_release.wait()

        workers._runtime_heartbeat = control_never_returns

    async def wire_components(**kwargs: Any) -> workers._Components:
        if arguments.mode == "inert":
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
