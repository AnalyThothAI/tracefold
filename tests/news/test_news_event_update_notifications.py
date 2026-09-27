"""NotificationPlanner: explicit content rules, named per-claim decisions and the key designation.

Every rule has a case where it applies and one where it does not. Judgment backends are test doubles.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from tracefold.news.updates.attention import AttentionAssessment, AttentionDecision
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
    generated: TaskBackend | None = None,
    native: TaskBackend | None = None,
    now_ms: int = STAMP + 60_000,
    judgments: NewsJudgments | None = None,
    assessor: Any | None = None,
) -> NotificationPlan:
    if judgments is None:
        judgments = NewsJudgments(generated=generated or TaskBackend(), native=native, cache=MemoryCache())
    planner = NotificationPlanner(judgments, assessor or Assessor())
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


class Assessor:
    identity = "scripted_editor_v1"

    def __init__(self, dispositions: dict[str, str] | None = None, *, error: BaseException | None = None):
        self.dispositions = dispositions or {}
        self.error = error
        self.calls: list[tuple[str, ...]] = []

    async def assess(self, claims, *, sources, watch_symbols):
        self.calls.append(tuple(row.ref for row in claims))
        if self.error is not None:
            raise self.error
        return AttentionAssessment(
            decisions=tuple(
                AttentionDecision(claim_ref=row.ref, disposition=self.dispositions.get(row.statement, "notify"))
                for row in claims
            )
        )


def test_real_litigation_and_normal_product_progress_are_ordinary_candidates():
    case = single("$ACME was sued for securities fraud; complaint seeks $2 billion.")
    product = single("ACME launched a paid feature that lowers operating costs.", mode="unknown")
    for update in (case, product):
        plan = run_plan(update)
        assert plan.action == "notify"
        assert plan.claim_decisions[0].reason == "editor_notify"
        assert plan.assessment_status == "available"


def test_promotion_and_mixed_news_are_decided_per_claim_without_a_mode_gate():
    ad = evidence("Rosen Law encourages investors to contact the firm.")
    lawsuit = evidence("ACME faces a new securities class action seeking $2 billion.")
    update = adopted((claim("ad", ad, mode="promotion"), ad), (claim("case", lawsuit, mode="unknown"), lawsuit))
    editor = Assessor({ad.text: "feed_only", lawsuit.text: "notify"})
    plan = run_plan(update, assessor=editor)
    assert reasons(plan, update) == {ad.text: "editor_feed_only", lawsuit.text: "editor_notify"}
    assert plan.selected_claim_refs == (next(c.ref for c in update.claims if c.statement == lawsuit.text),)


def test_key_marks_only_its_claim_and_optional_reason_is_not_required():
    major = evidence("Exchange halted withdrawals after a security breach.")
    minor = evidence("Exchange changed its developer documentation.")
    update = adopted((claim("major", major), major), (claim("minor", minor), minor))
    plan = run_plan(update, assessor=Assessor({major.text: "key", minor.text: "notify"}))
    assert plan.key and plan.action == "notify"
    assert reasons(plan, update) == {major.text: "editor_key", minor.text: "editor_notify"}
    assert all(row.reason_zh is None for row in plan.claim_decisions)


def test_protected_listing_and_structured_daily_move_bypass_editor_only_for_their_refs():
    listing = evidence("Venue lists ACME for trading.")
    other = evidence("A founder gives a promotional speech.")
    update = adopted((claim("listing", listing), listing), (claim("other", other), other))
    listing_ref = next(c.ref for c in update.claims if c.statement == listing.text)
    editor = Assessor({other.text: "feed_only"})
    plan = run_plan(update, reader(protected_listing_claim_refs=(listing_ref,)), assessor=editor)
    assert reasons(plan, update) == {listing.text: "protected_listing", other.text: "editor_feed_only"}
    assert editor.calls == [(next(c.ref for c in update.claims if c.statement == other.text),)]
    editor = Assessor()
    move = single(
        "Index rose 5% in one day.",
        mode="observation",
        kind="level_crossed",
        assets=(Asset(symbol="X", market_type="index", role="primary"),),
        quantities=({"name": "daily change", "value": "5", "unit": "%"},),
    )
    assert only_reason(run_plan(move, assessor=editor)) == "large_daily_move"
    assert editor.calls == []


def test_watchlist_is_context_not_an_automatic_push():
    update = single(
        "BTC giveaway. Contact us to claim.",
        mode="promotion",
        assets=(Asset(symbol="BTC", market_type="crypto", role="primary"),),
    )
    plan = run_plan(
        update, reader(watch_symbols=("BTC",)), assessor=Assessor({update.claims[0].statement: "feed_only"})
    )
    assert only_reason(plan) == "editor_feed_only"


def test_stale_retired_and_full_coverage_skip_attention():
    update = single()
    editor = Assessor()
    assert only_reason(run_plan(update, now_ms=STAMP + 13 * HOUR_MS, assessor=editor)) == "stale_source"
    assert editor.calls == []
    plan = run_plan(
        update,
        reader(receipts=(sent("机构宣布 25% 关税。"),)),
        generated=TaskBackend({"coverage": "full"}),
        assessor=editor,
    )
    assert only_reason(plan) == "covered_by_sent_receipt"
    assert editor.calls == []


def test_partial_coverage_is_still_an_editor_candidate():
    update = single()
    editor = Assessor()
    plan = run_plan(
        update,
        reader(receipts=(sent("仅提及关税。"),)),
        generated=TaskBackend({"coverage": "partial"}),
        assessor=editor,
    )
    assert only_reason(plan) == "editor_notify"
    assert len(editor.calls) == 1


def test_local_editor_failure_defaults_to_normal_notify_with_visible_status():
    update = single()
    plan = run_plan(update, assessor=Assessor(error=ProviderUnavailable("news_generation_unavailable")))
    assert plan.action == "notify" and not plan.key
    assert only_reason(plan) == "attention_unavailable_default_notify"
    assert plan.assessment_status == "unavailable"
    assert plan.assessment_error_code == "news_generation_unavailable"


def test_programming_failure_is_not_silently_treated_as_editor_outage():
    with pytest.raises(RuntimeError, match="bug"):
        run_plan(single(), assessor=Assessor(error=RuntimeError("bug")))


def test_editor_must_cover_exact_candidate_refs():
    class Wrong(Assessor):
        async def assess(self, claims, *, sources, watch_symbols):
            return AttentionAssessment(decisions=(AttentionDecision(claim_ref="claim:unknown", disposition="key"),))

    plan = run_plan(single(), assessor=Wrong())
    assert plan.assessment_status == "unavailable"
    assert plan.claim_decisions[0].reason == "attention_unavailable_default_notify"


def test_decision_fingerprint_ignores_reader_revision_stamp_but_includes_receipt_content():
    update = single()
    first = run_plan(update, reader(revision="r1"))
    second = run_plan(update, reader(revision="r2"))
    assert first.assessment_input_digest == second.assessment_input_digest
    changed = run_plan(
        update,
        reader(revision="r3", receipts=(sent("different copy"),)),
        generated=TaskBackend({"coverage": "partial"}),
    )
    assert changed.assessment_input_digest != first.assessment_input_digest


def test_decision_reason_contract_rejects_mismatch():
    with pytest.raises(ValidationError, match="news_claim_decision_reason_mismatch"):
        ClaimDecision(claim_ref="a", decision="notify", reason="stale_source")
