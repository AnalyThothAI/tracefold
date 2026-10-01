"""Behavior examples for the new core. No provider/PG credentials or calls."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.support.news_update_semantic import STAMP, draft, material, prior_of, update_one
from tracefold.news.notifications.novelty import ClaimLink, LinkedReceipt, reader_novelty
from tracefold.news.updates.assembly import assemble_update, proven_mismatches
from tracefold.news.updates.contracts import (
    Asset,
    Change,
    Citation,
    EventUpdate,
    Evidence,
    Extraction,
    FrozenInput,
    IdentityHint,
    PublicUpdate,
    RelationDraft,
    SupportDraft,
)
from tracefold.news.updates.public import public_updates


def next_revision(
    head: EventUpdate,
    text: str,
    *relations: RelationDraft,
    quantity: str = "25",
) -> EventUpdate:
    evidence = material(text, revision=2)
    source = FrozenInput(
        event_id=head.event_id, revision=2, lineage_id="line-1", evidence=(evidence,), prior=prior_of(head)
    )
    result = Extraction(claims=(draft(evidence, quantity=quantity),), relations=relations)
    update = assemble_update(source, result, head, adopted_at_ms=STAMP + 100)
    assert update is not None
    return update


def test_future_effective_date_does_not_replace_announced_phase() -> None:
    _, _, update = update_one()
    assert update.claims[0].fields.phase == "announced"
    assert update.claims[0].fields.effective_at == "2026-10-01"


def test_first_report_without_priors_is_a_new_fact_catalyst() -> None:
    _, _, update = update_one()
    assert [change.kind for change in update.changes] == ["new_fact"]
    rows = public_updates(update, semantic_completed_at_ms=STAMP + 9)
    assert [row.kind for row in rows] == ["catalyst_delta"]
    assert rows[0].claims[0].fields.assets[0].symbol == "CL"
    assert rows[0].first_available_at_ms == STAMP + 1
    assert "asset=CL; market=commodity; role=primary" in rows[0].text


def test_wording_and_model_rerun_do_not_create_content_revision() -> None:
    source, extraction, head = update_one()
    reworded = extraction.claims[0].model_copy(update={"statement": "Same fact, different wording."})
    changed = extraction.model_copy(update={"claims": (reworded,)})
    assert assemble_update(source, changed, head, adopted_at_ms=STAMP + 9999) is None


def test_a_different_content_reading_does_not_create_a_new_claim() -> None:
    source, extraction, head = update_one()
    fields = extraction.claims[0].fields.model_copy(update={"content_kind": "state_change"})
    reread = extraction.model_copy(update={"claims": (extraction.claims[0].model_copy(update={"fields": fields}),)})
    assert assemble_update(source, reread, head, adopted_at_ms=STAMP + 9999) is None


def test_expected_and_actual_with_identical_number_are_not_equivalent() -> None:
    _, _, head = update_one()
    current = draft(material("Observed tariff rate is 25%."), mode="observation")
    previous = head.claims[0].model_copy(
        update={"fields": head.claims[0].fields.model_copy(update={"mode": "forecast"})}
    )
    assert proven_mismatches(current, previous) == ("mode",)


def test_known_different_subject_identity_cannot_be_equivalent() -> None:
    source, extraction, head = update_one()
    quote = extraction.claims[0].citations[0].quote
    prior_hint = IdentityHint(key="subject_id", value="BR", evidence_ref=source.evidence[0].ref, surface=quote)
    prior = head.claims[0].model_copy(update={"known_identity": (prior_hint,)})
    hints = (IdentityHint(key="subject_id", value="TR", evidence_ref=source.evidence[0].ref, surface=quote),)
    assert proven_mismatches(extraction.claims[0], prior, hints) == ("subject_id",)


def crypto_listing(symbol: str, *, revision: int = 1, phase: str = "announced"):
    evidence = material(f"Aster listing: {symbol}", revision=revision)
    original = draft(evidence)
    fields = original.fields.model_copy(
        update={
            "subject": "Aster",
            "action": "listed",
            "object": f"{symbol} perpetual contract",
            "mode": "observation",
            "phase": phase,
            "content_kind": "state_change",
            "assets": (Asset(symbol=symbol, market_type="crypto", role="primary"),),
            "quantities": (),
            "effective_at": None,
        }
    )
    return evidence, original.model_copy(update={"fields": fields})


@pytest.mark.parametrize("symbols", [("SIUSDT", "CTUSDT"), ("SI", "CT")])
@pytest.mark.parametrize(
    "relation,kind", [("equivalent", None), ("adds_information", "new_fact"), ("real_world_change", "scope_change")]
)
def test_different_explicit_crypto_listings_are_independent_facts(symbols, relation, kind) -> None:
    earlier_evidence, earlier = crypto_listing(symbols[0])
    first_source = FrozenInput(event_id="aster", revision=1, lineage_id="aster", evidence=(earlier_evidence,))
    head = assemble_update(first_source, Extraction(claims=(earlier,)), None, adopted_at_ms=STAMP)
    assert head is not None
    current_evidence, current = crypto_listing(symbols[1], revision=2)
    source = FrozenInput(
        event_id="aster", revision=2, lineage_id="aster", evidence=(current_evidence,), prior=prior_of(head)
    )
    assert "listing_asset" in proven_mismatches(current, head.claims[0])
    updated = assemble_update(
        source,
        Extraction(
            claims=(current,),
            relations=(
                RelationDraft(slot=current.slot, previous_ref=head.claims[0].ref, relation=relation, change_kind=kind),
            ),
        ),
        head,
        adopted_at_ms=STAMP + 10,
    )
    assert updated is not None
    new = updated.claims[-1]
    assert new.ref != head.claims[0].ref
    assert [(change.kind, change.previous_ref) for change in updated.changes] == [("new_fact", None)]
    assert (
        reader_novelty(
            new.ref,
            (),
            (
                LinkedReceipt(
                    intent_id="si",
                    state="sent",
                    claim_refs=(head.claims[0].ref,),
                    settled_at_ms=STAMP,
                ),
            ),
        ).novelty
        == "unlinked"
    )


def test_same_crypto_listing_stage_change_remains_a_development() -> None:
    earlier_evidence, earlier = crypto_listing("CTUSDT")
    source = FrozenInput(event_id="aster", revision=1, lineage_id="aster", evidence=(earlier_evidence,))
    head = assemble_update(source, Extraction(claims=(earlier,)), None, adopted_at_ms=STAMP)
    assert head is not None
    current_evidence, current = crypto_listing("CTUSDT", revision=2, phase="effective")
    source = FrozenInput(
        event_id="aster", revision=2, lineage_id="aster", evidence=(current_evidence,), prior=prior_of(head)
    )
    updated = assemble_update(
        source,
        Extraction(
            claims=(current,),
            relations=(
                RelationDraft(
                    slot=current.slot,
                    previous_ref=head.claims[0].ref,
                    relation="real_world_change",
                    change_kind="phase_change",
                ),
            ),
        ),
        head,
        adopted_at_ms=STAMP + 10,
    )
    assert updated is not None
    links = tuple(
        ClaimLink(
            current_ref=change.current_ref,
            previous_ref=change.previous_ref,
            relation=change.relation,
            asserted_at_ms=STAMP + 10,
        )
        for change in updated.changes
        if change.previous_ref is not None
    )
    novelty = reader_novelty(
        updated.claims[-1].ref,
        links,
        (
            LinkedReceipt(
                intent_id="announcement",
                state="sent",
                claim_refs=(head.claims[0].ref,),
                settled_at_ms=STAMP,
            ),
        ),
    )
    assert novelty.novelty == "development"


def test_a_ticker_versus_pair_or_unquoted_asset_stays_an_unknown_identity() -> None:
    earlier_evidence, earlier = crypto_listing("CTUSDT")
    source = FrozenInput(event_id="aster", revision=1, lineage_id="aster", evidence=(earlier_evidence,))
    head = assemble_update(source, Extraction(claims=(earlier,)), None, adopted_at_ms=STAMP)
    assert head is not None
    _, current = crypto_listing("CT", revision=2)
    assert "listing_asset" not in proven_mismatches(current, head.claims[0])
    _, current = crypto_listing("SIUSDT", revision=2)
    unquoted = current.model_copy(update={"citations": earlier.citations})
    assert "listing_asset" not in proven_mismatches(unquoted, head.claims[0])


def correction_update() -> tuple[EventUpdate, EventUpdate]:
    _, _, head = update_one()
    evidence = material("Correction: the announced tariff was 50%, not 25%.", revision=2)
    source = FrozenInput(
        event_id=head.event_id, revision=2, lineage_id="line-1", evidence=(evidence,), prior=prior_of(head)
    )
    result = Extraction(
        claims=(draft(evidence, quantity="50"),),
        relations=(
            RelationDraft(slot="a", previous_ref=head.claims[0].ref, relation="corrects", change_kind="correction"),
        ),
        supports=(SupportDraft(slot="a", evidence_ref=evidence.ref, relation="reports"),),
    )
    updated = assemble_update(source, result, head, adopted_at_ms=STAMP + 100)
    assert updated is not None
    return head, updated


def test_correction_is_source_update_not_new_entry_signal() -> None:
    old, updated = correction_update()
    public = public_updates(updated, semantic_completed_at_ms=STAMP + 90)
    assert len(public) == 1
    assert public[0].kind == "source_update"
    assert public[0].affected_claim_refs == (old.claims[0].ref,)
    assert public[0].retired_claim_refs == (old.claims[0].ref,)
    assert public[0].superseded_claim_refs == ()
    assert old.claims[0].ref in updated.retired_claim_refs
    assert "50" in public[0].text
    assert updated.claims[-1].first_available_at_ms == STAMP + 2
    assert old.claims[0].first_available_at_ms == STAMP + 1


# ------------------------------------------------------------------ unresolved relations


def test_unresolved_relation_to_a_supplied_prior_is_possible_new_not_a_catalyst() -> None:
    _, _, head = update_one()
    updated = next_revision(
        head,
        "Agency tariff on imports is reported at 30%.",
        RelationDraft(slot="a", previous_ref=head.claims[0].ref, relation="unresolved"),
        quantity="30",
    )
    new = updated.claims[-1]
    assert [(change.kind, change.previous_ref, change.relation) for change in updated.changes] == [
        ("possible_new", head.claims[0].ref, "unresolved")
    ]
    assert new.ref != head.claims[0].ref
    # The claim is adopted content, but nothing is published to Trading as new.
    assert public_updates(updated, semantic_completed_at_ms=STAMP + 200) == ()


def test_a_missing_relation_or_a_refuted_equivalence_also_leaves_the_claim_possible_new() -> None:
    _, _, head = update_one()
    missing = next_revision(head, "Agency tariff on imports is reported at 30%.", quantity="30")
    assert [change.kind for change in missing.changes] == ["possible_new"]
    refuted = next_revision(
        head,
        "Agency tariff on imports is reported at 30%.",
        RelationDraft(slot="a", previous_ref=head.claims[0].ref, relation="equivalent"),
        quantity="30",
    )
    assert [change.kind for change in refuted.changes] == ["possible_new"]


def test_unrelated_to_every_supplied_prior_stays_a_new_fact() -> None:
    _, _, head = update_one()
    updated = next_revision(
        head,
        "Agency tariff on steel imports is 30%.",
        RelationDraft(slot="a", previous_ref=head.claims[0].ref, relation="unrelated"),
        quantity="30",
    )
    assert [change.kind for change in updated.changes] == ["new_fact"]
    rows = public_updates(updated, semantic_completed_at_ms=STAMP + 200)
    assert [row.kind for row in rows] == ["catalyst_delta"]
    assert rows[0].superseded_claim_refs == ()


def test_possible_new_requires_an_unresolved_prior_and_is_never_public() -> None:
    with pytest.raises(ValidationError, match="news_possible_new_requires_unresolved_prior"):
        Change(kind="possible_new", current_ref="cl:a")
    _, _, update = update_one()
    row = public_updates(update, semantic_completed_at_ms=STAMP)[0]
    unresolved = Change(
        kind="possible_new",
        current_ref=row.claim_refs[0],
        previous_ref="cl:b",
        previous_content_ref="update:b",
        relation="unresolved",
    )
    with pytest.raises(ValidationError, match="news_public_possible_new_not_publishable"):
        PublicUpdate.model_validate({**row.model_dump(), "changes": (unresolved,)})


# ------------------------------------------------------------------ claim-scoped public contract


def test_a_real_world_parameter_change_names_the_superseded_claim() -> None:
    _, _, head = update_one()
    updated = next_revision(
        head,
        "Agency raises the tariff to 50%.",
        RelationDraft(
            slot="a", previous_ref=head.claims[0].ref, relation="real_world_change", change_kind="parameter_change"
        ),
        quantity="50",
    )
    rows = public_updates(updated, semantic_completed_at_ms=STAMP + 200)
    assert [row.kind for row in rows] == ["catalyst_delta"]
    assert rows[0].superseded_claim_refs == (head.claims[0].ref,)
    assert rows[0].affected_claim_refs == (head.claims[0].ref,)
    assert rows[0].retired_claim_refs == ()
    assert "change=parameter_change" in rows[0].text


def test_added_information_does_not_supersede_the_earlier_claim() -> None:
    _, _, head = update_one()
    updated = next_revision(
        head,
        "Agency tariff exempts medicines.",
        RelationDraft(
            slot="a", previous_ref=head.claims[0].ref, relation="adds_information", change_kind="scope_change"
        ),
    )
    rows = public_updates(updated, semantic_completed_at_ms=STAMP + 200)
    assert [row.kind for row in rows] == ["catalyst_delta"]
    assert rows[0].affected_claim_refs == (head.claims[0].ref,)
    assert rows[0].superseded_claim_refs == ()


def test_public_identity_is_stable_across_recomputation() -> None:
    _, updated = correction_update()
    first = public_updates(updated, semantic_completed_at_ms=STAMP + 90)
    again = public_updates(EventUpdate.model_validate_json(updated.model_dump_json()), semantic_completed_at_ms=STAMP)
    assert [row.update_id for row in first] == [row.update_id for row in again]


def test_bad_quote_is_a_contract_error_not_no_news() -> None:
    source, extraction, _ = update_one()
    fabricated = (Citation(evidence_ref=source.evidence[0].ref, quote="fabricated quote"),)
    broken = extraction.model_copy(
        update={"claims": (extraction.claims[0].model_copy(update={"citations": fabricated}),)}
    )
    with pytest.raises(ValueError, match="news_citation_not_in_frozen_source"):
        assemble_update(source, broken, None, adopted_at_ms=STAMP)


def test_legacy_judgment_does_not_silently_upgrade_to_event_update() -> None:
    with pytest.raises(ValidationError):
        EventUpdate.model_validate(
            {"judgment_contract_version": "news_judgment_v3", "verdict": {"headline_zh": "旧记录"}}
        )


def test_source_authority_is_code_owned_provenance_not_evidence_identity() -> None:
    plain = material("Agency announces 25% tariff effective October 1.")
    named = Evidence.issue(plain.text, plain.source.model_copy(update={"source_authority": "issuer_first_party"}))
    assert named.ref == plain.ref
    assert Asset(symbol="CL", market_type="commodity", role="primary") in draft(plain).fields.assets
