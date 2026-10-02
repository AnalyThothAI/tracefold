"""Admission and semantic work fixtures shared by real PostgreSQL News tests."""

from __future__ import annotations

from typing import Any

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_0424_sql import EVIDENCE_VERSIONS_SQL
from tests.support.news_update_pg import ThreadedDb, sql
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.bus import RK_RAW_LIVE, BusMessage
from tracefold.news.pipeline.admission import append_admission_evidence
from tracefold.news.storage.events import prepare_evidence_snapshot
from tracefold.news.storage.semantic_jobs import semantic_job

TITLE = "Agency orders 25% tariff on steel imports from Canada"


class RecordingBus:
    prefix = ""
    last_publish_failure = None

    def __init__(self) -> None:
        self.published: list[BusMessage] = []

    async def broker_snapshot(self) -> dict[str, Any]:
        return {}

    async def publish(self, message: BusMessage) -> None:
        self.published.append(message)

    def wakes(self) -> list[str]:
        return [message.message_id for message in self.published if message.kind == "event"]


def raw(
    record: int,
    text: str,
    *,
    stamp: int,
    link: str | None = None,
    source: str = "Reuters",
    ingest_mode: str = "live",
    source_age_ms: int = 0,
    strategy_id: str = "1018",
) -> BusMessage:
    params = {
        "id": record,
        "text": text,
        "link": link or f"https://example.org/{record}",
        "source": source,
        "engineType": "news",
        "ts": stamp - source_age_ms,
        "coins": [{"symbol": "BTC", "grade": "A"}],
        "strategy": {"id": int(strategy_id), "name": "News Score > 70", "engine_type": "news", "source_type": "news"},
        "aiRating": {"score": 90},
    }
    return BusMessage(
        kind="raw",
        message_id=f"raw:{record}:{stamp}",
        routing_key=RK_RAW_LIVE.format(strategy_id=strategy_id),
        payload={"params": params, "strategy_id": strategy_id, "ingest_mode": ingest_mode, "observed_at_ms": stamp},
        trace_id=f"trace-{record}",
        occurred_at_ms=stamp,
    )


def event_of(record: int) -> str:
    rows = sql(
        """
        SELECT DISTINCT m.event_id FROM news_event_members m JOIN news_items i ON i.item_id = m.item_id
         WHERE i.source_item_key = %s
        """,
        (str(record),),
    )
    assert len(rows) == 1, rows
    return str(rows[0]["event_id"])


def work(event_id: str) -> dict[str, Any]:
    return semantic_job(sql("SELECT * FROM news_jobs WHERE job_kind='semantic' AND subject_id=%s", (event_id,))[0])


def snapshots(event_id: str) -> list[dict[str, Any]]:
    rows = sql(
        f"SELECT evidence_version, snapshot FROM ({EVIDENCE_VERSIONS_SQL}) WHERE event_id = %s"
        " ORDER BY evidence_version",
        (event_id,),
    )

    def current(repos):
        return repos.news.latest_evidence_snapshot(event_id)

    latest = ThreadedDb()._run("latest-evidence", current)
    if latest is not None and rows:
        rows[-1]["snapshot"] = latest["snapshot"]
    return rows


def add_member_evidence(event_id: str, item_id: str, text: str, *, now_ms: int) -> None:
    """A second member joins the Event the way admission records it: evidence and semantic work together."""

    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO news_items (
                  item_id, source_id, source_item_key, title, raw_first_line, description, canonical_url,
                  reporting_origin, published_at_ms, observed_at_ms, provider_metadata, provenance,
                  first_ingest_mode, trace_id, created_at_ms, updated_at_ms, source_artifact_id,
                  evidence_text, evidence_text_sha256
                ) VALUES (%(item)s, 'opennews', %(item)s, %(text)s, %(text)s, '', 'https://example.org/m',
                          'Wire', %(at)s, %(at)s, '{}'::jsonb, '[]'::jsonb, 'live', 'trace', %(at)s, %(at)s,
                          %(item)s, %(text)s, %(item)s)
                """,
                {"item": item_id, "text": text, "at": now_ms},
            )
            repos = repositories_for_connection(conn)
            repos.news.add_member(
                event_id=event_id,
                item_id=item_id,
                joined_at_ms=now_ms,
                match_kind="near",
                jaccard_estimate=0.8,
                provider_score=None,
                fact_id=f"fact-{item_id}",
                fact_text=text,
                now_ms=now_ms,
            )
            material = repos.news.evidence_snapshot_material(event_id=event_id, focus_item_id=None)
            snapshot = prepare_evidence_snapshot(material, event_id=event_id, now_ms=now_ms, focus_fact=None)
            assert append_admission_evidence(repos, snapshot, history_only=False, now_ms=now_ms) is not None
    finally:
        conn.close()
