"""News V3 consumers against real PostgreSQL with a recording fake bus.

Covers: Deduper raw -> event publication (+ idempotent redelivery), Triage fail-closed fallback with
``model=None``, Deliverer settlement when no sender is configured, and Control state writes.
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.repositories import repositories_for_connection
from tracefold.news.bus import (
    RK_RAW_LIVE,
    RK_VERDICT_ESCALATE,
    RK_VERDICT_PUSH,
    BusMessage,
    PermanentError,
    new_trace_id,
    now_ms,
)
from tracefold.news.consumers import DeduperConsumer, DelivererConsumer, TriageConsumer
from tracefold.news.control import apply_control, parse_control
from tracefold.news.models import TRIAGE_POLICY_VERSION

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "news_v3_hits_sample.json"
STRATEGY_IDS = ("1018", "1352", "1353")
WATCHLIST = frozenset({"BTC", "NVDA", "ETH"})
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
    """WorkerDatabase-like adapter over one test connection: the News lane runs inline."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.operations: list[str] = []

    @contextmanager
    def worker_session(self, name: str, *_args: Any, **_kwargs: Any):
        del name
        yield repositories_for_connection(self.conn)

    async def run_news(self, name: str, fn: Any, *args: Any, operation_timeout_seconds: float, **kwargs: Any):
        del operation_timeout_seconds
        self.operations.append(name)
        return fn(*args, **kwargs)


class InlineFiniteOperations:
    async def run(self, _name: str, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        kwargs.pop("timeout_seconds", None)
        kwargs.pop("allow_shutdown", None)
        return await fn(*args, **kwargs) if asyncio.iscoroutinefunction(fn) else fn(*args, **kwargs)


@pytest.fixture(scope="module")
def conn():
    connection = connect_postgres_test(read_only=False)
    migrate(connection)
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
    return DeduperConsumer(bus=bus, db=FakeWorkerDatabase(conn), strategy_ids=STRATEGY_IDS, watchlist_symbols=WATCHLIST)


def _triage(conn: Any, bus: FakeBus) -> TriageConsumer:
    return TriageConsumer(
        bus=bus,
        db=FakeWorkerDatabase(conn),
        model=None,
        watchlist_symbols=WATCHLIST,
        watchlist=sorted(WATCHLIST),
        hourly_cap=20,
        concurrency=1,
        circuit_failures=3,
        circuit_open_seconds=60.0,
    )


def _deliverer(conn: Any, bus: FakeBus) -> DelivererConsumer:
    deliverer = DelivererConsumer(
        bus=bus,
        db=FakeWorkerDatabase(conn),
        sender=None,
        finite_operations=InlineFiniteOperations(),
        min_interval_seconds=0.0,
        hourly_cap=20,
    )
    return deliverer


def test_deduper_publishes_each_new_candidate_once_and_marks_published(conn) -> None:
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
    published_ids = [str(message.payload["event_id"]) for message in bus.published]
    assert len(published_ids) == len(set(published_ids))
    assert all(message.message_id == f"event:{message.payload['event_id']}" for message in bus.published)
    high = [m for m in bus.published if m.routing_key.endswith(".high")]
    assert all(m.priority == 5 for m in high)
    assert all(m.priority == 0 for m in bus.published if m.routing_key.endswith(".normal"))

    rows = conn.execute(
        "SELECT event_id, admission, published_at_ms, family, priority FROM news_events WHERE event_id = ANY(%s)",
        (published_ids,),
    ).fetchall()
    assert len(rows) == len(published_ids)
    assert all(row["admission"] == "candidate" and row["published_at_ms"] is not None for row in rows)
    for row in rows:
        assert f"event.{row['family']}.{row['priority']}" in bus.routing_keys()
    unpublished_candidates = conn.execute(
        "SELECT count(*) AS n FROM news_events WHERE admission = 'candidate' AND published_at_ms IS NULL"
    ).fetchone()["n"]
    assert unpublished_candidates == 0
    suppressed = conn.execute("SELECT count(*) AS n FROM news_events WHERE admission <> 'candidate'").fetchone()["n"]
    assert suppressed > 0  # non-candidates are stored but never routed to Triage

    # Redelivery of every raw message is a no-op: same items, same events, zero new publishes.
    before = conn.execute(
        "SELECT (SELECT count(*) FROM news_items) AS items, (SELECT count(*) FROM news_events) AS events"
    ).fetchone()
    published_before = len(bus.published)
    asyncio.run(scenario())
    conn.commit()
    after = conn.execute(
        "SELECT (SELECT count(*) FROM news_items) AS items, (SELECT count(*) FROM news_events) AS events"
    ).fetchone()
    assert dict(after) == dict(before)
    assert len(bus.published) == published_before


def test_deduper_settles_unconfigured_strategy_and_rejects_missing_params(conn) -> None:
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
                "strategy": {"id": 9999, "name": "not allowlisted"},
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
    assert bus.published == []
    assert conn.execute("SELECT count(*) AS n FROM news_items WHERE source_item_key = '999999999'").fetchone()["n"] == 0


def _candidate_event(conn: Any, *, priority: str, grounded: bool) -> dict[str, Any] | None:
    grounded_clause = (
        "jsonb_array_length(grounded_assets) > 0" if grounded else "jsonb_array_length(grounded_assets) = 0"
    )
    return conn.execute(
        f"""
        SELECT event_id, storyline_key, priority, provider_score_max, grounded_assets, watchlist_hits
          FROM news_events
         WHERE admission = 'candidate' AND priority = %s AND {grounded_clause}
           AND event_id NOT IN (SELECT event_id FROM news_verdicts)
         ORDER BY opened_at_ms DESC, event_id
         LIMIT 1
        """,
        (priority,),
    ).fetchone()


def test_triage_without_model_is_fail_closed_and_only_rule_baseline_pushes(conn) -> None:
    bus = FakeBus()
    triage = _triage(conn, bus)
    strong = conn.execute(
        """
        SELECT event_id, priority FROM news_events
         WHERE admission = 'candidate' AND priority = 'high'
           AND jsonb_array_length(grounded_assets) > 0
           AND (jsonb_array_length(watchlist_hits) > 0 OR provider_score_max >= 90)
           AND event_id NOT IN (SELECT event_id FROM news_verdicts)
         ORDER BY opened_at_ms DESC, event_id LIMIT 1
        """
    ).fetchone()
    weak = _candidate_event(conn, priority="normal", grounded=False) or _candidate_event(
        conn, priority="normal", grounded=True
    )
    assert strong is not None and weak is not None
    stamp = now_ms()

    def _message(event_id: str, *, priority: int) -> BusMessage:
        return BusMessage(
            kind="event",
            message_id=f"event:{event_id}",
            routing_key="event.general.high" if priority else "event.general.normal",
            payload={"event_id": event_id},
            trace_id=new_trace_id(),
            occurred_at_ms=stamp,
            priority=priority,
        )

    async def scenario() -> None:
        await triage.handle(_message(str(strong["event_id"]), priority=5))
        await triage.handle(_message(str(weak["event_id"]), priority=0))
        # replay both: existing verdicts are honoured, nothing is re-published
        await triage.handle(_message(str(strong["event_id"]), priority=5))
        await triage.handle(_message(str(weak["event_id"]), priority=0))
        with pytest.raises(PermanentError, match="news_event_id_missing"):
            await triage.handle(_message("", priority=0))
        with pytest.raises(PermanentError, match="news_event_missing"):
            await triage.handle(_message("does-not-exist", priority=0))

    asyncio.run(scenario())
    conn.commit()

    verdicts = {
        row["event_id"]: dict(row)
        for row in conn.execute(
            "SELECT * FROM news_verdicts WHERE event_id = ANY(%s)", ([strong["event_id"], weak["event_id"]],)
        ).fetchall()
    }
    assert set(verdicts) == {strong["event_id"], weak["event_id"]}
    for row in verdicts.values():
        assert row["stage"] == "triage" and row["policy_version"] == TRIAGE_POLICY_VERSION
        assert row["degraded"] is True
        assert row["error_code"] == "news_triage_model_unconfigured"
        assert row["model_decision"] is None and row["model"] is None
        assert row["verdict"]["headline_zh"] == "模型不可用（规则兜底）"
    strong_row = verdicts[strong["event_id"]]
    weak_row = verdicts[weak["event_id"]]
    assert strong_row["rule_baseline_decision"] == "push"
    assert strong_row["final_decision"] == "escalate"
    assert strong_row["published_at_ms"] is not None
    assert weak_row["rule_baseline_decision"] == "drop"
    assert weak_row["final_decision"] == "drop"
    assert weak_row["published_at_ms"] is None

    routing = [(m.routing_key, m.payload["event_id"]) for m in bus.published]
    assert routing == [(RK_VERDICT_PUSH, strong["event_id"]), (RK_VERDICT_ESCALATE, strong["event_id"])]
    assert bus.published[0].message_id == f"push:{strong['event_id']}"
    assert bus.published[0].payload["kind"] == "first"
    context = conn.execute(
        "SELECT context_line, storyline_key FROM news_events WHERE event_id = %s", (strong["event_id"],)
    ).fetchone()
    assert "模型不可用" in context["context_line"]
    assert "→ escalate·high_priority_push" in context["context_line"]  # the feed shows the reason, not just the verdict
    # Triage wrote the final storyline key back and recorded it in the replayable trace.
    assert context["storyline_key"] == strong_row["trace"]["storyline_key"]
    assert conn.execute("SELECT count(*) AS n FROM news_verdicts").fetchone()["n"] == 2


def test_deliverer_without_sender_settles_terminal_delivery_unavailable(conn) -> None:
    bus = FakeBus()
    deliverer = _deliverer(conn, bus)
    row = conn.execute(
        "SELECT event_id FROM news_verdicts WHERE stage = 'triage' AND final_decision = 'escalate' LIMIT 1"
    ).fetchone()
    dropped = conn.execute(
        "SELECT event_id FROM news_verdicts WHERE stage = 'triage' AND final_decision = 'drop' LIMIT 1"
    ).fetchone()
    assert row is not None and dropped is not None
    event_id = str(row["event_id"])
    stamp = now_ms()

    def _push(event: str) -> BusMessage:
        return BusMessage(
            kind="verdict",
            message_id=f"push:{event}",
            routing_key=RK_VERDICT_PUSH,
            payload={"event_id": event, "kind": "first"},
            trace_id=new_trace_id(),
            occurred_at_ms=stamp,
        )

    async def scenario() -> None:
        await deliverer.handle(_push(event_id))
        await deliverer.handle(_push(event_id))  # redelivery: existing row keeps its terminal state
        await deliverer.handle(_push(str(dropped["event_id"])))  # drop verdict never creates a delivery
        with pytest.raises(PermanentError, match="news_delivery_inputs_missing"):
            await deliverer.handle(_push("does-not-exist"))

    asyncio.run(scenario())
    conn.commit()

    deliveries = conn.execute(
        "SELECT event_id, kind, state, error_code, settled_at_ms FROM news_deliveries ORDER BY event_id"
    ).fetchall()
    assert [dict(d) for d in deliveries] == [
        {
            "event_id": event_id,
            "kind": "first",
            "state": "terminal",
            "error_code": "delivery_unavailable",
            "settled_at_ms": deliveries[0]["settled_at_ms"],
        }
    ]
    assert deliveries[0]["settled_at_ms"] is not None
    assert bus.published == []
    repos = repositories_for_connection(conn)
    assert repos.news.sent_count_since(since_ms=0) == 0
    detail = repos.news.event_detail(event_id)
    assert detail is not None and detail["deliveries"][0]["state"] == "terminal"


def test_control_commands_write_control_state_directly(conn) -> None:
    """The CLI writes news_control_state in one transaction; consumers read it on every message."""

    repos = repositories_for_connection(conn)
    stamp = now_ms()

    def _apply(payload: dict[str, Any]) -> None:
        with repos.transaction():
            state = repos.news.read_control(now_ms=stamp)
            new_state = apply_control(state, parse_control(payload), now_ms=stamp)
            repos.news.write_control(paused=new_state["paused"], mutes=new_state["mutes"], now_ms=stamp)

    _apply({"action": "pause_delivery"})
    _apply({"action": "mute_symbol", "key": "btc", "ttl_ms": 3_600_000})
    _apply({"action": "mute_theme", "key": "rates", "ttl_ms": 3_600_000})
    with pytest.raises(ValueError, match="news_control_action_invalid"):
        parse_control({"action": "explode"})
    with pytest.raises(ValueError, match="news_control_key_required"):
        parse_control({"action": "mute_symbol"})
    conn.commit()

    state = repos.news.read_control(now_ms=now_ms())
    assert state["paused"] is True
    assert {(m["kind"], m["key"]) for m in state["mutes"]} == {("symbol", "BTC"), ("theme", "rates")}
    assert all(m["until_ms"] > stamp for m in state["mutes"])
    rows = conn.execute("SELECT count(*) AS n FROM news_control_state").fetchone()
    assert rows["n"] == 1

    _apply({"action": "resume_delivery"})
    _apply({"action": "unmute", "key": "BTC"})
    conn.commit()
    state = repos.news.read_control(now_ms=now_ms())
    assert state["paused"] is False
    assert [(m["kind"], m["key"]) for m in state["mutes"]] == [("theme", "rates")]
