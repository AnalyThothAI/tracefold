"""Regressions for occurrence reuse and source-specific evidence relationships.

The judgment backend below is a test double. These tests exercise the real
contract, semantic assembly, cache and public projection without provider I/O.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from tracefold.news.updates.contracts import (
    ChangeKind,
    Citation,
    Claim,
    ClaimFields,
    DraftClaim,
    EventUpdate,
    Evidence,
    Extraction,
    FrozenInput,
    PriorClaim,
    Quantity,
    Relation,
    RelationDraft,
    Source,
    SupportDraft,
)
from tracefold.news.updates.judgment import (
    Answer,
    BatchResult,
    Budget,
    ContractFault,
    NewsJudgments,
    Question,
    Task,
)
from tracefold.news.updates.public import public_updates
from tracefold.news.updates.semantics import SemanticAnalyzer, assemble_update

STAMP = 1_790_405_000_000


def evidence(text: str, revision: int, *, publisher: str = "wire") -> Evidence:
    return Evidence.issue(
        text,
        Source(
            publisher_id=publisher,
            artifact_id=f"report-{revision}",
            artifact_revision="1",
            first_available_at_ms=STAMP + revision,
        ),
    )


def draft(source: Evidence, value: str = "25", *, slot: str = "capacity") -> DraftClaim:
    return DraftClaim(
        slot=slot,
        statement=source.text,
        fields=ClaimFields(
            subject="Example Industries",
            action="set capacity",
            object="facility",
            mode="decision",
            phase="announced",
            quantities=(Quantity(name="capacity", value=value, unit="MW"),),
        ),
        citations=(Citation(evidence_ref=source.ref, quote=source.text),),
    )


def relation(
    prior: Claim,
    value: Relation,
    kind: ChangeKind | None = None,
    *,
    slot: str = "capacity",
) -> RelationDraft:
    return RelationDraft(slot=slot, previous_ref=prior.ref, relation=value, change_kind=kind)


def frozen(
    sources: tuple[Evidence, ...],
    revision: int,
    head: EventUpdate | None = None,
) -> FrozenInput:
    return FrozenInput(
        event_id="capacity-event",
        revision=revision,
        lineage_id="capacity-lineage",
        evidence=sources,
        prior=() if head is None else tuple(
            PriorClaim(event_id=head.event_id, content_revision=head.content_revision, claim=claim)
            for claim in head.claims
        ),
    )


def adopt(
    source: Evidence,
    revision: int,
    value: str,
    *,
    head: EventUpdate | None = None,
    relations: tuple[RelationDraft, ...] = (),
) -> EventUpdate:
    result = assemble_update(
        frozen((source,), revision, head),
        Extraction(
            claims=(draft(source, value),),
            relations=relations,
            supports=(SupportDraft(slot="capacity", evidence_ref=source.ref, relation="reports"),),
        ),
        head,
        adopted_at_ms=STAMP + revision + 100,
    )
    assert result is not None
    return result


def progressed() -> tuple[EventUpdate, EventUpdate]:
    first = adopt(evidence("Issuer announces 25 MW.", 1), 1, "25")
    second = adopt(
        evidence("Issuer now announces 50 MW.", 2), 2, "50", head=first,
        relations=(relation(first.claims[0], "real_world_change", "parameter_change"),),
    )
    return first, second


def repeat_latest(head: EventUpdate, revision: int = 3, *, reverse: bool = False) -> EventUpdate:
    relations = (
        relation(head.claims[-1], "equivalent"),
        *(relation(claim, "real_world_change", "parameter_change") for claim in head.claims[:-1]),
    )
    return adopt(
        evidence("A second wire repeats the latest 50 MW announcement.", revision),
        revision, "50", head=head, relations=tuple(reversed(relations)) if reverse else relations,
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_repeat_of_latest_occurrence_does_not_republish_ancestor_change(reverse: bool) -> None:
    _, head = progressed()
    updated = repeat_latest(head, reverse=reverse)
    assert {claim.ref for claim in updated.claims} == {claim.ref for claim in head.claims}
    assert {change.kind for change in updated.changes} == {"evidence_change"}
    rows = public_updates(updated, semantic_completed_at_ms=STAMP + 200)
    assert [row.kind for row in rows] == ["source_update"]
    assert rows[0].first_available_at_ms == head.claims[-1].first_available_at_ms


def test_repeat_stays_equivalent_after_later_evidence_update_and_json_roundtrip() -> None:
    _, head = progressed()
    source = evidence("Another source supports 50 MW.", 3)
    head = adopt(source, 3, "50", head=head, relations=(relation(head.claims[-1], "equivalent"),))
    assert {change.kind for change in head.changes} == {"evidence_change"}
    head = EventUpdate.model_validate_json(head.model_dump_json())
    updated = repeat_latest(head, revision=4)
    assert len(updated.claims) == 2
    assert all(row.kind == "source_update" for row in public_updates(updated, semantic_completed_at_ms=STAMP + 300))


def test_repeat_does_not_republish_transitive_ancestors() -> None:
    _, head = progressed()
    latest = adopt(
        evidence("Issuer increases capacity to 75 MW.", 3), 3, "75", head=head,
        relations=(relation(head.claims[-1], "real_world_change", "parameter_change"),),
    )
    updated = adopt(
        evidence("Second wire repeats 75 MW.", 4), 4, "75", head=latest,
        relations=(
            relation(latest.claims[-1], "equivalent"),
            relation(latest.claims[0], "real_world_change", "parameter_change"),
            relation(latest.claims[1], "real_world_change", "parameter_change"),
        ),
    )
    assert len(updated.claims) == 3
    assert {change.kind for change in updated.changes} == {"evidence_change"}


def test_real_reversal_is_not_suppressed_by_same_shape_in_older_history() -> None:
    first, head = progressed()
    source = evidence("Issuer reduces announced capacity back to 25 MW.", 3)
    updated = adopt(
        source, 3, "25", head=head,
        relations=(
            relation(first.claims[0], "equivalent"),
            relation(head.claims[-1], "real_world_change", "parameter_change"),
        ),
    )
    assert len(updated.claims) == 3
    assert updated.claims[-1].ref != first.claims[0].ref
    assert updated.claims[-1].first_available_at_ms == source.source.first_available_at_ms
    rows = public_updates(updated, semantic_completed_at_ms=STAMP + 200)
    assert [row.kind for row in rows] == ["catalyst_delta"]
    assert rows[0].claim_refs == (updated.claims[-1].ref,)


def test_invalid_equivalence_for_one_pair_does_not_erase_another_valid_pair() -> None:
    first, head = progressed()
    updated = adopt(
        evidence("Another wire repeats 50 MW.", 3), 3, "50", head=head,
        relations=(relation(first.claims[0], "equivalent"), relation(head.claims[-1], "equivalent")),
    )
    assert len(updated.claims) == 2
    assert {change.kind for change in updated.changes} == {"evidence_change"}


def test_exact_repeat_after_adoption_remains_idempotent() -> None:
    _, head = progressed()
    source = evidence("Another wire repeats 50 MW.", 3)
    relations = (
        relation(head.claims[0], "real_world_change", "parameter_change"),
        relation(head.claims[-1], "equivalent"),
    )
    extraction = Extraction(
        claims=(draft(source, "50"),), relations=relations,
        supports=(SupportDraft(slot="capacity", evidence_ref=source.ref, relation="reports"),),
    )
    updated = assemble_update(frozen((source,), 3, head), extraction, head, adopted_at_ms=STAMP + 100)
    assert updated is not None
    assert assemble_update(
        frozen((source,), 3, head), extraction, updated, adopted_at_ms=STAMP + 999,
    ) is None


@pytest.mark.parametrize("support_relation", ["supports", "refutes", "reports", "not_addressed", "unresolved"])
def test_nonquoted_source_relation_survives_adoption_and_public_projection(support_relation: str) -> None:
    report = evidence("Issuer announces 25 MW.", 1)
    other = evidence("Operator comments on the reported capacity.", 2, publisher="operator")
    supports = (
        SupportDraft(slot="capacity", evidence_ref=report.ref, relation="reports"),
        SupportDraft.model_validate({"slot": "capacity", "evidence_ref": other.ref, "relation": support_relation}),
    )
    result = assemble_update(
        frozen((report, other), 1), Extraction(claims=(draft(report),), supports=supports),
        None, adopted_at_ms=STAMP + 10,
    )
    assert result is not None
    assert {(row.evidence_ref, row.relation) for row in result.evidence_relations} == {
        (report.ref, "reports"), (other.ref, support_relation),
    }
    assert [citation.evidence_ref for citation in result.claims[0].citations] == [report.ref]
    rows = public_updates(result, semantic_completed_at_ms=STAMP + 9)
    assert len(rows) == 1
    assert other.ref in {item.ref for item in rows[0].evidence}
    assert f"/{other.ref}:{support_relation}" in rows[0].text


def test_new_refutation_updates_source_without_creating_a_new_catalyst() -> None:
    report = evidence("Issuer announces 25 MW.", 1)
    head = adopt(report, 1, "25")
    denial = evidence("Operator denies the reported 25 MW capacity.", 2, publisher="operator")
    extraction = Extraction(
        claims=(draft(report),), relations=(relation(head.claims[0], "equivalent"),),
        supports=(
            SupportDraft(slot="capacity", evidence_ref=report.ref, relation="reports"),
            SupportDraft(slot="capacity", evidence_ref=denial.ref, relation="refutes"),
        ),
    )
    updated = assemble_update(frozen((report, denial), 2, head), extraction, head, adopted_at_ms=STAMP + 20)
    assert updated is not None
    assert len(updated.claims) == 1
    rows = public_updates(updated, semantic_completed_at_ms=STAMP + 19)
    assert [row.kind for row in rows] == ["source_update"]
    assert rows[0].affected_claim_refs == (head.claims[0].ref,)
    assert rows[0].first_available_at_ms == head.claims[0].first_available_at_ms


def test_nonquoted_relation_cannot_reference_missing_frozen_evidence() -> None:
    report = evidence("Issuer announces 25 MW.", 1)
    absent = evidence("Operator denies the reported 25 MW capacity.", 2, publisher="operator")
    extraction = Extraction(
        claims=(draft(report),),
        supports=(SupportDraft(slot="capacity", evidence_ref=absent.ref, relation="refutes"),),
    )
    with pytest.raises(ContractFault, match="news_support_evidence_not_supplied"):
        assemble_update(frozen((report,), 1), extraction, None, adopted_at_ms=STAMP)


def test_unresolved_relationship_does_not_erase_an_adopted_refutation() -> None:
    report = evidence("Issuer announces 25 MW.", 1)
    denial = evidence("Operator denies the reported 25 MW capacity.", 2, publisher="operator")
    supports = (
        SupportDraft(slot="capacity", evidence_ref=report.ref, relation="reports"),
        SupportDraft(slot="capacity", evidence_ref=denial.ref, relation="refutes"),
    )
    head = assemble_update(
        frozen((report, denial), 1), Extraction(claims=(draft(report),), supports=supports),
        None, adopted_at_ms=STAMP + 10,
    )
    assert head is not None
    assert any(row.evidence_ref == denial.ref and row.relation == "refutes" for row in head.evidence_relations)
    retry = Extraction(
        claims=(draft(report),), relations=(relation(head.claims[0], "equivalent"),),
        supports=(supports[0], SupportDraft(slot="capacity", evidence_ref=denial.ref, relation="unresolved")),
    )
    assert assemble_update(frozen((report, denial), 2, head), retry, head, adopted_at_ms=STAMP + 20) is None


class MemoryCache:
    def __init__(self) -> None:
        self.values: dict[str, Answer] = {}

    async def get(self, key: str) -> Answer | None:
        return self.values.get(key)

    async def put(self, key: str, answer: Answer) -> None:
        self.values.setdefault(key, answer)


class UnusedExtractor:
    identity = "unused-extractor"

    async def extract(self, source: FrozenInput, *, extract_only: bool, timeout: float) -> Extraction:
        raise AssertionError("understanding must not re-extract supplied claims")


class SourceBackend:
    identity = "source-relationship-test-double"

    def __init__(self) -> None:
        self.calls: list[tuple[Task, tuple[str, ...]]] = []

    async def judge(self, task: Task, items: tuple[Question, ...], *, timeout: float) -> BatchResult:
        assert task == "support"
        refs = tuple(str(json.loads(item.payload_json)["evidence"]["ref"]) for item in items)
        self.calls.append((task, refs))
        return BatchResult(answers=tuple(
            Answer(item_id=item.item_id, value="refutes", backend=self.identity) for item in items
        ))


def test_analyzer_fills_only_missing_current_source_pairs_and_reuses_successful_cache() -> None:
    async def run() -> None:
        report = evidence("Issuer announces 25 MW.", 1)
        denial = evidence("Operator denies the reported 25 MW capacity.", 2, publisher="operator")
        source = frozen((report, denial), 1)
        extracted = Extraction(
            claims=(draft(report),),
            supports=(SupportDraft(slot="capacity", evidence_ref=report.ref, relation="reports"),),
        )
        backend = SourceBackend()
        analyzer = SemanticAnalyzer(UnusedExtractor(), NewsJudgments(generated=backend, cache=MemoryCache()))
        first = await analyzer.understand(source, extracted, Budget.start(5))
        second = await analyzer.understand(source, extracted, Budget.start(5))
        assert backend.calls == [("support", (denial.ref,))]
        assert first == second
        assert {(row.evidence_ref, row.relation) for row in first.supports} == {
            (report.ref, "reports"), (denial.ref, "refutes"),
        }

    asyncio.run(run())


def test_analyzer_does_not_rejudge_already_supplied_source_relations() -> None:
    async def run() -> None:
        report = evidence("Issuer announces 25 MW.", 1)
        denial = evidence("Operator denies the reported 25 MW capacity.", 2, publisher="operator")
        extracted = Extraction(
            claims=(draft(report),),
            supports=(
                SupportDraft(slot="capacity", evidence_ref=report.ref, relation="reports"),
                SupportDraft(slot="capacity", evidence_ref=denial.ref, relation="refutes"),
            ),
        )
        backend = SourceBackend()
        analyzer = SemanticAnalyzer(UnusedExtractor(), NewsJudgments(generated=backend, cache=MemoryCache()))
        result = await analyzer.understand(frozen((report, denial), 1), extracted, Budget.start(5))
        assert result == extracted
        assert backend.calls == []

    asyncio.run(run())
