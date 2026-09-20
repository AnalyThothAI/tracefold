from __future__ import annotations

import pytest

from tracefold.news.evidence import (
    CURRENT_CHARS,
    assemble_evidence,
    query_for,
    select_item,
    shortlist,
    text_sha,
)
from tracefold.news.models import MarketAsset
from tracefold.news.reader_history import assemble_reader_history
from tracefold.news.told_context import ToldLedgerSnapshot


def history_row(event_id: str, at_ms: int) -> dict:
    return dict(
        event_id=event_id,
        at_ms=at_ms,
        storyline_key="asset:equity:SEI",
        comparison_title="SEI announces acquisition pending approval",
        comparison_fingerprint=event_id,
        dedupe_family="general",
        grounded_assets=["SEI"],
        canonical_assets=["SEI"],
        assets=[{"symbol": "SEI", "market_type": "equity"}],
        magnitude=1,
        direction="neutral",
        headline_zh="收购计划",
        why_zh="仍待批准",
    )


def test_history_excludes_equal_cutoff_and_future_in_all_bands() -> None:
    rows = [history_row("before", 999), history_row("equal", 1000), history_row("future", 1001)]
    snapshot = assemble_reader_history(
        recent_rows=rows, similar_rows=rows, now_ms=1000, comparison_title=rows[0]["comparison_title"]
    )
    assert [row.event_id for row in snapshot.told_source_rows] == ["before"]


def test_unknown_provider_tag_cannot_override_known_market_conflict() -> None:
    row = history_row("equity", 999)
    row.update(history_scope="targeted", retrieval_reason="canonical_asset_overlap")
    selected = ToldLedgerSnapshot.select(
        [row], now_ms=1000, storyline_key="asset:crypto:SEI", symbols=(MarketAsset("SEI", "crypto"),)
    )
    assert selected.entries[0].tier == "recency"


def test_long_tail_conditions_have_exact_spans_and_explicit_omissions():
    text = "Acme acquisition announced. " + "Business background. " * 700 + "Not binding; approval pending."
    item = {"item_id": "one", "evidence_text": text, "provider_params_available_at_ms": 10}
    spans = select_item({}, item, kind="current", cutoff=11, budget=CURRENT_CHARS, prefix="c", reason="current_focus")
    assert sum(len(s.text) for s in spans) <= CURRENT_CHARS
    assert "approval pending" in " ".join(s.text for s in spans)
    assert all(text[s.span_start : s.span_end] == s.text and s.content_sha256 == text_sha(text) for s in spans)
    assert {s.coverage_status for s in spans} == {"selection_truncated"}


def test_numbered_focus_includes_shared_lead_without_sibling_amounts():
    text = "Today's announcements\n1. Acme acquires a plant for $10m.\n2. Other sells stock for $900m."
    item = {"evidence_text": text, "provider_params_available_at_ms": 10}
    card = {"leader_title": "Acme acquires a plant for $10m.", "focus_fact_method": "explicit_numbered"}
    spans = select_item(card, item, kind="current", cutoff=11, budget=1000, prefix="c", reason="current_focus")
    selected = " ".join(s.text for s in spans)
    assert "$10m" in selected and "Today's announcements" in selected
    assert "$900m" not in selected and "Other" not in selected


def test_origin_fact_duplicates_do_not_occupy_shortlist_slots():
    rows = [
        dict(
            event_id=str(i),
            item_id=str(i),
            priority=1,
            score=0.9,
            created_at_ms=10,
            source_artifact_id="shared",
            comparison_fingerprint="same",
        )
        for i in range(30)
    ]
    rows.append(
        dict(
            event_id="direct",
            item_id="direct",
            priority=1,
            score=0.8,
            created_at_ms=9,
            source_artifact_id="original",
            comparison_fingerprint="predecessor",
        )
    )
    for row in rows:
        row["leader_title"] = "Acme acquisition announced"
    query = query_for({"leader_title": "Acme acquisition approved"}, {}, cutoff=20)
    assert [r["event_id"] for r in shortlist(rows, query=query)] == ["0", "direct"]


def test_future_duplicate_does_not_hide_older_visible_receipt():
    snapshot = assemble_reader_history(recent_rows=[history_row("same", 1001), history_row("same", 999)], now_ms=1000)
    assert [row.at_ms for row in snapshot.told_source_rows] == [999]


def test_middle_condition_survives_tail_boilerplate_and_offsets():
    condition = "The agreement is non-binding and has not received regulatory approval. "
    text = "Acme acquisition announced. " + "Company background. " * 40 + condition + "Marketing boilerplate. " * 500
    card = {"leader_title": "Acme acquisition announced"}
    item = {"item_id": "leader", "evidence_text": text, "provider_params_available_at_ms": 10}
    spans = select_item(card, item, kind="current", cutoff=10, budget=600, prefix="c", reason="current_focus")
    assert condition.strip() in " ".join(s.text for s in spans)
    assert all(text[s.span_start : s.span_end] == s.text and s.content_sha256 == text_sha(text) for s in spans)
    assert spans == select_item(card, item, kind="current", cutoff=10, budget=600, prefix="c", reason="current_focus")


def test_frozen_members_deduplicate_bodies_not_urls_facts_or_sources():
    from tracefold.news.evidence import frozen_members, select_members

    card = {
        "leader_item_id": "leader",
        "leader_title": "Acme acquisition",
        "evidence_members": [
            {"item_id": name, "fact_id": name, "fact_text": "Acme acquisition", "joined_at_ms": i}
            for i, name in enumerate(["leader", "copy", "correction", "later", *map(str, range(20))])
        ],
    }
    members, excluded = frozen_members(card)
    assert len(members) == 16 and excluded == ("member_candidates_truncated",)
    metadata = [
        {"item_id": name, "evidence_text_sha256": digest, "provider_params_available_at_ms": stamp}
        for name, digest, stamp in [
            ("leader", "same", 1),
            ("copy", "same", 1),
            ("correction", "corrected", 1),
            ("later", "future", 3),
        ]
    ]
    chosen = select_members(members, metadata, cutoff=2)
    assert [r["item_id"] for r in chosen] == ["leader", "correction", "later"]


def test_members_share_global_budget_keep_incremental_conditions_and_conflicts():
    leader = "Acme acquisition announced. " + "Historical background. " * 500
    extra = "Acme acquisition announced. The agreement is non-binding and awaits approval. Amount is $2.5 million."
    rows = [
        {
            "item_id": str(i),
            "evidence_text": text,
            "provider_params_available_at_ms": 1,
            "canonical_url": "https://same.invalid/page",
            "fact_text": "Acme acquisition",
        }
        for i, text in enumerate(
            [
                leader,
                extra,
                "Acme says approval was received. " + "Further detail. " * 500,
                "Completion is conditional on financing by 30 June. " + "More context. " * 500,
            ]
        )
    ]
    card = {"leader_title": "Acme acquisition announced", "leader_item_id": "0"}
    prepared = assemble_evidence(
        card, rows[0], query=query_for(card, rows[0], cutoff=2), candidates=[], members=rows[1:]
    )
    text = " ".join(s.text for s in prepared.current_evidence)
    assert "non-binding" in text and "$2.5 million" in text and "approval was received" in text
    assert text.count("Acme acquisition announced.") == 1
    assert len(prepared.current_evidence) <= 12
    assert sum(len(s.text) for s in prepared.current_evidence) <= 6000
    assert len({s.ref_id for s in prepared.current_evidence}) == len(prepared.current_evidence)
    assert {s.source_item_id for s in prepared.current_evidence} == {"0", "1", "2", "3"}
    for span in prepared.current_evidence:
        source = rows[int(span.source_item_id)]["evidence_text"]
        assert source[span.span_start : span.span_end] == span.text


def test_member_late_body_and_numbered_siblings_use_frozen_fact_only():
    card = {"leader_title": "Acme acquisition", "leader_item_id": "one"}
    item = {"item_id": "one", "evidence_text": "Acme acquisition.", "provider_params_available_at_ms": 1}
    members = [
        {
            "item_id": "two",
            "fact_text": "Acme acquisition pending approval",
            "evidence_text": "Already completed.",
            "provider_params_available_at_ms": 3,
        },
        {
            "item_id": "three",
            "fact_text": "Acme acquisition financing pending",
            "evidence_text": "Digest\n1. Another company acquired for $900m.\n2. Unrelated stock sale.",
            "provider_params_available_at_ms": 1,
        },
    ]
    prepared = assemble_evidence(card, item, query=query_for(card, item, cutoff=2), candidates=[], members=members)
    text = " ".join(s.text for s in prepared.current_evidence)
    assert "pending approval" in text and "financing pending" in text
    assert "completed" not in text and "$900m" not in text
    assert set(prepared.missing) == {"legacy_excerpt_only", "relation_unproven"}


def test_related_budget_is_shared_by_actual_length_not_four_fixed_quotas():
    text = "Acme acquisition conditions: " + "x" * 650 + " approval pending."
    card = {"leader_title": "Acme acquisition"}
    row = {
        "item_id": "past",
        "leader_title": "Acme acquisition",
        "evidence_text": text,
        "provider_params_available_at_ms": 1,
    }
    prepared = assemble_evidence(card, {}, query=query_for(card, {}, cutoff=2), candidates=[row])
    assert sum(len(s.text) for s in prepared.related_evidence) > 600
    assert "approval pending" in " ".join(s.text for s in prepared.related_evidence)


def test_short_tickers_chinese_terms_and_generic_words():
    query = query_for(
        {"leader_title": "F announces acquisition; SEI公司收购协议尚待批准"},
        {},
        cutoff=2,
        assets=(MarketAsset("F", "equity"), MarketAsset("SEI", "crypto")),
    )
    assert {"f", "sei", "收购", "协议", "批准"} <= set(query.terms)
    assert not {"announce", "company", "公司"} & set(query.terms)


@pytest.mark.parametrize("reason", ["explicit_origin", "entity_event_terms", "text_similarity"])
def test_all_channels_reject_typed_collision_even_with_unknown_tag(reason):
    query = query_for(
        {"leader_title": "SEI acquisition agreement approved"}, {}, cutoff=2, assets=(MarketAsset("SEI", "crypto"),)
    )
    row = dict(
        event_id="past",
        item_id="past",
        priority=0,
        score=1,
        created_at_ms=1,
        comparison_fingerprint="past",
        leader_title="SEI acquisition agreement pending",
        retrieval_reason=reason,
        assets=[{"symbol": "SEI", "market_type": "equity"}],
        grounded_assets=["SEI"],
        asset_class="unknown",
    )
    assert shortlist([row], query=query) == []
    row["assets"] = [{"symbol": "SEI", "market_type": "unknown"}]
    assert shortlist([row], query=query)
    row["assets"] = [{"symbol": "SEI", "market_type": "crypto"}, {"symbol": "ACME", "market_type": "equity"}]
    assert shortlist([row], query=query)
    row["leader_title"] = "SEI company announces report"
    assert shortlist([row], query=query) == []
    row["leader_title"] = "Acme Global Corporation announces report"
    assert (
        shortlist(
            [row], query=query_for({"leader_title": "Acme Global Corporation announces acquisition"}, {}, cutoff=2)
        )
        == []
    )


def test_preparation_uses_only_bounded_batches_and_one_cutoff(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from tracefold.news.pipeline.triage_evidence import prepare_evidence

    def no_network(*args, **kwargs):
        raise AssertionError("evidence preparation attempted network I/O")

    monkeypatch.setattr("socket.getaddrinfo", no_network)
    monkeypatch.setattr("httpx.AsyncClient.request", no_network)
    body = "Acme acquisition announced."
    correction = body + " Agreement is non-binding."
    members = [{"item_id": str(i), "fact_id": str(i), "fact_text": body, "joined_at_ms": i} for i in range(30)]
    calls = []

    class Store:
        def evidence_member_metadata(self, ids):
            calls.append(("metadata", ids))
            return [
                {
                    "item_id": i,
                    "evidence_text_sha256": text_sha(correction if i == "3" else body),
                    "provider_params_available_at_ms": 1,
                }
                for i in ids
            ]

        def evidence_material(self, ids):
            calls.append(("material", ids))
            return [
                {
                    "item_id": i,
                    "evidence_text": correction if i == "3" else body,
                    "provider_params_available_at_ms": 1,
                    "canonical_url": "https://example.invalid/story",
                }
                for i in ids
            ]

        def evidence_candidates(self, query):
            calls.append(("candidates", query.cutoff_at_ms))
            return []

    class DB:
        async def read(self, name, fn):
            return fn(SimpleNamespace(news=Store()))

    stamps = iter([10])
    prepared = asyncio.run(
        prepare_evidence(
            DB(),
            {"leader_item_id": "0", "leader_title": body, "evidence_members": members},
            catalog={},
            clock=lambda: next(stamps),
        )
    )
    assert [name for name, _ in calls] == ["metadata", "material", "candidates"]
    assert len(calls[0][1]) == 16 and calls[1][1] == ["0", "3"]
    assert prepared.cutoff_at_ms == 10
    assert "non-binding" in " ".join(s.text for s in prepared.current_evidence)
    assert "member_candidates_truncated" in prepared.exclusions
    assert "document_status" not in prepared.model_dump()


def test_old_document_execution_is_rendered_from_archive_and_adapted_without_lookup():
    import copy

    from tracefold.news.evidence import execution_evidence_views
    from tracefold.news.learning.dataset import DevelopmentDatasetStore
    from tracefold.news.program.contracts import TriageContext

    context = TriageContext.from_card(
        {"leader_title": "Acme acquisition", "leader_description": "Approval pending"},
        watchlist=(),
        told_rows=(),
        now_ms=2,
        queue_lag_ms=0,
    ).model_dump(mode="json")
    old = context["prepared_evidence"]
    old.update(
        input_version="news_evidence_input_v1", document_status="cache_hit", document_receipt={"document_id": "old"}
    )
    old["current_evidence"][0]["document_id"] = "old"
    execution = {
        "execution_index": 0,
        "status": "success",
        "context": context,
        "trace": {"calls": [{"predictor": "reader_card", "validated_output": {"card": {"source_refs": []}}}]},
    }
    raw = [{"trace": {"program_execution_index": 0, "program_executions": [execution]}}]
    before = copy.deepcopy(raw)
    view = execution_evidence_views(raw)[0]
    assert view["document_status"] == "cache_hit"
    assert view["current_evidence"][0]["document_id"] == "old"
    assert view["reference_issues"] == ["empty_source_refs"]
    # No repository or model is installed; the archival branch must need neither.
    store = object.__new__(DevelopmentDatasetStore)
    adapted = store.build_context({"frozen_context": context}, None)
    assert adapted.prepared_evidence.input_version == "news_evidence_input_v2"
    assert "Approval pending" in " ".join(s.text for s in adapted.prepared_evidence.current_evidence)
    assert "document_status" not in adapted.prepared_evidence.model_dump()
    assert raw == before


def test_database_failure_is_not_empty_background():
    import asyncio

    from tracefold.news.pipeline.triage_evidence import prepare_evidence

    class BrokenDB:
        async def read(self, name, fn):
            raise RuntimeError("primary database unavailable")

    with pytest.raises(RuntimeError, match="primary database unavailable"):
        asyncio.run(prepare_evidence(BrokenDB(), {"leader_item_id": "leader"}, catalog={}))


def test_focus_has_priority_over_large_opening_boilerplate():
    text = "Corporate introduction " + "x" * 400 + ". Acme acquisition remains pending approval."
    spans = select_item(
        {"leader_title": "Acme acquisition"},
        {"item_id": "leader", "evidence_text": text, "provider_params_available_at_ms": 1},
        kind="current",
        cutoff=2,
        budget=450,
        prefix="c",
        reason="current_focus",
    )
    assert "Acme acquisition remains pending approval." in " ".join(s.text for s in spans)


def test_shared_event_nouns_do_not_link_different_subjects():
    row = dict(
        event_id="other",
        item_id="other",
        leader_title="Other acquisition agreement approved",
        priority=0,
        score=0.8,
        created_at_ms=1,
        comparison_fingerprint="other",
    )
    query = query_for({"leader_title": "Acme acquisition agreement pending"}, {}, cutoff=2)
    assert shortlist([row], query=query) == []


def test_missing_leader_body_uses_its_frozen_fact_without_other_preview_text():
    card = {
        "leader_item_id": "leader",
        "focus_fact_id": "focus",
        "leader_title": "Acme acquisition",
        "raw_first_line": "Digest: Other company sale worth $900m.",
        "leader_description": "Unrelated preview",
    }
    row = {
        "item_id": "leader",
        "fact_id": "focus",
        "fact_text": "Acme acquisition pending approval",
        "provider_params_available_at_ms": 3,
        "evidence_text": "Later completed acquisition",
    }
    prepared = assemble_evidence(card, row, query=query_for(card, row, cutoff=2), candidates=[])
    assert [s.text for s in prepared.current_evidence] == [row["fact_text"]]
    assert prepared.current_evidence[0].text_space == "frozen_member.fact_text"
    assert prepared.missing == ("legacy_excerpt_only",)
