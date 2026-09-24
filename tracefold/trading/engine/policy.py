"""Compile a recorded Agent answer into a deterministic, bounded decision."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from .contracts import AgentAssessment, Candidate, CandidateScore, Decision, WatchCondition


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
    evidence_catalog: dict[str, dict[str, Any]],
    watch_expires_at_ms: int | None = None,
) -> Decision:
    if assessment.brief_sha != brief_sha or assessment.candidate_menu_sha != candidate_menu_sha:
        raise InvalidAssessment("assessment_input_digest_mismatch")
    menu = {candidate.candidate_id: candidate for candidate in candidates}
    if len(menu) != len(candidates) or len(menu) > 2:
        raise InvalidAssessment("candidate_menu_invalid")
    cited = set(assessment.supporting_evidence + assessment.opposing_evidence)

    def available(ref: str) -> bool:
        item = evidence_catalog.get(ref) or {}
        cutoff = item.get("knowledge_cutoff_ms")
        event_at = item.get("event_at_ms")
        received_at = item.get("received_at_ms")
        values = item.get("values")
        return (
            item.get("status") == "ok"
            and isinstance(values, dict)
            and any(value is not None and value != "" for value in values.values())
            and bool(item.get("unit_definition"))
            and isinstance(cutoff, int)
            and isinstance(event_at, int)
            and isinstance(received_at, int)
            and event_at <= cutoff
            and received_at <= cutoff
        )

    scores: list[CandidateScore] = []
    seen: set[str] = set()
    for item in assessment.candidate_assessments:
        if item.candidate_id not in menu or item.candidate_id in seen:
            raise InvalidAssessment("candidate_outside_menu_or_duplicate")
        seen.add(item.candidate_id)
        total = 0
        known = 0
        for factor in item.factors:
            cited.update(factor.evidence_refs)
            if factor.status == "known" and factor.support_score is not None:
                if any(ref not in evidence_catalog for ref in factor.evidence_refs):
                    raise InvalidAssessment("assessment_evidence_ref_unknown")
                if any(not available(ref) for ref in factor.evidence_refs):
                    raise InvalidAssessment("known_factor_evidence_unavailable")
                known += 1
                total += factor.support_score
        value = Decimal(total) / Decimal(known) if known else None
        scores.append(CandidateScore(candidate_id=item.candidate_id, value=value, known_factors=known))
    if cited - evidence_catalog.keys():
        raise InvalidAssessment("assessment_evidence_ref_unknown")
    selected = menu.get(assessment.entry_candidate_id or "")
    if assessment.action == "TRADE":
        if selected is None or selected.candidate_id not in seen:
            raise InvalidAssessment("trade_candidate_not_assessed")
        if not selected.entry_ready:
            raise InvalidAssessment("trade_entry_condition_unmet")
        if set(selected.required_evidence_refs) - cited or any(
            not available(ref) for ref in selected.required_evidence_refs
        ):
            raise InvalidAssessment("trade_required_evidence_missing")
    watch: WatchCondition | None = None
    if assessment.action == "WATCH" and assessment.watch_intent == "closed_1m_price_crosses":
        watched = next((candidate for candidate in candidates if candidate.side == assessment.hypothesis_side), None)
        if (
            watched is not None
            and watched.watch_eligible
            and watched.candidate_id in seen
            and watch_expires_at_ms is not None
            and watch_expires_at_ms > watched.entry_observed_at_ms
        ):
            if set(watched.required_evidence_refs) - cited or any(
                not available(ref) for ref in watched.required_evidence_refs
            ):
                raise InvalidAssessment("watch_required_evidence_missing")
            watch = WatchCondition(
                kind="closed_1m_price_crosses",
                candidate_id=watched.candidate_id,
                feature_id=watched.entry_feature_id,
                operator=watched.entry_operator,
                level=watched.entry_level,
                unit="USDT/base_asset",
                frozen_at_ms=watched.entry_observed_at_ms,
                expires_at_ms=watch_expires_at_ms,
            )
    return Decision(
        action=assessment.action,
        entry_candidate_id=selected.candidate_id if selected is not None else None,
        side=selected.side if selected is not None else None,
        exit_plan=selected.exit_plan if selected is not None else None,
        scores=tuple(scores),
        reason=assessment.public_rationale,
        evidence_refs=tuple(sorted(cited)),
        watch_condition=watch,
        hypothesis_side=assessment.hypothesis_side,
        observation_note=assessment.observation_note,
    )
