from __future__ import annotations

import asyncio

from tracefold.app.workers import run_workers
from tracefold.platform.config.settings import load_settings
from tracefold.platform.observability import setup_logging


def handle_workers(_args: object) -> int:
    settings = load_settings(require_ws_token=False)
    setup_logging(settings.log_file)
    asyncio.run(run_workers(settings))
    return 0
