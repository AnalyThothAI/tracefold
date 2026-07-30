from __future__ import annotations

import uvicorn

from tracefold.app.worker_http import create_workers_app
from tracefold.platform.config.settings import load_settings
from tracefold.platform.observability import setup_logging

_WORKER_INTERNAL_PORT = 8766


def handle_workers(_args: object) -> int:
    settings = load_settings(require_ws_token=False)
    setup_logging(settings.log_file)
    uvicorn.run(
        create_workers_app(settings),
        host="127.0.0.1",
        port=_WORKER_INTERNAL_PORT,
        log_config=None,
    )
    return 0
