"""Lazy public entrypoint for the Workers process."""

from __future__ import annotations

from tracefold.platform.config.models import Settings


async def run_workers(settings: Settings) -> None:
    from tracefold.app.workers.root import run_workers as run

    await run(settings)


__all__ = ["run_workers"]
