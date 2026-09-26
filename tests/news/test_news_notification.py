"""Real receipt content, multi-claim selection, and card-independent public updates."""

from __future__ import annotations

import asyncio
import time

import pytest
from dspy.utils import DummyLM

from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.event_update import (
    ClaimComparison,
    ClaimDraft,
    EvidenceQuote,
    SourceEvidence,
    UnderstandingDraft,
    assemble_event_update,
)
from tracefold.news.judgment import JudgmentDeadline
from tracefold.news.notification import DeliveredContent, NotificationPlanner, NotificationPolicy
from tracefold.news.program.judgment import GenerativeNewsJudgmentBackend
from tracefold.news.public_updates import public_updates


def _update(*, mode: str = "decision", event: str = "event:1"):
    text = "Company announced a production expansion. It expects sales to increase next year."
    source = SourceEvidence(
        evidence_ref="source:1",
        source_item_id="item:1",
        source_artifact_id="artifact:1",
        source="Company",
        text=text,
        url="",
        reported_published_at_ms=None,
        content_sha256=canonical_sha(text),
        available_at_ms=1000,
    )
    return assemble_event_update(
        event_id=event,
        draft=UnderstandingDraft(
            claims=(
                ClaimDraft(
                    text="Company announced a production expansion.",
                    mode=mode,
                    phase="announced",
                    evidence_quotes=(
                        EvidenceQuote(evidence_ref="source:1", quote=text.split(". ", maxsplit=1)[0] + "."),
                    ),
                ),
                ClaimDraft(
                    text="It expects sales to increase next year.",
                    mode="forecast",
                    phase="not_applicable",
                    evidence_quotes=(
                        EvidenceQuote(evidence_ref="source:1", quote="It expects sales to increase next year."),
                    ),
                ),
            )
        ),
        evidence=(source,),
        first_available_at_ms=1000,
    )


def _receipt(text: str = "公司宣布扩产。", *, channel: str = "telegram") -> DeliveredContent:
    return DeliveredContent(
        intent_id="a" * 64,
        event_id="event:earlier",
        channel=channel,
        sent_at_ms=1100,
        content_sha256=canonical_sha(text),
        text=text,
    )


def _select(update, answers, receipts=(), *, now_ms=1200):
    calls = []

    def lm(_):
        calls.append(True)
        return DummyLM([answers])

    planner = NotificationPlanner(GenerativeNewsJudgmentBackend(lm))
    selection = asyncio.run(
        planner.select(
            update,
            receipts=receipts,
            channel="telegram",
            now_ms=now_ms,
            deadline=JudgmentDeadline.start(total_at=time.monotonic() + 10),
        )
    )
    return selection, calls


def test_no_receipt_means_no_coverage_call_and_both_claims_selected():
    update = _update()
    result, calls = _select(update, {})
    assert calls == []
    assert result.decision == "notify"
    assert set(result.selected_claim_ids) == {claim.claim_id for claim in update.claims}
    assert result.intent_id is not None


def test_partial_coverage_keeps_uncovered_claim_instead_of_dropping_story():
    update = _update()
    result, calls = _select(update, {"item_0_coverage": "full", "item_1_coverage": "partial"}, (_receipt(),))
    assert len(calls) == 1
    assert result.selected_claim_ids == (update.claims[1].claim_id,)
    assert result.decision == "notify"


@pytest.mark.parametrize("coverage", ["none", "partial", "unresolved", "unavailable"])
def test_unknown_or_incomplete_coverage_is_never_full_coverage(coverage):
    result, _ = _select(_update(), {"item_0_coverage": coverage, "item_1_coverage": coverage}, (_receipt(),))
    assert len(result.selected_claim_ids) == 2


def test_full_translation_coverage_suppresses_without_card_generation():
    result, _ = _select(_update(), {"item_0_coverage": "full", "item_1_coverage": "full"}, (_receipt(),))
    assert result.decision == "no_notification" and result.intent_id is None
    assert result.selected_claim_ids == ()
    assert result.reason == "delivered_content_covers_all"


def test_receipts_from_other_channel_do_not_cover_destination():
    result, calls = _select(_update(), {}, (_receipt(channel="email"),))
    assert len(result.selected_claim_ids) == 2 and calls == []


def test_comment_in_multi_claim_report_does_not_drop_concrete_forecast():
    update = _update(mode="commentary")
    result, calls = _select(update, {})
    assert calls == [] and len(result.selected_claim_ids) == 1
    assert next(claim for claim in update.claims if claim.claim_id == result.selected_claim_ids[0]).mode == "forecast"


def test_unknown_mode_not_silently_converted_to_no_news():
    result, _ = _select(_update(mode="unknown"), {})
    assert len(result.selected_claim_ids) == 2


def test_old_claim_does_not_become_fresh_when_recomputed():
    result, calls = _select(_update(), {}, now_ms=1000 + 13 * 3600_000)
    assert calls == [] and result.decision == "no_notification"
    assert NotificationPolicy(stale_source_max_age_s=0).stale_source_max_age_s == 0


def test_public_catalyst_is_card_independent_and_preserves_source_clock():
    update = _update()
    published = public_updates(update, previous=None, completed_at_ms=90000)
    assert len(published) == 1 and published[0].change_kind == "catalyst_delta"
    payload = published[0].payload()
    assert "headline" not in payload and "why" not in payload
    assert "[source:1] Company" in payload["source_text"]
    assert payload["source_recorded_at_ms"] == 1000
    assert payload["semantic_completed_at_ms"] == 90000
    assert public_updates(update, previous=update, completed_at_ms=100000) == ()


def test_report_correction_targets_prior_claim_and_is_not_new_catalyst():
    first = _update()
    old = first.claims[0]
    text = "Correction: the earlier announcement was withdrawn by the publisher, not by the company."
    evidence = SourceEvidence(
        evidence_ref="source:correction",
        source_item_id="item:correction",
        source_artifact_id="artifact:correction",
        source="Publisher",
        text=text,
        url="",
        reported_published_at_ms=None,
        content_sha256=canonical_sha(text),
        available_at_ms=3000,
    )
    draft = ClaimDraft(
        text=text,
        mode="observation",
        phase="not_applicable",
        evidence_quotes=(EvidenceQuote(evidence_ref=evidence.evidence_ref, quote=text),),
    )
    corrected = assemble_event_update(
        event_id=first.event_id,
        draft=UnderstandingDraft(
            claims=(draft,),
            comparisons=(
                ClaimComparison(
                    current_index=0,
                    previous=first.reference(old.claim_id),
                    relation="corrects_or_conflicts",
                    changes=("correction",),
                    cause="source_correction",
                ),
            ),
        ),
        evidence=(evidence,),
        first_available_at_ms=3000,
        previous_update=first,
        prior=first.prior_claims(),
    )
    published = public_updates(corrected, previous=first, completed_at_ms=4000)
    assert len(published) == 1 and published[0].change_kind == "source_update"
    assert published[0].previous_refs == (first.reference(old.claim_id),)
    assert published[0].event_update.event_id == first.event_id


def test_real_world_reversal_is_new_action_not_media_correction():
    first = _update()
    old = first.claims[0]
    text = "Company cancelled the expansion."
    evidence = SourceEvidence(
        evidence_ref="source:reversal",
        source_item_id="item:reversal",
        source_artifact_id="artifact:reversal",
        source="Company",
        text=text,
        url="",
        reported_published_at_ms=None,
        content_sha256=canonical_sha(text),
        available_at_ms=3000,
    )
    draft = ClaimDraft(
        text=text,
        mode="decision",
        phase="cancelled",
        evidence_quotes=(EvidenceQuote(evidence_ref=evidence.evidence_ref, quote=text),),
    )
    reversed_update = assemble_event_update(
        event_id=first.event_id,
        draft=UnderstandingDraft(
            claims=(draft,),
            comparisons=(
                ClaimComparison(
                    current_index=0,
                    previous=first.reference(old.claim_id),
                    relation="adds_information",
                    changes=("phase",),
                    cause="world_action",
                ),
            ),
        ),
        evidence=(evidence,),
        first_available_at_ms=3000,
        previous_update=first,
        prior=first.prior_claims(),
    )
    published = public_updates(reversed_update, previous=first, completed_at_ms=4000)
    assert len(published) == 1 and published[0].change_kind == "catalyst_delta"
    assert len(published[0].claim_refs) == 1 and published[0].first_available_at_ms == 3000


def test_unresolved_prior_relation_is_not_silently_a_new_catalyst():
    previous = _update()
    claim = previous.claims[0]
    draft = ClaimDraft.model_validate(claim.model_dump(exclude={"claim_id", "first_available_at_ms"}))
    incoming = assemble_event_update(
        event_id="another-event",
        draft=UnderstandingDraft(
            claims=(draft,),
            comparisons=(
                ClaimComparison(
                    current_index=0,
                    previous=previous.reference(claim.claim_id),
                    relation="unresolved",
                    changes=("unresolved",),
                ),
            ),
        ),
        evidence=previous.evidence,
        first_available_at_ms=2000,
        prior=previous.prior_claims(),
    )
    assert incoming.changes[0].kinds == ("unresolved",)
    assert public_updates(incoming, previous=None, completed_at_ms=3000) == ()
    selection, _ = _select(incoming, {})
    # The unresolved relation is not a notification veto or proof of coverage.
    assert selection.decision == "notify"


def test_public_contract_rejects_refs_from_another_content():
    from tracefold.news.public_updates import PublicNewsUpdate

    update = _update()
    packet = public_updates(update, previous=None, completed_at_ms=2000)[0]
    payload = packet.model_dump(mode="json")
    payload["claim_refs"][0]["content_id"] = "0" * 64
    with pytest.raises(ValueError, match="news_public_update_claim_reference_invalid"):
        PublicNewsUpdate.model_validate(payload)
