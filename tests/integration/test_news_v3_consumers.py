"""News V3 consumers against real PostgreSQL with a recording fake bus.

Covers: Deduper raw -> event publication (+ idempotent redelivery), Triage fail-closed fallback with
an unconfigured semantic Program, Deliverer settlement when no sender is configured, and Control state writes.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.app.workers.wiring.database import WorkerNewsDatabase
from tracefold.news.bus import (
    RK_RAW_LIVE,
    RK_RAW_RECOVERY,
    BusMessage,
    PermanentError,
    TransientError,
    new_trace_id,
    now_ms,
)
from tracefold.news.market_review.instrument_storage import InstrumentsRepository
from tracefold.news.models import ADMITTED_ADMISSIONS
from tracefold.news.pipeline.admission import DeduperConsumer

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "news_v3_hits_sample.json"
WATCHLIST = frozenset({"BTC", "NVDA", "ETH"})
PROGRAM_SHA256 = "9" * 64
EVENT_ROUTING_KEY = re.compile(r"^event\.[a-z_]+\.(high|normal)$")


class FakeBus:
    """Records publishes; consume is never needed because tests call ``handle`` directly."""

    def __init__(self) -> None:
        self.published: list[BusMessage] = []

    async def publish(self, message: BusMessage) -> None:
        self.published.append(message)

    def routing_keys(self) -> list[str]:
        return [message.routing_key for message in self.published]


class FakeWorkerDatabase:
    """WorkerDatabase-like adapter over one test connection: the News lane runs inline.

    The consumers see it through the production `WorkerNewsDatabase` port, so the session and
    transaction boundaries under test are the ones the composition root actually wires.
    """

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.operations: list[str] = []
        self._port = WorkerNewsDatabase(self)

    async def read(self, name: str, fn: Any, *, timeout_seconds: float = 3.0) -> Any:
        return await self._port.read(name, fn, timeout_seconds=timeout_seconds)

    async def tx(self, name: str, fn: Any, *, timeout_seconds: float = 3.0) -> Any:
        return await self._port.tx(name, fn, timeout_seconds=timeout_seconds)

    @contextmanager
    def worker_session(self, name: str, *_args: Any, **_kwargs: Any):
        del name
        yield repositories_for_connection(self.conn)

    async def run_news(self, name: str, fn: Any, *args: Any, operation_timeout_seconds: float, **kwargs: Any):
        del operation_timeout_seconds
        self.operations.append(name)
        return fn(*args, **kwargs)


class FailOnceWorkerDatabase(FakeWorkerDatabase):
    def __init__(self, conn: Any, *, fail_once: set[str]) -> None:
        super().__init__(conn)
        self.fail_once = set(fail_once)

    async def tx(self, name: str, fn: Any, *, timeout_seconds: float = 3.0) -> Any:
        if name in self.fail_once:
            self.fail_once.remove(name)
            raise TransientError(f"injected:{name}")
        return await super().tx(name, fn, timeout_seconds=timeout_seconds)


class TightCallbackWorkerDatabase(FakeWorkerDatabase):
    """Run every callback in a real 100 ms idle-timeout transaction and expose its dynamic extent."""

    def __init__(self, conn: Any) -> None:
        super().__init__(conn)
        self.inside_callback = False

    @contextmanager
    def worker_session(self, name: str, *_args: Any, **_kwargs: Any):
        del name
        with self.conn.transaction():
            self.conn.execute("SET LOCAL idle_in_transaction_session_timeout = '100ms'")
            self.inside_callback = True
            try:
                yield repositories_for_connection(self.conn)
            finally:
                self.inside_callback = False


class InlineFiniteOperations:
    async def run(self, _name: str, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        kwargs.pop("timeout_seconds", None)
        kwargs.pop("allow_shutdown", None)
        return await fn(*args, **kwargs) if asyncio.iscoroutinefunction(fn) else fn(*args, **kwargs)


@pytest.fixture(scope="module")
def conn(postgres_module_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
    yield connection
    connection.close()


def _raw_messages() -> list[BusMessage]:
    hits = json.loads(FIXTURE.read_text(encoding="utf-8"))
    stamp = now_ms()
    out: list[BusMessage] = []
    for hit in sorted(hits, key=lambda h: str(h.get("ts") or "")):
        strategy_id = str((hit.get("strategy") or {}).get("id") or "")
        out.append(
            BusMessage(
                kind="raw",
                message_id=f"raw:{hit.get('id')}",
                routing_key=RK_RAW_LIVE.format(strategy_id=strategy_id),
                payload={
                    "params": dict(hit),
                    "strategy_id": strategy_id,
                    "ingest_mode": "live",
                    "observed_at_ms": stamp,
                },
                trace_id=new_trace_id(),
                occurred_at_ms=stamp,
            )
        )
    return out


def _deduper(conn: Any, bus: FakeBus) -> DeduperConsumer:
    return DeduperConsumer(bus=bus, db=FakeWorkerDatabase(conn), watchlist_symbols=WATCHLIST)


def test_empty_instrument_catalog_is_cached_until_refresh(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    from tracefold.news.pipeline import admission

    stamp = now_ms()
    monkeypatch.setattr(admission, "now_ms", lambda: stamp)
    db = FakeWorkerDatabase(conn)
    deduper = DeduperConsumer(bus=FakeBus(), db=db, watchlist_symbols=WATCHLIST)
    message = _raw_messages()[0]
    assert repositories_for_connection(conn).instruments.instrument_classes() == {}
    conn.commit()

    async def exercise() -> None:
        nonlocal stamp
        await deduper.handle(message)
        await deduper.handle(message)
        assert db.operations.count("news_admission_instruments") == 1
        stamp += admission._INSTRUMENT_CACHE_TTL_MS
        await deduper.handle(message)
        assert db.operations.count("news_admission_instruments") == 2

    asyncio.run(exercise())


def test_deduper_wakes_semantic_work_for_every_admitted_event_and_redelivery_is_a_no_op(conn) -> None:
    bus = FakeBus()
    deduper = _deduper(conn, bus)
    messages = _raw_messages()

    async def scenario() -> None:
        for message in messages:
            await deduper.handle(message)

    asyncio.run(scenario())
    conn.commit()

    assert bus.published, "fixture must yield at least one candidate event"
    assert all(message.kind == "event" for message in bus.published)
    assert all(EVENT_ROUTING_KEY.match(key) for key in bus.routing_keys()), bus.routing_keys()
    woken = {str(message.payload["event_id"]) for message in bus.published}
    assert all(
        message.message_id == f"event:{message.payload['event_id']}:{message.payload['revision']}"
        for message in bus.published
    )
    high = [m for m in bus.published if m.routing_key.endswith(".high")]
    assert all(m.priority == 5 for m in high)
    assert all(m.priority == 0 for m in bus.published if m.routing_key.endswith(".normal"))

    rows = conn.execute(
        """
        SELECT e.event_id, e.admission, e.published_at_ms, e.dedupe_family, e.queue_priority,
               w.wanted_revision, w.done_revision, w.published_at_ms AS woken_at_ms
          FROM news_events e JOIN news_semantic_work w ON w.event_id = e.event_id
         WHERE e.event_id = ANY(%s)
        """,
        (sorted(woken),),
    ).fetchall()
    assert len(rows) == len(woken)
    # Both admitted admissions wake the semantic stage: `candidate` and `listing_deterministic` (#72).
    assert all(row["admission"] in ADMITTED_ADMISSIONS for row in rows)
    assert all(row["published_at_ms"] is not None and row["woken_at_ms"] is not None for row in rows)
    assert all(row["done_revision"] is None for row in rows)
    for row in rows:
        assert f"event.{row['dedupe_family']}.{row['queue_priority']}" in bus.routing_keys()
    unwoken_admitted = conn.execute(
        """
        SELECT count(*) AS n FROM news_events e
         WHERE e.admission = ANY(%s)
           AND NOT EXISTS (SELECT 1 FROM news_semantic_work w WHERE w.event_id = e.event_id)
        """,
        (sorted(ADMITTED_ADMISSIONS),),
    ).fetchone()["n"]
    assert unwoken_admitted == 0
    suppressed = conn.execute(
        """
        SELECT count(*) AS n FROM news_events e
         WHERE NOT (e.admission = ANY(%s))
           AND NOT EXISTS (SELECT 1 FROM news_semantic_work w WHERE w.event_id = e.event_id)
        """,
        (sorted(ADMITTED_ADMISSIONS),),
    ).fetchone()["n"]
    assert suppressed > 0  # suppressed admissions are stored but never wake semantics

    # Redelivery of every raw message is a no-op: same items, same events, same evidence, no new wake.
    def state() -> dict[str, Any]:
        return dict(
            conn.execute(
                """
                SELECT (SELECT count(*) FROM news_items) AS items,
                       (SELECT count(*) FROM news_events) AS events,
                       (SELECT count(*) FROM news_event_evidence_snapshots) AS snapshots,
                       (SELECT count(*) FROM news_item_revisions) AS revisions,
                       (SELECT coalesce(sum(wanted_revision), 0) FROM news_semantic_work) AS wanted
                """
            ).fetchone()
        )

    before = state()
    published_before = len(bus.published)
    asyncio.run(scenario())
    conn.commit()
    assert state() == before
    assert len(bus.published) == published_before


def test_recovery_raw_is_persisted_but_never_wakes_semantics_or_delivery(conn) -> None:
    original = _raw_messages()[0]
    params = {**dict(original.payload["params"]), "id": 9_187_001}
    recovery_message = BusMessage(
        kind="raw",
        message_id="raw:9187001",
        routing_key=RK_RAW_RECOVERY.format(strategy_id=original.payload["strategy_id"]),
        payload={**dict(original.payload), "params": params, "ingest_mode": "recovery"},
        trace_id="recovery-no-triage",
        occurred_at_ms=now_ms(),
    )
    bus = FakeBus()

    asyncio.run(_deduper(conn, bus).handle(recovery_message))
    conn.commit()

    rows = conn.execute(
        """
        SELECT DISTINCT e.event_id, e.admission, e.published_at_ms
          FROM news_events e
          JOIN news_event_members m ON m.event_id = e.event_id
          JOIN news_items i ON i.item_id = m.item_id
         WHERE i.source_item_key = '9187001'
        """
    ).fetchall()
    assert rows and all(row["admission"] == "recovery" and row["published_at_ms"] is None for row in rows)
    event_ids = [row["event_id"] for row in rows]
    downstream = conn.execute(
        """
        SELECT
          (SELECT count(*) FROM news_semantic_work WHERE event_id = ANY(%s)) AS semantic,
          (SELECT count(*) FROM news_deliveries WHERE event_id = ANY(%s)) AS deliveries
        """,
        (event_ids, event_ids),
    ).fetchone()
    assert downstream == {"semantic": 0, "deliveries": 0}
    assert bus.published == []


def test_admission_minhash_and_evidence_hash_run_outside_real_transactions(
    conn: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tracefold.news.pipeline import admission as admission_module

    original = _raw_messages()[0]
    params = {**dict(original.payload["params"]), "id": 9_187_002}
    message = BusMessage(
        kind="raw",
        message_id="raw:9187002",
        routing_key=original.routing_key,
        payload={**dict(original.payload), "params": params},
        trace_id="admission-transaction-boundary",
        occurred_at_ms=now_ms(),
    )
    boundary = TightCallbackWorkerDatabase(conn)
    bus = FakeBus()
    observed_inside_callback: list[bool] = []
    original_extract = admission_module.extract_fact_units
    original_minhash = admission_module.minhash_signature
    original_prepare_evidence = admission_module.prepare_evidence_snapshot

    def delayed_extract(*args: Any, **kwargs: Any) -> Any:
        observed_inside_callback.append(boundary.inside_callback)
        time.sleep(0.15)
        return original_extract(*args, **kwargs)

    def delayed_minhash(*args: Any, **kwargs: Any) -> Any:
        observed_inside_callback.append(boundary.inside_callback)
        time.sleep(0.15)
        return original_minhash(*args, **kwargs)

    def delayed_prepare_evidence(*args: Any, **kwargs: Any) -> Any:
        observed_inside_callback.append(boundary.inside_callback)
        time.sleep(0.15)
        return original_prepare_evidence(*args, **kwargs)

    monkeypatch.setattr(admission_module, "extract_fact_units", delayed_extract)
    monkeypatch.setattr(admission_module, "minhash_signature", delayed_minhash)
    monkeypatch.setattr(admission_module, "prepare_evidence_snapshot", delayed_prepare_evidence)
    deduper = DeduperConsumer(bus=bus, db=boundary, watchlist_symbols=WATCHLIST)

    asyncio.run(deduper.handle(message))
    conn.commit()

    assert observed_inside_callback and not any(observed_inside_callback)
    assert conn.execute("SELECT 1 FROM news_items WHERE source_item_key = '9187002'").fetchone()


def test_deduper_admits_an_unknown_strategy_and_rejects_missing_params(conn) -> None:
    """#126: no allowlist. A Strategy Tracefold has never seen is stored and gated like any other."""

    bus = FakeBus()
    deduper = _deduper(conn, bus)
    stamp = now_ms()
    foreign = BusMessage(
        kind="raw",
        message_id="raw:foreign",
        routing_key=RK_RAW_LIVE.format(strategy_id="9999"),
        payload={
            "params": {
                "id": 999_999_999,
                "engineType": "news",
                "text": "Some unconfigured strategy frame",
                "ts": stamp,
                "strategy": {"id": 9999, "name": "a Strategy the operator enabled provider-side"},
            },
            "strategy_id": "9999",
            "ingest_mode": "live",
            "observed_at_ms": stamp,
        },
        trace_id=new_trace_id(),
        occurred_at_ms=stamp,
    )
    malformed = BusMessage(
        kind="raw",
        message_id="raw:bad",
        routing_key="raw.opennews.1018",
        payload={},
        trace_id="t",
        occurred_at_ms=stamp,
    )

    async def scenario() -> None:
        await deduper.handle(foreign)
        with pytest.raises(PermanentError, match="news_raw_params_missing"):
            await deduper.handle(malformed)

    asyncio.run(scenario())
    conn.commit()
    # The frame became a real Item; the Gate, not a config list, decides what happens to it next.
    assert conn.execute("SELECT count(*) AS n FROM news_items WHERE source_item_key = '999999999'").fetchone()["n"] == 1
    strategies = conn.execute(
        "SELECT provider_metadata AS m FROM news_items WHERE source_item_key = '999999999'"
    ).fetchone()["m"]["strategies"]
    assert [row["id"] for row in strategies] == ["9999"]


def test_a_market_frame_that_matched_no_template_is_stored_raw_and_calls_no_model(
    conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#553. A provider format change is a fact about the frame, not a reason to lose it.

    The frame used to reach Triage, produce a pseudo verdict whose whole content was "this did not
    parse", and leave no ledger row. Now the Item is stored with its reason and the model is never
    consulted -- there is nothing here a model could answer.
    """

    stamp = now_ms()

    def unavailable_catalog(_self: Any) -> dict[str, str]:
        raise RuntimeError("catalog unavailable")

    monkeypatch.setattr(InstrumentsRepository, "instrument_classes", unavailable_catalog)
    bus = FakeBus()
    raw = BusMessage(
        kind="raw",
        message_id="raw:179-parse-failed",
        routing_key=RK_RAW_LIVE.format(strategy_id="1019"),
        payload={
            "params": {
                "id": 179_999_001,
                "engineType": "market",
                "text": "BTC OI provider format changed",
                "source": "binance",
                "ts": stamp,
                "strategy": {"id": 1019, "name": "OI Event Monitor", "sourceType": "market"},
            },
            "strategy_id": "1019",
            "ingest_mode": "live",
            "observed_at_ms": stamp,
        },
        trace_id=new_trace_id(),
        occurred_at_ms=stamp,
    )

    asyncio.run(_deduper(conn, bus).handle(raw))
    conn.commit()

    assert bus.published == []
    item = conn.execute(
        "SELECT item_id, market_kind, market_parse_status, market_parse_error, market_source_strategy_id"
        " FROM news_items WHERE source_item_key = '179999001'"
    ).fetchone()
    assert item is not None
    assert (item["market_kind"], item["market_parse_status"]) == ("oi", "raw")
    assert item["market_parse_error"] == "oi_template_unmatched"
    assert item["market_source_strategy_id"] == "1019"
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM news_oi_signals WHERE source_item_id = %s", (item["item_id"],)
        ).fetchone()["n"]
        == 0
    )
    # A market frame opens no Event, so it can want no semantic work.
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM news_semantic_work w JOIN news_event_members m ON m.event_id = w.event_id"
            " WHERE m.item_id = %s",
            (item["item_id"],),
        ).fetchone()["n"]
        == 0
    )

    news = repositories_for_connection(conn).news
    observed = conn.execute("SELECT observed_at_ms FROM news_items WHERE item_id = %s", (item["item_id"],)).fetchone()[
        "observed_at_ms"
    ]
    sources = {row["market_kind"]: row for row in news.market_sources(from_ms=observed, to_ms=observed + 1)}
    assert sources["oi"]["received"] >= 1
    assert sources["oi"]["raw"] >= 1
