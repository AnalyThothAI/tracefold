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
    if verdict.novelty_assessment == "rehash" and verdict.follow_up_needed:
        return VerifyResult(False, "rehash_follow_up")
    return VerifyResult(True)


def follow_up_adds_information(verdict: AnalystVerdict, *, triage_direction: str, triage_magnitude: int) -> bool:
    """A follow-up card ships only when the Analyst changed or added something the reader would act on: a
    direction/magnitude revision, or a genuine progression backed by history/verdict evidence."""

    if not verdict.follow_up_needed or verdict.novelty_assessment == "rehash":
        return False
    if not verdict.agrees_with_triage or verdict.revised_direction != triage_direction:
        return True
    if verdict.revised_magnitude != triage_magnitude:
        return True
    return verdict.novelty_assessment == "followup" and any(
        e.startswith(("history:", "verdict:", "macro:")) for e in verdict.context_evidence
    )


__all__ = ["VerifyResult", "follow_up_adds_information", "verify_verdict"]
