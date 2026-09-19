from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tracefold.news.evidence import (
    CURRENT_CHARS,
    DocumentResult,
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


def test_late_or_unrelated_page_cannot_expand_current_fact():
    card = {"leader_title": "Acme acquisition announced"}
    item = {"evidence_text": "Acme acquisition announced.", "provider_params_available_at_ms": 10}
    query = query_for(card, item, cutoff=11)
    late = DocumentResult(status="success", available_at_ms=12, extracted_text="Acme acquisition approved.")
    prepared = assemble_evidence(card, item, query=query, candidates=[], document=late)
    assert "document_after_cutoff" in prepared.exclusions
    assert not any(s.document_id for s in prepared.current_evidence)
    unrelated = late.model_copy(update={"available_at_ms": 10, "extracted_text": "Unrelated football match."})
    prepared = assemble_evidence(card, item, query=query, candidates=[], document=unrelated)
    assert "document_relation_unproven" in prepared.exclusions


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
    assert [r["event_id"] for r in shortlist(rows)] == ["0", "direct"]


def test_future_duplicate_does_not_hide_older_visible_receipt():
    snapshot = assemble_reader_history(recent_rows=[history_row("same", 1001), history_row("same", 999)], now_ms=1000)
    assert [row.at_ms for row in snapshot.told_source_rows] == [999]


@pytest.mark.parametrize("status", ["success", "timeout"])
def test_stale_preparation_never_refetches_and_cached_versions_are_reused(status):
    from tracefold.news.pipeline.triage_evidence import prepare_evidence

    class Store:
        cached = None

        def evidence_item(self, item_id):
            return {
                "item_id": item_id,
                "canonical_url": "https://example.org/story",
                "evidence_text": "Acme acquisition announced.",
                "provider_params_available_at_ms": 10,
            }

        def evidence_document(self, url, **kwargs):
            return self.cached

        def save_evidence_document(self, document):
            self.cached = document.model_dump()

        def evidence_candidates(self, query):
            return []

    store = Store()

    class DB:
        async def read(self, name, fn):
            return fn(SimpleNamespace(news=store))

        tx = read

    class Reader:
        calls = 0

        async def read(self, url):
            self.calls += 1
            text = "Acme acquisition still awaits approval. " * 4
            return DocumentResult(
                status=status,
                requested_url=url,
                normalized_url=url,
                final_url=url,
                document_id="doc" if status == "success" else "",
                available_at_ms=15,
                extracted_text=text,
                extracted_text_sha256=text_sha(text),
            )

    async def run():
        reader = Reader()
        card = {"event_id": "current", "leader_item_id": "item", "leader_title": "Acme acquisition announced"}
        first = await prepare_evidence(DB(), card, catalog={}, reader=reader, clock=lambda: 20)
        second = await prepare_evidence(DB(), card, catalog={}, reader=reader, clock=lambda: 30, allow_fetch=False)
        assert reader.calls == 1
        assert first.document_status == status
        assert second.document_status == ("cache_hit" if status == "success" else "already_attempted")
        assert first.current_evidence == second.current_evidence

    asyncio.run(run())
