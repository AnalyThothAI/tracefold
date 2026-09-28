"""Shared deterministic historical numbered Event head fixture."""

from __future__ import annotations

from tests.support.news_update_semantic import STAMP, draft, material
from tracefold.news.events.facts import extract_fact_units
from tracefold.news.updates.contracts import Citation, Extraction, FrozenInput
from tracefold.news.updates.semantics import assemble_update

BODY = (
    "1. Alpha approves a plan.\n"
    "2. Beta reports results.\n"
    "3. If your assets were taken\nIndependent whitehats rescued thousands of NFTs."
)


def historical_head(item_id: str = "digest"):
    evidence = material(BODY)
    evidence = evidence.model_copy(update={"source": evidence.source.model_copy(update={"record_id": item_id})})
    source = FrozenInput(event_id="event-alpha", revision=2, lineage_id="historical", evidence=(evidence,))
    valid = draft(evidence).model_copy(
        update={
            "slot": "a",
            "statement": "Alpha approves a plan.",
            "citations": (Citation(evidence_ref=evidence.ref, quote="Alpha approves a plan."),),
            "topics": ("alpha",),
        }
    )
    sibling = draft(evidence, quantity="30").model_copy(
        update={
            "slot": "b",
            "statement": "Beta reports results.",
            "citations": (Citation(evidence_ref=evidence.ref, quote="Beta reports results."),),
            "topics": ("beta",),
        }
    )
    head = assemble_update(source, Extraction(claims=(valid, sibling)), None, adopted_at_ms=STAMP + 1)
    assert head is not None
    units = extract_fact_units(item_id=item_id, raw_text=BODY, fallback_title="Digest")
    row = {
        "document": head.model_dump(mode="json"),
        "members": [
            {
                "item_id": item_id,
                "fact_id": units[0].fact_id,
                "fact_text": units[0].text,
                "title": "Digest",
                "description": "",
                "evidence_text": BODY,
            }
        ],
        "fact_scopes": {units[0].fact_id: units[0].as_dict()},
    }
    return head, row
