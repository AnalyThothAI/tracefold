"""verify_verdict(): deterministic evidence-consistency gate for Analyst output (pure)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .models import AnalystVerdict


@dataclass(frozen=True, slots=True)
class VerifyResult:
    ok: bool
    reason: str | None = None


def verify_verdict(
    verdict: AnalystVerdict,
    *,
    tool_evidence: Mapping[str, Mapping[str, Any]],
    triage_direction: str,
) -> VerifyResult:
    """All evidence ids must come from this run's tool returns; numbers must equal tool values."""

    for evidence_id in verdict.context_evidence:
        if evidence_id not in tool_evidence:
            return VerifyResult(False, "context_evidence_unknown")
    for reaction in verdict.market_reaction:
        source = tool_evidence.get(reaction.evidence_id)
        if source is None:
            return VerifyResult(False, "market_reaction_evidence_unknown")
        if str(source.get("symbol") or "").upper() != reaction.symbol.upper():
            return VerifyResult(False, "market_reaction_symbol_mismatch")
        for field in ("price_change_pct", "oi_change_pct"):
            claimed = getattr(reaction, field)
            actual = source.get(field)
            if claimed is None and actual is None:
                continue
            if claimed is None or actual is None or abs(float(claimed) - float(actual)) > 1e-6:
                return VerifyResult(False, f"market_reaction_{field}_mismatch")
        if int(source.get("window_min") or 0) != reaction.window_min:
            return VerifyResult(False, "market_reaction_window_mismatch")
    if not verdict.agrees_with_triage and verdict.revised_direction == triage_direction:
        return VerifyResult(False, "disagreement_without_revision")
    if verdict.revised_magnitude >= 2 and not (verdict.market_reaction or verdict.context_evidence):
        return VerifyResult(False, "magnitude_without_evidence")
    return VerifyResult(True)


__all__ = ["VerifyResult", "verify_verdict"]
