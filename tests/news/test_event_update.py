"""Event-local identity and source-grounded, multi-proposition understanding."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tracefold.news.event_update import (
    ClaimComparison,
    ClaimDraft,
    EvidenceRelationDraft,
    OpenQuestionDraft,
    Quantity,
    SourceEvidence,
    UnderstandingDraft,
    assemble_event_update,
    material_differences,
    notification_intent_id,
    source_text,
)


@pytest.fixture
def source() -> SourceEvidence:
    return SourceEvidence(
        evidence_ref="source:1",
        source_item_id="item:1",
        source_artifact_id="original:1",
        source="Issuer",
        url="https://example.test/statement",
        text="The company announced a 25 percent tariff effective October 1. It expects sales of 100 million USD.",
        content_sha256="a" * 64,
        available_at_ms=1000,
        reported_published_at_ms=900,
    )


def _claim(**updates: object) -> ClaimDraft:
    return ClaimDraft.model_validate(
        {
            "text": "The company announced a tariff effective October 1.",
            "subject": "Company",
            "action": "announced",
            "object": "tariff",
            "mode": "decision",
            "phase": "announced",
            "polarity": "affirmed",
            "effective_at": "October 1",
            "jurisdiction": "BR",
            "instrument_term": None,
            "quantities": [{"name": "tariff", "value": "25", "unit": "percent"}],
            "evidence_quotes": [
                {"evidence_ref": "source:1", "quote": "The company announced a 25 percent tariff effective October 1."}
            ],
            **updates,
        }
    )


def test_announcement_date_does_not_make_action_effective(source: SourceEvidence) -> None:
    update = assemble_event_update(
        event_id="event:1", draft=UnderstandingDraft(claims=(_claim(),)), evidence=(source,), first_available_at_ms=2000
    )
    assert update.claims[0].phase == "announced"
    assert update.claims[0].effective_at == "October 1"
    assert update.first_available_at_ms == 1000


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("jurisdiction", "JP"),
        ("statistical_period", "Q2"),
        ("instrument_term", "10Y"),
        ("mode", "forecast"),
        ("phase", "effective"),
        ("polarity", "negated"),
    ],
)
def test_known_semantic_conflicts_are_not_equivalent(field: str, value: str) -> None:
    base = _claim(statistical_period="Q1", instrument_term="5Y")
    current = base.model_copy(update={field: value})
    assert field in material_differences(current, base)


def test_actual_and_forecast_with_identical_numbers_are_different() -> None:
    actual = _claim(mode="observation", phase="not_applicable")
    forecast = _claim(mode="forecast", phase="not_applicable")
    assert material_differences(actual, forecast) == ("mode",)


def test_quantities_use_decimal_and_unit_not_shared_digit_tokens() -> None:
    assert Quantity(name="rate", value=Decimal("25.00"), unit="percent").value == Decimal(25)
    assert "quantities" in material_differences(
        _claim(quantities=[{"name": "tariff", "value": "35", "unit": "percent"}]), _claim()
    )
    assert "quantity_dimensions" in material_differences(
        _claim(quantities=[{"name": "tariff", "value": "25", "unit": "USD"}]), _claim()
    )


def test_original_quotes_and_references_must_exist(source: SourceEvidence) -> None:
    missing = _claim(evidence_quotes=[{"evidence_ref": "source:unknown", "quote": "not provided"}])
    with pytest.raises(ValueError, match="news_claim_quote_not_in_evidence"):
        assemble_event_update(
            event_id="event:1", draft=UnderstandingDraft(claims=(missing,)), evidence=(source,), first_available_at_ms=1
        )
    invented = _claim(
        evidence_quotes=[{"evidence_ref": source.evidence_ref, "quote": "The tariff is already effective."}]
    )
    with pytest.raises(ValueError, match="news_claim_quote_not_in_evidence"):
        assemble_event_update(
            event_id="event:1",
            draft=UnderstandingDraft(claims=(invented,)),
            evidence=(source,),
            first_available_at_ms=1,
        )


def test_model_rewording_reuses_adopted_claim_and_business_content(source: SourceEvidence) -> None:
    first = assemble_event_update(
        event_id="event:1", draft=UnderstandingDraft(claims=(_claim(),)), evidence=(source,), first_available_at_ms=1
    )
    reference = first.reference(first.claims[0].claim_id)
    second = assemble_event_update(
        event_id="event:1",
        draft=UnderstandingDraft(
            topics=("medtop:20000384",),
            claims=(_claim(text="A tariff announcement will apply on October 1."),),
            comparisons=(ClaimComparison(current_index=0, previous=reference, relation="equivalent"),),
        ),
        evidence=(source,),
        first_available_at_ms=9000,
        previous_update=first,
    )
    assert second.content_id == first.content_id
    assert second.claims == first.claims
    assert second.changes == ()
    assert notification_intent_id(
        update=first, claim_ids=[reference.claim_id], channel="telegram:room"
    ) == notification_intent_id(update=second, claim_ids=[reference.claim_id], channel="telegram:room")


def test_exact_retry_preserves_first_available_without_another_judgment(source: SourceEvidence) -> None:
    first = assemble_event_update(
        event_id="event:1", draft=UnderstandingDraft(claims=(_claim(),)), evidence=(source,), first_available_at_ms=1
    )
    second = assemble_event_update(
        event_id="event:1",
        draft=UnderstandingDraft(claims=(_claim(),)),
        evidence=(source.model_copy(update={"available_at_ms": 9000}),),
        first_available_at_ms=9000,
        previous_update=first,
    )
    assert first.content_id == second.content_id
    assert second.first_available_at_ms == 1000


def test_wrong_equivalence_cannot_hide_new_phase_and_parameter(source: SourceEvidence) -> None:
    first = assemble_event_update(
        event_id="event:1", draft=UnderstandingDraft(claims=(_claim(),)), evidence=(source,), first_available_at_ms=1
    )
    new_source = source.model_copy(
        update={"evidence_ref": "source:2", "text": "A 35 percent tariff is now effective.", "available_at_ms": 2000}
    )
    new_claim = _claim(
        text=new_source.text,
        phase="effective",
        quantities=[{"name": "tariff", "value": "35", "unit": "percent"}],
        evidence_quotes=[{"evidence_ref": "source:2", "quote": new_source.text}],
    )
    second = assemble_event_update(
        event_id="event:1",
        draft=UnderstandingDraft(
            claims=(new_claim,),
            comparisons=(
                ClaimComparison(
                    current_index=0, previous=first.reference(first.claims[0].claim_id), relation="equivalent"
                ),
            ),
        ),
        evidence=(new_source,),
        first_available_at_ms=2000,
        previous_update=first,
    )
    assert second.content_id != first.content_id
    assert any(c.phase == "effective" for c in second.claims)
    assert any(c.phase == "announced" for c in second.claims)
    assert second.changes[0].previous is not None


def test_attributed_forecast_is_not_certified_by_a_first_party_url(source: SourceEvidence) -> None:
    claim = _claim(
        text="The issuer expects sales of 100 million USD.",
        mode="forecast",
        phase="not_applicable",
        speaker="Issuer",
        evidence_quotes=[{"evidence_ref": source.evidence_ref, "quote": "It expects sales of 100 million USD."}],
    )
    update = assemble_event_update(
        event_id="event:1",
        draft=UnderstandingDraft(
            claims=(claim,),
            evidence_relations=(
                EvidenceRelationDraft(
                    claim_index=0, evidence_ref=source.evidence_ref, relation="supports", target="statement"
                ),
            ),
        ),
        evidence=(source,),
        first_available_at_ms=1,
    )
    assert update.claims[0].mode == "forecast"
    assert update.evidence_relations[0].target == "statement"
    assert "source_authority" not in update.model_dump()
    assert "direction" not in update.model_dump()


def test_source_correction_keeps_original_evidence_in_previous_version(source: SourceEvidence) -> None:
    first = assemble_event_update(
        event_id="event:1", draft=UnderstandingDraft(claims=(_claim(),)), evidence=(source,), first_available_at_ms=1
    )
    before = first.model_dump_json()
    correction = source.model_copy(
        update={
            "evidence_ref": "source:2",
            "text": "Correction: the announced tariff was 15 percent, not 25 percent.",
            "available_at_ms": 5000,
        }
    )
    corrected = _claim(
        text=correction.text,
        quantities=[{"name": "tariff", "value": "15", "unit": "percent"}],
        evidence_quotes=[{"evidence_ref": "source:2", "quote": correction.text}],
    )
    second = assemble_event_update(
        event_id="event:1",
        draft=UnderstandingDraft(
            claims=(corrected,),
            comparisons=(
                ClaimComparison(
                    current_index=0,
                    previous=first.reference(first.claims[0].claim_id),
                    relation="corrects_or_conflicts",
                    changes=("parameter", "correction"),
                    cause="source_correction",
                ),
            ),
        ),
        evidence=(correction,),
        first_available_at_ms=5000,
        previous_update=first,
    )
    assert first.model_dump_json() == before
    assert len(second.claims) == 1
    assert second.changes[0].kinds == ("correction",)
    assert second.changes[0].previous == first.reference(first.claims[0].claim_id)


def test_cross_event_translation_is_restatement_and_keeps_original_age(source: SourceEvidence) -> None:
    first = assemble_event_update(
        event_id="event:1", draft=UnderstandingDraft(claims=(_claim(),)), evidence=(source,), first_available_at_ms=1
    )
    translated_source = source.model_copy(
        update={
            "evidence_ref": "source:zh",
            "text": "公司宣布百分之二十五的关税，将于十月一日生效。",
            "available_at_ms": 10000,
        }
    )
    translated = _claim(
        text=translated_source.text, evidence_quotes=[{"evidence_ref": "source:zh", "quote": translated_source.text}]
    )
    second = assemble_event_update(
        event_id="event:2",
        draft=UnderstandingDraft(
            claims=(translated,),
            comparisons=(
                ClaimComparison(
                    current_index=0, previous=first.reference(first.claims[0].claim_id), relation="equivalent"
                ),
            ),
        ),
        evidence=(translated_source,),
        first_available_at_ms=10000,
        prior=first.prior_claims(),
    )
    assert second.changes[0].kinds == ("restatement",)
    assert second.claims[0].first_available_at_ms == 1000


def test_no_arbitrary_read_target_and_no_missing_claim_reference(source: SourceEvidence) -> None:
    with pytest.raises(ValueError, match="news_claim_output_reference_invalid"):
        UnderstandingDraft(
            claims=(_claim(),), open_questions=(OpenQuestionDraft(question="What changed?", claim_indices=(8,)),)
        )
    draft = UnderstandingDraft(
        claims=(_claim(),),
        open_questions=(OpenQuestionDraft(question="What changed?", claim_indices=(0,), target_id="arbitrary-url"),),
    )
    with pytest.raises(ValueError, match="news_question_read_target_invalid"):
        assemble_event_update(event_id="event:1", draft=draft, evidence=(source,), first_available_at_ms=1)


def test_intent_identity_ignores_output_order_and_text_formatting(source: SourceEvidence) -> None:
    other = _claim(
        text="The issuer expects sales of 100 million USD.",
        mode="forecast",
        phase="not_applicable",
        evidence_quotes=[{"evidence_ref": source.evidence_ref, "quote": "It expects sales of 100 million USD."}],
    )
    update = assemble_event_update(
        event_id="event:1",
        draft=UnderstandingDraft(claims=(_claim(), other)),
        evidence=(source,),
        first_available_at_ms=1,
    )
    reordered = assemble_event_update(
        event_id="event:1",
        draft=UnderstandingDraft(claims=(other, _claim())),
        evidence=(source,),
        first_available_at_ms=1,
    )
    refs = [c.claim_id for c in update.claims]
    assert update.content_id == reordered.content_id
    identity = notification_intent_id(update=update, claim_ids=refs, channel="telegram:room")
    assert identity == notification_intent_id(update=reordered, claim_ids=list(reversed(refs)), channel="telegram:room")
    assert identity != notification_intent_id(update=update, claim_ids=refs[:1], channel="telegram:room")
    assert identity != notification_intent_id(update=update, claim_ids=refs, channel="feishu:room")
    text = source_text(update, refs)
    assert source.text.split(" It expects")[0] in text
    assert "mode=forecast" in text
    assert "headline_zh" not in text


def test_reusing_evidence_ref_for_changed_bytes_is_rejected(source):
    original = assemble_event_update(
        event_id="event-1",
        draft=UnderstandingDraft(claims=(_claim(),)),
        evidence=(source,),
        first_available_at_ms=1000,
    )
    changed = source.model_copy(update={"text": source.text + " Changed text.", "content_sha256": "b" * 64})
    with pytest.raises(ValueError, match="news_evidence_reference_content_conflict"):
        assemble_event_update(
            event_id="event-1",
            draft=UnderstandingDraft(claims=(_claim(),)),
            evidence=(changed,),
            first_available_at_ms=2000,
            previous_update=original,
        )
