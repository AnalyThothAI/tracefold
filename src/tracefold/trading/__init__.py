"""Narrow public values and ports for the Trading bounded context.

App owns composition. Candidate rules, runners, persistence, and execution helpers stay under their
owning subpackages instead of becoming a second accidental API at this root.
"""

from __future__ import annotations

from .contracts import Bar, ExecutionReceipt, InstrumentRef, PreparedOrder, TradingMode

__all__ = ["Bar", "ExecutionReceipt", "InstrumentRef", "PreparedOrder", "TradingMode"]
