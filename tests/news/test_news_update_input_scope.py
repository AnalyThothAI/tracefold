"""Existing FactUnit boundaries and bounded prior recall reach the actual model input."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest

from tests.support.news_update_semantic import draft, generated, material
from tracefold.news.adapters.extraction import DspyExtractor
from tracefold.news.events.facts import extract_fact_units
from tracefold.news.events.gate import grounded_assets
from tracefold.news.storage.errors import EventUpdateConflict
from tracefold.news.storage.evidence import EvidenceStorage
from tracefold.news.storage.semantic_input import SemanticInputStorage, frozen_input
from tracefold.news.updates.assembly import assemble_update
from tracefold.news.updates.contracts import Citation, Extraction, FrozenInput, SourceAssetCandidate
from tracefold.news.updates.extraction import validate_extraction
from tracefold.news.updates.identity import canonical_json
from tracefold.news.updates.judgment import ContractFault
from tracefold.news.updates.projection import extraction_input, reading_view, reading_views

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


def test_provider_candidates_reach_model_by_source_without_grade_or_literal_ticker_admission(monkeypatch) -> None:
    data = input_material()
    data["members"][0]["provider_metadata"] = {
        "coins": [
            {"symbol": "xyz-NEARUSDT", "market_type": "crypto", "grade": "1"},
            {"symbol": "BETA", "market_type": "equity", "grade": "2"},
            {"symbol": "EURUSD", "market_type": "forex", "grade": "3"},
            {"symbol": "FUND", "market_type": "fund"},
        ]
    }
    data["members"][1]["provider_metadata"] = {"coins": [{"symbol": "NEAR", "market_type": "crypto"}]}
    source = frozen_input("event", data)
    first, second = source.evidence
    assert [row.symbol for row in source.asset_candidates[first.ref]] == ["xyz-NEARUSDT", "BETA", "EURUSD", "FUND"]
    assert [row.market_type for row in source.asset_candidates[first.ref]] == ["crypto", "equity", "fx", "unknown"]
    assert source.asset_candidates[first.ref][0].grade == "1"
    assert [row.symbol for row in source.asset_candidates[second.ref]] == ["NEAR"]
    calls = generated(monkeypatch, {"claims": []})
    asyncio.run(DspyExtractor(lambda: None, model_identity="test", topics={}).extract(source))
    sent = json.loads(calls[0]["evidence_json"])
    assert list(sent["asset_candidates"]) == ["e1", "e2"]
    assert sent["asset_candidates"]["e1"][0] == {"symbol": "xyz-NEARUSDT", "market_type": "crypto", "grade": "1"}
    assert "Beta announces" not in json.dumps(sent["evidence"][0]["segments"])


def test_candidate_changes_bind_only_current_pending_sources_and_keep_empty_read_identity() -> None:
    data = input_material()
    empty = frozen_input("event", data)
    first, second = empty.evidence
    empty_views = reading_views(empty)
    assert empty_views[0].read_ref == reading_view("event", first, empty.extraction_scopes, ()).read_ref
    data["members"][0]["provider_metadata"] = {"coins": [{"symbol": "NEAR", "market_type": "crypto", "grade": "1"}]}
    tagged = frozen_input("event", data)
    assert tagged.input_sha != empty.input_sha
    assert reading_views(tagged)[0].read_ref != empty_views[0].read_ref
    assert reading_views(tagged)[1].read_ref == empty_views[1].read_ref
    data["work"] = {"wanted_revision": 2, "lineage_id": "lineage", "processed_read_refs": [empty_views[1].read_ref]}
    pending = frozen_input("event", data)
    assert pending.evidence == (first,) and set(pending.asset_candidates) == {first.ref}
    # Tags on a source already settled for this task are absent from this round's input identity.
    data["members"][1]["provider_metadata"] = {"coins": []}
    assert frozen_input("event", data).input_sha == pending.input_sha
    data["members"][0]["provider_metadata"]["coins"][0]["grade"] = "3"
    changed = frozen_input("event", data)
    assert changed.input_sha != pending.input_sha
    assert reading_views(changed)[0].read_ref != reading_views(pending)[0].read_ref
    assert second.ref not in changed.asset_candidates


def test_source_candidate_keys_must_name_current_evidence() -> None:
    source = frozen_input("event", input_material())
    with pytest.raises(ValueError, match="news_asset_candidate_evidence_missing"):
        FrozenInput.model_validate(
            {**source.model_dump(), "asset_candidates": {"other": (SourceAssetCandidate(symbol="BTC"),)}}
        )


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
    assert "Beta announces" not in json.dumps(sent["evidence"][0]["segments"])
    assert sent["focus_claim_refs"] == []
    data["members"][0] = {"item_id": "digest", "fact_id": "beta", "fact_text": "Beta announces earnings."}
    data["fact_scopes"]["beta"] = {"method": "explicit_numbered", "context": "Results"}
    changed = frozen_input("near-event", data)
    assert changed.evidence == source.evidence and changed.input_sha != source.input_sha
    assert [s.fact_id for s in changed.extraction_scopes] == ["beta"]


def test_exact_member_recovers_its_own_numbered_scope_without_a_focus_snapshot() -> None:
    body = BODY + "\n（以上内容仅供参考，不构成投资建议）"
    leader = extract_fact_units(item_id="digest", raw_text=body, fallback_title="Digest")[0]
    exact = extract_fact_units(item_id="copy", raw_text=body, fallback_title="Digest")[0]
    assert leader.fact_id != exact.fact_id and leader.text == exact.text
    data = input_material()
    data["item_ids"] = ["digest", "copy"]
    data["items"] = [
        {**data["items"][0], "title": "Digest", "evidence_text": body},
        {**data["items"][0], "item_id": "copy", "source_item_key": "copy", "title": "Digest", "evidence_text": body},
    ]
    data["members"] = [
        {"item_id": "digest", "fact_id": leader.fact_id, "fact_text": leader.text},
        {"item_id": "copy", "fact_id": exact.fact_id, "fact_text": exact.text},
    ]
    # Only the leader is a focus FactUnit in the immutable snapshot. The exact
    # member must recover its own identity from its stored original Item.
    data["fact_scopes"] = {leader.fact_id: leader.as_dict()}
    source = frozen_input("near-event", data)
    assert len(source.evidence) == len(source.extraction_scopes) == 2
    assert {scope.fact_id for scope in source.extraction_scopes} == {leader.fact_id, exact.fact_id}
    for item, view in zip(source.evidence, reading_views(source), strict=True):
        assert item.text == body
        assert view.mode == "scoped"
        shown = " ".join(span.text for span in view.spans)
        assert "Exchange suspends $NEAR withdrawals" in shown
        assert "不构成投资建议" in shown
        assert "Beta announces earnings" not in shown
        sibling = draft(item).model_copy(
            update={"citations": (Citation(evidence_ref=item.ref, quote="Beta announces earnings."),)}
        )
        with pytest.raises(ContractFault, match="news_citation_not_in_visible_source"):
            validate_extraction(source, Extraction(claims=(sibling,)))


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
    view = next(view for view in reading_views(source) if view.evidence_ref == changed.ref)
    assert view.mode == "whole" and view.reason == "task_anchor_not_unique_in_source_version"


def test_numbered_continuations_and_shared_footer_keep_fact_identity_and_exact_raw_offsets() -> None:
    base = "1. Alpha approves a plan.\n2. Beta reports results.\n3. Gamma opens a plant."
    revised = (
        "1. Alpha approves a plan.\nOnly after regulator approval.\n"
        "2. Beta reports results.\n3. Gamma opens a plant.\n"
        "（以上内容仅供参考，不构成投资建议）"
    )
    original = extract_fact_units(item_id="digest", raw_text=base, fallback_title="Digest")
    units = extract_fact_units(item_id="digest", raw_text=revised, fallback_title="Digest")
    assert [unit.fact_id for unit in units] == [unit.fact_id for unit in original]
    assert "Only after regulator approval" in units[0].context
    assert all("以上内容仅供参考" in unit.context for unit in units)
    assert all(revised[unit.span_start : unit.span_end].startswith(f"{index}. ") for index, unit in enumerate(units, 1))
    html_body = (
        "Lead &amp; context<br/>1. Alpha approves a plan.<br/>2. Beta reports results.<br/>3. Gamma opens a plant."
    )
    html_units = extract_fact_units(item_id="digest", raw_text=html_body, fallback_title="Digest")
    assert html_body[html_units[0].span_start : html_units[0].span_end].startswith("1. Alpha")


def test_fourteen_numbered_event_tasks_keep_the_last_fact_without_a_quota() -> None:
    body = (
        "\n".join(f"{index}. Company reports routine development number {index}." for index in range(1, 14))
        + "\n14. Regulator approves the final safety condition."
    )
    units = extract_fact_units(item_id="digest", raw_text=body, fallback_title="Digest")
    assert len(units) == 14
    data = input_material()
    data["item_ids"] = ["digest"]
    data["items"] = [{**data["items"][0], "evidence_text": body}]
    data["members"] = [{"item_id": "digest", "fact_id": units[-1].fact_id, "fact_text": units[-1].text}]
    data["fact_scopes"] = {units[-1].fact_id: {"method": "explicit_numbered"}}
    source = frozen_input("last-event", data)
    shown = extraction_input(source)["evidence"][0]["segments"]
    assert "Regulator approves the final safety condition" in str(shown)
    assert "routine development number 13" not in str(shown)
    assert source.evidence[0].text == body


def test_last_numbered_fact_keeps_its_following_continuation_in_one_citable_span() -> None:
    body = (
        "1. Alpha approves a plan.\n2. Beta reports results.\n"
        "3. If your assets were taken\nIndependent whitehats rescued thousands of NFTs."
    )
    unit = extract_fact_units(item_id="digest", raw_text=body, fallback_title="Digest")[-1]
    data = input_material()
    data["item_ids"] = ["digest"]
    data["items"] = [{**data["items"][0], "evidence_text": body}]
    data["members"] = [{"item_id": "digest", "fact_id": unit.fact_id, "fact_text": unit.text}]
    data["fact_scopes"] = {unit.fact_id: unit.as_dict()}
    source = frozen_input("last-event", data)
    view = reading_views(source)[0]
    quote = "If your assets were taken\nIndependent whitehats rescued thousands of NFTs."
    assert view.mode == "scoped"
    assert any(quote in span.text and span.role == "task" for span in view.spans)
    assert "Beta reports results" not in str(view.spans)
    claim = draft(source.evidence[0]).model_copy(
        update={"citations": (Citation(evidence_ref=source.evidence[0].ref, quote=quote),)}
    )
    validate_extraction(source, Extraction(claims=(claim,)))


def test_fourteen_tasks_use_bounded_scope_material_and_keep_independent_extract_calls(monkeypatch) -> None:
    body = "\n".join(
        f"{index}. Company reports development number {index} with a distinct condition." for index in range(1, 15)
    )
    units = extract_fact_units(item_id="digest", raw_text=body, fallback_title="Digest")
    assert len(units) == 14
    calls = generated(monkeypatch, {"claims": []})
    extractor = DspyExtractor(lambda: None, model_identity="test", topics={})
    raw_chars = 0
    projected_chars = 0
    for unit in units:
        data = input_material()
        data["item_ids"] = ["digest"]
        data["items"] = [{**data["items"][0], "evidence_text": body}]
        data["members"] = [{"item_id": "digest", "fact_id": unit.fact_id, "fact_text": unit.text}]
        data["fact_scopes"] = {unit.fact_id: {"method": unit.method}}
        source = frozen_input(f"event-{unit.ordinal}", data)
        raw_chars += len(canonical_json(source.model_dump(mode="json")))
        projected_chars += len(canonical_json(extraction_input(source)))
        asyncio.run(extractor.extract(source))
    assert projected_chars < raw_chars
    assert len(calls) == 14
    assert "number 14" in calls[-1]["evidence_json"]
    assert "number 13" not in calls[-1]["evidence_json"]


def test_visible_citation_rejects_sibling_and_segment_join_while_preserving_full_source() -> None:
    data = input_material()
    source = frozen_input("event", data)
    assert source.evidence[0].text == BODY
    assert "Beta announces" not in json.dumps(extraction_input(source)["evidence"][0]["segments"])
    sibling = draft(source.evidence[0]).model_copy(
        update={"citations": (Citation(evidence_ref=source.evidence[0].ref, quote="Beta announces earnings."),)}
    )
    with pytest.raises(ContractFault, match="news_citation_not_in_visible_source"):
        validate_extraction(source, Extraction(claims=(sibling,)))
    lead = "Exchange bulletin\n" + BODY
    data["items"][0]["evidence_text"] = lead
    crossing = frozen_input("event", data)
    claim = draft(crossing.evidence[0]).model_copy(
        update={"citations": (Citation(evidence_ref=crossing.evidence[0].ref, quote="Exchange bulletin\n1. Exchange"),)}
    )
    with pytest.raises(ContractFault, match="news_citation_not_in_visible_source"):
        validate_extraction(crossing, Extraction(claims=(claim,)))


def test_read_identity_includes_scope_even_when_source_ref_is_already_done() -> None:
    data = input_material()
    data["work"] = {"wanted_revision": 1, "lineage_id": "lineage", "processed_read_refs": []}
    first = frozen_input("event", data)
    completed = [view.read_ref for view in reading_views(first)]
    data["work"] = {"wanted_revision": 2, "lineage_id": "lineage-2", "processed_read_refs": completed}
    assert frozen_input("event", data).evidence == ()
    data["members"].append({"item_id": "digest", "fact_id": "beta", "fact_text": "Beta announces earnings."})
    data["fact_scopes"]["beta"] = {"method": "explicit_numbered", "context": "Results"}
    expanded = frozen_input("event", data)
    assert [item.ref for item in expanded.evidence] == [first.evidence[0].ref]
    data["work"]["processed_read_refs"].append(reading_views(expanded)[0].read_ref)
    assert frozen_input("event", data).evidence == ()


def test_targeted_reanalysis_reads_only_the_requested_scope_after_the_migration() -> None:
    data = input_material()
    data["work"] = {"wanted_revision": 2, "lineage_id": "repair", "processed_read_refs": []}
    all_sources = frozen_input("event", data)
    target = reading_views(all_sources)[0].read_ref
    data["work"]["reanalysis_read_ref"] = target
    selected = frozen_input("event", data)
    assert [item.ref for item in selected.evidence] == [all_sources.evidence[0].ref]
    data["work"]["reanalysis_read_ref"] = "read:missing"
    with pytest.raises(EventUpdateConflict, match="news_reanalysis_read_scope_changed"):
        frozen_input("event", data)


def test_same_source_body_reappearing_after_an_intervening_revision_gets_a_new_task_read() -> None:
    source = frozen_input("event", input_material())
    first = source.evidence[0]
    returned = first.model_copy(
        update={"source": first.source.model_copy(update={"revision_sequence": first.source.revision_sequence + 2})}
    )
    assert returned.ref == first.ref and returned.text == first.text
    original_view = reading_views(source)[0]
    returned_view = reading_views(source.model_copy(update={"evidence": (returned,)}))[0]
    assert original_view.read_ref != returned_view.read_ref


@pytest.mark.parametrize("strong_only", [False, True])
def test_explicit_cashtag_survives_collision_but_plain_word_does_not(strong_only: bool) -> None:
    coins = [{"symbol": "NEAR", "grade": "A"}]
    assert grounded_assets("Withdrawals for $NEAR paused", coins, strong_only=strong_only) == ("NEAR",)
    assert grounded_assets("near-instant withdrawals", coins, strong_only=strong_only) == ()
    assert grounded_assets("$NEARLY rises", coins, strong_only=strong_only) == ()
    assert grounded_assets("$NEAR rises", [], strong_only=strong_only) == ()


@pytest.mark.parametrize("case", ["new_member", "numbered_task", "changed_body", "whole_item"])
def test_semantic_prior_query_uses_actual_pending_task_scope(case: str) -> None:
    data = input_material()
    # The already read leader had these same tags; newly attaching a candidate would correctly
    # make its unchanged body a new reading input rather than leave it settled.
    data["card"] = {
        "leader_item_id": "digest",
        "provider_metadata": {"coins": [{"symbol": "OLD", "grade": "A", "market_type": "equity"}]},
    }
    completed = [view.read_ref for view in reading_views(frozen_input("event", data))]
    data["work"] = {"wanted_revision": 2, "lineage_id": "l2", "processed_read_refs": []}
    if case == "new_member":
        data["work"]["processed_read_refs"] = completed[:1]
        data["items"][1]["evidence_text"] = "New issuer launches a $SI product."
    elif case == "whole_item":
        data["work"]["processed_read_refs"] = completed[:1]
        data["items"][1]["evidence_text"] = "Issuer launches a product."
        data["members"][1]["provider_metadata"] = {
            "coins": [
                {"symbol": "MYSTERY", "grade": "A"},
                {"symbol": "KNOWN", "grade": "A", "market_type": "equity"},
            ]
        }
    elif case == "changed_body":
        data["work"]["processed_read_refs"] = completed
        data["members"][0]["evidence_revisions"] = ["changed"]
        data["revisions"] = [
            {
                "item_id": "digest",
                "revision_sha256": "changed",
                "revision_sequence": 1,
                "evidence_text": BODY.replace("\n2.", "\nOnly after network recovery.\n2."),
                "published_at_ms": 150,
                "received_at_ms": 151,
            }
        ]
    if case in {"numbered_task", "changed_body"}:
        data["members"][0]["provider_metadata"] = {
            "coins": [
                {"symbol": "NEAR", "grade": "A", "market_type": "crypto"},
                {"symbol": "BETA", "grade": "A", "market_type": "equity"},
                {"symbol": "GAMMA", "grade": "A", "market_type": "equity"},
            ]
        }

    class Rows:
        def __init__(self, rows=(), one=None):
            self.rows, self.one = rows, one

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.one

    class Connection:
        def execute(self, sql, params=None):
            if "FROM news_semantic_work" in sql:
                return Rows(one=data["work"])
            if "FROM news_event_evidence_snapshots s" in sql:
                return Rows(
                    one={
                        "evidence_version": 2,
                        "fact_scopes": data["fact_scopes"],
                        "snapshot": {
                            "card": {
                                "leader_item_id": "digest",
                                "leader_title": "Old unrelated leader acquisition",
                                "asset_class": "crypto",
                                "grounded_assets": ["OLD"],
                                "provider_metadata": {
                                    "coins": [{"symbol": "OLD", "grade": "A", "market_type": "equity"}]
                                },
                            },
                            "members": data["members"],
                        },
                    }
                )
            if "FROM news_items" in sql:
                return Rows(rows=data["items"])
            if "FROM news_item_revisions" in sql:
                return Rows(rows=data.get("revisions", ()))
            if "FROM news_event_updates" in sql:
                return Rows()
            raise AssertionError(sql)

    class Candidates:
        def evidence_candidates(self, query):
            self.query = query
            return []

    candidates = Candidates()
    store = SemanticInputStorage(
        Connection(),
        evidence=cast(EvidenceStorage, candidates),
        head_document=lambda event_id: None,
    )
    result = store.semantic_input_material("event", now_ms=200)
    query = candidates.query
    shown = " ".join(query.texts)
    assert "Old unrelated leader acquisition" not in shown
    assert "Beta announces earnings" not in shown and "Gamma launches a product" not in shown
    assert "OLD" not in {asset.symbol for asset in query.assets}
    assert not {"BETA", "GAMMA"} & {asset.symbol for asset in query.assets}
    if case == "new_member":
        assert shown == "New issuer launches a $SI product."
        assert query.source_artifact_ids == ("followup",)
        assert [(asset.symbol, asset.market_type) for asset in query.assets] == [("SI", "unknown")]
    elif case == "whole_item":
        # Provider-resolved whole-item tags remain valid retrieval features without a literal ticker;
        # the Event's aggregate crypto class cannot invent the individual asset types.
        assert {(asset.symbol, asset.market_type) for asset in query.assets} == {
            ("MYSTERY", "unknown"),
            ("KNOWN", "equity"),
        }
    elif case == "changed_body":
        assert "Only after network recovery" in shown
        assert query.source_artifact_ids == ("digest",)
    else:
        assert "Exchange suspends $NEAR withdrawals" in shown
    assert result["related_heads"] == []


def test_a_mentioned_cashtag_does_not_become_the_actor_identity_or_veto_equivalence() -> None:
    from tracefold.news.updates.contracts import DraftClaim, RelationDraft

    def source_data(symbol: str, head=None):
        text = f"Exchange resumes withdrawals. ${symbol} remains volatile."
        return {
            "item_ids": ["item"],
            "items": [
                {
                    "item_id": "item",
                    "source_id": "wire",
                    "source_item_key": "item",
                    "observed_at_ms": 100,
                    "evidence_text": text,
                }
            ],
            "grounded_assets": [symbol],
            "head": None if head is None else head.model_dump(mode="json"),
        }

    def extraction(source, symbol: str, relations=()):
        evidence = source.evidence[0]
        claim = DraftClaim.model_validate(
            {
                "slot": "a",
                "statement": "Exchange resumes withdrawals.",
                "fields": {
                    "subject": "Exchange",
                    "action": "resumes",
                    "object": "withdrawals",
                    "mode": "observation",
                    "assets": [{"symbol": symbol, "market_type": "crypto", "role": "mentioned"}],
                },
                "citations": [{"evidence_ref": evidence.ref, "quote": evidence.text}],
            }
        )
        return Extraction(claims=(claim,), relations=relations)

    first = frozen_input("event", source_data("BTC"))
    head = assemble_update(first, extraction(first, "BTC"), None, adopted_at_ms=100)
    assert head is not None and head.claims[0].known_identity == ()
    second = frozen_input("event", source_data("ETH", head))
    assert second.identity_hints == ()
    updated = assemble_update(
        second,
        extraction(second, "ETH", (RelationDraft(slot="a", previous_ref=head.claims[0].ref, relation="equivalent"),)),
        head,
        adopted_at_ms=200,
    )
    assert updated is not None and updated.claims[0].ref == head.claims[0].ref


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
    from tests.support.news_update_semantic import MemoryCache, TaskBackend
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
    from tracefold.news.storage.semantic_input import _related_prior

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


# ---------------------------------------------------------------- #742 U3 / W6 / S3 / S5


def test_a_revised_headline_is_the_same_whole_item_fact() -> None:
    # U3: the headline "25 bps" revised to "50 bps" forked a second Event keyed on the new title.
    before = extract_fact_units(item_id="record", raw_text="Fed cuts 25 bps", fallback_title="Fed cuts 25 bps")
    after = extract_fact_units(item_id="record", raw_text="Fed cuts 50 bps", fallback_title="Fed cuts 50 bps")
    other = extract_fact_units(item_id="other", raw_text="Fed cuts 25 bps", fallback_title="Fed cuts 25 bps")
    assert before[0].fact_id == after[0].fact_id != other[0].fact_id
    assert after[0].text == "Fed cuts 50 bps"


def test_input_identity_ignores_related_claims_but_not_the_events_own() -> None:
    data = input_material()
    base = frozen_input("event", data)
    related = make_head("related", ["Exchange suspends NEAR withdrawals again."])
    data["related_heads"] = [related.model_dump(mode="json")]
    with_related = frozen_input("event", data)
    assert with_related.prior and with_related.input_sha == base.input_sha
    # Related claims are comparison candidates only; extraction never reads them.
    assert extraction_input(with_related)["prior"] == []
    own = make_head("event", ["Exchange suspends $NEAR withdrawals."])
    data["head"] = own.model_dump(mode="json")
    assert frozen_input("event", data).input_sha != base.input_sha


def test_only_current_own_claims_are_compared_and_quarantined_reads_are_not_resent() -> None:
    from tracefold.news.updates.contracts import PriorClaim, RelationDraft

    data = input_material()
    own = make_head("event", ["Old reading."])
    correction = material("Correction: the new reading.", revision=5)
    fixed = draft(correction).model_copy(
        update={"fields": draft(correction).fields.model_copy(update={"subject": "Correction"})}
    )
    retired = assemble_update(
        FrozenInput(
            event_id="event",
            revision=2,
            lineage_id="event-2",
            evidence=(correction,),
            prior=tuple(
                PriorClaim(event_id="event", content_revision=own.content_revision, claim=c) for c in own.claims
            ),
        ),
        Extraction(
            claims=(fixed,),
            relations=(
                RelationDraft(slot="a", previous_ref=own.claims[0].ref, relation="corrects", change_kind="correction"),
            ),
        ),
        own,
        adopted_at_ms=300,
    )
    assert retired is not None and retired.retired_claim_refs == (own.claims[0].ref,)
    data["head"] = retired.model_dump(mode="json")
    source = frozen_input("event", data)
    assert [row.claim.ref for row in source.own_prior] == [retired.claims[-1].ref]
    failed = [view.read_ref for view in reading_views(source)][:1]
    data["work"] = {"wanted_revision": 2, "lineage_id": "l2", "processed_read_refs": [], "failed_read_refs": failed}
    assert [item.ref for item in frozen_input("event", data).evidence] == [source.evidence[1].ref]


def test_a_strategy_resend_changes_the_snapshot_but_is_no_semantic_material() -> None:
    from tracefold.news.storage.events import prepare_evidence_snapshot

    card = {"event_id": "event", "leader_item_id": "item", "grounded_assets": ["BTC"], "provider_metadata": {}}
    member = {
        "item_id": "item",
        "fact_id": "fact",
        "fact_text": "Exchange suspends withdrawals.",
        "joined_at_ms": 1,
        "match_kind": "leader",
        "jaccard_estimate": None,
        "reporting_origin": "wire",
        "canonical_url": None,
        "provider_metadata": {"strategies": [{"id": "1018"}]},
        "provenance": ["1018"],
    }
    first = prepare_evidence_snapshot(
        {"card": card, "members": [member], "latest": None}, event_id="event", now_ms=1, focus_fact=None
    )
    assert first["semantic_changed"]
    latest = {
        "evidence_version": 1,
        "evidence_sha256": first["evidence_sha256"],
        "snapshot": json.loads(first["snapshot_json"]),
    }
    resent = {
        **member,
        "provider_metadata": {"strategies": [{"id": "1018"}, {"id": "2000"}]},
        "provenance": ["1018", "2000"],
    }
    again = prepare_evidence_snapshot(
        {"card": card, "members": [resent], "latest": latest}, event_id="event", now_ms=2, focus_fact=None
    )
    assert again["evidence_sha256"] != first["evidence_sha256"] and not again["semantic_changed"]
    revised = {**member, "evidence_revisions": ["rev-1"]}
    body = prepare_evidence_snapshot(
        {"card": card, "members": [revised], "latest": latest}, event_id="event", now_ms=3, focus_fact=None
    )
    assert body["semantic_changed"]
