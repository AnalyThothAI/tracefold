"""NotificationPlanner and `decide()`: ordered rules, reader novelty from links, one reader judgment per claim.

Every rule has a case where it applies and one where it does not. The reader judge is a test double.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tests.support.news_reader import FixedReader, Unavailable
from tests.support.news_update_semantic import MemoryCache
from tracefold.news.notifications.card import card_copy_material
from tracefold.news.notifications.contracts import ClaimDecision, DeliveredText, NotificationPlan, ReaderSnapshot
from tracefold.news.notifications.novelty import ClaimLink, LinkedReceipt
from tracefold.news.notifications.planner import NotificationPlanner
from tracefold.news.notifications.policy import READER_CUTS, READER_WAIT_MAX_MS, large_daily_move, stale_occurrence
from tracefold.news.updates.assembly import assemble_update
from tracefold.news.updates.contracts import (
    Asset,
    Citation,
    ClaimFields,
    DraftClaim,
    EventUpdate,
    Evidence,
    Extraction,
    FrozenInput,
    Source,
    SupportDraft,
)
from tracefold.news.updates.identity import digest, identity
from tracefold.news.updates.judgment import Budget

STAMP = 1_790_405_000_000
HOUR_MS = 60 * 60_000
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/news"
TARIFF = "medtop:20000384"
CRYPTO = "medtop:20001279"


def evidence(
    text: str,
    *,
    publisher: str = "wire",
    origin: str | None = None,
    authority: str = "unknown",
    available_at_ms: int = STAMP,
) -> Evidence:
    return Evidence.issue(
        text,
        Source.model_validate(
            {
                "publisher_id": publisher,
                "artifact_id": f"{publisher}:{digest(text)[:12]}",
                "artifact_revision": "1",
                "origin_id": origin,
                "first_available_at_ms": available_at_ms,
                "source_authority": authority,
            }
        ),
    )


def claim(
    slot: str,
    source: Evidence,
    *,
    mode: str = "decision",
    kind: str = "state_change",
    assets: tuple[Asset, ...] = (),
    statement: str | None = None,
    quantities: tuple[dict[str, str], ...] = (),
) -> DraftClaim:
    return DraftClaim(
        slot=slot,
        statement=statement or source.text,
        fields=ClaimFields.model_validate(
            {
                "subject": "Agency",
                "action": slot,
                "mode": mode,
                "content_kind": kind,
                "assets": assets,
                "quantities": quantities,
            }
        ),
        citations=(Citation(evidence_ref=source.ref, quote=source.text),),
    )


def adopted(
    *rows: tuple[DraftClaim, Evidence],
    extra: tuple[Evidence, ...] = (),
    supports: tuple[SupportDraft, ...] = (),
    topics: tuple[str, ...] = (),
) -> EventUpdate:
    items = {item.ref: item for _draft, item in rows} | {item.ref: item for item in extra}
    source = FrozenInput(event_id="event", revision=1, lineage_id="lineage", evidence=tuple(items.values()))
    extraction = Extraction(
        claims=tuple(draft.model_copy(update={"topics": topics}) for draft, _item in rows), supports=supports
    )
    update = assemble_update(source, extraction, None, adopted_at_ms=STAMP)
    assert update is not None
    return update


def single(text: str = "Agency orders a 25% tariff.", **fields: Any) -> EventUpdate:
    source = evidence(text)
    return adopted((claim("a", source, **fields), source))


def reader(**values: Any) -> ReaderSnapshot:
    return ReaderSnapshot.model_validate({"channel": "telegram:a", "revision": "r1", "receipts": (), **values})


def run_plan(
    update: EventUpdate,
    snapshot: ReaderSnapshot | None = None,
    *,
    now_ms: int = STAMP + 60_000,
    judge: Any | None = None,
    cache: MemoryCache | None = None,
) -> NotificationPlan:
    planner = NotificationPlanner(judge or FixedReader(), cache or MemoryCache())
    return asyncio.run(planner.plan(update, snapshot or reader(), Budget.start(5), now_ms=now_ms))


def reasons(plan: NotificationPlan, update: EventUpdate) -> dict[str, str]:
    statements = {row.ref: row.statement for row in update.claims}
    return {statements[row.claim_ref]: row.reason for row in plan.claim_decisions}


def only_reason(plan: NotificationPlan) -> str:
    assert len(plan.claim_decisions) == 1
    return plan.claim_decisions[0].reason


def sent(body: str, intent: str = "earlier", refs: tuple[str, ...] = ()) -> DeliveredText:
    return DeliveredText(
        intent_id=intent,
        channel="telegram:a",
        state="sent",
        body=body,
        payload_sha256=digest(body),
        received_at_ms=STAMP,
        provider_message_id="1",
    )


def link(current: str, previous: str, relation: str, at: int = STAMP) -> ClaimLink:
    return ClaimLink.model_validate(
        {"current_ref": current, "previous_ref": previous, "relation": relation, "asserted_at_ms": at}
    )


def delivered(intent: str, *refs: str, state: str = "sent", at: int = STAMP - 60_000) -> LinkedReceipt:
    return LinkedReceipt.model_validate(
        {"intent_id": intent, "state": state, "claim_refs": refs, "settled_at_ms": None if state == "sending" else at}
    )


def test_incremental_importance_decides_push_key_and_feed_against_the_backend_cuts() -> None:
    cuts = READER_CUTS["native"]
    for value, reason, key in (
        (3.5, "reader_key", True),
        (cuts.push, "reader_push", False),
        (1.0, "reader_feed", False),
    ):
        plan = run_plan(single(), judge=FixedReader(value))
        assert only_reason(plan) == reason and plan.key is key
        record = plan.claim_decisions[0].reader
        assert record is not None and record.novelty == "unlinked" and record.judgment is not None
        assert record.input_digest is not None and plan.reader_identity == FixedReader.identity


def test_rules_before_the_judgment_are_ordered_and_ask_nothing() -> None:
    judge = FixedReader(3.5)
    update = single()
    ref = update.claims[0].ref
    assert only_reason(run_plan(update, reader(invalidated_claim_refs=(ref,)), judge=judge)) == "retired"
    assert only_reason(run_plan(update, reader(blocked_claim_refs=(ref,)), judge=judge)) == "send_outcome_unresolved"
    assert only_reason(run_plan(update, reader(ambiguous_claim_refs=(ref,)), judge=judge)) == "send_outcome_ambiguous"
    assert only_reason(run_plan(update, now_ms=STAMP + 3 * HOUR_MS + 1, judge=judge)) == "stale_source"
    assert (
        only_reason(run_plan(update, reader(protected_listing_claim_refs=(ref,)), judge=judge)) == "protected_listing"
    )
    assert judge.asked == []


def test_novelty_from_persisted_links_decides_known_in_flight_and_corrections() -> None:
    judge = FixedReader(3.5)
    update = single()
    ref = update.claims[0].ref
    known = reader(links=(link(ref, "old", "equivalent"),), link_receipts=(delivered("r-old", "old"),))
    assert only_reason(run_plan(update, known, judge=judge)) == "known_to_reader"
    flight = reader(
        links=(link(ref, "old", "adds_information"),), link_receipts=(delivered("r-old", "old", state="sending"),)
    )
    plan = run_plan(update, flight, judge=judge)
    assert only_reason(plan) == "linked_send_in_flight" and plan.action == "unresolved"
    corrects = reader(links=(link(ref, "old", "corrects"),), link_receipts=(delivered("r-old", "old"),))
    earlier = sent("旧消息", "r-old")
    plan = run_plan(update, corrects.model_copy(update={"receipts": (earlier,)}), judge=FixedReader(0.1))
    assert only_reason(plan) == "correction_of_sent"
    record = plan.claim_decisions[0].reader
    assert record is not None and record.render == "correction" and record.earlier is not None
    assert record.earlier.body == "旧消息" and plan.earlier(ref) == record.earlier
    # A correction stays a push for twelve hours; an ordinary claim is stale after three.
    assert only_reason(run_plan(update, corrects, now_ms=STAMP + 5 * HOUR_MS, judge=FixedReader(0.1))) == (
        "correction_of_sent"
    )
    assert judge.asked == []


def test_an_increment_is_scored_on_what_it_adds_with_the_linked_message_first() -> None:
    update = single()
    ref = update.claims[0].ref
    earlier = sent("英伟达宣布1500亿美元回购", "r-old")
    snapshot = reader(
        receipts=(earlier, sent("无关消息", "r-other")),
        receipt_intents_by_claim={ref: ("r-old",)},
        links=(link(ref, "old", "adds_information"),),
        link_receipts=(delivered("r-old", "old"),),
    )
    cuts = READER_CUTS["native"]
    judge = FixedReader(cuts.held, anchor="m1")
    plan = run_plan(update, snapshot, judge=judge)
    assert only_reason(plan) == "reader_push"
    assert judge.asked[0].messages == ("英伟达宣布1500亿美元回购",)
    record = plan.claim_decisions[0].reader
    assert record is not None and record.novelty == "increment" and record.render == "increment"
    assert record.message_intents == ("r-old",)
    assert [row.intent_id for row in plan.compared_receipts] == ["r-old"]
    material = card_copy_material(
        update.claims, {item.ref: item.source for item in update.evidence}, {ref: record.earlier}
    )
    assert material[0]["earlier"] == {"render": "increment", "delivered_text": "英伟达宣布1500亿美元回购"}
    # The reader already has the core fact: what the increment adds needs the key cut.
    assert only_reason(run_plan(update, snapshot, judge=FixedReader(cuts.held - 0.01, anchor="m1"))) == "reader_feed"
    # P014 (2026-09-29): a link to an unrelated earlier push that the anchor does not confirm is no "补充".
    plan = run_plan(update, snapshot, judge=FixedReader(cuts.held))
    record = plan.claim_decisions[0].reader
    assert only_reason(plan) == "reader_push" and record is not None
    assert (record.novelty, record.render, record.earlier, plan.earlier(ref)) == ("increment", "full", None, None)


def occurred(occurred_at: str, *, quote: str | None = None, kind: str = "state_change", **fields: Any) -> EventUpdate:
    """One claim reporting what happened on `occurred_at`, first visible at STAMP (2026-09-26 UTC)."""

    item = evidence(quote or f"Agency acted on {occurred_at}.")
    draft = DraftClaim(
        slot="a",
        statement=item.text,
        fields=ClaimFields.model_validate(
            {"subject": "Agency", "action": "acted", "content_kind": kind, "occurred_at": occurred_at, **fields}
        ),
        citations=(Citation(evidence_ref=item.ref, quote=item.text),),
    )
    return adopted((draft, item))


@pytest.mark.parametrize(
    ("update", "stale"),
    [
        # P014: an X roundup of old stablecoin news, "Hong Kong licensed its first issuers in April".
        (occurred("April", quote="Hong Kong licensed its first stablecoin issuers in April."), True),
        (occurred("late July", kind="other"), True),
        (occurred("October 2025", kind="official_measure", quote="Live since October 2025."), True),
        # P008: the extractor dated "Sept. 10" 2023; the year nearest the source still makes it old.
        (occurred("2023-09-10", quote="Drone strikes damaged the East-West pipeline on Sept. 10."), True),
        (occurred("2025-09-20", quote="On 20 September 2025 the plant closed."), True),
        # An invented year on a recent day, a figure's month (its statistical period), a speaker's statement,
        # the current month and a day written out are not read as old.
        (occurred("2023-09-25", quote="The plant closed yesterday."), False),
        (occurred("2023-09-29", quote="The plant closes on Sept. 29."), False),
        (occurred("August", kind="new_quantity", quote="Exports rose 5% in August."), False),
        (occurred("April", speaker="Minister", quote="The minister said the law passed in April."), False),
        (occurred("September"), False),
        (occurred("Sept. 10"), False),
        (occurred("decade"), False),
    ],
)
def test_a_claim_reporting_an_old_occurrence_is_not_notified(update: EventUpdate, stale: bool) -> None:
    """#742 PR-4: freshness is the claim's first visibility; the day it reports must be recent as well."""

    assert stale_occurrence(update.claims[0]) is stale
    reason = only_reason(run_plan(update, judge=FixedReader(3.5)))
    assert reason == ("stale_occurrence" if stale else "reader_key")


def test_an_old_occurrence_follows_the_source_age_and_never_holds_a_correction() -> None:
    judge = FixedReader(0.1)
    update = occurred("April")
    ref = update.claims[0].ref
    assert only_reason(run_plan(update, now_ms=STAMP + 3 * HOUR_MS + 1, judge=judge)) == "stale_source"
    known = reader(links=(link(ref, "old", "equivalent"),), link_receipts=(delivered("r-old", "old"),))
    assert only_reason(run_plan(update, known, judge=judge)) == "stale_occurrence"
    corrects = reader(links=(link(ref, "old", "corrects"),), link_receipts=(delivered("r-old", "old"),))
    assert only_reason(run_plan(update, corrects, judge=judge)) == "correction_of_sent"
    assert judge.asked == []


def test_an_unavailable_judgment_waits_then_is_recorded_unassessed_and_never_cached() -> None:
    update = single()
    judge = Unavailable()
    cache = MemoryCache()
    plan = run_plan(update, judge=judge, cache=cache)
    assert only_reason(plan) == "reader_unavailable" and plan.action == "unresolved"
    late = run_plan(update, judge=judge, cache=cache, now_ms=update.adopted_at_ms + READER_WAIT_MAX_MS + 1)
    assert only_reason(late) == "reader_unassessed" and late.action == "no_notification"
    assert judge.calls == 2 and cache.values == {}


def test_judgments_are_reused_per_claim_so_a_sibling_change_asks_only_the_new_claim() -> None:
    first, second = evidence("Agency orders a 25% tariff."), evidence("Agency delays the plan.")
    cache = MemoryCache()
    judge = FixedReader()
    run_plan(adopted((claim("a", first), first)), judge=judge, cache=cache)
    run_plan(adopted((claim("a", first), first), (claim("b", second), second)), judge=judge, cache=cache)
    assert [row.claim.statement for row in judge.asked] == ["Agency orders a 25% tariff.", "Agency delays the plan."]
    # The same plan asked again, as after a lost CAS, asks nothing.
    run_plan(adopted((claim("a", first), first), (claim("b", second), second)), judge=judge, cache=cache)
    assert len(judge.asked) == 2


def test_large_daily_move_fires_only_on_same_day_moves_of_a_whole_market() -> None:
    cases = json.loads((FIXTURES / "large_daily_move_2026-09-28.json").read_text(encoding="utf-8"))["cases"]
    fired = []
    for case in cases:
        fields = ClaimFields.model_validate(case["fields"])
        item = evidence(case["statement"])
        update = adopted(
            (
                DraftClaim(
                    slot="a",
                    statement=case["statement"],
                    fields=fields,
                    citations=(Citation(evidence_ref=item.ref, quote=case["statement"]),),
                ),
                item,
            )
        )
        assert large_daily_move(update.claims[0]) is case["large_daily_move"], case["statement"]
        fired.append(case["large_daily_move"])
        if case["large_daily_move"] and fields.mode == "observation":
            assert only_reason(run_plan(update, judge=FixedReader(0.1))) == "large_daily_move"
    assert (sum(fired), len(fired)) == (6, 17)


def test_card_copy_input_changes_for_same_ref_with_changed_expression_or_source():
    update = single()
    original = update.claims[0]
    sources = {item.ref: item.source for item in update.evidence}

    def copy_input(claim, provenance=sources) -> str:
        return identity("news_card_copy_input", "composer-v1", card_copy_material((claim,), provenance))

    baseline = copy_input(original)
    changed_fields = original.fields.model_copy(update={"conditions": ("only after approval",)})
    assert copy_input(original.model_copy(update={"statement": "Agency proposes a 25% tariff."})) != baseline
    assert copy_input(original.model_copy(update={"fields": changed_fields})) != baseline
    changed_source = next(iter(sources.values())).model_copy(update={"attribution": "Second agency"})
    assert copy_input(original, {next(iter(sources)): changed_source}) != baseline


def test_decision_reason_contract_rejects_mismatch():
    with pytest.raises(ValidationError, match="news_claim_decision_reason_mismatch"):
        ClaimDecision(claim_ref="a", decision="notify", reason="stale_source")


def test_a_policy_change_reuses_model_evidence_but_records_a_new_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    from tracefold.news.notifications import planner as planning

    update = single()
    judge, cache = FixedReader(), MemoryCache()
    before = run_plan(update, judge=judge, cache=cache)
    monkeypatch.setattr(planning, "NOTIFICATION_POLICY_IDENTITY", "next-policy")
    after = run_plan(update, judge=judge, cache=cache)
    assert len(judge.asked) == 1
    assert before.reader_identity == after.reader_identity == judge.identity
    assert before.input_digest != after.input_digest and before.record_ref != after.record_ref
    assert before.intent_id == after.intent_id
