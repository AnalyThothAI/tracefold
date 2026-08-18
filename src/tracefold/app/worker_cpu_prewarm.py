from __future__ import annotations

import importlib
import sys

_PROJECTION_CPU_MODULES = (
    "tracefold.macro.projection",
    "tracefold.market.profiles.profile_projection",
)


def prewarm_projection_cpu_modules() -> tuple[str, ...]:
    """Import Profile and Macro compute in their spawned process."""

    for module_name in _PROJECTION_CPU_MODULES:
        importlib.import_module(module_name)
    return _PROJECTION_CPU_MODULES


def projection_cpu_modules_loaded() -> tuple[str, ...]:
    return tuple(module_name for module_name in _PROJECTION_CPU_MODULES if module_name in sys.modules)


__all__ = ["prewarm_projection_cpu_modules", "projection_cpu_modules_loaded"]
