"""The durable-event plane across a real RabbitMQ and a real PostgreSQL at the same time (#400).

`test_news_crash_replay.py` injects broker faults into a fake bus so a publish can fail at an exact
instant; that is the right tool for the windows around a *publish*. The windows this module covers are
the ones only a real broker can open: a consumer channel that dies with the message unacked, a broker
that restarts while a message is in flight, and the delivery counter the broker itself maintains. The
convergence being checked is durable — one Event, one Verdict, one Delivery — so it is read back from
PostgreSQL rather than from a call sequence.

`test_news_bus_rabbitmq.py` owns the settlement contract itself. Here it is a given, and what matters is
what PostgreSQL looks like once the broker has exercised it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import socket
import subprocess
import time
import urllib.request
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import pytest
from psycopg.errors import UniqueViolation

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.app.workers.wiring.database import WorkerNewsDatabase
from tracefold.integrations.rabbitmq import RabbitMQBus, topology
from tracefold.news import broker_policy
from tracefold.news.bus import (
    Q_DEAD,
    Q_DELIVER,
    Q_RAW,
    Q_TRIAGE,
    RK_RAW_LIVE,
    BusMessage,
    new_trace_id,
    now_ms,
)
from tracefold.news.pipeline.admission import DeduperConsumer
from tracefold.news.pipeline.receiver import OpenNewsReceiver

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("rabbitmq_url")]

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "news_v3_hits_sample.json"
WATCHLIST = frozenset({"BTC", "NVDA", "ETH"})
AMQP_URL = os.environ.get("TRACEFOLD_TEST_AMQP_URL", "amqp://tracefold:tracefold@127.0.0.1:5672/")
MANAGEMENT_URL = os.environ.get(
    "TRACEFOLD_TEST_RABBITMQ_MANAGEMENT_URL",
    f"http://{urlsplit(AMQP_URL).hostname or '127.0.0.1'}:15672",
).rstrip("/")
FAST_DELAY_MS = 500


class _Database:
    """The production News database port over one real test connection."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self._port = WorkerNewsDatabase(self)

    async def read(self, name: str, fn: Any, *, timeout_seconds: float = 3.0) -> Any:
        return await self._port.read(name, fn, timeout_seconds=timeout_seconds)

    async def tx(self, name: str, fn: Any, *, timeout_seconds: float = 3.0) -> Any:
        return await self._port.tx(name, fn, timeout_seconds=timeout_seconds)

    @contextmanager
    def worker_session(self, name: str, *_args: Any, **_kwargs: Any) -> Iterator[Any]:
        del name
        yield repositories_for_connection(self.connection)

    async def run_news(self, name: str, fn: Any, *args: Any, operation_timeout_seconds: float, **kwargs: Any) -> Any:
        del operation_timeout_seconds, name
        return fn(*args, **kwargs)


@pytest.fixture(scope="module")
def _module_connection(postgres_module_clone_dsn: str) -> Iterator[Any]:
    connection = connect_postgres_test(read_only=False)
    yield connection
    connection.close()


@pytest.fixture
def conn(_module_connection: Any) -> Iterator[Any]:
    _module_connection.execute(
        "TRUNCATE news_items, news_opennews_incidents, news_event_evidence_snapshots RESTART IDENTITY CASCADE"
    )
    _module_connection.commit()
    yield _module_connection
    _module_connection.rollback()


@asynccontextmanager
async def _bus() -> AsyncIterator[RabbitMQBus]:
    """One connected, policy-provisioned topology, built inside the caller's event loop.

    The connection and its channels belong to the loop that opened them, so every scenario runs in a
    single `asyncio.run` rather than reusing a bus across loops.
    """

    prefix = f"tf_plane_{uuid.uuid4().hex[:8]}"
    instance = RabbitMQBus(
        url=AMQP_URL,
        name_prefix=prefix,
        connect_timeout_seconds=5,
        management_url=MANAGEMENT_URL,
        retry_delay_ms=FAST_DELAY_MS,
    )
    await instance.connect()
    await instance.apply_policies()
    try:
        yield instance
    finally:
        await instance.delete_topology()
        await instance.close()


def _one_hit() -> dict[str, Any]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rows = payload["data"] if isinstance(payload, dict) else payload
    return dict(rows[0])


def _raw_message(hit: dict[str, Any]) -> BusMessage:
    strategy_id = str((hit.get("strategy") or {}).get("id") or "")
    stamp = now_ms()
    return BusMessage(
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


class _InjectedFailure(RuntimeError):
    """Stands in for a process that died between the write and the commit."""


def _events(connection: Any) -> list[dict[str, Any]]:
    return [
        dict(row) for row in connection.execute("SELECT * FROM news_events ORDER BY opened_at_ms, event_id").fetchall()
    ]


def _open_incidents(connection: Any) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM news_opennews_incidents WHERE closed_at_ms IS NULL ORDER BY incident_id"
        ).fetchall()
    ]


def test_a_committed_event_whose_channel_dies_before_the_ack_converges_to_one_event(conn: Any) -> None:
    """Commit, then lose the channel before settling. The broker redelivers; PostgreSQL stays at one.

    This is the window the durable-event plane exists for. The Deduper's transaction has committed, so
    the material Event is a fact, but the broker was never told the delivery finished, so it hands the
    same raw frame to the next consumer. What must not happen is a second Event; what must not happen
    instead is the frame disappearing because the first consumer crashed after committing.
    """

    hit = _one_hit()
    database = _Database(conn)
    seen: list[int] = []
    redelivered: list[int] = []

    async def scenario() -> None:
        async with _bus() as bus:
            await bus.publish(_raw_message(hit))
            deduper = DeduperConsumer(bus=bus, db=database, watchlist_symbols=WATCHLIST)

            async def crash_after_commit(message: BusMessage) -> None:
                seen.append(message.attempt)
                await deduper.handle(message)
                raise RuntimeError("channel died after the transaction committed")

            with pytest.raises(BaseExceptionGroup):
                await asyncio.wait_for(
                    bus.consume(Q_RAW, crash_after_commit, prefetch=1, stop_event=asyncio.Event()),
                    timeout=60,
                )
            assert len(_events(conn)) == 1

            stop = asyncio.Event()

            async def second_pass(message: BusMessage) -> None:
                redelivered.append(message.attempt)
                await deduper.handle(message)
                stop.set()

            await asyncio.wait_for(bus.consume(Q_RAW, second_pass, prefetch=1, stop_event=stop), timeout=120)

    asyncio.run(scenario())

    assert seen == [1]
    # The broker counts a channel that died with an unacked message as a failed delivery, so the replay
    # is attempt 2 rather than a fresh first attempt.
    assert redelivered == [2]
    events = _events(conn)
    assert len(events) == 1
    assert int(conn.execute("SELECT count(*) AS n FROM news_items").fetchone()["n"]) == 1
    # One raw frame, delivered twice, produced exactly one Triage message: the second pass recognised
    # the Event it had already admitted instead of publishing it again.
    assert (
        int(
            conn.execute(
                "SELECT count(*) AS n FROM news_event_members WHERE event_id = %s", (events[0]["event_id"],)
            ).fetchone()["n"]
        )
        == 1
    )


@pytest.mark.slow
def test_a_broker_restart_mid_flight_redelivers_the_unacked_frame(conn: Any, restartable_rabbitmq: str) -> None:
    """A restarted broker must hand back what it never saw acked, not silently drop it.

    The single persisted node is the deployment boundary this proves: process, channel and broker
    restart on one durable volume. It says nothing about surviving the loss of that volume.
    """

    hit = _one_hit()
    database = _Database(conn)
    recovered: list[str] = []

    async def scenario() -> None:
        async with _bus() as bus:
            await bus.publish(_raw_message(hit))
            held = asyncio.Event()

            async def never_settle(_message: BusMessage) -> None:
                held.set()
                await asyncio.Future()

            stop = asyncio.Event()
            consumer = asyncio.create_task(bus.consume(Q_RAW, never_settle, prefetch=1, stop_event=stop))
            await asyncio.wait_for(held.wait(), timeout=60)
            consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError, BaseExceptionGroup):
                await consumer

            await asyncio.to_thread(_restart_broker, restartable_rabbitmq)
            await bus.close()

            deduper = DeduperConsumer(bus=bus, db=database, watchlist_symbols=WATCHLIST)
            resumed = asyncio.Event()

            async def after_restart(message: BusMessage) -> None:
                recovered.append(message.message_id)
                await deduper.handle(message)
                resumed.set()

            await asyncio.wait_for(bus.consume(Q_RAW, after_restart, prefetch=1, stop_event=resumed), timeout=240)

    asyncio.run(scenario())

    assert recovered == [f"raw:{hit['id']}"]
    assert len(_events(conn)) == 1


def test_a_queue_byte_bound_opens_the_broker_backpressure_incident_recovery_reads(conn: Any) -> None:
    """The byte bound is not just a rejection: it has to land on the lane that gets the frames back.

    A rejected publish becomes a typed `BrokerBackpressure`, which the Receiver turns into a durable
    `broker_backpressure` incident with `recovery_status='pending'`. That row is what official-history
    Recovery later reads, so the bound costs a backfill rather than a lost frame.
    """

    hit = _one_hit()
    strategy_id = str((hit.get("strategy") or {}).get("id") or "")
    database = _Database(conn)
    requested: list[int] = []

    class _Recovery:
        def request(self) -> None:
            requested.append(1)

    async def scenario() -> None:
        async with _bus() as bus:
            name = f"{bus.prefix}-raw-tiny"
            _put_policy(
                name,
                {
                    "pattern": f"^{bus.queue_name(Q_RAW).replace('.', chr(92) + '.')}$",
                    "apply-to": "queues",
                    "priority": broker_policy.POLICY_PRIORITY + 10,
                    "definition": {"max-length-bytes": 4096, "overflow": "reject-publish"},
                },
            )
            try:
                deadline = asyncio.get_running_loop().time() + 30
                while asyncio.get_running_loop().time() < deadline:
                    row = await bus.effective_policies()
                    if row[bus.queue_name(Q_RAW)].get("max-length-bytes") == 4096:
                        break
                    await asyncio.sleep(0.3)
                receiver = OpenNewsReceiver(bus=bus, db=database, ws_client=None, recovery=_Recovery())
                for index in range(64):
                    await receiver._publish_frame(
                        {"params": {**hit, "id": f"{hit['id']}-{index}", "text": "y" * 900}},
                        strategy_id=strategy_id,
                    )
                    if _open_incidents(conn):
                        break
                assert [row["cause_class"] for row in _open_incidents(conn)] == ["broker_backpressure"]
                assert _open_incidents(conn)[0]["recovery_status"] == "pending"
            finally:
                _delete_policy(name)

            # The bound lifted: the next accepted frame closes the incident and asks Recovery to backfill.
            deadline = asyncio.get_running_loop().time() + 30
            while asyncio.get_running_loop().time() < deadline:
                row = await bus.effective_policies()
                if row[bus.queue_name(Q_RAW)].get("max-length-bytes") != 4096:
                    break
                await asyncio.sleep(0.3)
            await _drain(bus)
            receiver = OpenNewsReceiver(bus=bus, db=database, ws_client=None, recovery=_Recovery())
            await receiver._publish_frame({"params": {**hit, "id": f"{hit['id']}-after"}}, strategy_id=strategy_id)

    asyncio.run(scenario())

    assert _open_incidents(conn) == []
    closed = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM news_opennews_incidents WHERE cause_class = 'broker_backpressure'"
        ).fetchall()
    ]
    assert len(closed) == 1 and closed[0]["closed_at_ms"] is not None
    # Recovery is what turns the rejected frames back into facts, so it must have been asked to run.
    assert requested


async def _drain(bus: RabbitMQBus) -> None:
    """Consume whatever the tiny bound let through, so the queue is empty for the next publish."""

    stop = asyncio.Event()

    async def swallow(_message: BusMessage) -> None:
        if (await bus.queue_depths())[bus.queue_name(Q_RAW)]["messages"] <= 1:
            stop.set()

    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(bus.consume(Q_RAW, swallow, prefetch=8, stop_event=stop), timeout=60)


def _put_policy(name: str, body: dict[str, Any]) -> None:
    _management_request("PUT", f"/api/policies/{quote(_vhost(), safe='')}/{quote(name, safe='')}", body)


def _delete_policy(name: str) -> None:
    _management_request("DELETE", f"/api/policies/{quote(_vhost(), safe='')}/{quote(name, safe='')}", None)


def _vhost() -> str:
    return unquote(urlsplit(AMQP_URL).path.lstrip("/")) or "/"


def _management_request(method: str, path: str, body: dict[str, Any] | None) -> None:
    parsed = urlsplit(AMQP_URL)
    token = base64.b64encode(
        f"{unquote(parsed.username or 'guest')}:{unquote(parsed.password or 'guest')}".encode()
    ).decode("ascii")
    request = urllib.request.Request(  # noqa: S310 - test-only HTTP management endpoint
        f"{MANAGEMENT_URL}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=10):
        return


def test_an_incident_open_that_never_committed_converges_on_the_next_attempt(conn: Any) -> None:
    """A transaction that dies before committing leaves nothing, and the retry yields exactly one row.

    This is the failure the in-memory flag used to hide: the process believed it had opened an incident
    that PostgreSQL never recorded. Now the only record is the row, so the next attempt simply writes it.
    """

    news = repositories_for_connection(conn).news
    with pytest.raises(_InjectedFailure), conn.transaction():
        news.open_incident(cause_class="triage_circuit_open", now_ms=now_ms())
        raise _InjectedFailure("the process died before the commit")
    assert _open_incidents(conn) == []

    first = news.open_incident(cause_class="triage_circuit_open", now_ms=now_ms())
    second = news.open_incident(cause_class="triage_circuit_open", now_ms=now_ms())

    assert first == second
    assert [row["incident_id"] for row in _open_incidents(conn)] == [first]


def test_an_incident_close_that_never_committed_converges_on_the_next_attempt(conn: Any) -> None:
    """A close that never committed leaves the incident open; closing again reaches zero and stays there."""

    news = repositories_for_connection(conn).news
    news.open_incident(cause_class="triage_circuit_open", now_ms=now_ms())

    with pytest.raises(_InjectedFailure), conn.transaction():
        news.close_open_incidents(cause_classes=["triage_circuit_open"], now_ms=now_ms())
        raise _InjectedFailure("the process died before the commit")
    assert len(_open_incidents(conn)) == 1

    assert news.close_open_incidents(cause_classes=["triage_circuit_open"], now_ms=now_ms()) == 1
    assert _open_incidents(conn) == []
    # Idempotent: a restart that reconciles again finds nothing left to close.
    assert news.close_open_incidents(cause_classes=["triage_circuit_open"], now_ms=now_ms()) == 0
    assert _open_incidents(conn) == []


def test_concurrent_incident_opens_converge_through_the_partial_unique_index(conn: Any) -> None:
    """Two writers, no application lock: PostgreSQL decides, and the loser reads the winner's row.

    The uniqueness is the index's, not a coordination protocol's, which is why the second connection
    returns the same incident id instead of inserting a rival row.
    """

    other = connect_postgres_test(read_only=False)
    try:
        stamp = now_ms()
        first = repositories_for_connection(conn).news.open_incident(cause_class="broker_unavailable", now_ms=stamp)
        second = repositories_for_connection(other).news.open_incident(
            cause_class="broker_unavailable", now_ms=stamp + 1
        )

        assert first == second
        rows = _open_incidents(conn)
        assert [row["incident_id"] for row in rows] == [first]
        # The second open is a re-observation, not a new incident: the opening instant is preserved.
        assert int(rows[0]["opened_at_ms"]) == stamp
        assert int(rows[0]["updated_at_ms"]) == stamp + 1

        # Two open incidents of one cause class are what the index makes impossible.
        with pytest.raises(UniqueViolation, match="ux_news_opennews_incidents_open_cause"):
            conn.execute(
                """
                INSERT INTO news_opennews_incidents (
                  cause_class, opened_at_ms, planned, recovery_status, created_at_ms, updated_at_ms
                ) VALUES ('broker_unavailable', %s, false, 'pending', %s, %s)
                """,
                (stamp, stamp, stamp),
            )
    finally:
        other.close()


def test_different_cause_classes_may_be_open_at_the_same_time(conn: Any) -> None:
    """The invariant is one open incident *per cause class*, not one open incident overall."""

    stamp = now_ms()
    news = repositories_for_connection(conn).news
    news.open_incident(cause_class="broker_unavailable", now_ms=stamp)
    news.open_incident(cause_class="triage_circuit_open", now_ms=stamp)

    assert sorted(row["cause_class"] for row in _open_incidents(conn)) == [
        "broker_unavailable",
        "triage_circuit_open",
    ]


def _restart_broker(container: str) -> None:
    subprocess.run(
        ["docker", "restart", "-t", "30", container],
        capture_output=True,
        check=True,
    )
    parsed = urlsplit(AMQP_URL)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((parsed.hostname or "127.0.0.1", parsed.port or 5672), timeout=2):
                return
        except OSError:
            time.sleep(2)
    raise AssertionError(f"the broker did not come back after restarting {container}")


def test_the_topology_this_module_used_is_the_final_one() -> None:
    async def scenario() -> None:
        async with _bus() as bus:
            assert set(topology(bus.prefix).queue_names) == {
                bus.queue_name(name) for name in (Q_RAW, Q_TRIAGE, Q_DELIVER, Q_DEAD)
            }
            assert await bus.topology_drift() == {"queues": [], "exchanges": []}

    asyncio.run(scenario())
