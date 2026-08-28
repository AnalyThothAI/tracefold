from __future__ import annotations

from tracefold.app.manual_executor_root import run_manual_executor
from tracefold.platform.config.loader import load_settings
from tracefold.platform.observability import setup_logging


def handle_manual_executor(_args: object) -> int:
    settings = load_settings(require_ws_token=False)
    setup_logging(settings.log_file)
    run_manual_executor(settings)
    return 0


__all__ = ["handle_manual_executor"]
