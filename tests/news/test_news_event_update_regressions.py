"""Regressions for occurrence reuse and source-specific evidence relationships.

The judgment backend below is a test double. These tests exercise the real
contract, semantic assembly, cache and public projection without provider I/O.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tests.support.news_update_semantic import MemoryCache
from tracefold.news.updates.assembly import assemble_update, proven_mismatches
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
from tracefold.news.updates.semantics import SemanticAnalyzer

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
        prior=()
        if head is None
        else tuple(
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
        evidence("Issuer now announces 50 MW.", 2),
        2,
        "50",
        head=first,
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
        revision,
        "50",
        head=head,
        relations=tuple(reversed(relations)) if reverse else relations,
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
        evidence("Issuer increases capacity to 75 MW.", 3),
        3,
        "75",
        head=head,
        relations=(relation(head.claims[-1], "real_world_change", "parameter_change"),),
    )
    updated = adopt(
        evidence("Second wire repeats 75 MW.", 4),
        4,
        "75",
        head=latest,
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
        source,
        3,
        "25",
        head=head,
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


@pytest.mark.parametrize(("external_relation", "change_kind"), [("conflicts", "conflict"), ("corrects", "correction")])
def test_equivalent_local_claim_keeps_external_relation_without_a_duplicate(
    external_relation: Relation, change_kind: ChangeKind
) -> None:
    # BUG-D shape: the second source repeats the adopted proposition, while a
    # newly considered claim in another Event disagrees with it.
    original = evidence("A wire says a second US underwater drone was captured.", 1)
    head = adopt(original, 1, "25")
    later = evidence("Another wire reports the same capture and disputes a separate report.", 2)
    external = head.claims[0].model_copy(update={"ref": "external-claim"})
    source = frozen((later,), 2, head)
    source = source.model_copy(
        update={
            "prior": (
                *source.prior,
                PriorClaim(event_id="external-event", content_revision="external-rev", claim=external),
            )
        }
    )
    result = assemble_update(
        source,
        Extraction(
            claims=(draft(later),),
            relations=(
                relation(head.claims[0], "equivalent"),
                relation(external, external_relation, change_kind),
            ),
            supports=(SupportDraft(slot="capacity", evidence_ref=later.ref, relation="reports"),),
        ),
        head,
        adopted_at_ms=STAMP + 200,
    )
    assert result is not None
    assert [claim.ref for claim in result.claims] == [head.claims[0].ref]
    assert result.claims[0].first_available_at_ms == head.claims[0].first_available_at_ms
    assert any(
        change.current_ref == head.claims[0].ref and change.previous_ref == external.ref and change.kind == change_kind
        for change in result.changes
    )
    assert any(
        link.claim_ref == head.claims[0].ref and link.evidence_ref == later.ref for link in result.evidence_relations
    )


def test_equivalent_local_claim_with_additional_prior_relation_is_only_a_source_update() -> None:
    first, head = progressed()
    later = evidence("A later wire repeats the 50 MW announcement.", 3)
    result = adopt(
        later,
        3,
        "50",
        head=head,
        relations=(
            relation(head.claims[-1], "equivalent"),
            relation(first.claims[0], "adds_information", "new_fact"),
        ),
    )
    assert len(result.claims) == len(head.claims)
    assert any(change.relation == "adds_information" for change in result.changes)
    assert [row.kind for row in public_updates(result, semantic_completed_at_ms=STAMP + 300)] == ["source_update"]


def test_same_complete_headline_survives_an_unrelated_relation_misread() -> None:
    # The fifth production BUG-D revision repeats a syndicated headline exactly.
    # The second extraction omits an earlier quantity and the relation says
    # unrelated, though neither source reports a different occurrence.
    headline = "Seven people killed in strike on market in Yemen, Houthi-run health ministry says"
    first_source = evidence(headline, 1, publisher="history")
    later_source = evidence(headline, 2, publisher="wire")
    first_draft = DraftClaim(
        slot="casualties",
        statement="Houthi-run health ministry says seven people were killed in a strike on a market in Yemen.",
        fields=ClaimFields(
            subject="Yemen's Houthi-Run Health Ministry",
            action="reported casualty figures from a strike on a market in Taiz",
            object="strike on market in Taiz",
            mode="observation",
            quantities=(
                Quantity(name="people killed", value="7", unit="people"),
                Quantity(name="people wounded", value="40", unit="people"),
            ),
        ),
        citations=(Citation(evidence_ref=first_source.ref, quote=headline),),
    )
    head = assemble_update(frozen((first_source,), 1), Extraction(claims=(first_draft,)), None, adopted_at_ms=STAMP + 1)
    assert head is not None
    second_draft = first_draft.model_copy(
        update={
            "fields": first_draft.fields.model_copy(
                update={"quantities": (Quantity(name="people killed", value="7", unit="people"),)}
            ),
            "citations": (Citation(evidence_ref=later_source.ref, quote=headline),),
        }
    )
    result = assemble_update(
        frozen((later_source,), 2, head),
        Extraction(
            claims=(second_draft,),
            relations=(relation(head.claims[0], "unrelated", slot="casualties"),),
            supports=(SupportDraft(slot="casualties", evidence_ref=later_source.ref, relation="reports"),),
        ),
        head,
        adopted_at_ms=STAMP + 2,
    )
    assert result is not None
    assert [claim.ref for claim in result.claims] == [head.claims[0].ref]
    assert any(
        link.claim_ref == head.claims[0].ref and link.evidence_ref == later_source.ref
        for link in result.evidence_relations
    )
    assert [row.kind for row in public_updates(result, semantic_completed_at_ms=STAMP + 2)] == ["source_update"]


def test_matching_statement_alone_does_not_override_an_unrelated_relation() -> None:
    first_source = evidence("Issuer announced 25 MW for the first site.", 1)
    head = adopt(first_source, 1, "25")
    later_source = evidence("Issuer announced 25 MW for a different site.", 2)
    repeated_statement = draft(later_source).model_copy(
        update={
            "statement": head.claims[0].statement,
            "fields": draft(later_source).fields.model_copy(update={"object": "a different facility"}),
        }
    )
    result = assemble_update(
        frozen((later_source,), 2, head),
        Extraction(
            claims=(repeated_statement,),
            relations=(relation(head.claims[0], "unrelated"),),
            supports=(SupportDraft(slot="capacity", evidence_ref=later_source.ref, relation="reports"),),
        ),
        head,
        adopted_at_ms=STAMP + 2,
    )
    assert result is not None
    assert len(result.claims) == 2


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("polarity", "negative"),
        ("phase", "effective"),
        ("statistical_period", "2026 Q3"),
    ],
)
def test_explicit_proposition_mismatch_cannot_reuse_an_equivalent_hint(field: str, changed: object) -> None:
    source = evidence("Issuer announces 25 MW subject to approval in 2026 Q2.", 1)
    original = draft(source).model_copy(
        update={
            "fields": draft(source).fields.model_copy(
                update={
                    "polarity": "affirmative",
                    "conditions": ("subject to approval",),
                    "statistical_period": "2026 Q2",
                }
            )
        }
    )
    head = assemble_update(frozen((source,), 1), Extraction(claims=(original,)), None, adopted_at_ms=STAMP + 1)
    assert head is not None
    candidate = original.model_copy(update={"fields": original.fields.model_copy(update={field: changed})})
    assert proven_mismatches(candidate, head.claims[0])


def test_invalid_equivalence_for_one_pair_does_not_erase_another_valid_pair() -> None:
    first, head = progressed()
    updated = adopt(
        evidence("Another wire repeats 50 MW.", 3),
        3,
        "50",
        head=head,
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
        claims=(draft(source, "50"),),
        relations=relations,
        supports=(SupportDraft(slot="capacity", evidence_ref=source.ref, relation="reports"),),
    )
    updated = assemble_update(frozen((source,), 3, head), extraction, head, adopted_at_ms=STAMP + 100)
    assert updated is not None
    assert (
        assemble_update(
            frozen((source,), 3, head),
            extraction,
            updated,
            adopted_at_ms=STAMP + 999,
        )
        is None
    )


@pytest.mark.parametrize("support_relation", ["supports", "refutes", "reports", "unresolved"])
def test_nonquoted_source_relation_survives_adoption_and_public_projection(support_relation: str) -> None:
    report = evidence("Issuer announces 25 MW.", 1)
    other = evidence("Operator comments on the reported capacity.", 2, publisher="operator")
    supports = (
        SupportDraft(slot="capacity", evidence_ref=report.ref, relation="reports"),
        SupportDraft.model_validate({"slot": "capacity", "evidence_ref": other.ref, "relation": support_relation}),
    )
    result = assemble_update(
        frozen((report, other), 1),
        Extraction(claims=(draft(report),), supports=supports),
        None,
        adopted_at_ms=STAMP + 10,
    )
    assert result is not None
    assert {(row.evidence_ref, row.relation) for row in result.evidence_relations} == {
        (report.ref, "reports"),
        (other.ref, support_relation),
    }
    assert [citation.evidence_ref for citation in result.claims[0].citations] == [report.ref]
    rows = public_updates(result, semantic_completed_at_ms=STAMP + 9)
    assert len(rows) == 1
    assert other.ref in {item.ref for item in rows[0].evidence}
    assert f"/{other.ref}:{support_relation}" in rows[0].text


def test_material_that_does_not_address_an_adopted_claim_is_no_relationship_and_no_revision() -> None:
    # #742 W6: unrelated material arriving beside an adopted claim's sources is judged `not_addressed`. It
    # is no relationship, so it changes no content: no adoption, no notification reset, no source_update.
    report = evidence("Issuer announces 25 MW.", 1)
    head = adopt(report, 1, "25")
    unrelated = evidence("A separate exchange lists a new token.", 2, publisher="exchange")
    extraction = Extraction(
        claims=(draft(report),),
        relations=(relation(head.claims[0], "equivalent"),),
        supports=(
            SupportDraft(slot="capacity", evidence_ref=report.ref, relation="reports"),
            SupportDraft(slot="capacity", evidence_ref=unrelated.ref, relation="not_addressed"),
        ),
    )
    assert assemble_update(frozen((report, unrelated), 2, head), extraction, head, adopted_at_ms=STAMP + 20) is None
    first = assemble_update(
        frozen((report, unrelated), 1),
        Extraction(claims=(draft(report),), supports=extraction.supports),
        None,
        adopted_at_ms=STAMP + 10,
    )
    assert first is not None
    assert {row.evidence_ref for row in first.evidence_relations} == {report.ref}


def test_new_refutation_updates_source_without_creating_a_new_catalyst() -> None:
    report = evidence("Issuer announces 25 MW.", 1)
    head = adopt(report, 1, "25")
    denial = evidence("Operator denies the reported 25 MW capacity.", 2, publisher="operator")
    extraction = Extraction(
        claims=(draft(report),),
        relations=(relation(head.claims[0], "equivalent"),),
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
        frozen((report, denial), 1),
        Extraction(claims=(draft(report),), supports=supports),
        None,
        adopted_at_ms=STAMP + 10,
    )
    assert head is not None
    assert any(row.evidence_ref == denial.ref and row.relation == "refutes" for row in head.evidence_relations)
    retry = Extraction(
        claims=(draft(report),),
        relations=(relation(head.claims[0], "equivalent"),),
        supports=(supports[0], SupportDraft(slot="capacity", evidence_ref=denial.ref, relation="unresolved")),
    )
    assert assemble_update(frozen((report, denial), 2, head), retry, head, adopted_at_ms=STAMP + 20) is None


class UnusedExtractor:
    identity = "unused-extractor"

    async def extract(self, source: FrozenInput) -> Extraction:
        raise AssertionError("understanding must not re-extract supplied claims")


class SourceBackend:
    identity = "source-relationship-test-double"

    def __init__(self) -> None:
        self.calls: list[tuple[Task, tuple[str, ...]]] = []

    async def judge(self, task: Task, items: tuple[Question, ...], *, context_json: str | None) -> BatchResult:
        assert task == "support"
        refs = tuple(str(json.loads(item.payload_json)["evidence"]["ref"]) for item in items)
        self.calls.append((task, refs))
        return BatchResult(
            answers=tuple(Answer(item_id=item.item_id, value="refutes", backend=self.identity) for item in items)
        )


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
            (report.ref, "reports"),
            (denial.ref, "refutes"),
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


def test_content_that_returns_to_an_earlier_state_is_a_new_adoption() -> None:
    report = evidence("Issuer announces 25 MW.", 1)
    operator = evidence("Operator comments on the reported 25 MW capacity.", 2, publisher="operator")

    def reading(value: str) -> tuple[SupportDraft, SupportDraft]:
        return (
            SupportDraft(slot="capacity", evidence_ref=report.ref, relation="reports"),
            SupportDraft.model_validate({"slot": "capacity", "evidence_ref": operator.ref, "relation": value}),
        )

    first = assemble_update(
        frozen((report, operator), 1),
        Extraction(claims=(draft(report),), supports=reading("refutes")),
        None,
        adopted_at_ms=STAMP + 10,
    )
    assert first is not None
    heads = [first]
    for revision, value in ((2, "supports"), (3, "refutes")):
        head = heads[-1]
        update = assemble_update(
            frozen((report, operator), revision, head),
            Extraction(
                claims=(draft(report),),
                relations=(relation(head.claims[0], "equivalent"),),
                supports=reading(value),
            ),
            head,
            adopted_at_ms=STAMP + 10 * revision,
        )
        assert update is not None
        assert update.previous_content_revision == head.content_revision
        heads.append(update)

    # A→B→A: the material is the first state again, but the adoption is a distinct, chained revision.
    assert heads[2].content_sha == heads[0].content_sha
    assert len({update.content_revision for update in heads}) == 3
    assert EventUpdate.model_validate_json(heads[2].model_dump_json()) == heads[2]


# ---------------------------------------------------------------- #742 S5 / S6: relation correctness

HOUR = 3_600_000


def _external(head: EventUpdate, ref: str, *, first_available_at_ms: int) -> PriorClaim:
    claim = head.claims[0].model_copy(update={"ref": ref, "first_available_at_ms": first_available_at_ms})
    return PriorClaim(event_id="external-event", content_revision="external-rev", claim=claim)


def _with_prior(source: FrozenInput, *rows: PriorClaim) -> FrozenInput:
    return source.model_copy(update={"prior": (*source.prior, *rows)})


@pytest.mark.parametrize("relation_value", ["corrects", "real_world_change"])
def test_a_restated_older_claim_cannot_correct_or_supersede_a_newer_one(relation_value: Relation) -> None:
    # Production 2026-09-28: "SpaceX prepares to send Starship to orbit" (first seen 9 h earlier) was restated
    # by a later revision and judged to correct "SpaceX says Starship is in orbit"; Trading refused entry.
    original = evidence("SpaceX prepares to send Starship rocket to orbit for first time.", 1)
    head = adopt(original, 1, "25")
    newer = _external(head, "cl:in-orbit", first_available_at_ms=STAMP + 9 * HOUR)
    restated = evidence("SpaceX prepares to send Starship to orbit, a wire repeats.", 2)
    kind: ChangeKind = "correction" if relation_value == "corrects" else "parameter_change"
    result = assemble_update(
        _with_prior(frozen((restated,), 2, head), newer),
        Extraction(
            claims=(draft(restated),),
            relations=(relation(head.claims[0], "equivalent"), relation(newer.claim, relation_value, kind)),
            supports=(SupportDraft(slot="capacity", evidence_ref=restated.ref, relation="reports"),),
        ),
        head,
        adopted_at_ms=STAMP + 10 * HOUR,
    )
    assert result is not None
    assert result.retired_claim_refs == () and result.superseded_claim_refs == ()
    assert all(change.previous_ref != newer.claim.ref for change in result.changes)
    assert all(row.retired_claim_refs == () for row in public_updates(result, semantic_completed_at_ms=STAMP))


def test_a_late_arriving_older_report_is_an_unresolved_comparison_not_a_correction() -> None:
    late = Evidence.issue(
        "Issuer announces 25 MW.",
        Source(
            publisher_id="wire",
            artifact_id="late",
            artifact_revision="1",
            published_at_ms=STAMP,
            first_available_at_ms=STAMP + 10 * HOUR,
        ),
    )
    head = adopt(evidence("Issuer announces 50 MW.", 1), 1, "50")
    newer = _external(head, "cl:newer", first_available_at_ms=STAMP + 2 * HOUR)
    source = FrozenInput(event_id="late-event", revision=1, lineage_id="late", evidence=(late,), prior=(newer,))
    result = assemble_update(
        source,
        Extraction(
            claims=(draft(late),),
            relations=(relation(newer.claim, "corrects", "correction"),),
            supports=(SupportDraft(slot="capacity", evidence_ref=late.ref, relation="reports"),),
        ),
        None,
        adopted_at_ms=STAMP + 10 * HOUR,
    )
    assert result is not None
    assert [(change.kind, change.previous_ref) for change in result.changes] == [("possible_new", newer.claim.ref)]
    assert public_updates(result, semantic_completed_at_ms=STAMP) == ()


def test_a_conflict_annotates_a_new_claim_instead_of_swallowing_its_catalyst() -> None:
    head = adopt(evidence("Issuer announces 25 MW.", 1), 1, "25")
    other = _external(head, "cl:other-report", first_available_at_ms=STAMP + 1)
    denial = evidence("Operator says capacity is only 10 MW.", 2)
    source = FrozenInput(event_id="denial", revision=1, lineage_id="denial", evidence=(denial,), prior=(other,))
    result = assemble_update(
        source,
        Extraction(
            claims=(draft(denial, "10"),),
            relations=(relation(other.claim, "conflicts", "conflict"),),
            supports=(SupportDraft(slot="capacity", evidence_ref=denial.ref, relation="reports"),),
        ),
        None,
        adopted_at_ms=STAMP + 50,
    )
    assert result is not None
    assert sorted(change.kind for change in result.changes) == ["conflict", "new_fact"]
    assert [row.kind for row in public_updates(result, semantic_completed_at_ms=STAMP)] == [
        "catalyst_delta",
        "source_update",
    ]


def test_an_established_conflict_is_not_published_again_by_a_later_restatement() -> None:
    from tracefold.news.updates.contracts import EstablishedRelation

    head = adopt(evidence("A wire says a drone was captured.", 1), 1, "25")
    external = _external(head, "cl:external", first_available_at_ms=STAMP + 1)
    later = evidence("Another wire repeats the capture.", 2)
    extraction = Extraction(
        claims=(draft(later),),
        relations=(relation(head.claims[0], "equivalent"), relation(external.claim, "conflicts", "conflict")),
        supports=(SupportDraft(slot="capacity", evidence_ref=later.ref, relation="reports"),),
    )
    first = assemble_update(
        _with_prior(frozen((later,), 2, head), external), extraction, head, adopted_at_ms=STAMP + 20
    )
    assert first is not None and any(change.kind == "conflict" for change in first.changes)
    again = evidence("A third wire repeats the capture.", 3)
    established = EstablishedRelation(
        current_ref=head.claims[0].ref, previous_ref=external.claim.ref, relation="conflicts"
    )
    source = _with_prior(frozen((again,), 3, first), external).model_copy(
        update={"established_relations": (established,)}
    )
    repeated = assemble_update(
        source,
        extraction.model_copy(
            update={
                "claims": (draft(again),),
                "supports": (SupportDraft(slot="capacity", evidence_ref=again.ref, relation="reports"),),
            }
        ),
        first,
        adopted_at_ms=STAMP + 30,
    )
    assert repeated is not None
    assert {change.kind for change in repeated.changes} == {"evidence_change"}


def test_a_to_b_to_a_compares_only_the_current_claim_and_keeps_a_distinct_occurrence() -> None:
    first, head = progressed()
    # The superseded 25 MW claim is no comparison candidate any more (#742 S5): only 50 MW is current.
    current = tuple(
        PriorClaim(event_id=head.event_id, content_revision=head.content_revision, claim=claim)
        for claim in head.current_claims
    )
    assert [row.claim.ref for row in current] == [head.claims[-1].ref]
    back = evidence("Issuer cuts capacity back to 25 MW.", 3)
    source = frozen((back,), 3, head).model_copy(update={"prior": current})
    updated = assemble_update(
        source,
        Extraction(
            claims=(draft(back, "25"),),
            relations=(relation(head.claims[-1], "real_world_change", "parameter_change"),),
            supports=(SupportDraft(slot="capacity", evidence_ref=back.ref, relation="reports"),),
        ),
        head,
        adopted_at_ms=STAMP + 300,
    )
    assert updated is not None
    assert updated.claims[-1].ref not in {first.claims[0].ref, head.claims[-1].ref}
    assert updated.superseded_claim_refs == tuple(sorted({first.claims[0].ref, head.claims[-1].ref}))
    assert {change.kind for change in updated.changes} == {"parameter_change"}


def test_an_unestablished_phase_is_not_a_phase_change() -> None:
    from tracefold.news.updates.assembly import relation_change

    report = evidence("Issuer announces 25 MW.", 1)
    head = adopt(report, 1, "25")
    prior = head.claims[0].model_copy(update={"fields": head.claims[0].fields.model_copy(update={"phase": None})})
    current = draft(evidence("Issuer changes the site.", 2)).model_copy(
        update={"fields": draft(report).fields.model_copy(update={"phase": "unknown"})}
    )
    assert relation_change(current, prior, "real_world_change") == "scope_change"


# ---------------------------------------------------------------- #742 W3: the Nvidia buyback pushes

NVIDIA = Path(__file__).resolve().parents[1] / "fixtures" / "news" / "semantic_nvidia_buyback_2026-09-28.jsonl"


class RecordedAnswers:
    """The production pairwise answer for every pair; supports are `reports`."""

    identity = "nvidia-buyback-fixture"

    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row
        self.pairs: list[str] = []

    async def judge(self, task: Task, items: tuple[Question, ...], *, context_json: str | None) -> BatchResult:
        answers = []
        for item in items:
            payload = json.loads(item.payload_json)
            if task == "relation":
                self.pairs.append(payload["previous"]["ref"])
                value = self.row["production_relations"][payload["current"]["slot"]][payload["previous"]["ref"]]
            else:
                value = "reports"
            answers.append(Answer(item_id=item.item_id, value=value, backend=self.identity))
        return BatchResult(answers=tuple(answers))


@pytest.mark.parametrize("push", [3, 4, 5])
def test_nvidia_buyback_pushes_still_link_to_the_already_pushed_claims(push: int) -> None:
    # 2026-09-28: seven Events carried the same $150B buyback and pushed seven cards. Reader novelty (PR-2)
    # reads the links from each later push to the claims already pushed; they must still form.
    row = next(json.loads(line) for line in NVIDIA.open() if json.loads(line)["push"] == push)
    source = FrozenInput(
        event_id=row["event_id"],
        revision=row["input_revision"],
        lineage_id=f"nvidia-{push}",
        evidence=tuple(Evidence.model_validate(item) for item in row["evidence"]),
        prior=tuple(PriorClaim.model_validate(prior) for prior in row["prior"]),
    )
    head = None if row["head"] is None else EventUpdate.model_validate(row["head"])
    extracted = Extraction.model_validate({"claims": row["claims"], "supports": row["supports"]})
    backend = RecordedAnswers(row)
    analyzer = SemanticAnalyzer(UnusedExtractor(), NewsJudgments(generated=backend, cache=MemoryCache()))
    understood = asyncio.run(analyzer.understand(source, extracted, Budget.start(5)))
    update = assemble_update(source, understood, head, adopted_at_ms=row["completed_at_ms"] + 1)
    assert update is not None
    linked = {change.previous_ref for change in update.changes if change.relation in {"adds_information", "equivalent"}}
    assert set(row["required_links"]) <= linked
    # Every supplied current prior is judged; no model outside the relation judge settles a pair.
    assert sorted(backend.pairs) == sorted(prior["claim"]["ref"] for prior in row["prior"] for _ in row["claims"])
