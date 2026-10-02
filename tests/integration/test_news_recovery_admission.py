"""Recovery/live twins use real admission, evidence and durable semantic work."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from tests.support.news_update_admission import RecordingBus, event_of, raw, work
from tests.support.news_update_pg import STAMP, ThreadedDb, seed_event, sql
from tracefold.news.pipeline.admission import DeduperConsumer
from tracefold.news.storage.semantic_store import PgSemanticStore, PgSourceReader
from tracefold.news.updates.contracts import ReadTarget

pytestmark = pytest.mark.integration
TITLE = "Agency orders 25% tariff on steel imports from Canada"


def _frame(record: int, *, age: int, recovery: bool = True, text: str = TITLE):
    frame = raw(record, text, stamp=STAMP, ingest_mode="recovery" if recovery else "live")
    return replace(frame, payload={**frame.payload, "params": {**frame.payload["params"], "ts": STAMP - age}})


@pytest.mark.parametrize("age", [18_000, 30 * 60_000, 30 * 60_000 + 1])
def test_recovery_event_admission_and_live_twin_grouping(postgres_clone_dsn: str, monkeypatch, age: int) -> None:
    monkeypatch.setattr("tracefold.news.pipeline.admission.now_ms", lambda: STAMP)
    bus = RecordingBus()
    consumer = DeduperConsumer(bus=bus, db=ThreadedDb(), watchlist_symbols=frozenset())

    async def scenario() -> None:
        await consumer.handle(_frame(79101, age=age))
        if age <= 30 * 60_000:
            source = await PgSemanticStore(ThreadedDb()).input_for(event_of(79101))
            assert source.evidence[0].source.first_available_at_ms == STAMP - age
        await consumer.handle(_frame(79102, age=0, recovery=False))

    asyncio.run(scenario())
    recovered, live = event_of(79101), event_of(79102)
    history = age > 30 * 60_000
    row = sql("SELECT admission,ingest_mode FROM news_events WHERE event_id=%s", (recovered,))[0]
    assert row == {"admission": "recovery" if history else "candidate", "ingest_mode": "recovery"}
    assert (recovered != live) == history
    jobs = sql("SELECT subject_id FROM news_jobs WHERE job_kind='semantic'")
    assert {row["subject_id"] for row in jobs} == {live}
    assert work(live)["wanted_revision"] >= 1
    assert bus.wakes()


@pytest.mark.parametrize("age", [18_000, 30 * 60_000 + 1])
def test_new_recovery_evidence_wakes_a_live_event_only_when_timely(
    postgres_clone_dsn: str, monkeypatch, age: int
) -> None:
    monkeypatch.setattr("tracefold.news.pipeline.admission.now_ms", lambda: STAMP)
    bus = RecordingBus()
    consumer = DeduperConsumer(bus=bus, db=ThreadedDb(), watchlist_symbols=frozenset())

    async def scenario() -> None:
        await consumer.handle(_frame(79111, age=0, recovery=False))
        bus.published.clear()
        await consumer.handle(_frame(79112, age=age, text=TITLE + " effective next month"))

    asyncio.run(scenario())
    event_id = event_of(79111)
    assert event_of(79112) == event_id
    assert work(event_id)["wanted_revision"] == (2 if age <= 30 * 60_000 else 1)
    assert bool(bus.wakes()) == (age <= 30 * 60_000)


@pytest.mark.parametrize("mode", ["live", "recovery"])
@pytest.mark.parametrize("published", [STAMP - 18_000, STAMP + 1])
def test_frozen_input_and_optional_read_share_the_first_available_clock(
    postgres_clone_dsn: str, mode: str, published: int
) -> None:
    seed_event("source-clock", at_ms=STAMP)
    sql(
        "UPDATE news_items SET first_ingest_mode=%s,published_at_ms=%s WHERE item_id='it-source-clock'",
        (mode, published),
    )
    db = ThreadedDb()
    target = ReadTarget(
        ref="news_item:it-source-clock", action="load_prior_statement", description="Stored reporting source"
    )

    async def scenario() -> None:
        frozen = await PgSemanticStore(db).input_for("source-clock")
        optional = await PgSourceReader(db).read(target)
        assert len(frozen.evidence) == len(optional) == 1
        expected = min(STAMP, published) if mode == "recovery" else STAMP
        assert frozen.evidence[0].source.first_available_at_ms == expected
        assert optional[0].source.first_available_at_ms == expected
        assert optional[0] == frozen.evidence[0]

    asyncio.run(scenario())


def test_recovery_receipts_count_as_sent_but_do_not_enter_live_latency(postgres_clone_dsn: str) -> None:
    from tests.integration.test_news_event_update_store import seed_sent_receipt
    from tests.support.news_update_pg import seed_event
    from tracefold.news.storage.feed_sql import STATUS_DELIVERY_SQL

    for event_id, delay in (("live", 5_000), ("recovered", 600_000)):
        seed_event(event_id, at_ms=STAMP)
        seed_sent_receipt(
            event_id,
            intent_id=f"sent-{event_id}",
            content_revision="a" * 64,
            claim_refs=[],
            body="Agency orders tariff",
            settled_at_ms=STAMP + delay,
        )
    sql("UPDATE news_events SET ingest_mode='recovery' WHERE event_id='recovered'")
    result = sql(STATUS_DELIVERY_SQL, (STAMP,) * 5)[0]
    assert result["sent_24h"] == result["sent_1h"] == 2
    assert result["e2e_p50_ms"] == result["e2e_p95_ms"] == 5_000
