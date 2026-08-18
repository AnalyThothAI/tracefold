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
    """Every cited evidence id must come from this run's evidence bundle registry."""

    for evidence_id in verdict.context_evidence:
        if evidence_id not in tool_evidence:
            return VerifyResult(False, "context_evidence_unknown")
    if not verdict.agrees_with_triage and verdict.revised_direction == triage_direction:
        return VerifyResult(False, "disagreement_without_revision")
    if verdict.revised_magnitude >= 2 and not verdict.context_evidence:
        return VerifyResult(False, "magnitude_without_evidence")
    return VerifyResult(True)


__all__ = ["VerifyResult", "verify_verdict"]
