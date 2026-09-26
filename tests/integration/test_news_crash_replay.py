"""The News failure windows, against real PostgreSQL and the production pipeline objects.

Every stage of the News pipeline has a moment between "the outside world has been told" and "the
database knows it" — a broker publish before its mark, an external send before its settlement, a
model answer before the evidence it was asked about is re-read. A process that dies inside one of
those windows is the ordinary case, not the exotic one, and what the pipeline must not do is either
lose the fact or produce it twice. That is a property of the durable rows, so it is checked here
against real rows rather than against a call sequence.

What is real and what is not. PostgreSQL is real, and every consumer is the production class wired
through the production `WorkerNewsDatabase` port, so the transaction boundaries under test are the
ones the composition root actually creates. The broker and the push sender are fakes — but only as
*fault injectors*: they exist to make a publish or a send fail at an exact instant, which a real
broker cannot be asked to do on cue. Nothing is asserted about how often a private method was
called; the assertions are durable rows, what reached the queue, and what a redelivery does.

The broker's own redelivery contract — that a `TransientError` returns the message and a
`PermanentError` dead-letters it — belongs to `test_news_bus_rabbitmq.py` and is not restated here.
A redelivery in this module is the consumer being handed the same message again, which is what the
broker does once that contract holds.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.app.workers.wiring.database import WorkerNewsDatabase
from tracefold.news.bus import (
    RK_RAW_LIVE,
    BrokerUnavailable,
    BusMessage,
    TransientError,
    new_trace_id,
    now_ms,
)
from tracefold.news.opennews import (
    _SNOWFLAKE_SHIFT,
    _X_SNOWFLAKE_EPOCH_MS,
    OpenNewsHistoryError,
    source_artifact_identity,
)
from tracefold.news.pipeline.admission import DeduperConsumer
from tracefold.news.pipeline.maintenance import JanitorLoop
from tracefold.news.pipeline.receiver import OpenNewsReceiver
from tracefold.news.pipeline.recovery import RecoveryRunner

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "news_v3_hits_sample.json"
WATCHLIST = frozenset({"BTC", "NVDA", "ETH"})


class RecordingBus:
    """Records publishes, and fails the exact ones a scenario asks it to."""

    def __init__(self) -> None:
        self.published: list[BusMessage] = []
        self.fail_kinds: set[str] = set()

    async def publish(self, message: BusMessage) -> None:
        if message.kind in self.fail_kinds:
            raise BrokerUnavailable(f"broker refused {message.kind}")
        self.published.append(message)

    def of_kind(self, kind: str) -> list[BusMessage]:
        return [message for message in self.published if message.kind == kind]


class FaultInjectingDatabase:
    """The production News database port over one test connection, with named transactions armable to fail.

    The names are the production operation names the pipeline passes to `db.tx`, so arming one
    reproduces the real window: the publish or the send has happened, and the write that records it
    is the thing that does not.
    """

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.fail_operations: set[str] = set()
        self.seen: list[str] = []
        self._port = WorkerNewsDatabase(self)

    async def read(self, name: str, fn: Any, *, timeout_seconds: float = 3.0) -> Any:
        return await self._port.read(name, fn, timeout_seconds=timeout_seconds)

    async def tx(self, name: str, fn: Any, *, timeout_seconds: float = 3.0) -> Any:
        self.seen.append(name)
        if name in self.fail_operations:
            raise TransientError(f"injected_fault:{name}")
        return await self._port.tx(name, fn, timeout_seconds=timeout_seconds)

    @contextmanager
    def worker_session(self, name: str, *_args: Any, **_kwargs: Any):
        del name
        yield repositories_for_connection(self.conn)

    async def run_news(self, name: str, fn: Any, *args: Any, operation_timeout_seconds: float, **kwargs: Any):
        del operation_timeout_seconds, name
        return fn(*args, **kwargs)


@pytest.fixture(scope="module")
def _module_connection(postgres_module_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
    yield connection
    connection.close()


@pytest.fixture
def conn(_module_connection: Any):
    """One private database for the module; each scenario starts from an empty News plane.

    Truncating three roots is enough: `news_events` references `news_items`, and every projection,
    verdict, delivery and asset row hangs off one of those, so `CASCADE` reaches all of them.
    """

    _module_connection.execute(
        "TRUNCATE news_items, news_opennews_incidents, news_event_evidence_snapshots RESTART IDENTITY CASCADE"
    )
    _module_connection.commit()
    return _module_connection


def _hits() -> list[dict[str, Any]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _one_hit() -> dict[str, Any]:
    """One frame the Gate admits: a scored, watchlist-grounded item rather than a suppressed one.

    Its provider timestamp is restamped to now. The recovery window is derived from the incident's
    own clock, so a fixture frame published in the past would fall outside every window this module
    opens and recovery would correctly recover nothing — which would make the scenario vacuous.
    """

    published = datetime.now(UTC)
    stamp = int(published.timestamp() * 1000)
    for hit in _hits():
        rating = hit.get("aiRating") or {}
        coins = hit.get("coins") or []
        if float(rating.get("score") or 0) >= 70 and coins:
            return {**hit, "ts": published.isoformat(), "link": _fresh_status_url(stamp)}
    raise AssertionError("fixture no longer contains an admissible frame")


def _fresh_status_url(published_at_ms: int) -> str:
    """An X status URL whose Snowflake really encodes `published_at_ms`.

    `source_age_s` is the gap between when the artifact was created and when the provider pushed it,
    and it is read straight out of the status id. Keeping the fixture's original 2026-08-18 link
    would make every frame here hours stale, which the v10 policy correctly withholds — a real rule
    firing on a fixture artifact rather than on anything these scenarios are about.
    """

    url = f"https://x.com/TheBlockCo/status/{(published_at_ms - _X_SNOWFLAKE_EPOCH_MS) << _SNOWFLAKE_SHIFT}"
    assert source_artifact_identity(url)[1] == published_at_ms, "the minted Snowflake must decode to its own instant"
    return url


def _raw_message(hit: dict[str, Any], *, ingest_mode: str = "live") -> BusMessage:
    strategy_id = str((hit.get("strategy") or {}).get("id") or "")
    stamp = now_ms()
    return BusMessage(
        kind="raw",
        message_id=f"raw:{hit.get('id')}",
        routing_key=RK_RAW_LIVE.format(strategy_id=strategy_id),
        payload={
            "params": dict(hit),
            "strategy_id": strategy_id,
            "ingest_mode": ingest_mode,
            "observed_at_ms": stamp,
        },
        trace_id=new_trace_id(),
        occurred_at_ms=stamp,
    )


def _deduper(db: FaultInjectingDatabase, bus: RecordingBus) -> DeduperConsumer:
    return DeduperConsumer(bus=bus, db=db, watchlist_symbols=WATCHLIST)


def _events(conn: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute("SELECT * FROM news_events ORDER BY opened_at_ms, event_id").fetchall()]


def _count(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> int:
    return int(conn.execute(sql, params).fetchone()["n"])


def _evidence_versions(conn: Any, event_id: str) -> list[int]:
    rows = conn.execute(
        "SELECT evidence_version FROM news_event_evidence_snapshots WHERE event_id = %s ORDER BY evidence_version",
        (event_id,),
    ).fetchall()
    return [int(row["evidence_version"]) for row in rows]


# --------------------------------------------------------------- Receiver: outage, incident, recovery


class _HistoryClient:
    """The official OpenNews history endpoint, returning the frames the outage lost.

    The page carries a second, older frame on purpose. Recovery only reports `recovered` once it has
    walked back past the start of the incident window; a page whose oldest entry is still inside the
    window means the backlog might continue on the next page, and the honest answer there is
    `partial`. Returning only the lost frame would exercise that branch instead of this one.
    """

    def __init__(self, hit: dict[str, Any], *, strategy_id: str, older: dict[str, Any]) -> None:
        self.hit = hit
        self.older = older
        self.strategy_id = strategy_id
        self.hits_calls = 0

    async def get_strategy_list(self, **_kwargs: Any) -> dict[str, Any]:
        return {"success": True, "data": [{"id": self.strategy_id, "name": "recovered", "enabled": True}]}

    async def get_strategy_hits(self, **kwargs: Any) -> dict[str, Any]:
        self.hits_calls += 1
        if int(kwargs.get("page") or 1) > 1:
            return {"success": True, "data": [], "page": 2, "limit": 100, "total": 2}
        return {"success": True, "data": [dict(self.hit), dict(self.older)], "page": 1, "limit": 100, "total": 2}


class _EmptyHistoryClient:
    def __init__(self, *, response_page_offset: int = 0) -> None:
        self.response_page_offset = response_page_offset

    async def get_strategy_list(self, **_kwargs: Any) -> dict[str, Any]:
        return {"success": True, "data": [{"id": 1018, "name": "empty", "enabled": True}]}

    async def get_strategy_hits(self, *, page: int, **_kwargs: Any) -> dict[str, Any]:
        return {
            "success": True,
            "data": [],
            "page": page + self.response_page_offset,
            "limit": 100,
            "usage": {},
        }


def _closed_recovery_incident(conn: Any) -> int:
    stamp = now_ms()
    repos = repositories_for_connection(conn)
    with repos.transaction():
        incident_id = repos.news.open_incident(
            cause_class="broker_unavailable",
            now_ms=stamp - 1_000,
        )
        assert repos.news.close_open_incidents(cause_classes=["broker_unavailable"], now_ms=stamp) == 1
    return incident_id


def _recovery_incident(conn: Any, incident_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM news_opennews_incidents WHERE incident_id = %s",
        (incident_id,),
    ).fetchone()
    assert row is not None
    return dict(row)


def test_official_empty_history_without_total_durably_recovers_the_incident(conn) -> None:
    incident_id = _closed_recovery_incident(conn)
    bus = RecordingBus()
    recovery = RecoveryRunner(
        bus=bus,
        db=FaultInjectingDatabase(conn),
        history_client=_EmptyHistoryClient(),
    )

    assert asyncio.run(recovery._recover_pending()) == "success"
    conn.commit()

    incident = _recovery_incident(conn, incident_id)
    assert (incident["recovery_status"], incident["recovered_count"], incident["last_error_code"]) == (
        "recovered",
        0,
        None,
    )
    assert bus.of_kind("raw") == []


def test_history_page_mismatch_durably_records_the_bounded_reason(conn) -> None:
    incident_id = _closed_recovery_incident(conn)
    recovery = RecoveryRunner(
        bus=RecordingBus(),
        db=FaultInjectingDatabase(conn),
        history_client=_EmptyHistoryClient(response_page_offset=1),
    )

    with pytest.raises(OpenNewsHistoryError, match=r"^opennews_history_payload_page_mismatch$"):
        asyncio.run(recovery._recover_pending())
    conn.commit()

    incident = _recovery_incident(conn, incident_id)
    assert (incident["recovery_status"], incident["last_error_code"]) == (
        "pending",
        "opennews_history_payload_page_mismatch",
    )


def test_history_hit_without_timestamp_stays_pending_with_the_bounded_reason(conn) -> None:
    incident_id = _closed_recovery_incident(conn)
    hit = {**_one_hit(), "ts": "invalid"}
    strategy_id = str((hit.get("strategy") or {}).get("id") or "")
    older = {
        **hit,
        "id": f"{hit['id']}-older",
        "ts": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
    }
    recovery = RecoveryRunner(
        bus=RecordingBus(),
        db=FaultInjectingDatabase(conn),
        history_client=_HistoryClient(hit, strategy_id=strategy_id, older=older),
    )

    with pytest.raises(OpenNewsHistoryError, match=r"^opennews_history_payload_hit_contract_invalid$"):
        asyncio.run(recovery._recover_pending())
    conn.commit()

    incident = _recovery_incident(conn, incident_id)
    assert (incident["recovery_status"], incident["last_error_code"]) == (
        "pending",
        "opennews_history_payload_hit_contract_invalid",
    )


def test_a_broker_outage_becomes_one_incident_that_official_recovery_settles_into_one_event(conn) -> None:
    """Receiver publish failure -> durable incident -> official recovery -> dedupe: one material Event.

    The frame the outage refused is not held in memory anywhere. What survives the window is the
    incident row, and the only thing that can turn it back into a fact is the provider's own history.
    The last step is the one that matters most: when the same frame later also arrives live, it must
    not become a second Event, because dedupe identity is not aware of which lane carried it.
    """

    hit = _one_hit()
    strategy_id = str((hit.get("strategy") or {}).get("id") or "")
    bus = RecordingBus()
    db = FaultInjectingDatabase(conn)
    receiver = OpenNewsReceiver(bus=bus, db=db, ws_client=None, recovery=None)

    bus.fail_kinds = {"raw"}
    asyncio.run(receiver._publish_frame({"params": dict(hit)}, strategy_id=strategy_id))
    conn.commit()

    assert bus.published == []
    incidents = [dict(row) for row in conn.execute("SELECT * FROM news_opennews_incidents").fetchall()]
    assert [row["cause_class"] for row in incidents] == ["broker_unavailable"]
    assert incidents[0]["closed_at_ms"] is None
    assert incidents[0]["recovery_status"] == "pending"
    assert _count(conn, "SELECT count(*) AS n FROM news_items") == 0

    # The broker comes back. The next successful frame closes the window, which is what makes the
    # incident visible to recovery: `pending_recovery_incidents` only selects closed ones.
    bus.fail_kinds = set()
    asyncio.run(receiver._publish_frame({"params": {**hit, "id": f"{hit['id']}-later"}}, strategy_id=strategy_id))
    conn.commit()
    closed = [dict(row) for row in conn.execute("SELECT * FROM news_opennews_incidents").fetchall()]
    assert closed[0]["closed_at_ms"] is not None

    older = {
        **hit,
        "id": f"{hit['id']}-before-the-window",
        "ts": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
    }
    history = _HistoryClient({**hit, "id": f" {hit['id']} "}, strategy_id=strategy_id, older=older)
    recovery = RecoveryRunner(bus=bus, db=db, history_client=history)
    asyncio.run(recovery._recover_pending())
    conn.commit()

    settled = [dict(row) for row in conn.execute("SELECT * FROM news_opennews_incidents").fetchall()]
    assert settled[0]["recovery_status"] == "recovered"
    assert settled[0]["recovered_count"] == 1
    recovered = [message for message in bus.of_kind("raw") if message.payload["ingest_mode"] == "recovery"]
    assert len(recovered) == 1

    deduper = _deduper(db, bus)
    asyncio.run(deduper.handle(recovered[0]))
    conn.commit()
    after_recovery = _events(conn)
    assert len(after_recovery) == 1

    # The same frame arriving live afterwards is the same material fact, not a second one.
    asyncio.run(deduper.handle(_raw_message(hit)))
    conn.commit()
    assert [row["event_id"] for row in _events(conn)] == [row["event_id"] for row in after_recovery]
    assert _count(conn, "SELECT count(*) AS n FROM news_items WHERE source_item_key = %s", (str(hit["id"]),)) == 1
    # The recovered Item is the artifact the outage lost, not a re-mint: its source identity is the
    # status the frame pointed at, and it decodes to the instant the frame claimed.
    artifact_id, artifact_published_at_ms = source_artifact_identity(str(hit["link"]))
    stored = conn.execute(
        "SELECT canonical_url FROM news_items WHERE source_item_key = %s", (str(hit["id"]),)
    ).fetchone()
    assert stored is not None
    assert source_artifact_identity(str(stored["canonical_url"])) == (artifact_id, artifact_published_at_ms)


def test_a_killed_receiver_becomes_a_process_outage_the_next_one_recovers_into_one_event(conn) -> None:
    """#425: the provider gap a killed Receiver leaves behind is recovered like any other incident.

    Nothing survives a fatal cancellation in the process that dies — the Workers root has already closed
    business admission, and a SIGKILL never reaches application code — so the durable row is the only
    handover. The successor reads `connected` still true, opens the interval at the last write that
    proves the old process was alive, and its own connection closes it. From there this is the ordinary
    path: official history refills the window, and a frame that also arrives live is the same fact.
    """

    hit = _one_hit()
    strategy_id = str((hit.get("strategy") or {}).get("id") or "")
    bus = RecordingBus()
    db = FaultInjectingDatabase(conn)

    # What the killed process left behind: connected, with its last write timestamped.
    repos = repositories_for_connection(conn)
    seeded = repos.news.ingest_liveness()
    assert seeded is not None
    alive_at_ms = int(seeded["updated_at_ms"])
    with repos.transaction():
        repos.news.update_ingest_state(now_ms=alive_at_ms, connected=True)
    conn.commit()

    successor = OpenNewsReceiver(bus=bus, db=db, ws_client=None, recovery=None)
    asyncio.run(successor._record_a_predecessor_that_never_reported_a_disconnect())
    conn.commit()

    opened = [dict(row) for row in conn.execute("SELECT * FROM news_opennews_incidents").fetchall()]
    assert [row["cause_class"] for row in opened] == ["process_outage"]
    assert opened[0]["opened_at_ms"] == alive_at_ms
    assert opened[0]["planned"] is False, "a kill is not a planned shutdown"
    assert opened[0]["closed_at_ms"] is None and opened[0]["recovery_status"] == "pending"

    # The new connection closes the window; that is what makes it visible to Recovery.
    asyncio.run(successor._connected())
    conn.commit()
    closed = [dict(row) for row in conn.execute("SELECT * FROM news_opennews_incidents").fetchall()]
    assert closed[0]["closed_at_ms"] is not None

    older = {
        **hit,
        "id": f"{hit['id']}-before-the-window",
        "ts": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
    }
    history = _HistoryClient({**hit, "id": f" {hit['id']} "}, strategy_id=strategy_id, older=older)
    recovery = RecoveryRunner(bus=bus, db=db, history_client=history)
    asyncio.run(recovery._recover_pending())
    conn.commit()

    settled = [dict(row) for row in conn.execute("SELECT * FROM news_opennews_incidents").fetchall()]
    assert settled[0]["recovery_status"] == "recovered" and settled[0]["recovered_count"] == 1
    recovered = [message for message in bus.of_kind("raw") if message.payload["ingest_mode"] == "recovery"]
    assert len(recovered) == 1

    deduper = _deduper(db, bus)
    asyncio.run(deduper.handle(recovered[0]))
    conn.commit()
    after_recovery = _events(conn)
    assert len(after_recovery) == 1

    # The same frame arriving live afterwards is the same material fact, not a second one.
    asyncio.run(deduper.handle(_raw_message(hit)))
    conn.commit()
    assert [row["event_id"] for row in _events(conn)] == [row["event_id"] for row in after_recovery]
    assert _count(conn, "SELECT count(*) AS n FROM news_items WHERE source_item_key = %s", (str(hit["id"]),)) == 1


class _QuietSocket:
    """The provider socket reduced to what the Receiver loop calls: it connects and then stays quiet."""

    def __init__(self) -> None:
        self.connected = 0
        self.closed = 0

    async def connect(self) -> None:
        self.connected += 1

    async def receive(self) -> Any:
        await asyncio.Event().wait()

    async def close(self) -> None:
        self.closed += 1


def test_a_cancelled_receiver_leaves_the_liveness_row_connected_and_writes_no_disconnect(conn) -> None:
    """#425: a killed Receiver's last durable word is `connected`, because it never gets another one.

    The Workers root closes business admission before it cancels this task, and a SIGKILL never
    reaches application code at all, so the dying process must not relabel its own death — a
    `planned_shutdown` row here would tell the successor there was no gap to recover. What the row
    has to say is nothing: still connected, at the last write the old process managed.
    """

    repos = repositories_for_connection(conn)
    seeded = repos.news.ingest_liveness()
    assert seeded is not None
    # A predecessor that did report its disconnect, so the successor's startup opens no window of its
    # own and every row below is one this process wrote.
    with repos.transaction():
        repos.news.update_ingest_state(now_ms=int(seeded["updated_at_ms"]) + 1, connected=False)
    conn.commit()

    db = FaultInjectingDatabase(conn)
    socket = _QuietSocket()
    receiver = OpenNewsReceiver(bus=RecordingBus(), db=db, ws_client=socket, recovery=None)

    async def killed() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(receiver.run(stop_event=stop))
        for _ in range(200):
            if socket.connected:
                break
            await asyncio.sleep(0.001)
        else:  # pragma: no cover - the loop connects on its first pass
            raise AssertionError("the receiver never reached its socket")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(killed())
    conn.commit()

    causes = [dict(row)["cause_class"] for row in conn.execute("SELECT * FROM news_opennews_incidents").fetchall()]
    assert causes == [], "a kill writes no disconnect of any kind, and least of all a planned one"
    liveness = repos.news.ingest_liveness()
    assert liveness is not None and liveness["connected"] is True, "the last durable word is still `connected`"


def test_a_predecessor_whose_clock_ran_ahead_cannot_open_an_outage_in_the_future(conn) -> None:
    """`updated_at_ms` only moves forward, so a fast predecessor leaves a timestamp this process has not reached.

    Trusting it would open an interval that this same process then closes *before* it began, and
    `news_opennews_incidents_check` refuses `closed_at_ms < opened_at_ms` — so the Receiver would die
    on the row it had just written, every single time it started.
    """

    repos = repositories_for_connection(conn)
    seeded = repos.news.ingest_liveness()
    assert seeded is not None
    ahead_ms = max(int(seeded["updated_at_ms"]), now_ms()) + 3_600_000
    with repos.transaction():
        repos.news.update_ingest_state(now_ms=ahead_ms, connected=True)
    conn.commit()
    assert int(repos.news.ingest_liveness()["updated_at_ms"]) == ahead_ms

    successor = OpenNewsReceiver(bus=RecordingBus(), db=FaultInjectingDatabase(conn), ws_client=None, recovery=None)
    before = now_ms()
    asyncio.run(successor._record_a_predecessor_that_never_reported_a_disconnect())
    conn.commit()

    opened = [dict(row) for row in conn.execute("SELECT * FROM news_opennews_incidents").fetchall()]
    assert [row["cause_class"] for row in opened] == ["process_outage"]
    assert before <= int(opened[0]["opened_at_ms"]) <= now_ms(), "an outage cannot have begun in the future"

    # The proof that the clamp is load-bearing: this same process can close what it opened.
    asyncio.run(successor._connected())
    conn.commit()
    closed = conn.execute("SELECT opened_at_ms, closed_at_ms FROM news_opennews_incidents").fetchone()
    assert closed["closed_at_ms"] is not None and int(closed["closed_at_ms"]) >= int(closed["opened_at_ms"])


# ------------------------------------------------------- Deduper: published to the broker, unmarked in the row


class _OneClaimAnalyzer:
    """The analyzer boundary with one grounded claim per input; no model."""

    identity = "crash-replay-analyzer-v1"
    judgments: Any = None

    async def extract(self, source: Any, budget: Any) -> Any:
        from tracefold.news.updates.contracts import Citation, ClaimFields, DraftClaim, Extraction

        evidence = source.evidence[0]
        quote = evidence.text.splitlines()[0]
        return Extraction(
            claims=(
                DraftClaim(
                    slot="a",
                    statement=quote,
                    fields=ClaimFields(subject="source", action="reports", mode="observation"),
                    citations=(Citation(evidence_ref=evidence.ref, quote=quote),),
                ),
            )
        )

    async def understand(
        self, source: Any, extracted: Any, budget: Any, *, rebase_only: bool = False, final_attempt: bool = True
    ) -> Any:
        return extracted


def _semantic_worker(db: FaultInjectingDatabase, bus: RecordingBus) -> Any:
    from tracefold.news.pipeline.semantic import SemanticWorker
    from tracefold.news.storage.event_update_store import PgNewsStore
    from tracefold.news.updates.service import NewsAgent

    store = PgNewsStore(db, watch_symbols=WATCHLIST)
    agent = NewsAgent(store, _OneClaimAnalyzer(), program_identity="crash-replay-program")  # type: ignore[arg-type]
    return SemanticWorker(
        bus=bus,
        db=db,
        store=store,
        agent=agent,
        concurrency=1,
        circuit_failures=3,
        circuit_open_seconds=60.0,
        program_identity="crash-replay-program",
    )


def test_a_mark_failure_after_a_successful_wake_is_re_woken_and_adopted_exactly_once(conn) -> None:
    """Wake publish success -> post-publish mark failure -> janitor re-wake -> one adopted EventUpdate.

    `publish_semantic_wake` is a commit-then-publish step and it suppresses the mark's own failure on
    purpose: the semantic work is already durable, and raising would only re-run the whole admission.
    The cost is a work row whose wake looks unrecorded while the message is in flight, so the janitor
    wakes it a second time -- and the durable answer must still be one Event and one adoption.
    """

    bus = RecordingBus()
    db = FaultInjectingDatabase(conn)
    db.fail_operations = {"news_semantic_wake_mark"}

    asyncio.run(_deduper(db, bus).handle(_raw_message(_one_hit())))
    conn.commit()

    events = _events(conn)
    assert len(events) == 1
    event_id = str(events[0]["event_id"])
    assert events[0]["published_at_ms"] is None, "the mark is exactly what the injected fault stopped"
    work = conn.execute("SELECT * FROM news_semantic_work WHERE event_id = %s", (event_id,)).fetchone()
    assert work["wanted_revision"] == 1 and work["published_at_ms"] is None
    assert [message.message_id for message in bus.of_kind("event")] == [f"event:{event_id}:1"]

    db.fail_operations = set()
    woken = asyncio.run(JanitorLoop(db=db, cold_db=db, bus=bus).repair_semantic_wakes())
    conn.commit()

    assert woken == 1
    assert [message.message_id for message in bus.of_kind("event")] == [f"event:{event_id}:1"] * 2
    assert dict(_events(conn)[0])["published_at_ms"] is not None

    # Both wakes reach the semantic worker. The second finds the revision done and runs no turn.
    worker = _semantic_worker(db, bus)
    for message in bus.of_kind("event"):
        asyncio.run(worker.handle(message))
    conn.commit()

    assert _count(conn, "SELECT count(*) AS n FROM news_event_updates WHERE event_id = %s", (event_id,)) == 1
    assert _count(conn, "SELECT count(*) AS n FROM news_semantic_observations WHERE event_id = %s", (event_id,)) == 1
    work = conn.execute("SELECT * FROM news_semantic_work WHERE event_id = %s", (event_id,)).fetchone()
    assert work["done_revision"] == 1 and work["last_outcome"] == "adopted"
    assert _count(conn, "SELECT count(*) AS n FROM news_verdicts WHERE event_id = %s", (event_id,)) == 0
    assert len(_events(conn)) == 1
