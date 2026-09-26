"""Deterministic public facts; a ReaderCard is never an input to Trading."""
from __future__ import annotations

from .contracts import Claim, EventUpdate, PublicUpdate
from .identity import identity

CATALYST_CHANGES = frozenset({"new_fact", "parameter_change", "phase_change", "scope_change"})
SOURCE_CHANGES = frozenset({"correction", "conflict", "evidence_change"})


def claim_text(claim: Claim) -> str:
    fields = claim.fields
    lines = [f"[{claim.ref}] {claim.statement}",
             f"subject={fields.subject}; action={fields.action}; object={fields.object}; mode={fields.mode}; phase={fields.phase or 'not_applicable'}; polarity={fields.polarity}"]
    if fields.speaker:
        lines.append(f"speaker={fields.speaker}")
    for name in ("occurred_at", "effective_at", "statistical_period"):
        value = getattr(fields, name)
        if value:
            lines.append(f"{name}={value}")
    lines.extend(f"condition={value}" for value in fields.conditions)
    lines.extend(f"quantity={q.name}:{q.value} {q.unit}; period={q.period or 'unknown'}" for q in fields.quantities)
    lines.extend(f"evidence={c.evidence_ref}; quote={c.quote}" for c in claim.citations)
    return "\n".join(lines)


def public_updates(update: EventUpdate, *, semantic_completed_at_ms: int) -> tuple[PublicUpdate, ...]:
    """At most one catalyst delta and one source update per adopted revision.

    Explanatory prose and equivalent translations are not additional triggers.
    A correction is dispatched to its own receipt/update method before target
    selection. Each consumer must use update_id as its atomic receive identity.
    """
    rows = []
    by_ref = {claim.ref: claim for claim in update.claims}
    for kind, kinds in (("catalyst_delta", CATALYST_CHANGES), ("source_update", SOURCE_CHANGES)):
        changes = tuple(change for change in update.changes if change.kind in kinds)
        if not changes:
            continue
        refs = tuple(sorted({change.current_ref for change in changes}))
        claims = tuple(by_ref[ref] for ref in refs)
        relationships = tuple(r for r in update.evidence_relations if r.claim_ref in refs)
        citations = {citation.evidence_ref for claim in claims for citation in claim.citations} | {r.evidence_ref for r in relationships}
        previous = tuple(sorted({change.previous_content_ref for change in changes if change.previous_content_ref}))
        affected = tuple(sorted({change.previous_ref for change in changes if change.previous_ref}))
        if kind == "source_update" and not previous:
            # No published ancestor exists for a first report. Do not invent a
            # target revision or turn unsupported evidence into a new entry signal.
            continue
        rows.append(PublicUpdate.model_validate({
            "update_id": identity("public", update.event_id, update.content_revision, kind, sorted(refs)),
            "kind": kind, "event_id": update.event_id, "content_revision": update.content_revision,
            "claim_refs": refs, "claims": claims,
            "evidence": tuple(e for e in update.evidence if e.ref in citations), "changes": changes,
            "evidence_relations": relationships, "previous_content_refs": previous, "affected_claim_refs": affected,
            "first_available_at_ms": min(c.first_available_at_ms for c in claims),
            "semantic_completed_at_ms": semantic_completed_at_ms,
            "text": "\n\n".join([*(claim_text(c) for c in claims), *(f"source={e.source.publisher_id}; origin={e.source.origin_id or 'unknown'}; evidence={e.ref}; text={e.text}" for e in update.evidence if e.ref in citations), *(f"relationship={r.claim_ref}/{r.evidence_ref}:{r.relation}" for r in relationships)]),
        }))
    return tuple(rows)
