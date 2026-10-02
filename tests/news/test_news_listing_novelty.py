"""Keep the existing listing novelty protections while replacing candidate retrieval."""

from __future__ import annotations

import pytest

from tests.support.news_update_semantic import draft, material
from tracefold.news.notifications.novelty import ClaimLink, LinkedReceipt, reader_novelty
from tracefold.news.storage.notification_context import listing_compatible_links
from tracefold.news.updates.assembly import assemble_update
from tracefold.news.updates.contracts import Asset, Extraction, FrozenInput
from tracefold.news.updates.identity import digest

STAMP = 1_790_405_000_000


def claim(statement: str, *, asset: str | None = None, market: str = "commodity", subject: str | None = None):
    evidence = material(statement)
    original = draft(evidence)
    fields = original.fields.model_copy(
        update={
            "subject": subject or asset or statement,
            "object": "",
            "assets": () if asset is None else (Asset(symbol=asset, market_type=market, role="primary"),),
        }
    )
    update = assemble_update(
        FrozenInput(event_id=digest(statement), revision=1, lineage_id=digest(statement), evidence=(evidence,)),
        Extraction(claims=(original.model_copy(update={"statement": statement, "fields": fields}),)),
        None,
        adopted_at_ms=STAMP,
    )
    assert update is not None
    return update.claims[0]


def listing_claim(symbol: str):
    original = claim(f"Aster lists {symbol} perpetual", asset=symbol, market="crypto", subject="Aster")
    return original.model_copy(update={"fields": original.fields.model_copy(update={"action": "listed"})})


@pytest.mark.parametrize("relation", ["equivalent", "adds_information", "real_world_change"])
def test_proven_different_old_listing_links_no_longer_establish_reader_novelty(relation) -> None:
    ct, si = listing_claim("CTUSDT"), listing_claim("SIUSDT")
    links = (ClaimLink(current_ref=ct.ref, previous_ref=si.ref, relation=relation, asserted_at_ms=STAMP - 1),)
    receipts = (LinkedReceipt(intent_id="si", state="sent", claim_refs=(si.ref,), settled_at_ms=STAMP - 2),)
    assert reader_novelty(ct.ref, links, receipts).novelty != "unlinked"
    valid = listing_compatible_links(links, {ct.ref: ct, si.ref: si})
    assert reader_novelty(ct.ref, valid, receipts).novelty == "unlinked"
    # Removing either original claim loses the proof; free-text differences alone cannot retract a link.
    assert listing_compatible_links(links, {ct.ref: ct}) == links
    assert listing_compatible_links(links, {si.ref: si}) == links


def test_two_hop_listing_protection_uses_known_intermediate_claim_and_keeps_unknowns() -> None:
    ct, si = listing_claim("CTUSDT"), listing_claim("SIUSDT")
    middle = ct.model_copy(update={"ref": "cl:intermediate-ct"})
    links = (
        ClaimLink(current_ref=ct.ref, previous_ref=middle.ref, relation="equivalent", asserted_at_ms=STAMP - 2),
        ClaimLink(current_ref=middle.ref, previous_ref=si.ref, relation="adds_information", asserted_at_ms=STAMP - 1),
    )
    receipts = (LinkedReceipt(intent_id="si", state="sent", claim_refs=(si.ref,), settled_at_ms=STAMP - 3),)
    assert reader_novelty(ct.ref, links, receipts).novelty == "increment"
    valid = listing_compatible_links(links, {ct.ref: ct, middle.ref: middle, si.ref: si})
    assert reader_novelty(ct.ref, valid, receipts).novelty == "unlinked"
    unknown = listing_compatible_links(links, {ct.ref: ct, si.ref: si})
    assert reader_novelty(ct.ref, unknown, receipts).novelty == "increment"


def test_listing_link_filter_does_not_revive_older_assertions_or_remove_corrections() -> None:
    ct, si = listing_claim("CTUSDT"), listing_claim("SIUSDT")
    correction = ClaimLink(current_ref=ct.ref, previous_ref=si.ref, relation="corrects", asserted_at_ms=STAMP - 2)
    wrong = ClaimLink(current_ref=ct.ref, previous_ref=si.ref, relation="equivalent", asserted_at_ms=STAMP - 1)
    claims = {ct.ref: ct, si.ref: si}
    assert listing_compatible_links((correction,), claims) == (correction,)
    assert listing_compatible_links((correction, wrong), claims) == ()
    effective = ct.model_copy(
        update={"ref": "cl:effective-ct", "fields": ct.fields.model_copy(update={"phase": "effective"})}
    )
    development = ClaimLink(
        current_ref=effective.ref, previous_ref=ct.ref, relation="real_world_change", asserted_at_ms=STAMP - 1
    )
    assert listing_compatible_links((development,), {effective.ref: effective, ct.ref: ct}) == (development,)
