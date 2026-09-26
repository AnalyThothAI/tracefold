"""Deterministic public facts; a ReaderCard is never an input to Trading."""

from __future__ import annotations

from typing import Final, Literal

from .contracts import Change, ChangeKind, Claim, EventUpdate, PublicUpdate

PublicKind = Literal["catalyst_delta", "source_update"]

# `possible_new` and `restatement` are in neither set: an unresolved comparison and a repeat are adopted
# content, but neither is a new fact for Trading nor an amendment of one.
CATALYST_CHANGES: Final[frozenset[ChangeKind]] = frozenset(
    {"new_fact", "parameter_change", "phase_change", "scope_change"}
)
SOURCE_CHANGES: Final[frozenset[ChangeKind]] = frozenset({"correction", "conflict", "evidence_change"})
# A catalyst change whose earlier claim no longer describes the world.
SUPERSEDING_CHANGES: Final[frozenset[ChangeKind]] = frozenset({"parameter_change", "phase_change"})


def claim_text(claim: Claim) -> str:
    fields = claim.fields
    lines = [
        f"[{claim.ref}] {claim.statement}",
        f"subject={fields.subject}; action={fields.action}; object={fields.object}; mode={fields.mode}; "
        f"phase={fields.phase or 'not_applicable'}; polarity={fields.polarity}; content_kind={fields.content_kind}",
    ]
    if fields.speaker:
        lines.append(f"speaker={fields.speaker}")
    for name in ("occurred_at", "effective_at", "statistical_period"):
        value = getattr(fields, name)
        if value:
            lines.append(f"{name}={value}")
    lines.extend(f"condition={value}" for value in fields.conditions)
    lines.extend(
        f"quantity={quantity.name}:{quantity.value} {quantity.unit}; period={quantity.period or 'unknown'}"
        for quantity in fields.quantities
    )
    lines.extend(f"asset={asset.symbol}; market={asset.market_type}; role={asset.role}" for asset in fields.assets)
    lines.extend(f"evidence={citation.evidence_ref}; quote={citation.quote}" for citation in claim.citations)
    return "\n".join(lines)


def change_text(change: Change) -> str:
    return (
        f"change={change.kind}; current={change.current_ref}; previous={change.previous_ref or 'none'}; "
        f"relation={change.relation or 'none'}"
    )


def _superseded(changes: tuple[Change, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                change.previous_ref
                for change in changes
                if change.previous_ref is not None
                and (change.kind in SUPERSEDING_CHANGES or change.relation == "real_world_change")
            }
        )
    )


def _retired(changes: tuple[Change, ...], update: EventUpdate) -> tuple[str, ...]:
    retired = set(update.retired_claim_refs)
    return tuple(
        sorted(
            {
                change.previous_ref
                for change in changes
                if change.kind == "correction" and change.previous_ref is not None and change.previous_ref in retired
            }
        )
    )


def public_updates(update: EventUpdate, *, semantic_completed_at_ms: int) -> tuple[PublicUpdate, ...]:
    """At most one catalyst delta and one source update per adopted revision.

    Explanatory prose and equivalent translations are not additional triggers.
    A correction is dispatched to its own receipt/update method before target
    selection. Each consumer must use update_id as its atomic receive identity.
    """
    rows = []
    by_ref = {claim.ref: claim for claim in update.claims}
    sections: tuple[tuple[PublicKind, frozenset[ChangeKind]], ...] = (
        ("catalyst_delta", CATALYST_CHANGES),
        ("source_update", SOURCE_CHANGES),
    )
    for kind, kinds in sections:
        changes = tuple(change for change in update.changes if change.kind in kinds)
        if not changes:
            continue
        previous = tuple(sorted({change.previous_content_ref for change in changes if change.previous_content_ref}))
        if kind == "source_update" and not previous:
            # No published ancestor exists for a first report. Do not invent a
            # target revision or turn unsupported evidence into a new entry signal.
            continue
        refs = tuple(sorted({change.current_ref for change in changes}))
        claims = tuple(by_ref[ref] for ref in refs)
        relationships = tuple(row for row in update.evidence_relations if row.claim_ref in refs)
        cited = {citation.evidence_ref for claim in claims for citation in claim.citations}
        cited |= {row.evidence_ref for row in relationships}
        evidence = tuple(item for item in update.evidence if item.ref in cited)
        text = [
            *(claim_text(claim) for claim in claims),
            *(change_text(change) for change in changes),
            *(
                f"source={item.source.publisher_id}; origin={item.source.origin_id or 'unknown'}; "
                f"authority={item.source.source_authority}; evidence={item.ref}; text={item.text}"
                for item in evidence
            ),
            *(f"relationship={row.claim_ref}/{row.evidence_ref}:{row.relation}" for row in relationships),
        ]
        rows.append(
            PublicUpdate(
                update_id=PublicUpdate.identity_for(update.event_id, update.content_revision, kind, refs),
                kind=kind,
                event_id=update.event_id,
                content_revision=update.content_revision,
                claim_refs=refs,
                claims=claims,
                evidence=evidence,
                changes=changes,
                evidence_relations=relationships,
                previous_content_refs=previous,
                affected_claim_refs=tuple(sorted({change.previous_ref for change in changes if change.previous_ref})),
                superseded_claim_refs=_superseded(changes) if kind == "catalyst_delta" else (),
                retired_claim_refs=_retired(changes, update) if kind == "source_update" else (),
                first_available_at_ms=min(claim.first_available_at_ms for claim in claims),
                semantic_completed_at_ms=semantic_completed_at_ms,
                text="\n\n".join(text),
            )
        )
    return tuple(rows)
