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
    BusMessage,
    TransientError,
    new_trace_id,
    now_ms,
)
from tracefold.news.models import TRIAGE_POLICY_VERSION
from tracefold.news.opennews import _SNOWFLAKE_SHIFT, _X_SNOWFLAKE_EPOCH_MS, source_artifact_identity
from tracefold.news.pipeline.admission import DeduperConsumer
from tracefold.news.pipeline.delivery import DelivererConsumer
from tracefold.news.pipeline.maintenance import JanitorLoop
from tracefold.news.pipeline.receiver import OpenNewsReceiver
from tracefold.news.pipeline.recovery import RecoveryRunner
from tracefold.news.pipeline.triage import TriageConsumer

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "news_v3_hits_sample.json"
WATCHLIST = frozenset({"BTC", "NVDA", "ETH"})
PROGRAM_VERSION = "news_semantic_program_crash_replay_v1"
PROGRAM_SHA256 = "7" * 64


class BrokerUnavailable(RuntimeError):
    """Named to match what the Receiver classifies: anything that is not backpressure is unavailability."""


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


def test_a_broker_outage_becomes_one_incident_that_official_recovery_settles_into_one_event(conn) -> None:
    """Receiver publish failure -> durable incident -> official recovery -> dedupe: one material Event.

    The frame the outage refused is not held in memory anywhere. What survives the window is the
    incident row, and the only thing that can turn it back into a fact is the provider's own history.
    The last step is the one that matters most: when the same frame later also arrives live, it must
    not become a second Event, because dedupe identity is not aware of which lane carried it.
    """

    hit = _one_hit()
    strategy_id = str((hit.get("strategy") or {}).get("id") or "")
    published_at_ms = int(_raw_message(hit).payload["observed_at_ms"])
    bus = RecordingBus()
    db = FaultInjectingDatabase(conn)
    receiver = OpenNewsReceiver(bus=bus, db=db, ws_client=None, history_client=None, recovery=None)

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
    history = _HistoryClient(hit, strategy_id=strategy_id, older=older)
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
    assert published_at_ms > 0


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
    republished = asyncio.run(janitor.republish_unpublished())
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

    def send_card(self, card: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        self.sent.append(dict(card))
        if self.fail_send:
            raise RuntimeError("provider refused the send")
        return {"provider": "test", "message_id": len(self.sent), "pushed_at_ms": now_ms()}

    def close(self) -> None:
        return None


def _pushable_event(conn: Any, db: FaultInjectingDatabase, bus: RecordingBus) -> str:
    """Drive a real frame through admission and Triage, then promote its verdict to a delivering one.

    Everything the Deliverer reads — the Event card, its admission routing, the delivery timing, the
    latest triage verdict — is written by the production consumers here. Only the decision itself is
    forced: with no model configured Triage is fail-closed and reaches `drop`, and this module is
    about the delivery window rather than about what a model would have said.
    """

    asyncio.run(_deduper(db, bus).handle(_raw_message(_one_hit())))
    conn.commit()
    event_id = str(_events(conn)[0]["event_id"])
    for message in bus.of_kind("event"):
        asyncio.run(_triage(db, bus).handle(message))
    conn.commit()
    updated = conn.execute(
        "UPDATE news_verdicts SET final_decision = 'push' WHERE event_id = %s AND stage = 'triage'",
        (event_id,),
    )
    conn.commit()
    assert int(getattr(updated, "rowcount", 0) or 0) == 1, "Triage must have written exactly one verdict to promote"
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

    deliverer = DelivererConsumer(
        bus=bus,
        db=db,
        sender=sender,
        finite_operations=InlineFiniteOperations(),
        min_interval_seconds=0.0,
    )
    asyncio.run(
        deliverer.handle(
            BusMessage(
                kind="verdict",
                message_id=f"push:{event_id}",
                routing_key="verdict.push",
                payload={"event_id": event_id, "kind": "first"},
                trace_id=new_trace_id(),
                occurred_at_ms=now_ms(),
            )
        )
    )
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
    message = BusMessage(
        kind="verdict",
        message_id=f"push:{event_id}",
        routing_key="verdict.push",
        payload={"event_id": event_id, "kind": "first"},
        trace_id=new_trace_id(),
        occurred_at_ms=now_ms(),
    )
    deliverer = DelivererConsumer(
        bus=bus,
        db=db,
        sender=sender,
        finite_operations=InlineFiniteOperations(),
        min_interval_seconds=0.0,
    )

    db.fail_operations = {"news_delivery_settle"}
    with pytest.raises(RuntimeError, match="news_delivery_settlement_unavailable"):
        asyncio.run(deliverer.handle(message))
    conn.commit()

    assert len(sender.sent) == 1, "the card really did reach the provider"
    repos = repositories_for_connection(conn)
    stranded = repos.news.delivery(event_id=event_id, kind="first")
    assert stranded is not None and stranded["state"] == "sending"

    # Redelivery: the row still says `sending`, so this is the unknown-outcome case, not a retry.
    db.fail_operations = set()
    asyncio.run(deliverer.handle(message))
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


def test_a_verdict_mark_failure_redelivers_the_decision_into_one_delivery_lifecycle(conn) -> None:
    """Verdict publish success -> mark failure -> redelivery: one Verdict, one delivery lifecycle.

    `_publish_decision` is the same commit-then-publish shape as the Event outbox, and it suppresses
    its mark for the same reason. The consequence is a settled verdict the row still calls
    unpublished, so a redelivered Event republishes the decision. Two identical verdict messages then
    reach the Deliverer, and the reader must still receive exactly one card.
    """

    bus = RecordingBus()
    db = FaultInjectingDatabase(conn)
    sender = _RecordingSender()

    asyncio.run(_deduper(db, bus).handle(_raw_message(_one_hit())))
    conn.commit()
    event_id = str(_events(conn)[0]["event_id"])
    event_message = bus.of_kind("event")[0]

    judge = _EvidenceMovingJudge(_deduper(db, bus), [])
    db.fail_operations = {"news_triage_mark_published"}
    asyncio.run(_triage(db, bus, judge=judge).handle(event_message))
    conn.commit()

    verdict_row = conn.execute(
        "SELECT final_decision, published_at_ms FROM news_verdicts WHERE event_id = %s AND stage = 'triage'",
        (event_id,),
    ).fetchone()
    throttle = conn.execute(
        "SELECT final_decision, throttled_by, trace FROM news_verdicts WHERE event_id = %s", (event_id,)
    ).fetchone()
    assert verdict_row["final_decision"] in {"push", "escalate"}, dict(throttle)
    assert verdict_row["published_at_ms"] is None, "the mark is exactly what the injected fault stopped"
    assert len(bus.of_kind("verdict")) == 1

    # Redelivery of the Event: the verdict is already settled, so it is republished, never re-judged.
    db.fail_operations = set()
    asyncio.run(_triage(db, bus, judge=judge).handle(event_message))
    conn.commit()

    assert judge.asks == 1, "the second pass must not ask the model again"
    assert len(bus.of_kind("verdict")) == 2
    assert _count(conn, "SELECT count(*) AS n FROM news_verdicts WHERE event_id = %s", (event_id,)) == 1
    assert (
        conn.execute(
            "SELECT published_at_ms FROM news_verdicts WHERE event_id = %s AND stage = 'triage'", (event_id,)
        ).fetchone()["published_at_ms"]
        is not None
    )

    deliverer = DelivererConsumer(
        bus=bus,
        db=db,
        sender=sender,
        finite_operations=InlineFiniteOperations(),
        min_interval_seconds=0.0,
    )
    for message in bus.of_kind("verdict"):
        asyncio.run(deliverer.handle(message))
    conn.commit()

    assert len(sender.sent) == 1, "two verdict messages, one card on the reader's screen"
    deliveries = [
        dict(row)
        for row in conn.execute(
            "SELECT kind, state, error_code FROM news_deliveries WHERE event_id = %s", (event_id,)
        ).fetchall()
    ]
    assert deliveries == [{"kind": "first", "state": "sent", "error_code": None}]
