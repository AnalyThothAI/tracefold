"""News public updates built by the real producer for Trading tests.

Every payload comes from News' own `assemble_update` and `public_updates`; only clocks,
wording and the asset are chosen here. `outbox_row` is the News outbox mapping for an
adopted revision: `source_fact_key` is the Event and `source_revision` its content revision.
"""

from __future__ import annotations

from typing import Any

from tracefold.news.updates.contracts import (
    ChangeKind,
    DraftClaim,
    EventUpdate,
    Evidence,
    Extraction,
    FrozenInput,
    PriorClaim,
    PublicUpdate,
    Relation,
    RelationDraft,
    Source,
    SupportDraft,
)
from tracefold.news.updates.public import public_updates
from tracefold.news.updates.semantics import assemble_update


def evidence(text: str, *, revision: int, first_available_at_ms: int) -> Evidence:
    return Evidence.issue(
        text,
        Source(
            publisher_id="wire",
            artifact_id="release-1",
            artifact_revision=str(revision),
            first_available_at_ms=first_available_at_ms,
            origin_id="issuer",
        ),
    )


def draft(item: Evidence, *, symbol: str, quantity: str) -> DraftClaim:
    return DraftClaim.model_validate(
        {
            "slot": "a",
            "statement": item.text,
            "fields": {
                "subject": f"{symbol} protocol",
                "action": "set swap fee",
                "object": "swaps",
                "mode": "decision",
                "phase": "announced",
                "content_kind": "official_measure",
                "quantities": [{"name": "fee", "value": quantity, "unit": "bps"}],
                "assets": [{"symbol": symbol, "market_type": "crypto", "role": "primary"}],
            },
            "citations": [{"evidence_ref": item.ref, "quote": item.text}],
        }
    )


def _only(update: EventUpdate, completed_at_ms: int) -> PublicUpdate:
    (row,) = public_updates(update, semantic_completed_at_ms=completed_at_ms)
    return row


def first_report(
    *,
    event_id: str,
    first_available_at_ms: int,
    completed_at_ms: int,
    symbol: str = "SOL",
) -> tuple[EventUpdate, PublicUpdate]:
    item = evidence(
        f"{symbol} protocol sets the swap fee to 25 bps.", revision=1, first_available_at_ms=first_available_at_ms
    )
    source = FrozenInput(event_id=event_id, revision=1, lineage_id=f"{event_id}:line", evidence=(item,))
    extraction = Extraction(
        claims=(draft(item, symbol=symbol, quantity="25"),),
        supports=(SupportDraft(slot="a", evidence_ref=item.ref, relation="reports"),),
    )
    update = assemble_update(source, extraction, None, adopted_at_ms=completed_at_ms)
    assert update is not None
    return update, _only(update, completed_at_ms)


def next_update(
    head: EventUpdate,
    text: str,
    *,
    previous_ref: str,
    relation: Relation,
    change_kind: ChangeKind | None,
    quantity: str,
    revision: int,
    first_available_at_ms: int,
    completed_at_ms: int,
    symbol: str = "SOL",
) -> tuple[EventUpdate, PublicUpdate]:
    item = evidence(text, revision=revision, first_available_at_ms=first_available_at_ms)
    prior = tuple(
        PriorClaim(event_id=head.event_id, content_revision=head.content_revision, claim=claim) for claim in head.claims
    )
    source = FrozenInput(
        event_id=head.event_id, revision=revision, lineage_id=f"{head.event_id}:line", evidence=(item,), prior=prior
    )
    extraction = Extraction(
        claims=(draft(item, symbol=symbol, quantity=quantity),),
        relations=(RelationDraft(slot="a", previous_ref=previous_ref, relation=relation, change_kind=change_kind),),
        supports=(SupportDraft(slot="a", evidence_ref=item.ref, relation="reports"),),
    )
    update = assemble_update(source, extraction, head, adopted_at_ms=completed_at_ms)
    assert update is not None
    return update, _only(update, completed_at_ms)


def outbox_row(update: PublicUpdate) -> dict[str, Any]:
    return {
        "kind": "catalyst" if update.kind == "catalyst_delta" else "source_update",
        "source_fact_key": update.event_id,
        "source_revision": update.content_revision,
        "payload": update.model_dump(mode="json"),
        "source_recorded_at_ms": update.semantic_completed_at_ms,
    }
