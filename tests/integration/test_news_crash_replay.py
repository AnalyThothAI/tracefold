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
import inspect
import json
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_judgment import semantic_judgment, triage_verdict
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
from tracefold.news.models import TRIAGE_POLICY_VERSION
from tracefold.news.opennews import (
    _SNOWFLAKE_SHIFT,
    _X_SNOWFLAKE_EPOCH_MS,
    OpenNewsHistoryError,
    source_artifact_identity,
)
from tracefold.news.pipeline.admission import DeduperConsumer
from tracefold.news.pipeline.delivery import DELIVERY_ATTEMPTS_MAX, DELIVERY_RETRY_DELAY_MS, DelivererLoop
from tracefold.news.pipeline.maintenance import JanitorLoop
from tracefold.news.pipeline.receiver import OpenNewsReceiver
from tracefold.news.pipeline.recovery import RecoveryRunner
from tracefold.news.pipeline.triage import TriageConsumer
from tracefold.news.program.artifact import load_stable_program_artifact
from tracefold.news.program.runtime import PROGRAM_VERSION

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "news_v3_hits_sample.json"
WATCHLIST = frozenset({"BTC", "NVDA", "ETH"})
PROGRAM_SHA256 = load_stable_program_artifact().program_sha256


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


class InlineFiniteOperations:
    async def run(self, _name: str, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        kwargs.pop("timeout_seconds", None)
        kwargs.pop("allow_shutdown", None)
        return await fn(*args, **kwargs) if inspect.iscoroutinefunction(fn) else fn(*args, **kwargs)


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


def _triage(db: FaultInjectingDatabase, bus: RecordingBus, *, judge: Any = None) -> TriageConsumer:
    return TriageConsumer(
        bus=bus,
        db=db,
        judge=judge,
        program_version=PROGRAM_VERSION,
        program_sha256=PROGRAM_SHA256,
        watchlist_symbols=WATCHLIST,
        watchlist=sorted(WATCHLIST),
        concurrency=1,
        circuit_failures=3,
        circuit_open_seconds=60.0,
        runtime_manifest={"manifest_sha": "e" * 64},
    )


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


def test_a_mark_failure_after_a_successful_event_publish_leaves_one_event_and_one_verdict(conn) -> None:
    """Event publish success -> post-publish mark failure -> janitor redelivery -> one current Verdict.

    `publish_event` is a commit-then-publish outbox step and it suppresses the mark's own failure on
    purpose, because the Event has already reached Triage and raising would only re-run the whole
    admission. The cost of that choice is a row that says `published_at_ms IS NULL` when the message
    is already in flight, so the janitor will send it a second time — and the durable answer must
    still be exactly one Event and exactly one current Verdict.
    """

    bus = RecordingBus()
    db = FaultInjectingDatabase(conn)
    db.fail_operations = {"news_event_mark_published"}

    asyncio.run(_deduper(db, bus).handle(_raw_message(_one_hit())))
    conn.commit()

    events = _events(conn)
    assert len(events) == 1
    event_id = str(events[0]["event_id"])
    assert events[0]["published_at_ms"] is None, "the mark is exactly what the injected fault stopped"
    assert [message.payload["event_id"] for message in bus.of_kind("event")] == [event_id]

    # The janitor re-publishes anything the outbox still believes never left.
    db.fail_operations = set()
    conn.execute(
        "UPDATE news_events SET opened_at_ms = opened_at_ms - 60000, created_at_ms = created_at_ms - 60000"
        " WHERE event_id = %s",
        (event_id,),
    )
    conn.commit()
    janitor = JanitorLoop(db=db, cold_db=db, bus=bus)
    republished = asyncio.run(janitor.repair_event_handoffs())
    conn.commit()

    assert republished == 1
    assert [message.payload["event_id"] for message in bus.of_kind("event")] == [event_id, event_id]
    assert dict(_events(conn)[0])["published_at_ms"] is not None

    # Both copies reach Triage. The second must find the settled verdict rather than judge again.
    triage = _triage(db, bus)
    for message in bus.of_kind("event"):
        asyncio.run(triage.handle(message))
    conn.commit()

    verdicts = conn.execute(
        "SELECT * FROM news_verdicts WHERE event_id = %s AND stage = 'triage' AND policy_version = %s",
        (event_id, TRIAGE_POLICY_VERSION),
    ).fetchall()
    assert len(verdicts) == 1
    assert len(_events(conn)) == 1


# ------------------------------------------------------------------- Deliverer: begin, send, settle


class _RecordingSender:
    """A push sender that can fail, and that always records what it was asked to send."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail_send = False

    def prepare(self) -> None:
        return None

    def send_card(self, card: Any, *, channel_payload: Mapping[str, Any], **_kwargs: Any) -> dict[str, Any]:
        del card
        self.sent.append(dict(channel_payload))
        if self.fail_send:
            raise RuntimeError("provider refused the send")
        return {"provider": "test", "message_id": len(self.sent), "pushed_at_ms": now_ms()}

    def close(self) -> None:
        return None


def _pushable_event(conn: Any, db: FaultInjectingDatabase, bus: RecordingBus) -> str:
    """Drive a real frame through admission and the current model/policy path to one delivering verdict.

    Everything the Deliverer reads — the Event card, its admission routing, the delivery timing, the
    latest triage verdict and its DecisionResult — is written by the production consumers here.
    """

    deduper = _deduper(db, bus)
    asyncio.run(deduper.handle(_raw_message(_one_hit())))
    conn.commit()
    event_id = str(_events(conn)[0]["event_id"])
    judge = _EvidenceMovingJudge(deduper, [])
    for message in bus.of_kind("event"):
        asyncio.run(_triage(db, bus, judge=judge).handle(message))
    conn.commit()
    verdict = conn.execute(
        "SELECT final_decision FROM news_verdicts WHERE event_id = %s AND stage = 'triage'",
        (event_id,),
    ).fetchone()
    assert verdict is not None and verdict["final_decision"] == "push"
    return event_id


def test_a_crash_between_begin_and_settle_is_durably_ambiguous_and_never_resends(conn) -> None:
    """Begin-send-settle: a known-unsent card may retry; an unknown remote outcome may not.

    These are different facts and the reader is on the other side of the difference. A send that
    never happened costs nothing to retry. A send whose outcome the process never learned may
    already be on someone's screen, so the only safe answer is a durable `terminal` marked
    ambiguous — never a second initial send.
    """

    bus = RecordingBus()
    db = FaultInjectingDatabase(conn)
    sender = _RecordingSender()
    event_id = _pushable_event(conn, db, bus)
    repos = repositories_for_connection(conn)

    # Known unsent: `begin_delivery` claimed the row, nothing was sent, and the process died.
    with repos.transaction():
        assert repos.news.begin_delivery(event_id=event_id, kind="first", card={"x": 1}, now_ms=now_ms()) == "new"
    conn.commit()
    assert sender.sent == []

    deliverer = DelivererLoop(
        db=db,
        sender=sender,
        finite_operations=InlineFiniteOperations(),
        min_interval_seconds=0.0,
    )
    asyncio.run(deliverer.deliver(event_id=event_id, kind="first"))
    conn.commit()

    delivery = repos.news.delivery(event_id=event_id, kind="first")
    assert delivery is not None
    assert (delivery["state"], delivery["error_code"]) == ("terminal", "ambiguous_after_crash")
    assert sender.sent == [], "an interrupted delivery is never re-sent, because its outcome is unknown"

    rows = conn.execute("SELECT count(*) AS n FROM news_deliveries WHERE event_id = %s", (event_id,)).fetchone()
    assert int(rows["n"]) == 1


def test_a_settlement_failure_after_a_successful_send_never_produces_a_second_send(conn) -> None:
    """Verdict delivered, settlement lost: the redelivery must not put a second card on the screen."""

    bus = RecordingBus()
    db = FaultInjectingDatabase(conn)
    sender = _RecordingSender()
    event_id = _pushable_event(conn, db, bus)
    deliverer = DelivererLoop(
        db=db,
        sender=sender,
        finite_operations=InlineFiniteOperations(),
        min_interval_seconds=0.0,
    )

    db.fail_operations = {"news_delivery_settle"}
    with pytest.raises(RuntimeError, match="news_delivery_settlement_unavailable"):
        asyncio.run(deliverer.deliver(event_id=event_id, kind="first"))
    conn.commit()

    assert len(sender.sent) == 1, "the card really did reach the provider"
    repos = repositories_for_connection(conn)
    stranded = repos.news.delivery(event_id=event_id, kind="first")
    assert stranded is not None and stranded["state"] == "sending"

    # Re-claim: the row still says `sending`, so this is the unknown-outcome case, not a retry.
    db.fail_operations = set()
    asyncio.run(deliverer.deliver(event_id=event_id, kind="first"))
    conn.commit()

    settled = repos.news.delivery(event_id=event_id, kind="first")
    assert settled is not None
    assert (settled["state"], settled["error_code"]) == ("terminal", "ambiguous_after_crash")
    assert len(sender.sent) == 1
    assert _count(conn, "SELECT count(*) AS n FROM news_deliveries WHERE event_id = %s", (event_id,)) == 1


# ------------------------------------------------------- Triage: evidence that moves while the model thinks


class _EvidenceMovingJudge:
    """A model seam that lets a stronger member land while it is thinking, on every ask.

    This is the production cause, not a synthetic one: a second outlet reporting the same fact joins
    the Event as a new member, which appends a new immutable evidence version. Doing it from inside
    `judge()` puts the change exactly where the race is — after the model read the evidence and
    before the persist step re-reads it.
    """

    def __init__(self, deduper: DeduperConsumer, frames: list[dict[str, Any]]) -> None:
        self.deduper = deduper
        self.frames = list(frames)
        self.asks = 0

    async def judge(self, _context: Any) -> Any:
        self.asks += 1
        if self.frames:
            await self.deduper.handle(_raw_message(self.frames.pop(0)))
        return semantic_judgment(triage_verdict(), program_version=PROGRAM_VERSION, program_sha256=PROGRAM_SHA256)


def test_a_second_evidence_change_refuses_to_bind_the_stale_judgment(conn) -> None:
    """Evidence change -> one re-ask -> a second change: no judgment is written over evidence it never read.

    One re-ask is deliberate and bounded. A judgment produced against evidence v1 may be discarded
    and asked again against v2, because nothing durable has been written yet. What may not happen is
    the second one: a judgment produced against v2, landing on v3, would be a verdict whose stated
    inputs are not the inputs it saw. The pipeline's answer is to write nothing and raise a
    `TransientError`, which returns the message to the durable retry lane instead.
    """

    bus = RecordingBus()
    db = FaultInjectingDatabase(conn)
    first = _one_hit()
    stronger = [
        {**first, "id": int(first["id"]) + offset, "source": outlet, "link": f"https://x.com/{outlet}/status/{offset}"}
        for offset, outlet in ((1, "SecondOutlet"), (2, "ThirdOutlet"))
    ]

    asyncio.run(_deduper(db, bus).handle(_raw_message(first)))
    conn.commit()
    event_id = str(_events(conn)[0]["event_id"])
    assert _evidence_versions(conn, event_id) == [1]

    judge = _EvidenceMovingJudge(_deduper(db, bus), stronger)
    triage = _triage(db, bus, judge=judge)

    with pytest.raises(TransientError, match="news_event_evidence_changed"):
        asyncio.run(triage.handle(bus.of_kind("event")[0]))
    conn.commit()

    assert judge.asks == 2, "exactly one re-ask: the first change is retried, the second is not"
    assert _evidence_versions(conn, event_id) == [1, 2, 3]
    assert _count(conn, "SELECT count(*) AS n FROM news_verdicts WHERE event_id = %s", (event_id,)) == 0
    assert bus.of_kind("verdict") == []

    # The retry lane hands the same message back once the evidence has settled. Now it is judged.
    settled_judge = _EvidenceMovingJudge(_deduper(db, bus), [])
    asyncio.run(_triage(db, bus, judge=settled_judge).handle(bus.of_kind("event")[0]))
    conn.commit()

    assert settled_judge.asks == 1
    verdicts = conn.execute(
        "SELECT final_decision, evidence_version FROM news_verdicts WHERE event_id = %s AND stage = 'triage'",
        (event_id,),
    ).fetchall()
    assert len(verdicts) == 1
    assert int(verdicts[0]["evidence_version"]) == 3, "the verdict names the evidence it actually read"


def _second_admissible_hit() -> dict[str, Any]:
    """A second scored, grounded frame whose text is unrelated to `_one_hit()`, so it opens its own Event."""

    first_id = str(_one_hit()["id"])
    stamp = now_ms()
    for hit in _hits():
        if str(hit["id"]) == first_id:
            continue
        rating = hit.get("aiRating") or {}
        if float(rating.get("score") or 0) >= 70 and (hit.get("coins") or []):
            return {**hit, "ts": datetime.now(UTC).isoformat(), "link": _fresh_status_url(stamp)}
    raise AssertionError("fixture no longer contains a second admissible frame")


class _CardLandingDatabase(FaultInjectingDatabase):
    """Another process settles a card on its own connection, after the refresh and before the lock.

    The window this reproduces is narrower than the one a re-read can see: Triage refreshes the
    reader ledger outside any transaction, and only then opens the persist transaction and takes the
    storyline lock. A card that commits between those two moments is invisible to the refresh and
    visible inside the lock, which is exactly why the locked step re-reads the ledger revision
    instead of trusting the snapshot it arrived with.
    """

    def __init__(self, conn: Any, *, delivered_event_id: str) -> None:
        super().__init__(conn)
        self.delivered_event_id = delivered_event_id
        self.armed = True

    async def tx(self, name: str, fn: Any, *, timeout_seconds: float = 3.0) -> Any:
        if name == "news_triage_persist" and self.armed:
            self.armed = False
            self._settle_on_another_connection()
        return await super().tx(name, fn, timeout_seconds=timeout_seconds)

    def _settle_on_another_connection(self) -> None:
        other = connect_postgres_test(read_only=False)
        try:
            repos = repositories_for_connection(other)
            stamp = now_ms()
            with repos.transaction():
                assert (
                    repos.news.begin_delivery(
                        event_id=self.delivered_event_id,
                        kind="first",
                        card={"headline_zh": "另一条已推送的卡片"},
                        now_ms=stamp,
                    )
                    == "new"
                )
                assert repos.news.settle_delivery(
                    event_id=self.delivered_event_id,
                    kind="first",
                    state="sent",
                    receipt={"provider": "test", "message_id": 1, "pushed_at_ms": stamp},
                    error_code=None,
                    now_ms=stamp,
                )
            other.commit()
        finally:
            other.close()


def test_a_card_that_lands_after_the_refresh_is_seen_inside_the_storyline_lock(conn) -> None:
    """A push committed between the ledger refresh and the lock must not be judged around.

    The evidence race above has a sibling on the other input the model is shown: the cards the
    reader already received. `reader_history_revision` is a compare-and-swap token over the settled
    deliveries, re-read inside the persist transaction under `lock_storyline`, and the writer that
    moves it here is a genuinely separate connection whose commit PostgreSQL — not a script — makes
    visible. Losing that CAS costs one bounded re-ask, and the verdict that lands names the ledger it
    actually read.
    """

    bus = RecordingBus()
    db = FaultInjectingDatabase(conn)
    delivered_event_id = _pushable_event(conn, db, bus)

    asyncio.run(_deduper(db, bus).handle(_raw_message(_second_admissible_hit())))
    conn.commit()
    judged = [message for message in bus.of_kind("event") if message.payload["event_id"] != delivered_event_id]
    assert len(judged) == 1, "the second frame has to open its own Event, not join the first"
    event_id = str(judged[0].payload["event_id"])

    racing_db = _CardLandingDatabase(conn, delivered_event_id=delivered_event_id)
    judge = _EvidenceMovingJudge(_deduper(racing_db, bus), [])
    asyncio.run(_triage(racing_db, bus, judge=judge).handle(judged[0]))
    conn.commit()

    assert racing_db.armed is False, "the card has to land while the judgment is being persisted"
    assert judge.asks == 2, "the lost CAS buys exactly one re-ask"
    verdicts = conn.execute(
        "SELECT trace FROM news_verdicts WHERE event_id = %s AND stage = 'triage'", (event_id,)
    ).fetchall()
    assert len(verdicts) == 1, "the stale round writes nothing"
    assert verdicts[0]["trace"]["reasked_after_told_change"] is True
    settled = _count(
        conn,
        "SELECT count(*) AS n FROM news_deliveries WHERE kind = 'first' AND state = 'sent'",
    )
    assert settled == 1, "the racing card is a durable row, not a scripted return value"


def test_a_redelivered_triage_message_owes_one_card_and_the_loop_sends_it_once(conn) -> None:
    """Verdict and queue row commit together, so a redelivered Event adds no second card.

    This is the window `publish_verdict` could not close: it committed the verdict in one transaction
    and published the handoff in another, and the mark that recorded the publish could fail on its
    own, leaving a settled verdict the row still called unpublished. A redelivered Event then
    republished the decision and two identical messages reached the Deliverer. The handoff is now a
    `news_delivery_queue` row inside the verdict's own transaction: there is no second write to fail,
    the redelivery finds the verdict and stops, and the loop claims exactly one intent (#598 D2).
    """

    bus = RecordingBus()
    db = FaultInjectingDatabase(conn)
    sender = _RecordingSender()

    asyncio.run(_deduper(db, bus).handle(_raw_message(_one_hit())))
    conn.commit()
    event_id = str(_events(conn)[0]["event_id"])
    event_message = bus.of_kind("event")[0]

    judge = _EvidenceMovingJudge(_deduper(db, bus), [])
    asyncio.run(_triage(db, bus, judge=judge).handle(event_message))
    conn.commit()

    verdict_row = conn.execute(
        "SELECT final_decision, published_at_ms FROM news_verdicts WHERE event_id = %s AND stage = 'triage'",
        (event_id,),
    ).fetchone()
    assert verdict_row["final_decision"] in {"push", "escalate"}
    assert verdict_row["published_at_ms"] is not None, "the marker is written in the verdict's own transaction"
    assert bus.of_kind("verdict") == [], "the handoff is a row, not a message"
    assert _queue_rows(conn, event_id) == [{"kind": "first", "state": "pending", "attempts": 0, "error_code": None}]

    # Redelivery of the Event: the verdict is already settled, so nothing at all happens.
    asyncio.run(_triage(db, bus, judge=judge).handle(event_message))
    conn.commit()

    assert judge.asks == 1, "the second pass must not ask the model again"
    assert _count(conn, "SELECT count(*) AS n FROM news_verdicts WHERE event_id = %s", (event_id,)) == 1
    assert _queue_rows(conn, event_id) == [{"kind": "first", "state": "pending", "attempts": 0, "error_code": None}]

    deliverer = DelivererLoop(
        db=db,
        sender=sender,
        finite_operations=InlineFiniteOperations(),
        min_interval_seconds=0.0,
    )
    asyncio.run(deliverer.advance())
    asyncio.run(deliverer.advance())
    conn.commit()

    assert len(sender.sent) == 1, "one intent, one card on the reader's screen"
    assert _queue_rows(conn, event_id) == [], "a delivered card is no longer owed"
    deliveries = [
        dict(row)
        for row in conn.execute(
            "SELECT kind, state, error_code FROM news_deliveries WHERE event_id = %s", (event_id,)
        ).fetchall()
    ]
    assert deliveries == [{"kind": "first", "state": "sent", "error_code": None}]


def test_two_delivery_loops_claim_disjoint_intents_and_the_reader_gets_one_card(conn) -> None:
    """Two claimers over one due intent: `SKIP LOCKED` gives it to one of them and no card is doubled.

    The old lane rented this from RabbitMQ's `x-single-active-consumer`. It is now the claim's own
    property, and `news_deliveries`'s `ON CONFLICT (event_id, kind)` is still the authority underneath
    it: even a claimer that somehow held the same row could not write a second ledger row (#598 D2).
    """

    bus = RecordingBus()
    db = FaultInjectingDatabase(conn)
    sender = _RecordingSender()
    event_id = _pushable_event(conn, db, bus)
    repos = repositories_for_connection(conn)
    stamp = now_ms()

    # A second connection is what makes this a race rather than two calls in one session.
    other = connect_postgres_test(read_only=False)
    try:
        first = repos.news.claim_due_deliveries(
            now_ms=stamp,
            next_attempt_at_ms=stamp + DELIVERY_RETRY_DELAY_MS,
            attempts_max=DELIVERY_ATTEMPTS_MAX,
            limit=5,
        )
        with other.transaction():
            second = repositories_for_connection(other).news.claim_due_deliveries(
                now_ms=stamp,
                next_attempt_at_ms=stamp + DELIVERY_RETRY_DELAY_MS,
                attempts_max=DELIVERY_ATTEMPTS_MAX,
                limit=5,
            )
        conn.commit()
    finally:
        other.close()

    assert [row["event_id"] for row in first] == [event_id]
    assert second == [], "the row the first claimer holds is skipped, never waited on"

    deliverer = DelivererLoop(
        db=db, sender=sender, finite_operations=InlineFiniteOperations(), min_interval_seconds=0.0
    )
    asyncio.run(deliverer.deliver(event_id=event_id, kind="first"))
    asyncio.run(deliverer.deliver(event_id=event_id, kind="first"))
    conn.commit()

    assert len(sender.sent) == 1
    assert _count(conn, "SELECT count(*) AS n FROM news_deliveries WHERE event_id = %s", (event_id,)) == 1


def test_a_crash_between_the_claim_and_the_ledger_row_leaves_the_card_claimable(conn) -> None:
    """A claimed intent nobody finished comes back when its lease expires, and gives up after three.

    The broker used to redeliver an unacked message and dead-letter it once `delivery-limit` was
    spent. Those are the same two numbers here -- three attempts, 30 s apart -- and they are now due
    times and an attempt count in PostgreSQL, so the process that died is not part of the mechanism.
    """

    bus = RecordingBus()
    db = FaultInjectingDatabase(conn)
    event_id = _pushable_event(conn, db, bus)
    repos = repositories_for_connection(conn)
    stamp = now_ms()

    def _claim(at_ms: int) -> list[dict[str, Any]]:
        claimed = repos.news.claim_due_deliveries(
            now_ms=at_ms,
            next_attempt_at_ms=at_ms + DELIVERY_RETRY_DELAY_MS,
            attempts_max=DELIVERY_ATTEMPTS_MAX,
            limit=5,
        )
        conn.commit()
        return claimed

    # First attempt: claimed, then the process dies before `begin_delivery`. No ledger row exists.
    assert [row["attempts"] for row in _claim(stamp)] == [1]
    assert _count(conn, "SELECT count(*) AS n FROM news_deliveries WHERE event_id = %s", (event_id,)) == 0
    assert _claim(stamp + DELIVERY_RETRY_DELAY_MS - 1) == [], "the lease is the wait, and it has not run out"

    # Second and third attempts arrive when the lease does, and the third is the last.
    assert [row["attempts"] for row in _claim(stamp + DELIVERY_RETRY_DELAY_MS)] == [2]
    assert [row["attempts"] for row in _claim(stamp + 2 * DELIVERY_RETRY_DELAY_MS)] == [DELIVERY_ATTEMPTS_MAX]

    # A fourth is not granted: the intent becomes this lane's dead letter, kept where it can be read.
    assert _claim(stamp + 3 * DELIVERY_RETRY_DELAY_MS) == []
    assert _queue_rows(conn, event_id) == [
        {
            "kind": "first",
            "state": "dead",
            "attempts": DELIVERY_ATTEMPTS_MAX,
            "error_code": "news_delivery_attempts_exhausted",
        }
    ]
    assert _count(conn, "SELECT count(*) AS n FROM news_deliveries WHERE event_id = %s", (event_id,)) == 0


def test_a_deferred_delivery_keeps_its_intent_and_gives_up_with_the_reason_recorded(conn) -> None:
    """A News lane that cannot admit the read spends an attempt, records why, and retries on the lease."""

    bus = RecordingBus()
    db = FaultInjectingDatabase(conn)
    sender = _RecordingSender()
    event_id = _pushable_event(conn, db, bus)
    deliverer = DelivererLoop(
        db=db, sender=sender, finite_operations=InlineFiniteOperations(), min_interval_seconds=0.0
    )

    db.fail_operations = {"news_delivery_begin"}
    for _ in range(DELIVERY_ATTEMPTS_MAX):
        asyncio.run(deliverer.advance())
        conn.commit()
        # Each turn claims at most one attempt: the lease holds the row until it is due again.
        conn.execute("UPDATE news_delivery_queue SET next_attempt_at_ms = 0 WHERE state = 'pending'")
        conn.commit()

    assert sender.sent == []
    assert _queue_rows(conn, event_id) == [
        {
            "kind": "first",
            "state": "dead",
            "attempts": DELIVERY_ATTEMPTS_MAX,
            "error_code": "news_delivery_deferred:TransientError",
        }
    ]

    db.fail_operations = set()
    asyncio.run(deliverer.advance())
    conn.commit()
    assert sender.sent == [], "a dead intent is never claimed again"


def _queue_rows(conn: Any, event_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT kind, state, attempts, error_code FROM news_delivery_queue WHERE event_id = %s ORDER BY kind",
            (event_id,),
        ).fetchall()
    ]
