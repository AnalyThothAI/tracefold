from __future__ import annotations

import pytest

from tracefold.news.evidence import (
    query_for,
    shortlist,
)
from tracefold.news.models import MarketAsset
from tracefold.news.reader_history import assemble_reader_history


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


def test_old_document_execution_is_rendered_from_archive_without_lookup():
    """A legacy Triage execution archived with a webpage-document receipt still renders as it was stored."""

    import copy

    from tracefold.news.evidence import execution_evidence_views

    prepared = {
        "input_version": "news_evidence_input_v1",
        "selector_version": "local_focus_sentences_v2",
        "cutoff_at_ms": 2,
        "current_evidence": [
            {
                "ref_id": "c1",
                "material_kind": "current",
                "document_id": "old",
                "text": "Acme acquisition\n\nApproval pending",
                "selection_reason": "current_focus",
                "coverage_status": "legacy_excerpt_only",
            }
        ],
        "related_evidence": [],
        "candidate_count": 0,
        "selected_count": 0,
        "exclusions": [],
        "missing": ["legacy_excerpt_only"],
        "elapsed_ms": 0,
        "document_status": "cache_hit",
        "document_receipt": {"document_id": "old"},
    }
    execution = {
        "execution_index": 0,
        "status": "success",
        "context": {"evidence": {"focus_fact_id": "f1"}, "prepared_evidence": prepared},
        "trace": {"calls": [{"predictor": "reader_card", "validated_output": {"card": {"source_refs": []}}}]},
    }
    raw = [{"trace": {"program_execution_index": 0, "program_executions": [execution]}}]
    before = copy.deepcopy(raw)
    view = execution_evidence_views(raw)[0]
    assert view["document_status"] == "cache_hit"
    assert view["document_receipt"] == {"document_id": "old"}
    assert view["current_evidence"][0]["document_id"] == "old"
    assert view["focus_fact_id"] == "f1" and view["selected"] is True
    assert view["reference_issues"] == ["empty_source_refs"]
    assert raw == before


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
