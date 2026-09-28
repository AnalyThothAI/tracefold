"""Frozen analysis result shared by the frame reader and its read-only tools."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from tracefold.trading.engine.brief import AnalystBrief
from tracefold.trading.engine.plans import EntryPlan


@dataclass(frozen=True, slots=True)
class PreparedAnalysis:
    evidence_ref: str
    brief_ref: str
    brief: AnalystBrief
    reference_price: Decimal
    reference_at_ms: int
    plans: tuple[EntryPlan, ...]
    source_history: tuple[dict[str, Any], ...] = ()
    source_amendments: tuple[dict[str, Any], ...] = ()
