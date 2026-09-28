"""Append a sourced correction when an Event head attributed a sibling fact to itself."""

from __future__ import annotations

from collections.abc import Iterable

from .contracts import Change, EventUpdate, content_material, content_revision_for, semantic_state
from .identity import digest


def retract_out_of_scope(head: EventUpdate, claim_refs: Iterable[str], *, adopted_at_ms: int) -> EventUpdate:
    """Retire proven misattributions without changing the old knowledge or delivery ledger."""

    refs = frozenset(claim_refs)
    active = {claim.ref for claim in head.claims} - set(head.retired_claim_refs) - set(head.superseded_claim_refs)
    if not refs or not refs <= active:
        raise ValueError("news_scope_repair_claims_not_active")
    retired = tuple(sorted(set(head.retired_claim_refs) | refs))
    inactive = set(retired) | set(head.superseded_claim_refs)
    implications = tuple(row for row in head.implications if not set(row.claim_refs) & inactive)
    questions = tuple(row for row in head.open_questions if not set(row.claim_refs) & inactive)
    topics = tuple(sorted({topic for claim in head.claims if claim.ref not in inactive for topic in claim.topics}))
    changes = tuple(
        Change(
            kind="scope_retraction",
            current_ref=ref,
            previous_ref=ref,
            previous_content_ref=head.ref,
        )
        for ref in sorted(refs)
    )
    sha = digest(
        content_material(
            head.event_id,
            (claim.ref for claim in head.claims),
            retired,
            head.evidence_relations,
            state=semantic_state(
                head.claims,
                implications,
                questions,
                head.evidence,
                head.evidence_relations,
                head.superseded_claim_refs,
            ),
        )
    )
    return EventUpdate(
        event_id=head.event_id,
        input_revision=head.input_revision,
        content_sha=sha,
        content_revision=content_revision_for(sha, head.content_revision),
        previous_content_revision=head.content_revision,
        adopted_at_ms=adopted_at_ms,
        topics=topics,
        claims=head.claims,
        evidence=head.evidence,
        evidence_relations=head.evidence_relations,
        retired_claim_refs=retired,
        superseded_claim_refs=head.superseded_claim_refs,
        changes=changes,
        implications=implications,
        open_questions=questions,
    )
