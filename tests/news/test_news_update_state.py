"""Current knowledge and source-version regressions; production reducers, no model or database."""

import pytest

from tests.support.news_event_updates import STAMP, _draft, first_update, material, raised_update
from tracefold.news.updates.contracts import (
    Citation,
    Evidence,
    Extraction,
    FrozenInput,
    PriorClaim,
    QuestionResolution,
    RelationDraft,
    SupportDraft,
)
from tracefold.news.updates.judgment import ContractFault
from tracefold.news.updates.notification import corroborated, is_key
from tracefold.news.updates.public import public_updates
from tracefold.news.updates.semantics import assemble_update


def source_for(head, evidence, *, event_id=None):
    return FrozenInput(
        event_id=event_id or head.event_id,
        revision=head.input_revision + 1,
        lineage_id="next",
        evidence=(evidence,),
        prior=tuple(
            PriorClaim(event_id=head.event_id, content_revision=head.content_revision, claim=c) for c in head.claims
        ),
        open_questions={gap.ref: gap for gap in head.open_questions},
    )


def test_cross_event_correction_targets_the_external_claim_without_importing_it():
    head = first_update("E1")
    ev = material("Correction: tariff is 50%, not 25%.", revision=2)
    update = assemble_update(
        source_for(head, ev, event_id="E2"),
        Extraction(
            claims=(_draft(ev, rate="50"),),
            relations=(
                RelationDraft(slot="a", previous_ref=head.claims[0].ref, relation="corrects", change_kind="correction"),
            ),
        ),
        None,
        adopted_at_ms=STAMP + 100,
    )
    assert update is not None
    assert head.claims[0].ref not in {c.ref for c in update.claims}
    assert update.retired_claim_refs == ()
    (public,) = public_updates(update, semantic_completed_at_ms=STAMP + 100)
    assert public.retired_claim_refs == (head.claims[0].ref,)


def test_unrelated_claim_keeps_old_gap_implication_and_topic():
    head = first_update("E1")
    ev = material("Agency announces a separate vehicle measure.", publisher="second")
    draft = _draft(ev, rate="10").model_copy(update={"topics": ("separate-topic",)})
    update = assemble_update(
        source_for(head, ev),
        Extraction(
            claims=(draft,),
            relations=(RelationDraft(slot="a", previous_ref=head.claims[0].ref, relation="unrelated"),),
        ),
        head,
        adopted_at_ms=STAMP + 100,
    )
    assert update is not None
    assert update.open_questions == head.open_questions
    assert update.implications == head.implications
    assert set(update.topics) == {*head.topics, "separate-topic"}


def test_explicit_grounded_resolution_is_adopted_without_new_catalyst():
    head = first_update("E1")
    ev = material("The order has now been signed.", publisher="order")
    extracted = Extraction(
        claims=(),
        resolved_questions=(
            QuestionResolution(
                question_ref=head.open_questions[0].ref,
                citations=(Citation(evidence_ref=ev.ref, quote=ev.text),),
            ),
        ),
    )
    update = assemble_update(source_for(head, ev), extracted, head, adopted_at_ms=STAMP + 100)
    assert update is not None and update.content_revision != head.content_revision
    assert update.claims == head.claims and update.open_questions == ()
    assert public_updates(update, semantic_completed_at_ms=STAMP + 100) == ()
    assert head.open_questions  # Immutable history survives.
    with pytest.raises(ContractFault, match="news_question_not_supplied"):
        assemble_update(
            source_for(head, ev),
            extracted.model_copy(
                update={
                    "resolved_questions": (
                        extracted.resolved_questions[0].model_copy(update={"question_ref": "not-supplied"}),
                    )
                }
            ),
            head,
            adopted_at_ms=STAMP + 100,
        )


def test_supersession_and_annotations_survive_a_later_evidence_update():
    head = first_update("E1")
    raised = raised_update(head)
    assert head.claims[0].ref in raised.superseded_claim_refs
    ev = material("Agency confirms the tariff is 50%.", publisher="confirmation")
    updated = assemble_update(
        source_for(raised, ev),
        Extraction(
            claims=(_draft(ev, rate="50"),),
            relations=tuple(
                RelationDraft(
                    slot="a",
                    previous_ref=c.ref,
                    relation="equivalent" if c.ref == raised.claims[-1].ref else "unrelated",
                )
                for c in raised.claims
            ),
        ),
        raised,
        adopted_at_ms=STAMP + 200,
    )
    assert updated is not None
    assert updated.superseded_claim_refs == raised.superseded_claim_refs
    assert updated.open_questions == raised.open_questions


@pytest.mark.parametrize("authority", ["unknown", "issuer_first_party"])
def test_attribution_and_authority_corrections_do_not_accumulate_corroboration(authority):
    ev = material("Agency announces 25% tariff.", origin="A", authority=authority)
    ev = Evidence.issue(ev.text, ev.source.model_copy(update={"record_id": "same-item", "revision_sequence": 0}))
    first = assemble_update(
        FrozenInput(event_id="E", revision=1, lineage_id="l", evidence=(ev,)),
        Extraction(
            claims=(_draft(ev, rate="25"),),
            supports=(SupportDraft(slot="a", evidence_ref=ev.ref, relation="supports"),),
        ),
        None,
        adopted_at_ms=STAMP,
    )
    revised = Evidence.issue(
        ev.text,
        ev.source.model_copy(
            update={
                "origin_id": "B",
                "source_authority": "unknown",
                "artifact_revision": "r2",
                "revision_sequence": 1,
            }
        ),
    )
    updated = assemble_update(
        source_for(first, revised),
        Extraction(
            claims=(_draft(revised, rate="25"),),
            relations=(RelationDraft(slot="a", previous_ref=first.claims[0].ref, relation="equivalent"),),
            supports=(SupportDraft(slot="a", evidence_ref=revised.ref, relation="supports"),),
        ),
        first,
        adopted_at_ms=STAMP + 100,
    )
    assert updated is not None and not corroborated(updated.claims[0], updated)
    assert len(updated.evidence) == 2 and len(updated.evidence_relations) == 2


@pytest.mark.parametrize("legacy_record", [False, True])
def test_unproductive_source_replacement_removes_old_authority_without_inventing_refutation(legacy_record):
    ev = material("Agency announces 25% tariff.", authority="issuer_first_party")
    ev = Evidence.issue(ev.text, ev.source.model_copy(update={"record_id": None if legacy_record else "same-item"}))
    first = assemble_update(
        FrozenInput(event_id="E", revision=1, lineage_id="l", evidence=(ev,)),
        Extraction(
            claims=(_draft(ev, rate="25"),),
            supports=(SupportDraft(slot="a", evidence_ref=ev.ref, relation="supports"),),
        ),
        None,
        adopted_at_ms=STAMP,
    )
    revised = Evidence.issue(
        "This page has been replaced.",
        ev.source.model_copy(
            update={
                "origin_id": "B",
                "source_authority": "unknown",
                "artifact_revision": "r2",
                "revision_sequence": 1,
                "record_id": "same-item",
            }
        ),
    )
    updated = assemble_update(source_for(first, revised), Extraction(claims=()), first, adopted_at_ms=STAMP + 100)
    assert updated is not None and not corroborated(updated.claims[0], updated)
    assert any(r.evidence_ref == revised.ref and r.relation == "unresolved" for r in updated.evidence_relations)
    (public,) = public_updates(updated, semantic_completed_at_ms=STAMP + 100)
    assert public.kind == "source_update" and not public.retired_claim_refs


def test_v1_archive_identity_survives_v2_topic_transition():
    from tracefold.news.updates.contracts import EventUpdate, content_revision_for
    from tracefold.news.updates.identity import digest

    head = first_update("E1")
    legacy = head.model_copy(
        update={
            "schema_version": "news_event_update_v1",
            "claims": tuple(c.model_copy(update={"topics": ()}) for c in head.claims),
        }
    )
    sha = digest(legacy.content_material())
    document = legacy.model_dump(mode="json")
    document.update(content_sha=sha, content_revision=content_revision_for(sha, None))
    for claim in document["claims"]:
        del claim["topics"]
    for evidence in document["evidence"]:
        del evidence["source"]["record_id"]
        del evidence["source"]["revision_sequence"]
    del document["superseded_claim_refs"]
    legacy = EventUpdate.model_validate(document)
    assert legacy.content_sha == sha
    assert is_key(legacy.claims[0], legacy) == is_key(head.claims[0], head) is True
    ev = material("Agency adds a separate provision.", publisher="new")
    updated = assemble_update(source_for(legacy, ev), Extraction(claims=()), legacy, adopted_at_ms=STAMP + 100)
    assert updated is not None and updated.schema_version == "news_event_update_v2"
    assert updated.topics == legacy.topics
    assert updated.open_questions == legacy.open_questions
    assert legacy.model_dump(mode="json")["claims"][0]["topics"] == []


def test_added_parameter_information_does_not_retire_the_previous_claim():
    head = first_update("E1")
    ev = material("The existing tariff also has a 10% exemption.", publisher="detail")
    updated = assemble_update(
        source_for(head, ev),
        Extraction(
            claims=(_draft(ev, rate="10"),),
            relations=(
                RelationDraft(
                    slot="a",
                    previous_ref=head.claims[0].ref,
                    relation="adds_information",
                    change_kind="parameter_change",
                ),
            ),
        ),
        head,
        adopted_at_ms=STAMP + 100,
    )
    assert updated is not None and not updated.superseded_claim_refs
    assert updated.open_questions == head.open_questions
    assert not public_updates(updated, semantic_completed_at_ms=STAMP + 100)[0].superseded_claim_refs
