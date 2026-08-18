from __future__ import annotations

import uvicorn

from tracefold.app.http.app import create_app
from tracefold.platform.config.settings import load_settings
from tracefold.platform.observability import setup_logging


def handle_serve(_args: object) -> int:
    settings = load_settings()
    setup_logging(settings.log_file)
    uvicorn.run(
        create_app(settings=settings),
        host=settings.api.host,
        port=settings.api.port,
        log_config=None,
    )
    return 0
