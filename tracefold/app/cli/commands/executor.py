"""Run the application-image DEMO execution process."""

from __future__ import annotations

import asyncio
import signal

from tracefold.app.executor import run_executor
from tracefold.platform.config.loader import load_settings
from tracefold.platform.observability import setup_logging


async def _run() -> None:
    settings = load_settings(require_ws_token=False)
    setup_logging(settings.log_file.with_name("executor.log"))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signo in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signo, stop.set)
    if not settings.trading.enabled or not settings.trading.execution.enabled:
        await stop.wait()
        return
    await run_executor(settings, stop)


def handle_executor(_args: object) -> int:
    asyncio.run(_run())
    return 0
