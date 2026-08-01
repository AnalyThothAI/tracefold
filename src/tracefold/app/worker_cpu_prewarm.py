from __future__ import annotations

import importlib
import sys

_WORKER_CPU_MODULES = (
    "tracefold.integrations.news_feeds.rss",
    "tracefold.macro.projection",
    "tracefold.market.profiles.profile_projection",
    "tracefold.market.radar.microbatch",
    "tracefold.market.radar.projection_worker",
    "tracefold.news.projection",
)


def prewarm_worker_cpu_modules() -> tuple[str, ...]:
    """Import every production CPU-task module in the sole spawned process."""

    for module_name in _WORKER_CPU_MODULES:
        importlib.import_module(module_name)
    return _WORKER_CPU_MODULES


def worker_cpu_modules_loaded() -> tuple[str, ...]:
    return tuple(module_name for module_name in _WORKER_CPU_MODULES if module_name in sys.modules)


__all__ = ["prewarm_worker_cpu_modules", "worker_cpu_modules_loaded"]
