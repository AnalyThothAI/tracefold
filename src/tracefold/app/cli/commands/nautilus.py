"""The one process entry point for the Binance USD-M Demo execution authority."""

from __future__ import annotations

from tracefold.platform.config.loader import load_settings
from tracefold.platform.observability import setup_logging


def handle_nautilus(_args: object) -> int:
    from tracefold.app.nautilus.root import run_nautilus

    settings = load_settings(require_ws_token=False)
    setup_logging(settings.log_file)
    run_nautilus(settings)
    return 0


__all__ = ["handle_nautilus"]
