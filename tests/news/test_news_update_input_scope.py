"""Existing FactUnit boundaries and bounded prior recall reach the actual model input."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from tests.news.test_news_event_updates_core import draft, material
from tests.news.test_news_update_generation import generated
from tracefold.news.events.gate import grounded_assets
from tracefold.news.storage.event_updates import EventUpdateStorage, frozen_input
from tracefold.news.updates.contracts import Extraction, FrozenInput
from tracefold.news.updates.dspy_backend import DspyExtractor
from tracefold.news.updates.semantics import assemble_update

BODY = "1. Exchange suspends $NEAR withdrawals.\n2. Beta announces earnings.\n3. Gamma launches a product."


def input_material() -> dict[str, Any]:
    return {
        "item_ids": ["digest", "followup"],
        "items": [
            {
                "item_id": item,
                "source_id": "wire",
                "source_item_key": item,
                "observed_at_ms": 100,
                "evidence_text": text,
            }
            for item, text in [("digest", BODY), ("followup", "Only the NEAR network is affected.")]
        ],
        "members": [
            {"item_id": "digest", "fact_id": "near", "fact_text": "Exchange suspends $NEAR withdrawals."},
            {"item_id": "followup", "fact_id": "network", "fact_text": "Only the NEAR network is affected."},
        ],
        "fact_scopes": {
            "near": {"method": "explicit_numbered", "context": "Exchange bulletin"},
            "network": {"method": "whole_item", "context": ""},
        },
    }


def test_member_scopes_preserve_whole_sources_and_do_not_scope_other_members(monkeypatch) -> None:
    data = input_material()
    source = frozen_input("near-event", data)
    assert [s.fact_id for s in source.extraction_scopes] == ["near"]
    assert source.extraction_scopes[0].evidence_ref == source.evidence[0].ref
    assert source.evidence[0].text == BODY
    assert source.evidence[1].text == "Only the NEAR network is affected."
    calls = generated(monkeypatch, {"claims": []})
    asyncio.run(DspyExtractor(lambda: None, model_identity="test", topics={}).extract(source))
    sent = json.loads(calls[0]["evidence_json"])
    assert sent["extraction_scopes"][0]["evidence_ref"] == sent["evidence"][0]["ref"] == "e1"
    assert sent["extraction_scopes"][0]["context"] == "Exchange bulletin"
    assert sent["focus_claim_refs"] == []
    data["members"][0] = {"item_id": "digest", "fact_id": "beta", "fact_text": "Beta announces earnings."}
    data["fact_scopes"]["beta"] = {"method": "explicit_numbered", "context": "Results"}
    changed = frozen_input("near-event", data)
    assert changed.evidence == source.evidence and changed.input_sha != source.input_sha
    assert [s.fact_id for s in changed.extraction_scopes] == ["beta"]


def test_whole_item_keeps_whole_item_extraction_and_multiple_scopes_are_unioned() -> None:
    data = input_material()
    data["fact_scopes"]["near"]["method"] = "whole_item"
    assert frozen_input("event", data).extraction_scopes == ()
    data["fact_scopes"]["near"]["method"] = "explicit_numbered"
    data["members"].append({"item_id": "digest", "fact_id": "beta", "fact_text": "Beta announces earnings."})
    data["fact_scopes"]["beta"] = {"method": "explicit_numbered"}
    assert [s.fact_id for s in frozen_input("event", data).extraction_scopes] == ["near", "beta"]


def test_revision_retains_scope_but_never_slices_new_body_using_old_offsets() -> None:
    data = input_material()
    revised = "Correction: withdrawals remain available except on the NEAR network."
    data["revisions"] = [
        {
            "item_id": "digest",
            "revision_sha256": "new",
            "revision_sequence": 1,
            "evidence_text": revised,
            "published_at_ms": 110,
            "received_at_ms": 120,
        }
    ]
    data["fact_scopes"]["near"].update(span_start=9999, span_end=10000)
    source = frozen_input("event", data)
    changed = next(e for e in source.evidence if e.source.artifact_revision == "new")
    scope = next(s for s in source.extraction_scopes if s.evidence_ref == changed.ref)
    assert scope.fact_text == "Exchange suspends $NEAR withdrawals."
    assert changed.text == revised


@pytest.mark.parametrize("strong_only", [False, True])
def test_explicit_cashtag_survives_collision_but_plain_word_does_not(strong_only: bool) -> None:
    coins = [{"symbol": "NEAR", "grade": "A"}]
    assert grounded_assets("Withdrawals for $NEAR paused", coins, strong_only=strong_only) == ("NEAR",)
    assert grounded_assets("near-instant withdrawals", coins, strong_only=strong_only) == ()
    assert grounded_assets("$NEARLY rises", coins, strong_only=strong_only) == ()
    assert grounded_assets("$NEAR rises", [], strong_only=strong_only) == ()


def test_candidate_types_come_from_individual_assets_never_event_group() -> None:
    class Store(EventUpdateStorage):
        def evidence_candidates(self, query):
            self.query = query
            return []

    store = Store()
    card = {
        "asset_class": "crypto",
        "grounded_assets": ["MYSTERY", "KNOWN"],
        "provider_metadata": {"coins": [{"symbol": "KNOWN", "market_type": "equity"}]},
    }
    assert store._related_event_ids("event", card, {}, now_ms=100) == []
    assert [(a.symbol, a.market_type) for a in store.query.assets] == [("MYSTERY", "unknown"), ("KNOWN", "equity")]


def make_head(event: str, texts: list[str]):
    sources = tuple(material(t, revision=i + 1) for i, t in enumerate(texts))
    update = assemble_update(
        FrozenInput(event_id=event, revision=1, lineage_id=event, evidence=sources),
        Extraction(
            claims=tuple(
                draft(e).model_copy(
                    update={
                        "slot": str(i),
                        "statement": e.text,
                        "fields": draft(e).fields.model_copy(update={"subject": e.text}),
                    }
                )
                for i, e in enumerate(sources)
            )
        ),
        None,
        adopted_at_ms=200,
    )
    assert update is not None
    return update


def test_related_claims_rank_all_candidates_against_new_material_before_eight_limit() -> None:
    noise = make_head("first", [f"Company {i} announces quarterly earnings." for i in range(8)])
    relevant = make_head("last", ["Exchange suspends $NEAR withdrawals."])
    data = input_material()
    data["related_heads"] = [h.model_dump(mode="json") for h in (noise, relevant)]
    result = frozen_input("event", data)
    assert len(result.prior) == 8 and result.prior[0].claim.ref == relevant.claims[0].ref
    assert frozen_input("event", data).prior == result.prior
    own = make_head("event", [f"Local fact {i} about an earlier change." for i in range(10)])
    data["head"] = own.model_dump(mode="json")
    result = frozen_input("event", data)
    assert len([p for p in result.prior if p.event_id == "event"]) == 10
    assert len([p for p in result.prior if p.event_id != "event"]) == 8


def test_scope_metadata_cannot_supply_a_fact_citation(monkeypatch) -> None:
    from tests.news.test_news_event_update_judgments import MemoryCache
    from tests.news.test_news_event_update_notifications import TaskBackend
    from tracefold.news.updates.judgment import Budget, ContractFault, NewsJudgments
    from tracefold.news.updates.semantics import SemanticAnalyzer

    source = frozen_input("event", input_material())
    reply = draft(source.evidence[0]).model_dump(mode="json")
    reply["citations"] = [{"evidence_ref": "e1", "quote": "Exchange bulletin"}]
    generated(monkeypatch, {"claims": [reply]})
    analyzer = SemanticAnalyzer(
        DspyExtractor(lambda: None, model_identity="test", topics={}),
        NewsJudgments(generated=TaskBackend({}), cache=MemoryCache()),
        topics=(),
    )
    with pytest.raises(ContractFault, match="news_citation_not_in_frozen_source"):
        asyncio.run(analyzer.extract(source, Budget.start(5)))


def test_explicit_predecessor_survives_external_budget_and_own_claim_is_not_duplicated() -> None:
    from tracefold.news.storage.event_updates import _related_prior

    old = make_head("old", ["The original exchange statement."])
    noise = make_head("noise", [f"Exchange {i} changes NEAR network withdrawals." for i in range(8)])
    result = _related_prior(
        [noise.model_dump(mode="json"), old.model_dump(mode="json")],
        set(),
        evidence=(material("Exchange changes NEAR network withdrawals."),),
        preferred_refs={old.claims[0].ref},
    )
    assert len(result) == 8 and result[0].claim.ref == old.claims[0].ref
    result = _related_prior(
        [noise.model_dump(mode="json"), old.model_dump(mode="json")],
        {old.claims[0].ref},
        evidence=(),
        preferred_refs={old.claims[0].ref},
    )
    assert len(result) == 8 and old.claims[0].ref not in {p.claim.ref for p in result}
