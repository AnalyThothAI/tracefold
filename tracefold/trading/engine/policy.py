"""Compile a recorded Agent answer into a deterministic, bounded decision."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from .contracts import AgentAssessment, Candidate, CandidateScore, Decision


class InvalidAssessment(ValueError):
    pass


def decision_identity(case_id: str, decision: dict[str, object]) -> str:
    data = json.dumps(
        (case_id, decision), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode()
    return hashlib.sha256(data).hexdigest()


def compile_assessment(
    *,
    assessment: AgentAssessment,
    brief_sha: str,
    candidate_menu_sha: str,
    candidates: tuple[Candidate, ...],
    evidence_refs: frozenset[str],
) -> Decision:
    if assessment.brief_sha != brief_sha or assessment.candidate_menu_sha != candidate_menu_sha:
        raise InvalidAssessment("assessment_input_digest_mismatch")
    menu = {candidate.candidate_id: candidate for candidate in candidates}
    if len(menu) != len(candidates) or len(menu) > 2:
        raise InvalidAssessment("candidate_menu_invalid")
    cited = set(assessment.supporting_evidence + assessment.opposing_evidence)
    scores: list[CandidateScore] = []
    seen: set[str] = set()
    for item in assessment.candidate_assessments:
        if item.candidate_id not in menu or item.candidate_id in seen:
            raise InvalidAssessment("candidate_outside_menu_or_duplicate")
        seen.add(item.candidate_id)
        total = 0
        covered = 0
        for factor in item.factors:
            cited.update(factor.evidence_refs)
            if factor.status == "known" and factor.support_score is not None:
                covered += factor.weight_bps
                total += factor.weight_bps * factor.support_score
        value = Decimal(total) / Decimal(10_000) if covered == 10_000 else None
        scores.append(CandidateScore(candidate_id=item.candidate_id, value=value, covered_weight_bps=covered))
    if cited - evidence_refs:
        raise InvalidAssessment("assessment_evidence_ref_unknown")
    selected = menu.get(assessment.selected_candidate_id or "")
    if assessment.action == "TRADE":
        if selected is None or selected.candidate_id not in seen:
            raise InvalidAssessment("trade_candidate_not_assessed")
        score = next(score for score in scores if score.candidate_id == selected.candidate_id)
        if score.covered_weight_bps < 10_000:
            raise InvalidAssessment("trade_assessment_partial")
        if set(selected.required_evidence_refs) - cited:
            raise InvalidAssessment("trade_required_evidence_missing")
    return Decision(
        action=assessment.action,
        selected_candidate_id=selected.candidate_id if selected is not None else None,
        side=selected.side if selected is not None else None,
        exit_plan=selected.exit_plan if selected is not None else None,
        scores=tuple(scores),
        reason=assessment.public_rationale,
        evidence_refs=tuple(sorted(cited)),
        watch_condition=assessment.watch_condition,
    )
