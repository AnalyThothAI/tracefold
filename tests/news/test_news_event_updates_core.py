"""Behavior examples for the new core. No provider/PG credentials or calls."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from tracefold.news.updates.contracts import (
    ClaimFields, DraftClaim, Evidence, Extraction, FrozenInput, IdentityHint, PriorClaim,
    Quantity, RelationDraft, Source, SupportDraft, Citation, EventUpdate,
)
from tracefold.news.updates.identity import digest
from tracefold.news.updates.judgment import (
    Answer, BatchResult, Budget, NewsJudgments, ProviderUnavailable, Question,
)
from tracefold.news.updates.notification import DeliveredText, NotificationPlanner, ReaderSnapshot
from tracefold.news.updates.public import public_updates
from tracefold.news.updates.semantics import assemble_update, equivalent_is_possible
from tracefold.news.updates.service import PublicRelay

STAMP = 1_790_405_000_000


def material(text: str, *, revision: int = 1, publisher: str = "wire") -> Evidence:
    return Evidence.issue(text, Source(publisher_id=publisher, artifact_id="release-1", artifact_revision=str(revision),
        first_available_at_ms=STAMP + revision, origin_id="issuer"))


def draft(evidence: Evidence, *, quantity: str = "25", mode: str = "decision", phase: str = "announced") -> DraftClaim:
    return DraftClaim.model_validate({"slot": "a", "statement": evidence.text,
        "fields": {"subject": "Agency", "action": "set tariff", "object": "imports", "mode": mode,
                   "phase": phase, "effective_at": "2026-10-01", "quantities": [{"name": "rate", "value": quantity, "unit": "%"}]},
        "citations": [{"evidence_ref": evidence.ref, "quote": evidence.text}]})


def update_one():
    evidence = material("Agency announces 25% tariff effective October 1.")
    item = draft(evidence)
    source = FrozenInput(event_id="event-1", revision=1, lineage_id="line-1", evidence=(evidence,))
    extracted = Extraction(claims=(item,), supports=(SupportDraft(slot="a", evidence_ref=evidence.ref, relation="reports"),))
    return source, extracted, assemble_update(source, extracted, None, adopted_at_ms=STAMP + 5)


def test_future_effective_date_does_not_replace_announced_phase() -> None:
    _, _, update = update_one()
    assert update is not None
    assert update.claims[0].fields.phase == "announced"
    assert update.claims[0].fields.effective_at == "2026-10-01"


def test_wording_and_model_rerun_do_not_create_content_revision() -> None:
    source, extraction, head = update_one()
    changed = extraction.model_copy(update={"claims": (extraction.claims[0].model_copy(update={"statement": "Same fact, different wording."}),)})
    assert assemble_update(source, changed, head, adopted_at_ms=STAMP + 9999) is None


def test_expected_and_actual_with_identical_number_are_not_equivalent() -> None:
    _, _, head = update_one()
    current = draft(material("Observed tariff rate is 25%."), mode="observation")
    assert head is not None
    previous = head.claims[0].model_copy(update={"fields": head.claims[0].fields.model_copy(update={"mode": "forecast"})})
    assert not equivalent_is_possible(current, previous)


def test_known_different_country_cannot_be_equivalent() -> None:
    source, extraction, head = update_one()
    assert head is not None
    quote = extraction.claims[0].citations[0].quote
    prior = head.claims[0].model_copy(update={"known_identity": (IdentityHint(key="country", value="BR", evidence_ref=source.evidence[0].ref, surface=quote),)})
    hints = (IdentityHint(key="country", value="TR", evidence_ref=source.evidence[0].ref, surface=quote),)
    assert not equivalent_is_possible(extraction.claims[0], prior, hints)


def correction_update():
    _, _, head = update_one()
    assert head is not None
    evidence = material("Correction: the announced tariff was 50%, not 25%.", revision=2)
    source = FrozenInput(event_id=head.event_id, revision=2, lineage_id="line-1", evidence=(evidence,),
        prior=(PriorClaim(event_id=head.event_id, content_revision=head.content_revision, claim=head.claims[0]),))
    result = Extraction(claims=(draft(evidence, quantity="50"),),
        relations=(RelationDraft(slot="a", previous_ref=head.claims[0].ref, relation="corrects", change_kind="correction"),),
        supports=(SupportDraft(slot="a", evidence_ref=evidence.ref, relation="reports"),))
    return head, assemble_update(source, result, head, adopted_at_ms=STAMP + 100)


def test_correction_is_source_update_not_new_entry_signal() -> None:
    old, updated = correction_update()
    assert updated is not None
    public = public_updates(updated, semantic_completed_at_ms=STAMP + 90)
    assert len(public) == 1
    assert public[0].kind == "source_update"
    assert public[0].affected_claim_refs == (old.claims[0].ref,)
    assert old.claims[0].ref in updated.retired_claim_refs
    assert "50" in public[0].text
    assert updated.claims[-1].first_available_at_ms == STAMP + 2
    assert old.claims[0].first_available_at_ms == STAMP + 1


def test_bad_quote_is_a_contract_error_not_no_news() -> None:
    source, extraction, _ = update_one()
    broken = extraction.model_copy(update={"claims": (extraction.claims[0].model_copy(update={"citations": (Citation(evidence_ref=source.evidence[0].ref, quote="fabricated quote"),)}),)})
    with pytest.raises(ValueError, match="news_citation_not_in_frozen_source"):
        assemble_update(source, broken, None, adopted_at_ms=STAMP)


def test_legacy_judgment_does_not_silently_upgrade_to_event_update() -> None:
    with pytest.raises(ValidationError):
        EventUpdate.model_validate({"judgment_contract_version": "news_judgment_v3", "verdict": {"headline_zh": "旧记录"}})


class MemoryCache:
    def __init__(self):
        self.data = {}
    async def get(self, key):
        return self.data.get(key)
    async def put(self, key, value):
        self.data.setdefault(key, value)


class Backend:
    identity = "test-backend"
    def __init__(self, value="none", fail_batch=None, cancel=False):
        self.value, self.fail_batch, self.cancel, self.calls = value, fail_batch, cancel, []
    async def judge(self, task, items, *, timeout):
        self.calls.append(tuple(i.item_id for i in items))
        if self.cancel:
            raise asyncio.CancelledError
        if len(self.calls) == self.fail_batch:
            raise ProviderUnavailable("controlled provider failure")
        return BatchResult(answers=tuple(Answer(item_id=i.item_id, value=self.value, backend=self.identity) for i in items))


def test_failed_native_batch_does_not_rejudge_successful_batch_or_truncate_tail() -> None:
    async def run():
        native, generated = Backend(fail_batch=2), Backend()
        judgments = NewsJudgments(generated=generated, native=native, cache=MemoryCache(), batch_size=3)
        items = tuple(Question(item_id=str(i), payload_json="{}") for i in range(8))
        answers = await judgments.judge("coverage", items, Budget.start(10))
        assert len(answers) == 8
        assert len(native.calls) == 3
        assert generated.calls == [("3", "4", "5")]
    asyncio.run(run())


def test_cancelled_native_batch_never_falls_back() -> None:
    async def run():
        generated = Backend()
        judgments = NewsJudgments(generated=generated, native=Backend(cancel=True), cache=MemoryCache())
        with pytest.raises(asyncio.CancelledError):
            await judgments.judge("coverage", (Question(item_id="a", payload_json="{}"),), Budget.start(5))
        assert generated.calls == []
    asyncio.run(run())


def test_partial_actual_delivery_does_not_suppress_new_details() -> None:
    async def run():
        _, _, head = update_one()
        backend = Backend(value="partial")
        planner = NotificationPlanner(NewsJudgments(generated=backend, cache=MemoryCache()))
        body = "Agency discussed tariffs."
        receipt = DeliveredText(intent_id="prior", channel="telegram:channel-a", state="sent", body=body,
            payload_sha256=digest(body), received_at_ms=STAMP, provider_message_id="1")
        reader = ReaderSnapshot(channel=receipt.channel, revision="r1", receipts=(receipt,))
        plan = await planner.plan(head, reader, Budget.start(5), now_ms=STAMP + 10)
        assert plan.action == "notify"
        assert len(plan.selected_claim_refs) == 1
    asyncio.run(run())


def test_ambiguous_or_unsent_copy_is_not_reader_coverage() -> None:
    async def run():
        _, _, head = update_one()
        backend = Backend(value="full")
        planner = NotificationPlanner(NewsJudgments(generated=backend, cache=MemoryCache()))
        body = "Draft copy only."
        receipt = DeliveredText(intent_id="prior", channel="c", state="not_sent", body=body, payload_sha256=digest(body))
        plan = await planner.plan(head, ReaderSnapshot(channel="c", revision="r", receipts=(receipt,)), Budget.start(5), now_ms=STAMP + 10)
        assert plan.action == "notify"
        assert backend.calls == []
    asyncio.run(run())


def test_source_update_dispatch_does_not_enter_catalyst_consumer() -> None:
    async def run():
        _, updated = correction_update()
        rows = public_updates(updated, semantic_completed_at_ms=STAMP + 90)
        class Store:
            acknowledged = []
            async def pending_public_updates(self, limit): return rows
            async def acknowledge_public_update(self, update_id): self.acknowledged.append(update_id)
        class Receiver:
            updates = []
            async def receive_catalyst(self, update): raise AssertionError("correction reached entry")
            async def receive_source_update(self, update): self.updates.append(update.update_id)
        store, receiver = Store(), Receiver()
        assert await PublicRelay(store, receiver).advance() == 1
        assert store.acknowledged == receiver.updates
    asyncio.run(run())
