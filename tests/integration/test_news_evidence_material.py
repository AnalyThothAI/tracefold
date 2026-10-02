from __future__ import annotations

from contextlib import closing

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_0424_sql import ITEM_REVISIONS_SQL
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.evidence import text_sha
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
            f"SELECT evidence_text, received_at_ms FROM ({ITEM_REVISIONS_SQL}) WHERE item_id=%s", (batch.item_id,)
        ).fetchall()
        assert [(r["evidence_text"], r["received_at_ms"]) for r in revisions] == [
            ("BTC acquisition agreement announced.\nConflicting executed status.", 4000)
        ]


def test_new_details_never_query_retired_webpage_storage(postgres_clone_dsn):
    with closing(connect_postgres_test(read_only=False)) as conn:
        repos = repositories_for_connection(conn)
        event = admit(repos, "BTC acquisition announced.").results[0].event_id

        class NoDocumentReads:
            def execute(self, sql, params=None):
                assert "news_evidence_documents" not in str(sql)
                return conn.execute(sql, params)

        detail = repositories_for_connection(NoDocumentReads()).news.event_detail(event)
        assert detail["event"]["event_id"] == event
