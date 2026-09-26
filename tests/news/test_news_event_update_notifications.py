"""NotificationPlanner: explicit content rules, named per-claim decisions and the key designation.

Every rule has a case where it applies and one where it does not. Judgment backends are test doubles.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from tracefold.news.updates.contracts import (
    Asset,
    Citation,
    ClaimFields,
    DraftClaim,
    EventUpdate,
    Evidence,
    Extraction,
    FrozenInput,
    PriorClaim,
    RelationDraft,
    Source,
    SupportDraft,
)
from tracefold.news.updates.identity import digest
from tracefold.news.updates.judgment import (
    Answer,
    BatchResult,
    Budget,
    NewsJudgments,
    ProviderUnavailable,
    Question,
    Task,
)
from tracefold.news.updates.notification import (
    ClaimDecision,
    DeliveredText,
    NotificationPlan,
    NotificationPlanner,
    ReaderSnapshot,
)
from tracefold.news.updates.semantics import assemble_update

STAMP = 1_790_405_000_000
HOUR_MS = 60 * 60_000
TARIFF = "medtop:20000384"
CRYPTO = "medtop:20001279"


class MemoryCache:
    def __init__(self) -> None:
        self.values: dict[str, Answer] = {}

    async def get(self, key: str) -> Answer | None:
        return self.values.get(key)

    async def put(self, key: str, answer: Answer) -> None:
        self.values.setdefault(key, answer)


class TaskBackend:
    """Answers each task with one value; a missing task is a provider failure."""

    def __init__(self, values: dict[Task, str | bool] | None = None, *, identity: str = "generated") -> None:
        self.identity = identity
        self.values = values or {}
        self.calls: list[tuple[Task, tuple[str, ...]]] = []

    async def judge(self, task: Task, items: tuple[Question, ...], *, context_json: str | None) -> BatchResult:
        self.calls.append((task, tuple(item.item_id for item in items)))
        if task not in self.values:
            raise ProviderUnavailable("controlled provider failure")
        value = self.values[task]
        return BatchResult(
            answers=tuple(Answer(item_id=item.item_id, value=value, backend=self.identity) for item in items)
        )


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
    extraction = Extraction(claims=tuple(draft for draft, _item in rows), supports=supports, topics=topics)
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
    generated: TaskBackend | None = None,
    native: TaskBackend | None = None,
    now_ms: int = STAMP + 60_000,
    judgments: NewsJudgments | None = None,
) -> NotificationPlan:
    if judgments is None:
        judgments = NewsJudgments(generated=generated or TaskBackend(), native=native, cache=MemoryCache())
    planner = NotificationPlanner(judgments)
    return asyncio.run(planner.plan(update, snapshot or reader(), Budget.start(5), now_ms=now_ms))


def reasons(plan: NotificationPlan, update: EventUpdate) -> dict[str, str]:
    statements = {row.ref: row.statement for row in update.claims}
    return {statements[row.claim_ref]: row.reason for row in plan.claim_decisions}


def only_reason(plan: NotificationPlan) -> str:
    assert len(plan.claim_decisions) == 1
    return plan.claim_decisions[0].reason


def sent(body: str) -> DeliveredText:
    return DeliveredText(
        intent_id="earlier",
        channel="telegram:a",
        state="sent",
        body=body,
        payload_sha256=digest(body),
        received_at_ms=STAMP,
        provider_message_id="1",
    )


# ------------------------------------------------------------------ retired claims


def test_a_retired_claim_is_recorded_and_its_correction_is_notified() -> None:
    first = single("Agency orders a 25% tariff.")
    correction = evidence("Correction: the tariff is 50%, not 25%.", available_at_ms=STAMP + 5)
    source = FrozenInput(
        event_id="event",
        revision=2,
        lineage_id="lineage",
        evidence=(correction,),
        prior=(PriorClaim(event_id="event", content_revision=first.content_revision, claim=first.claims[0]),),
    )
    extraction = Extraction(
        claims=(claim("a", correction),),
        relations=(
            RelationDraft(slot="a", previous_ref=first.claims[0].ref, relation="corrects", change_kind="correction"),
        ),
    )
    update = assemble_update(source, extraction, first, adopted_at_ms=STAMP + 10)
    assert update is not None
    plan = run_plan(update)
    assert reasons(plan, update) == {
        "Agency orders a 25% tariff.": "retired",
        "Correction: the tariff is 50%, not 25%.": "actionable_content",
    }
    assert plan.action == "notify"


# ------------------------------------------------------------------ watchlist guard


BTC = Asset(symbol="BTC", market_type="crypto", role="primary")


def test_watchlist_primary_asset_is_notified_whatever_its_mode_or_content() -> None:
    update = single("Analyst says BTC could fall.", mode="commentary", kind="schedule", assets=(BTC,))
    assert only_reason(run_plan(update, reader(watch_symbols=("btc",)))) == "watchlist_hit"


def test_without_a_watchlist_hit_content_rules_apply() -> None:
    mentioned = Asset(symbol="BTC", market_type="crypto", role="mentioned")
    update = single("Analyst says BTC could fall.", mode="commentary", assets=(mentioned,))
    assert only_reason(run_plan(update, reader(watch_symbols=("BTC",)))) == "mode_commentary"
    assert only_reason(run_plan(single("Analyst says BTC could fall.", mode="commentary", assets=(BTC,)))) == (
        "mode_commentary"
    )


def test_watchlist_hit_is_still_subject_to_staleness_and_coverage() -> None:
    update = single("Exchange halts BTC withdrawals.", assets=(BTC,))
    stale = run_plan(update, reader(watch_symbols=("BTC",)), now_ms=STAMP + 13 * HOUR_MS)
    assert only_reason(stale) == "stale_source"
    covered = run_plan(
        update,
        reader(watch_symbols=("BTC",), receipts=(sent("交易所暂停 BTC 提币。"),)),
        generated=TaskBackend({"coverage": "full"}),
    )
    assert only_reason(covered) == "covered_by_sent_receipt"


# ------------------------------------------------------------------ mode


@pytest.mark.parametrize("mode", ["commentary", "promotion", "forecast"])
def test_commentary_promotion_and_forecast_are_not_notified(mode: str) -> None:
    plan = run_plan(single(mode=mode))
    assert only_reason(plan) == f"mode_{mode}"
    assert plan.claim_decisions[0].decision == "not_notified"
    assert plan.action == "no_notification"


@pytest.mark.parametrize("mode", ["observation", "decision", "commitment", "conditional_threat", "guidance"])
def test_concrete_acts_intents_threats_and_guidance_are_notified(mode: str) -> None:
    plan = run_plan(single(mode=mode))
    assert only_reason(plan) == "actionable_content"
    assert plan.action == "notify"


def test_unknown_mode_is_reasked_once_by_the_generated_backend_not_a_native_vote() -> None:
    generated = TaskBackend({"mode": "decision"})
    native = TaskBackend({"mode": "commentary"}, identity="native")
    plan = run_plan(single(mode="unknown"), generated=generated, native=native)
    assert only_reason(plan) == "actionable_content"
    assert [call[0] for call in generated.calls] == ["mode"]
    assert native.calls == []


@pytest.mark.parametrize(
    ("answer", "expected"),
    [("unknown", "mode_unknown"), ("promotion", "mode_promotion"), (None, "mode_unknown")],
)
def test_a_mode_that_stays_uncertain_completes_the_plan(answer: str | None, expected: str) -> None:
    generated = TaskBackend({} if answer is None else {"mode": answer})
    plan = run_plan(single(mode="unknown"), generated=generated)
    assert only_reason(plan) == expected
    # Content uncertainty completes the plan; it is never left pending for another retry.
    assert plan.action == "no_notification"
    assert plan.deferred_claim_refs == ()


def test_a_stale_claim_of_unknown_mode_is_not_reasked() -> None:
    generated = TaskBackend({"mode": "decision"})
    plan = run_plan(single(mode="unknown"), generated=generated, now_ms=STAMP + 13 * HOUR_MS)
    assert only_reason(plan) == "stale_source"
    assert generated.calls == []


def test_a_retried_plan_reuses_the_reasked_mode() -> None:
    generated = TaskBackend({"mode": "unknown"})
    judgments = NewsJudgments(generated=generated, cache=MemoryCache())
    update = single(mode="unknown")
    run_plan(update, judgments=judgments)
    run_plan(update, judgments=judgments)
    assert len(generated.calls) == 1


# ------------------------------------------------------------------ content kind


def test_a_schedule_is_not_notified() -> None:
    assert only_reason(run_plan(single("FOMC minutes are due tomorrow.", kind="schedule"))) == "content_schedule"
    assert only_reason(run_plan(single("The Fed cut rates by 25 bp.", kind="official_measure"))) == (
        "actionable_content"
    )


def _move(
    market: str, *, percent: str | None = None, kind: str = "level_crossed", mode: str = "observation"
) -> EventUpdate:
    asset = Asset.model_validate({"symbol": "X", "market_type": market, "role": "primary"})
    quantities = () if percent is None else ({"name": "change", "value": percent, "unit": "%"},)
    return single("A market moved.", kind=kind, mode=mode, assets=(asset,), quantities=quantities)


@pytest.mark.parametrize(
    ("basis", "market", "percent", "expected"),
    [
        ("level_crossed", "crypto", None, "actionable_content"),
        ("period_record", "commodity", None, "actionable_content"),
        ("depeg_or_physical", "crypto", None, "actionable_content"),
        ("quantified_flow", "crypto", None, "actionable_content"),
        ("quote_only", "crypto", "3", "price_report_without_basis"),
        ("quote_only", "commodity", "6", "large_daily_move"),
        ("quote_only", "index", "-5.2", "large_daily_move"),
        ("quote_only", "equity", "7", "price_report_without_basis"),
        ("quote_only", "commodity", "2.66", "price_report_without_basis"),
    ],
)
def test_an_observed_market_move_needs_a_judged_basis_or_the_owner_exception(
    basis: str, market: str, percent: str | None, expected: str
) -> None:
    backend = TaskBackend({"market_basis": basis})
    assert only_reason(run_plan(_move(market, percent=percent), generated=backend)) == expected
    assert [task for task, _items in backend.calls] == ["market_basis"]


def test_the_basis_is_judged_by_the_native_backend_when_it_is_configured() -> None:
    native, generated = TaskBackend({"market_basis": "quote_only"}, identity="native"), TaskBackend()
    assert only_reason(run_plan(_move("crypto"), native=native, generated=generated)) == "price_report_without_basis"
    assert [task for task, _items in native.calls] == ["market_basis"]
    assert generated.calls == []


def test_an_unresolved_basis_is_reasked_once_and_then_does_not_withhold_the_claim() -> None:
    generated = TaskBackend({"market_basis": "unresolved"})
    assert only_reason(run_plan(_move("crypto"), generated=generated)) == "actionable_content"
    assert [task for task, _items in generated.calls] == ["market_basis", "market_basis"]


def test_the_exception_reads_the_structured_percentage_not_the_prose() -> None:
    backend = TaskBackend({"market_basis": "quote_only"})
    # The quote says "6%", but no structured quantity carries it: code computes only on extracted numbers.
    update = single(
        "WTI settles 6% higher on the day.",
        kind="level_crossed",
        mode="observation",
        assets=(Asset.model_validate({"symbol": "CL", "market_type": "commodity", "role": "primary"}),),
    )
    assert only_reason(run_plan(update, generated=backend)) == "price_report_without_basis"


@pytest.mark.parametrize("mode", ["decision", "commitment", "guidance"])
def test_an_action_that_carries_an_amount_is_not_asked_about_a_market_basis(mode: str) -> None:
    # 2026-09-26 replay: "US Treasury says will buy up to $6 bln of 20-30 year debt" was read as a
    # quantified flow and withheld as a price report. The basis question is about observed market moves.
    backend = TaskBackend()
    update = single("US Treasury will buy up to $6 bln of 20-30 year debt.", kind="quantified_flow", mode=mode)
    assert only_reason(run_plan(update, generated=backend)) == "actionable_content"
    assert backend.calls == []


# ------------------------------------------------------------------ staleness


def test_a_stale_source_is_not_notified_but_a_fresh_one_is() -> None:
    update = single()
    assert only_reason(run_plan(update, now_ms=STAMP + 13 * HOUR_MS)) == "stale_source"
    assert only_reason(run_plan(update, now_ms=STAMP + 11 * HOUR_MS)) == "actionable_content"


def test_a_correction_of_an_old_report_is_exempt_from_staleness() -> None:
    first = single("Agency orders a 25% tariff.")
    correction = evidence("Agency now says the tariff is 50%.", available_at_ms=STAMP)
    source = FrozenInput(
        event_id="event",
        revision=2,
        lineage_id="lineage",
        evidence=(correction,),
        prior=(PriorClaim(event_id="event", content_revision=first.content_revision, claim=first.claims[0]),),
    )
    extraction = Extraction(
        claims=(claim("a", correction),),
        relations=(
            RelationDraft(slot="a", previous_ref=first.claims[0].ref, relation="conflicts", change_kind="conflict"),
        ),
    )
    update = assemble_update(source, extraction, first, adopted_at_ms=STAMP)
    assert update is not None
    assert reasons(run_plan(update, now_ms=STAMP + 13 * HOUR_MS), update) == {
        "Agency orders a 25% tariff.": "stale_source",
        "Agency now says the tariff is 50%.": "actionable_content",
    }


# ------------------------------------------------------------------ coverage and in-flight sends


def test_only_a_fully_covering_sent_receipt_suppresses() -> None:
    update = single()
    snapshot = reader(receipts=(sent("机构宣布 25% 关税。"),))
    full = run_plan(update, snapshot, generated=TaskBackend({"coverage": "full"}))
    assert only_reason(full) == "covered_by_sent_receipt"
    assert full.action == "no_notification"
    partial = run_plan(update, snapshot, generated=TaskBackend({"coverage": "partial"}))
    assert only_reason(partial) == "actionable_content"
    unavailable = run_plan(update, snapshot, generated=TaskBackend())
    assert only_reason(unavailable) == "actionable_content"


def test_ambiguous_or_unsent_copy_is_not_reader_coverage() -> None:
    update = single()
    body = "Draft copy only."
    for state in ("not_sent", "ambiguous"):
        receipt = DeliveredText(
            intent_id="prior", channel="telegram:a", state=state, body=body, payload_sha256=digest(body)
        )
        generated = TaskBackend({"coverage": "full"})
        plan = run_plan(update, reader(receipts=(receipt,)), generated=generated)
        assert plan.action == "notify"
        assert generated.calls == []


def test_an_unsettled_overlapping_send_is_the_only_pending_reason() -> None:
    one = evidence("Agency orders a 25% tariff.")
    two = evidence("Agency exempts medicines from the tariff.")
    update = adopted((claim("a", one), one), (claim("b", two), two))
    blocked = next(row.ref for row in update.claims if row.statement == one.text)
    plan = run_plan(update, reader(blocked_claim_refs=(blocked,)))
    assert reasons(plan, update) == {one.text: "send_outcome_unresolved", two.text: "actionable_content"}
    assert plan.action == "notify"
    assert plan.deferred_claim_refs == (blocked,)
    assert blocked not in plan.selected_claim_refs
    alone = single("Agency orders a 25% tariff.")
    pending = run_plan(alone, reader(blocked_claim_refs=(alone.claims[0].ref,)))
    assert pending.action == "unresolved"
    assert pending.reason == "send_outcome_unresolved"


def test_every_claim_gets_one_named_decision() -> None:
    one = evidence("Agency orders a 25% tariff.")
    two = evidence("A columnist calls the tariff reckless.")
    three = evidence("The Senate meets on the tariff on Friday.")
    update = adopted(
        (claim("a", one), one),
        (claim("b", two, mode="commentary"), two),
        (claim("c", three, kind="schedule"), three),
    )
    plan = run_plan(update)
    assert reasons(plan, update) == {
        one.text: "actionable_content",
        two.text: "mode_commentary",
        three.text: "content_schedule",
    }
    assert [row.decision for row in plan.claim_decisions] == ["notify", "not_notified", "not_notified"]


def test_a_decision_must_match_its_reason() -> None:
    with pytest.raises(ValidationError, match="news_claim_decision_reason_mismatch"):
        ClaimDecision(claim_ref="a", decision="notify", reason="stale_source")


# ------------------------------------------------------------------ key designation


def key_update(
    *,
    kind: str = "state_change",
    topics: tuple[str, ...] = (TARIFF,),
    origins: tuple[str | None, ...] = ("issuer", "customs"),
    relation: str = "supports",
    authority: str = "unknown",
) -> EventUpdate:
    items = tuple(
        evidence(
            f"Agency orders a 25% tariff (copy {index}).", publisher=f"wire{index}", origin=origin, authority=authority
        )
        for index, origin in enumerate(origins)
    )
    supports = tuple(
        SupportDraft.model_validate({"slot": "a", "evidence_ref": item.ref, "relation": relation}) for item in items
    )
    return adopted((claim("a", items[0], kind=kind), items[0]), extra=items[1:], supports=supports, topics=topics)


def test_a_corroborated_state_change_in_a_key_family_is_key() -> None:
    assert run_plan(key_update()).key is True
    assert run_plan(key_update(kind="official_measure")).key is True


def test_one_named_authority_corroborates_alone() -> None:
    assert run_plan(key_update(origins=("sec",), authority="regulatory_filing")).key is True
    assert run_plan(key_update(origins=("someone",))).key is False


def test_copies_of_one_origin_do_not_corroborate() -> None:
    assert run_plan(key_update(origins=("issuer", "issuer"))).key is False
    # An unknown origin falls back to the publisher, which differs here.
    assert run_plan(key_update(origins=(None, None))).key is True


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "new_quantity"},
        {"topics": (CRYPTO,)},
        {"relation": "reports"},
    ],
)
def test_key_needs_the_content_kind_topic_family_and_supporting_sources(changes: dict[str, Any]) -> None:
    plan = run_plan(key_update(**changes))
    assert plan.action == "notify"
    assert plan.key is False


def test_key_is_never_set_without_a_notification() -> None:
    update = key_update()
    plan = run_plan(
        update, reader(receipts=(sent("机构宣布 25% 关税。"),)), generated=TaskBackend({"coverage": "full"})
    )
    assert plan.action == "no_notification"
    assert plan.key is False
    with pytest.raises(ValidationError, match="news_plan_key_without_notification"):
        NotificationPlan.model_validate({**plan.model_dump(), "key": True})
