"""Per-claim retrieval scope survives semantic assembly and head adoption races."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any, Literal

import pytest

from tests.support.news_update_semantic import STAMP, MemoryCache, draft, material, prior_of
from tracefold.news.updates.assembly import assemble_update
from tracefold.news.updates.contracts import (
    DraftClaim,
    EventUpdate,
    Extraction,
    FrozenInput,
    PriorClaim,
    Relation,
    RelationDraft,
    SemanticLease,
    SupportDraft,
)
from tracefold.news.updates.judgment import Answer, BatchResult, NewsJudgments, Question, Task
from tracefold.news.updates.ports import PriorBatch, SemanticCheckpoint, SemanticObservation
from tracefold.news.updates.semantics import SemanticAnalyzer
from tracefold.news.updates.service import NewsAgent


def source_and_extraction() -> tuple[FrozenInput, Extraction]:
    tariff = material("Agency announces 25% tariff effective October 1.", revision=2)
    buyback = material("Company approves a 100 million dollar buyback.", revision=2, publisher="company")
    second = draft(buyback, quantity="100").model_dump(mode="json")
    second["slot"] = "b"
    second["fields"].update(
        subject="Company",
        action="approve buyback",
        object="shares",
        effective_at=None,
        quantities=[{"name": "amount", "value": "100", "unit": "million USD"}],
        assets=[],
    )
    extraction = Extraction(
        claims=(draft(tariff), DraftClaim.model_validate(second)),
        supports=(
            SupportDraft(slot="a", evidence_ref=tariff.ref, relation="reports"),
            SupportDraft(slot="b", evidence_ref=buyback.ref, relation="reports"),
        ),
    )
    return FrozenInput(event_id="current", revision=2, lineage_id="line", evidence=(tariff, buyback)), extraction


def prior_update(event_id: str, *, quantity: str = "20") -> EventUpdate:
    evidence = material(f"Earlier report announces {quantity}% tariff.", publisher=event_id)
    source = FrozenInput(event_id=event_id, revision=1, lineage_id=f"{event_id}-line", evidence=(evidence,))
    update = assemble_update(
        source,
        Extraction(
            claims=(draft(evidence, quantity=quantity),),
            supports=(SupportDraft(slot="a", evidence_ref=evidence.ref, relation="reports"),),
        ),
        None,
        adopted_at_ms=STAMP + 5,
    )
    assert update is not None
    return update


class Extractor:
    identity = "relation-scope-reader"

    def __init__(self, extraction: Extraction) -> None:
        self.extraction = extraction
        self.calls = 0

    async def extract(self, source: FrozenInput) -> Extraction:
        self.calls += 1
        return self.extraction


class Recall:
    def __init__(self, selected: Mapping[str, tuple[PriorClaim, ...]]) -> None:
        self.selected = selected
        self.calls = 0

    async def priors(self, source: FrozenInput, extracted: Extraction) -> PriorBatch:
        self.calls += 1
        return PriorBatch(
            by_slot=self.selected,
            diagnostics={slot: {"selected_count": len(rows)} for slot, rows in self.selected.items()},
        )


class Backend:
    identity = "relation-scope-backend"

    def __init__(self, values: Mapping[tuple[str, str], Relation] | None = None) -> None:
        self.values = values or {}
        self.relation_batches: list[tuple[tuple[str, str], ...]] = []

    async def judge(self, task: Task, items: tuple[Question, ...], *, context_json: str | None) -> BatchResult:
        answers = []
        pairs = []
        for item in items:
            payload = json.loads(item.payload_json)
            if task == "relation":
                pair = (payload["current"]["slot"], payload["previous"]["ref"])
                pairs.append(pair)
                value = self.values.get(pair, "unrelated")
            else:
                assert task == "support"
                value = "not_addressed"
            answers.append(Answer(item_id=item.item_id, value=value, backend=self.identity))
        if pairs:
            self.relation_batches.append(tuple(pairs))
        return BatchResult(answers=tuple(answers))


class Store:
    """The first failed CAS can expose a concurrent head on the second read."""

    def __init__(self, *, raced_head: EventUpdate | None = None) -> None:
        self.current_head: EventUpdate | None = None
        self.raced_head = raced_head
        self.extractions: dict[str, Extraction] = {}
        self.observations: dict[str, SemanticObservation] = {}
        self.attempts: list[dict[str, Any]] = []
        self.finished: list[str] = []

    async def head(self, event_id: str) -> EventUpdate | None:
        return self.current_head

    async def checkpoint(self, work_id: str) -> SemanticCheckpoint | None:
        extracted = self.extractions.get(work_id)
        return None if extracted is None else SemanticCheckpoint(work_id=work_id, extraction=extracted)

    async def save_extraction(self, work_id: str, extracted: Extraction) -> Extraction:
        return self.extractions.setdefault(work_id, extracted)

    async def save_observation(self, observation: SemanticObservation) -> SemanticObservation:
        return self.observations.setdefault(observation.result_id, observation)

    async def atomic_adopt(self, **kwargs: Any) -> bool:
        self.attempts.append(kwargs)
        if self.raced_head is not None and len(self.attempts) == 1:
            self.current_head = self.raced_head
            return False
        assert kwargs["expected_head_ref"] == (None if self.current_head is None else self.current_head.ref)
        self.current_head = kwargs["update"]
        return True

    async def finish_semantic_work(self, work_id: str, *, lease: SemanticLease, reason: str) -> None:
        self.finished.append(reason)

    async def defer_semantic_event(self, lease: SemanticLease, *, reason: str) -> None:
        pytest.fail(f"bounded adoption unexpectedly deferred: {reason}")


def run_agent(
    *,
    unresolved: tuple[str, str] | None = None,
    raced_head: EventUpdate | None = None,
    empty_slot: Literal["missing", "empty"] | None = None,
) -> tuple[Store, Backend, Extractor, Recall, dict[str, PriorClaim]]:
    source, extracted = source_and_extraction()
    priors = {"a": prior_of(prior_update("tariff-prior"))[0], "b": prior_of(prior_update("buyback-prior"))[0]}
    selected = {slot: (prior,) for slot, prior in priors.items()}
    if empty_slot == "missing":
        del selected["b"]
    elif empty_slot == "empty":
        selected["b"] = ()
    recall = Recall(selected)
    backend = Backend({unresolved: "unresolved"} if unresolved is not None else None)
    extractor = Extractor(extracted)
    store = Store(raced_head=raced_head)
    analyzer = SemanticAnalyzer(extractor, NewsJudgments(generated=backend, cache=MemoryCache()), topics=())
    agent = NewsAgent(store, analyzer, program_identity="scope-test", recall=recall, clock=lambda: STAMP + 100)
    assert asyncio.run(agent.process(SemanticLease(source=source, lease_token="lease", attempts=1))) == "adopted"
    assert extractor.calls == recall.calls == len(store.extractions) == 1
    assert store.finished == ["adopted"]
    return store, backend, extractor, recall, priors


def test_disjoint_recall_pairs_publish_both_unrelated_claims_as_new_facts() -> None:
    store, backend, _, _, priors = run_agent()
    adopted = store.attempts[-1]
    update = adopted["update"]
    assert {change.kind for change in update.changes} == {"new_fact"}
    assert {change.current_ref for change in update.changes} == {claim.ref for claim in update.claims}
    assert backend.relation_batches == [tuple((slot, prior.claim.ref) for slot, prior in priors.items())]
    assert len(adopted["public"]) == 1
    public = adopted["public"][0]
    assert public.kind == "catalyst_delta"
    assert set(public.claim_refs) == {claim.ref for claim in update.claims}
    assert adopted["observation"].input_manifest["recall"]["pair_count"] == 2


def test_unresolved_selected_prior_only_blocks_its_own_claim() -> None:
    selected = prior_of(prior_update("tariff-prior"))[0]
    store, backend, _, _, priors = run_agent(unresolved=("a", selected.claim.ref))
    adopted = store.attempts[-1]
    update = adopted["update"]
    refs = {claim.fields.subject: claim.ref for claim in update.claims}
    assert [(change.kind, change.current_ref, change.previous_ref) for change in update.changes] == [
        ("possible_new", refs["Agency"], priors["a"].claim.ref),
        ("new_fact", refs["Company"], None),
    ]
    assert backend.relation_batches == [tuple((slot, prior.claim.ref) for slot, prior in priors.items())]
    assert [(row.kind, row.claim_refs) for row in adopted["public"]] == [("catalyst_delta", (refs["Company"],))]


@pytest.mark.parametrize("empty_slot", ["missing", "empty"])
def test_claim_with_no_external_candidates_is_new_while_another_claim_has_a_prior(empty_slot) -> None:
    store, backend, _, _, priors = run_agent(empty_slot=empty_slot)
    adopted = store.attempts[-1]
    update = adopted["update"]
    assert {change.kind for change in update.changes} == {"new_fact"}
    assert len(update.changes) == 2
    assert backend.relation_batches == [(("a", priors["a"].claim.ref),)]
    assert adopted["observation"].input_manifest["recall"]["pair_count"] == 1
    assert [(row.kind, set(row.claim_refs)) for row in adopted["public"]] == [
        ("catalyst_delta", {claim.ref for claim in update.claims})
    ]


def test_cas_rebase_compares_new_local_head_without_expanding_foreign_recall() -> None:
    head = prior_update("current")
    local_ref = head.claims[0].ref
    store, backend, _, _, priors = run_agent(unresolved=("a", local_ref), raced_head=head)
    assert len(store.attempts) == 2
    assert store.attempts[0]["expected_head_ref"] is None
    adopted = store.attempts[-1]
    assert adopted["expected_head_ref"] == head.ref
    assert backend.relation_batches == [
        tuple((slot, prior.claim.ref) for slot, prior in priors.items()),
        (("a", local_ref), ("b", local_ref)),
    ]
    new_claims = {claim.fields.subject: claim.ref for claim in adopted["update"].claims if claim.ref != local_ref}
    assert [(change.kind, change.current_ref, change.previous_ref) for change in adopted["update"].changes] == [
        ("possible_new", new_claims["Agency"], local_ref),
        ("new_fact", new_claims["Company"], None),
    ]
    assert [(row.kind, row.claim_refs) for row in adopted["public"]] == [("catalyst_delta", (new_claims["Company"],))]
    assert adopted["observation"].input_manifest["recall"]["pair_count"] == 4


@pytest.mark.parametrize("pairs, expected", [(None, "possible_new"), (frozenset(), "new_fact")])
def test_empty_relation_scope_differs_from_unbounded_legacy_scope(pairs, expected: str) -> None:
    source, extracted = source_and_extraction()
    prior = prior_of(prior_update("external"))[0]
    source = source.model_copy(update={"prior": (prior,)})
    extracted = extracted.model_copy(update={"claims": extracted.claims[:1], "supports": extracted.supports[:1]})
    update = assemble_update(source, extracted, None, adopted_at_ms=STAMP + 100, relation_pairs=pairs)
    assert update is not None
    assert [(change.kind, change.previous_ref) for change in update.changes] == [
        (expected, prior.claim.ref if expected == "possible_new" else None)
    ]


@pytest.mark.parametrize("location", ["selected", "local_source"])
def test_missing_in_scope_relation_remains_possible_new(location: str) -> None:
    source, extracted = source_and_extraction()
    head = prior_update("external" if location == "selected" else source.event_id)
    prior = prior_of(head)[0]
    source = source.model_copy(update={"prior": (prior,)})
    extracted = extracted.model_copy(update={"claims": extracted.claims[:1], "supports": extracted.supports[:1]})
    pairs = frozenset({("a", prior.claim.ref)}) if location == "selected" else frozenset()
    update = assemble_update(
        source,
        extracted,
        None,
        adopted_at_ms=STAMP + 100,
        relation_pairs=pairs,
    )
    assert update is not None
    assert [(change.kind, change.previous_ref, change.previous_content_ref) for change in update.changes] == [
        ("possible_new", prior.claim.ref, head.ref)
    ]


def test_refuted_equivalent_pair_stays_unresolved_without_cross_slot_contamination() -> None:
    source, extracted = source_and_extraction()
    selected, excluded = (
        prior_of(prior_update(event, quantity=value))[0] for event, value in (("selected", "50"), ("excluded", "25"))
    )
    source = source.model_copy(update={"prior": (selected, excluded)})
    extracted = extracted.model_copy(
        update={
            "claims": extracted.claims[:1],
            "supports": extracted.supports[:1],
            "relations": (RelationDraft(slot="a", previous_ref=selected.claim.ref, relation="equivalent"),),
        }
    )
    update = assemble_update(
        source,
        extracted,
        None,
        adopted_at_ms=STAMP + 100,
        relation_pairs=frozenset({("a", selected.claim.ref)}),
    )
    assert update is not None
    assert [(change.kind, change.previous_ref) for change in update.changes] == [("possible_new", selected.claim.ref)]
