"""Free, source-owned adapters for the Macro dataset registry."""

from .client import MacroSourceClient, MacroSourceError, MacroSourceUnavailable

__all__ = ["MacroSourceClient", "MacroSourceError", "MacroSourceUnavailable"]
