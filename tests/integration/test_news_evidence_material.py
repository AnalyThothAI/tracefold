from __future__ import annotations

from contextlib import closing

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.evidence import query_for, text_sha
from tracefold.news.opennews import parse_opennews_message
from tracefold.news.pipeline.admission import admit_frame

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


def test_raw_payload_late_fill_and_evidence_revisions(postgres_clone_dsn):
    with closing(connect_postgres_test(read_only=False)) as conn:
        repos = repositories_for_connection(conn)
        text = (
            "BTC acquisition agreement announced.<br/>"
            + "Detailed business background. " * 90
            + "Not binding; approval is pending."
        )
        batch = admit(repos, text)
        item = repos.news.evidence_material([batch.item_id])[0]
        assert item["provider_params_available_at_ms"] == 1000
        assert item["evidence_text"].endswith("Not binding; approval is pending.")
        assert text_sha(item["evidence_text"]) == item["evidence_text_sha256"]
        before = item["provider_params_sha256"]
        admit(repos, text, stamp=2000)
        assert repos.news.evidence_material([batch.item_id])[0]["provider_params_sha256"] == before
        # Simulate a genuine pre-cut empty payload, then fill today. No historical time fabrication.
        conn.execute(
            "UPDATE news_items SET provider_params='{}', provider_params_sha256=NULL, evidence_text=NULL, "
            "provider_params_available_at_ms=NULL WHERE item_id=%s",
            (batch.item_id,),
        )
        admit(repos, text, stamp=3000)
        item = repos.news.evidence_material([batch.item_id])[0]
        assert item["provider_params_available_at_ms"] == 3000
        assert item["evidence_text"].endswith("approval is pending.")
        # A changed body of the same provider record is kept as a later revision beside the first; the
        # first body and its clock are unchanged, and redelivering the revision writes nothing new (#706).
        revised = "BTC acquisition agreement announced.<br/>Conflicting executed status."
        admit(repos, revised, stamp=4000)
        admit(repos, revised, stamp=5000)
        row = conn.execute("SELECT evidence_text FROM news_items WHERE item_id=%s", (batch.item_id,)).fetchone()
        assert row["evidence_text"] == item["evidence_text"]
        revisions = conn.execute(
            "SELECT evidence_text, received_at_ms FROM news_item_revisions WHERE item_id=%s", (batch.item_id,)
        ).fetchall()
        assert [(r["evidence_text"], r["received_at_ms"]) for r in revisions] == [
            ("BTC acquisition agreement announced.\nConflicting executed status.", 4000)
        ]


def test_real_candidate_channels_keep_unknown_and_exclude_known_symbol_conflict(postgres_clone_dsn):
    from tracefold.news.evidence import shortlist
    from tracefold.news.models import MarketAsset
    from tracefold.news.storage.evidence import BACKGROUND_CANDIDATES_SQL, background_parameters

    with closing(connect_postgres_test(read_only=False)) as conn:
        repos = repositories_for_connection(conn)
        first = admit(repos, "BTC acquisition agreement announced; approval pending.", record=21, stamp=1000)
        second = admit(repos, "BTC acquisition agreement approved; execution pending.", record=22, stamp=2000)
        item = repos.news.evidence_material([second.item_id])[0]
        query = query_for(
            event_id=second.results[0].event_id,
            task_texts=(item["evidence_text"],),
            source_items=(item,),
            cutoff=3000,
            assets=[MarketAsset("BTC", "crypto")],
        )
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


def test_empty_asset_query_skips_entity_scan_and_keeps_text_candidates(postgres_clone_dsn):
    from tracefold.news.storage.evidence import BACKGROUND_CANDIDATES_SQL, background_parameters

    with closing(connect_postgres_test(read_only=False)) as conn:
        repos = repositories_for_connection(conn)
        first = admit(repos, "Business acquisition agreement announced; approval pending.", record=23, stamp=1000)
        query = query_for(
            event_id="later",
            task_texts=("Business acquisition agreement announced; approval pending.",),
            cutoff=2000,
        )
        assert not query.assets
        candidates = repos.news.evidence_candidates(query)
        assert first.item_id in {row["item_id"] for row in candidates}
        plan = conn.execute(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + BACKGROUND_CANDIDATES_SQL, background_parameters(query)
        ).fetchone()["QUERY PLAN"][0]["Plan"]

        def nodes(node):
            yield node
            for child in node.get("Plans", ()):
                yield from nodes(child)

        # Returned candidates still need their source tags projected. No other asset scan is
        # owed by an empty query, including when PostgreSQL inlines or removes the entity CTE.
        assert sum(
            node["Actual Loops"] for node in nodes(plan) if node.get("Relation Name") == "news_event_assets"
        ) <= len(candidates)


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


def test_real_semantic_candidate_routes_recall_pair_spelling_and_member_topic(postgres_clone_dsn):
    from tracefold.news.entities import asset_features
    from tracefold.news.models import MarketAsset

    with closing(connect_postgres_test(read_only=False)) as conn:
        repos = repositories_for_connection(conn)
        first = admit(repos, "Aster lists SIUSDT perpetual with 5x leverage.", record=31, stamp=1000)
        event = first.results[0].event_id
        conn.execute(
            "INSERT INTO news_event_assets(event_id, symbol, market_type, opened_at_ms) "
            "VALUES (%s, 'SIUSDT', NULL, 1000) ON CONFLICT DO NOTHING",
            (event,),
        )
        query = query_for(
            event_id="later",
            cutoff=2000,
            task_texts=("Aster DEX上线$SI，最大5倍杠杆",),
            assets=(MarketAsset("SI", "crypto"),),
        )
        rows = repos.news.evidence_candidates(query)
        assert event in {row["event_id"] for row in rows}
        assert next(row for row in rows if row["event_id"] == event)["retrieval_reason"] == "entity_event_terms"
        # A lexical quote suffix is only a comparison candidate; it does not resolve the venue contract.
        assert asset_features("SIUSDT", "crypto")[0].key != asset_features("SI", "crypto")[0].key

        child = admit(repos, "Atlas launches a quantum networking product.", record=32, stamp=1100)
        conn.execute(
            "INSERT INTO news_event_members(event_id,item_id,joined_at_ms,match_kind,fact_id,fact_text) "
            "VALUES (%s,%s,1100,'near','member-topic','Atlas launches a quantum networking product.')",
            (event, child.item_id),
        )
        member_query = query_for(
            event_id=child.results[0].event_id,
            cutoff=2000,
            task_texts=("Atlas launches a quantum networking product.",),
        )
        matched = repos.news.evidence_candidates(member_query)
        assert event in {row["event_id"] for row in matched}
        assert max(row["score"] for row in matched if row["event_id"] == event) == 1.0
        before_member = query_for(
            event_id="earlier",
            cutoff=1050,
            task_texts=("Atlas launches a quantum networking product.",),
        )
        assert event not in {row["event_id"] for row in repos.news.evidence_candidates(before_member)}


@pytest.mark.parametrize(("pair", "base", "partial"), [("ABCFDUSD", "ABC", "ABCFD"), ("BTCBUSD", "BTC", "BTCB")])
def test_real_semantic_pair_route_uses_only_the_first_valid_quote_suffix(postgres_clone_dsn, pair, base, partial):
    with closing(connect_postgres_test(read_only=False)) as conn:
        repos = repositories_for_connection(conn)
        first = admit(repos, f"Aster lists {pair} perpetual with leverage.", record=40, stamp=1000)
        event = first.results[0].event_id
        # Isolate this actual admission's candidate tag from the helper's default BTC provider tag.
        conn.execute("DELETE FROM news_event_assets WHERE event_id=%s", (event,))
        conn.execute(
            "INSERT INTO news_event_assets(event_id, symbol, market_type, opened_at_ms) VALUES (%s,%s,'crypto',1000)",
            (event, pair),
        )
        for symbol, expected in ((base, True), (partial, False)):
            query = query_for(event_id="later", cutoff=2000, task_texts=(f"Aster批准${symbol}独家交易。",))
            assert (event in {row["event_id"] for row in repos.news.evidence_candidates(query)}) is expected
