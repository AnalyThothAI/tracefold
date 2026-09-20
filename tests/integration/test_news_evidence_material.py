from __future__ import annotations

import asyncio
import json
import time
from contextlib import closing

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.evidence import assemble_evidence, query_for, text_sha
from tracefold.news.opennews import parse_opennews_message
from tracefold.news.pipeline.admission import admit_frame
from tracefold.news.pipeline.triage_evidence import prepare_evidence
from tracefold.news.program.contracts import TriageContext

pytestmark = pytest.mark.integration


def admit(repos, text: str, *, record: int = 664, stamp: int = 1000):
    event = parse_opennews_message(
        {
            "method": "strategy.triggered",
            "params": {
                "id": record,
                "text": text,
                "link": f"https://example.org/{record}",
                "source": "Reuters",
                "engineType": "news",
                "ts": stamp,
                "coins": [{"symbol": "BTC", "grade": "A"}],
                "strategy": {"id": 1018, "name": "News Score > 70", "engine_type": "news", "source_type": "news"},
            },
        }
    )
    assert event is not None
    return admit_frame(
        repos,
        event=event,
        ingest_mode="live",
        observed_at_ms=stamp,
        trace_id="evidence-test",
        watchlist_symbols=frozenset(),
        now_ms=stamp,
    )


def test_raw_payload_late_fill_conflict_and_frozen_spans(postgres_clone_dsn):
    with closing(connect_postgres_test(read_only=False)) as conn:
        repos = repositories_for_connection(conn)
        text = (
            "BTC acquisition agreement announced.<br/>"
            + "Detailed business background. " * 90
            + "Not binding; approval is pending."
        )
        batch = admit(repos, text)
        event_id = batch.results[0].event_id
        item = repos.news.evidence_material([batch.item_id])[0]
        card = repos.news.event_card(event_id)
        assert item["provider_params_available_at_ms"] == 1000
        assert item["evidence_text"].endswith("Not binding; approval is pending.")
        assert text_sha(item["evidence_text"]) == item["evidence_text_sha256"]
        before = item["provider_params_sha256"]
        admit(repos, text, stamp=2000)
        assert repos.news.evidence_material([batch.item_id])[0]["provider_params_sha256"] == before
        # Simulate a genuine pre-cut empty payload, then fill today. No historical time fabrication.
        conn.execute(
            "UPDATE news_items SET provider_params='{}', provider_params_sha256=NULL, evidence_text=NULL, "
            "provider_params_available_at_ms=NULL, provider_params_conflict_at_ms=NULL, "
            "provider_params_conflict_sha256=NULL WHERE item_id=%s",
            (batch.item_id,),
        )
        admit(repos, text, stamp=3000)
        item = repos.news.evidence_material([batch.item_id])[0]
        assert item["provider_params_available_at_ms"] == 3000
        old = assemble_evidence(card, item, query=query_for(card, item, cutoff=2000), candidates=[])
        assert old.missing == ("legacy_excerpt_only",)
        assert all("Not binding" not in s.text for s in old.current_evidence)
        current = assemble_evidence(card, item, query=query_for(card, item, cutoff=3001), candidates=[])
        assert "approval is pending" in " ".join(s.text for s in current.current_evidence)
        for span in current.current_evidence:
            assert item["evidence_text"][span.span_start : span.span_end] == span.text
        admit(repos, "BTC acquisition agreement announced.<br/>Conflicting executed status.", stamp=4000)
        row = conn.execute(
            "SELECT provider_params_conflict_at_ms, evidence_text FROM news_items WHERE item_id=%s", (batch.item_id,)
        ).fetchone()
        assert row["provider_params_conflict_at_ms"] == 4000
        assert row["evidence_text"] == item["evidence_text"]
        conflict = repos.news.evidence_material([batch.item_id])[0]
        assert (
            "provider_payload_conflict"
            in assemble_evidence(card, conflict, query=query_for(card, conflict, cutoff=4001), candidates=[]).missing
        )


def test_unsent_background_is_separate_and_three_predictors_share_frozen_input(postgres_clone_dsn):
    with closing(connect_postgres_test(read_only=False)) as conn:
        repos = repositories_for_connection(conn)
        a = admit(repos, "BTC acquisition agreement announced; regulatory approval pending.", record=1, stamp=1000)
        b = admit(repos, "BTC acquisition agreement approved by regulator; execution pending.", record=2, stamp=2000)
        card = repos.news.event_card(b.results[0].event_id)

        class DB:
            async def read(self, name, fn, **kwargs):
                if name == "news_evidence_background":
                    # A concurrent merge changes the leader after shortlist selection.
                    # Loading the selected immutable Item must not follow that new pointer.
                    conn.execute(
                        "UPDATE news_events SET leader_item_id=%s WHERE event_id=%s", (b.item_id, a.results[0].event_id)
                    )
                return fn(repos)

            async def tx(self, name, fn, **kwargs):
                return fn(repos)

        # Use actual preparation/SQL at a realistic cutoff while keeping records inside the window.
        import time

        stamp = int(time.time() * 1000)
        conn.execute("UPDATE news_events SET created_at_ms=%s", (stamp - 1000,))
        prepared = asyncio.run(prepare_evidence(DB(), card, catalog={"BTC": ["crypto"]}))
        assert any(span.source_item_id == a.item_id for span in prepared.related_evidence)
        history = repos.news.reader_history(event_id=card["event_id"], now_ms=prepared.cutoff_at_ms)
        assert history.told_source_rows == ()
        context = TriageContext.from_card(
            card, watchlist=(), told_rows=(), now_ms=prepared.cutoff_at_ms, queue_lag_ms=0, prepared_evidence=prepared
        )
        assert context.event_semantics_payload()["event_status"]["told"] == []
        assert context.event_semantics_payload()["related_evidence"]
        assert "related_evidence" not in context.taxonomy_payload()
        assert "event_status" not in context.reader_card_payload()
        assert context.taxonomy_payload()["current_evidence"] == context.reader_card_payload()["current_evidence"]
        frozen = context.model_dump_json()
        conn.execute("UPDATE news_items SET description='later preview mutation'")
        assert TriageContext.model_validate_json(frozen).model_dump_json() == frozen


def test_delivery_history_uses_sent_context_not_mutable_verdict_or_event(postgres_clone_dsn):
    with closing(connect_postgres_test(read_only=False)) as conn:
        repos = repositories_for_connection(conn)
        event_id = admit(repos, "BTC acquisition agreement approved.").results[0].event_id
        history = dict(
            storyline_key="asset:crypto:BTC",
            comparison_title="original fact",
            comparison_fingerprint="old",
            dedupe_family="general",
            assets=[dict(symbol="BTC", market_type="crypto")],
            canonical_assets=["BTC"],
            grounded_assets=["BTC"],
            magnitude=2,
            direction="bullish",
            headline_zh="已发送原卡片",
            why_zh="原判断",
            policy_version="bound-policy",
        )
        assert (
            repos.news.begin_delivery(
                event_id=event_id, kind="first", card={}, now_ms=1500, history_context_json=json.dumps(history)
            )
            == "new"
        )
        conn.execute("UPDATE news_deliveries SET state='sent', settled_at_ms=2000 WHERE event_id=%s", (event_id,))
        conn.execute(
            "UPDATE news_events SET storyline_key='changed', comparison_title='changed' WHERE event_id=%s", (event_id,)
        )
        actual = repos.news.reader_history(event_id="candidate", now_ms=2001)
        assert actual.recent_seen_rows[0].comparison_title == "original fact"
        assert actual.recent_seen_rows[0].storyline_key == "asset:crypto:BTC"
        assert repos.news.reader_history(event_id="candidate", now_ms=2000).told_source_rows == ()
        assert repos.news.reader_history_revision(now_ms=1999)[0] == 1


def test_raw_to_native_execution_to_read_only_detail_and_settled_replay(postgres_clone_dsn):
    from tests.integration.test_news_crash_replay import FaultInjectingDatabase, RecordingBus, _triage
    from tests.news.test_news_program_routing import _route, _semantics
    from tracefold.news.bus import BusMessage
    from tracefold.news.program.artifact import load_stable_program_state
    from tracefold.news.program.module import NativeNewsProgram
    from tracefold.news.program.routing import RoutedSemanticJudge

    with closing(connect_postgres_test(read_only=False)) as conn:
        repos = repositories_for_connection(conn)
        stamp = int(time.time() * 1000)
        original = "BTC acquisition announced.\n" + "Business details. " * 450 + "Not binding; approval pending."
        batch = admit(repos, original, stamp=stamp)
        event_id = batch.results[0].event_id
        state = load_stable_program_state()
        judge = RoutedSemanticJudge(
            NativeNewsProgram(state),
            primary=_route(
                state,
                route="primary",
                semantics=[_semantics()],
                cards=[
                    {
                        "card": {
                            "headline_zh": "比特币相关收购仍待批准",
                            "why_zh": "协议尚无约束力。",
                            "source_refs": ["c1"],
                        }
                    }
                ],
            ),
        )
        consumer = _triage(FaultInjectingDatabase(conn), RecordingBus(), judge=judge)
        message = BusMessage(
            kind="event",
            message_id="evidence-chain",
            routing_key="news.event",
            payload={"event_id": event_id},
            trace_id="evidence-chain",
            occurred_at_ms=stamp,
        )
        asyncio.run(consumer.handle(message))
        detail = repos.news.event_detail(event_id)
        visible = detail["evidence_inputs"]
        assert len(visible) == 1 and visible[0]["selected"]
        assert visible[0]["declared_source_refs"] == ["c1"]
        assert "approval pending" in " ".join(s["text"] for s in visible[0]["current_evidence"])
        verdict = repos.news.latest_verdict(event_id=event_id, stage="triage")
        execution = verdict["trace"]["program_executions"][0]
        assert [call["predictor"] for call in execution["trace"]["calls"]] == [
            "event_semantics",
            "taxonomy",
            "reader_card",
        ]
        assert all(call["request_sha256"] for call in execution["trace"]["calls"])
        frozen = json.dumps(visible, sort_keys=True)
        # The scripted route has no further semantic/card responses: either read/replay calling
        # the model again would fail. A newer payload is displayed separately, never injected.
        conn.execute(
            "UPDATE news_items SET provider_params_available_at_ms=%s WHERE item_id=%s",
            (visible[0]["cutoff_at_ms"] + 1, batch.item_id),
        )
        asyncio.run(consumer.handle(message))

        class NoDocumentReads:
            def execute(self, sql, params=None):
                assert "news_evidence_documents" not in str(sql)
                return conn.execute(sql, params)

        detail = repositories_for_connection(NoDocumentReads()).news.event_detail(event_id)
        assert json.dumps(detail["evidence_inputs"], sort_keys=True) == frozen
        assert detail["late_evidence"][0]["material_id"] == batch.item_id


def test_frozen_members_use_real_bounded_batches_and_ignore_later_membership(postgres_clone_dsn):
    with closing(connect_postgres_test(read_only=False)) as conn:
        repos = repositories_for_connection(conn)
        leader = admit(repos, "BTC acquisition announced.", record=10, stamp=1000)
        supplement = admit(
            repos, "BTC acquisition announced. Agreement is non-binding; approval is pending.", record=11, stamp=1100
        )
        late = admit(repos, "BTC acquisition funding completed for $99 million.", record=12, stamp=1200)
        event_id = leader.results[0].event_id

        def join(batch, stamp):
            source = repos.news.event_card(batch.results[0].event_id)
            repos.news.add_member(
                event_id=event_id,
                item_id=batch.item_id,
                joined_at_ms=stamp,
                match_kind="near",
                jaccard_estimate=0.9,
                provider_score=None,
                fact_id=source["focus_fact_id"],
                fact_text=source["leader_title"],
                now_ms=stamp,
            )

        join(supplement, 1300)
        repos.news.append_evidence_snapshot(event_id=event_id, now_ms=1300)
        card = repos.news.event_card(event_id)
        join(late, 1400)
        repos.news.append_evidence_snapshot(event_id=event_id, now_ms=1400)
        assert late.item_id not in {m["item_id"] for m in card["evidence_members"]}
        statements = []

        class RecordingConnection:
            def execute(self, sql, params=None):
                statements.append((str(sql), params))
                return conn.execute(sql, params)

        observed = repositories_for_connection(RecordingConnection())

        class DB:
            async def read(self, name, fn):
                return fn(observed)

        prepared = asyncio.run(prepare_evidence(DB(), card, catalog={"BTC": ["crypto"]}, clock=lambda: 1350))
        text = " ".join(s.text for s in prepared.current_evidence)
        assert "non-binding" in text and "$99 million" not in text
        assert {s.source_item_id for s in prepared.current_evidence} == {leader.item_id, supplement.item_id}
        body_queries = [(sql, params) for sql, params in statements if "evidence_text," in sql]
        assert len(body_queries) <= 2  # one current batch and at most one related batch
        assert all("ANY" in sql and len(params[0]) <= 4 for sql, params in body_queries)
        assert len(statements) <= 4
        assert not any("news_evidence_documents" in sql for sql, _ in statements)
        conn.execute(
            "UPDATE news_items SET provider_params_available_at_ms=2000 WHERE item_id=%s", (supplement.item_id,)
        )
        missing = asyncio.run(prepare_evidence(DB(), card, catalog={}, clock=lambda: 1350))
        assert "legacy_excerpt_only" in missing.missing
        assert all(
            s.text_space.startswith("frozen_")
            for s in missing.current_evidence
            if s.source_item_id == supplement.item_id
        )


def test_real_candidate_channels_keep_unknown_and_exclude_known_symbol_conflict(postgres_clone_dsn):
    from tracefold.news.evidence import shortlist
    from tracefold.news.models import MarketAsset
    from tracefold.news.storage.evidence import BACKGROUND_CANDIDATES_SQL, background_parameters

    with closing(connect_postgres_test(read_only=False)) as conn:
        repos = repositories_for_connection(conn)
        first = admit(repos, "BTC acquisition agreement announced; approval pending.", record=21, stamp=1000)
        second = admit(repos, "BTC acquisition agreement approved; execution pending.", record=22, stamp=2000)
        card = repos.news.event_card(second.results[0].event_id)
        item = repos.news.evidence_material([second.item_id])[0]
        query = query_for(card, item, cutoff=3000, assets=[MarketAsset("BTC", "crypto")])
        unknown = repos.news.evidence_candidates(query)
        assert first.item_id in {r["item_id"] for r in shortlist(unknown, query=query)}
        # Explicit-source links are subject to the same identity filter as the other channels.
        conn.execute("UPDATE news_items SET canonical_url=%s WHERE item_id=%s", (item["canonical_url"], first.item_id))
        conn.execute(
            "INSERT INTO news_event_assets(event_id, symbol, market_type, opened_at_ms) "
            "VALUES (%s, 'BTC', 'equity', 1000) ON CONFLICT (event_id, symbol) DO UPDATE SET market_type='equity'",
            (first.results[0].event_id,),
        )
        candidates = repos.news.evidence_candidates(query)
        assert candidates and candidates[0]["retrieval_reason"] == "explicit_origin"
        assert shortlist(candidates, query=query) == []
        plan = conn.execute(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + BACKGROUND_CANDIDATES_SQL, background_parameters(query)
        ).fetchone()["QUERY PLAN"][0]
        assert plan["Plan"]["Actual Rows"] <= 64


def test_archived_webpage_rows_remain_append_only_and_new_details_never_query_them(postgres_clone_dsn):
    from psycopg.errors import RaiseException

    with closing(connect_postgres_test(read_only=False)) as conn:
        repos = repositories_for_connection(conn)
        event = admit(repos, "BTC acquisition announced.").results[0].event_id
        conn.execute("""INSERT INTO news_evidence_documents
            (document_id, requested_url, final_url, normalized_url, response_sha256, extracted_text_sha256,
             extractor_version, extracted_text, observed_at_ms, available_at_ms, content_type, extraction_status)
            VALUES ('archive', 'https://archive.invalid', 'https://archive.invalid', 'https://archive.invalid',
                    'old-response', 'old-text', 'old', 'Archived text', 1, 1, 'text/plain', 'success')""")
        with pytest.raises(RaiseException, match="news_document_append_only"), repos.transaction():
            conn.execute("UPDATE news_evidence_documents SET extracted_text='rewritten' WHERE document_id='archive'")

        class NoDocumentReads:
            def execute(self, sql, params=None):
                assert "news_evidence_documents" not in str(sql)
                return conn.execute(sql, params)

        detail = repositories_for_connection(NoDocumentReads()).news.event_detail(event)
        assert detail["event"]["event_id"] == event
        assert (
            conn.execute("SELECT extracted_text FROM news_evidence_documents WHERE document_id='archive'").fetchone()[
                "extracted_text"
            ]
            == "Archived text"
        )
