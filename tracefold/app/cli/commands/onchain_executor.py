from __future__ import annotations

from tracefold.app.onchain_executor_root import run_onchain_executor
from tracefold.platform.config.loader import load_settings
from tracefold.platform.observability import setup_logging


def handle_onchain_executor(_args: object) -> int:
    settings = load_settings(require_ws_token=False)
    setup_logging(settings.log_file)
    run_onchain_executor(settings)
    return 0


__all__ = ["handle_onchain_executor"]
