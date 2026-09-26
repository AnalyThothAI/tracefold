"""News-owned public semantic updates; App maps these values into Trading ports."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final, Literal

from pydantic import Field, model_validator

from .artifact_identity import canonical_sha
from .event_update import ClaimReference, Digest, EventUpdate, Exact, Ref, source_text

PUBLIC_UPDATE_SCHEMA: Final = "news_public_update_v1"


class PublicNewsUpdate(Exact):
    schema_version: Literal["news_public_update_v1"] = PUBLIC_UPDATE_SCHEMA
    update_id: Digest
    change_kind: Literal["catalyst_delta", "source_update"]
    event_id: Ref
    content_id: Digest
    claim_refs: tuple[ClaimReference, ...]
    previous_refs: tuple[ClaimReference, ...] = ()
    first_available_at_ms: int = Field(ge=0)
    semantic_completed_at_ms: int = Field(ge=0)
    source_text: str
    event_update: EventUpdate

    @model_validator(mode="after")
    def _references_match_content(self) -> PublicNewsUpdate:
        if self.event_id != self.event_update.event_id or self.content_id != self.event_update.content_id:
            raise ValueError("news_public_update_content_mismatch")
        expected = {self.event_update.reference(claim.claim_id) for claim in self.event_update.claims}
        if (
            not self.claim_refs
            or len(self.claim_refs) != len(set(self.claim_refs))
            or not set(self.claim_refs) <= expected
        ):
            raise ValueError("news_public_update_claim_reference_invalid")
        if self.change_kind == "source_update" and not self.previous_refs:
            raise ValueError("news_public_update_previous_reference_required")
        return self

    def payload(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        selected = {ref.claim_id for ref in self.claim_refs}
        data["assets"] = [
            asset.model_dump(mode="json")
            for claim in self.event_update.claims
            if claim.claim_id in selected
            for asset in claim.assets
        ]
        # Existing trigger freshness fields now project source availability, not
        # the completion time of a model or a newer member of the same Event.
        data["source_recorded_at_ms"] = self.first_available_at_ms
        data["provider_event_at_ms"] = self.first_available_at_ms
        data["source_event_id"] = self.event_id
        return data


def public_updates(
    update: EventUpdate,
    *,
    previous: EventUpdate | None,
    completed_at_ms: int,
) -> tuple[PublicNewsUpdate, ...]:
    if previous is not None and previous.content_id == update.content_id:
        return ()
    catalyst: set[str] = set()
    correction: set[str] = set()
    old_refs: dict[str, ClaimReference] = {}
    for change in update.changes:
        if change.cause in {"source_correction", "evidence_change"} or set(change.kinds) <= {
            "evidence",
            "correction",
            "retraction",
        }:
            correction.add(change.current_claim_id)
            if change.previous is not None:
                old_refs[change.previous.key] = change.previous
        elif not set(change.kinds) <= {"restatement", "unresolved"}:
            catalyst.add(change.current_claim_id)
    if previous is None and not update.changes:
        catalyst.update(claim.claim_id for claim in update.claims)
    # Same proposition with additional support is a source update, even when the
    # understanding backend did not explicitly emit a second change record.
    if previous is not None:
        before = {claim.claim_id: claim for claim in previous.claims}
        after = {claim.claim_id: claim for claim in update.claims}
        if previous.evidence_relations != update.evidence_relations:
            before_relations = set(previous.evidence_relations)
            for relation in update.evidence_relations:
                if relation not in before_relations and relation.claim_id in before and relation.claim_id in after:
                    correction.add(relation.claim_id)
                    ref = previous.reference(relation.claim_id)
                    old_refs[ref.key] = ref
    # A correction is not an entry signal; do not accidentally emit both kinds
    # for the same changed claim because a model also supplied 'new_fact'.
    catalyst.difference_update(correction)
    current_claims = {claim.claim_id: claim for claim in update.claims}
    catalyst = {
        claim_id
        for claim_id in catalyst
        if current_claims[claim_id].mode not in {"commentary", "promotion", "calendar"}
    }
    outputs: list[PublicNewsUpdate] = []
    for kind, selected in (("catalyst_delta", catalyst), ("source_update", correction)):
        if not selected:
            continue
        previous_refs: Sequence[ClaimReference] = (
            tuple(old_refs[key] for key in sorted(old_refs)) if kind == "source_update" else ()
        )
        # Source updates must target existing published claims, not create an
        # unreferenced global invalidation of everything about the Event.
        if kind == "source_update" and not previous_refs:
            continue
        ids = tuple(sorted(selected))
        outputs.append(
            PublicNewsUpdate(
                update_id=canonical_sha((PUBLIC_UPDATE_SCHEMA, update.event_id, update.content_id, kind, ids)),
                change_kind=kind,
                event_id=update.event_id,
                content_id=update.content_id,
                claim_refs=tuple(update.reference(claim_id) for claim_id in ids),
                previous_refs=tuple(previous_refs),
                first_available_at_ms=min(current_claims[claim_id].first_available_at_ms for claim_id in ids),
                semantic_completed_at_ms=completed_at_ms,
                source_text=source_text(update, ids),
                event_update=update,
            )
        )
    return tuple(outputs)
