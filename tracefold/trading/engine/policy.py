"""Compile one recorded model proposal against frozen, code-owned conditions."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .contracts import AgentAssessment, Candidate, Decision, WatchCondition


class InvalidAssessment(ValueError):
    """A malformed reference or candidate identity, not a rejected strategy proposal."""


def decision_identity(case_id: str, decision: dict[str, object]) -> str:
    data = json.dumps(
        (case_id, decision), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode()
    return hashlib.sha256(data).hexdigest()


def compile_assessment(
    *,
    assessment: AgentAssessment,
    candidates: tuple[Candidate, ...],
    evidence_catalog: dict[str, dict[str, Any]],
    watch_expires_at_ms: int | None = None,
) -> Decision:
    menu = {candidate.candidate_id: candidate for candidate in candidates}
    if len(menu) != len(candidates) or len(menu) > 2:
        raise InvalidAssessment("candidate_menu_invalid")

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

    cited = set(assessment.supporting_evidence + assessment.opposing_evidence)
    if cited - evidence_catalog.keys():
        raise InvalidAssessment("assessment_evidence_ref_unknown")
    if any(not available(ref) for ref in cited):
        raise InvalidAssessment("assessment_evidence_unavailable")

    selected = menu.get(assessment.entry_candidate_id or "")
    action = assessment.action
    reason_code = (
        "model_no_trade" if action == "NO_TRADE" else "model_watch" if action == "WATCH" else "confirmed_entry"
    )
    watch: WatchCondition | None = None
    if action == "TRADE":
        if selected is None:
            raise InvalidAssessment("trade_candidate_outside_menu")
        if any(not available(ref) for ref in selected.required_evidence_refs):
            action, reason_code = "NO_TRADE", "required_evidence_unavailable"
        elif not selected.entry_ready:
            action, reason_code = "NO_TRADE", selected.strategy_gate_reason or "entry_condition_unmet"
    elif action == "WATCH":
        eligible = [candidate for candidate in candidates if candidate.watch_eligible]
        if len(eligible) == 2 and watch_expires_at_ms is not None:
            if any(not available(ref) for candidate in eligible for ref in candidate.required_evidence_refs):
                action, reason_code = "NO_TRADE", "required_evidence_unavailable"
            elif watch_expires_at_ms <= eligible[0].entry_observed_at_ms:
                action, reason_code = "NO_TRADE", "watch_expired"
            else:
                long = next(candidate for candidate in eligible if candidate.side == "long")
                short = next(candidate for candidate in eligible if candidate.side == "short")
                watch = WatchCondition(
                    kind="closed_1m_range_cross",
                    upper_level=long.entry_level,
                    lower_level=short.entry_level,
                    previous_close=long.entry_observed,
                    exit_plan=long.exit_plan,
                    source_first_visible_at_ms=long.source_first_visible_at_ms,
                    unit="USDT/base_asset",
                    frozen_at_ms=long.entry_observed_at_ms,
                    expires_at_ms=watch_expires_at_ms,
                )
        else:
            action, reason_code = "NO_TRADE", "watch_condition_unavailable"

    return Decision(
        action=action,
        entry_candidate_id=selected.candidate_id if action == "TRADE" and selected else None,
        side=selected.side if action == "TRADE" and selected else None,
        exit_plan=selected.exit_plan if action == "TRADE" and selected else None,
        reason=assessment.public_rationale,
        reason_code=reason_code,
        evidence_refs=tuple(sorted(cited)),
        watch_condition=watch,
        hypothesis_side=assessment.hypothesis_side,
        research_notes=assessment.research_notes,
    )
