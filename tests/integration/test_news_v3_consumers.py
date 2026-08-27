"""News V3 consumers against real PostgreSQL with a recording fake bus.

Covers: Deduper raw -> event publication (+ idempotent redelivery), Triage fail-closed fallback with
an unconfigured semantic Program, Deliverer settlement when no sender is configured, and Control state writes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.repository_session import repositories_for_connection
from tracefold.app.workers.wiring.database import WorkerNewsDatabase
from tracefold.news.bus import (
    RK_RAW_LIVE,
    RK_VERDICT_PUSH,
    BusMessage,
    PermanentError,
    new_trace_id,
    now_ms,
)
from tracefold.news.models import ADMITTED_ADMISSIONS, TRIAGE_POLICY_VERSION
from tracefold.news.pipeline.admission import DeduperConsumer
from tracefold.news.pipeline.delivery import DelivererConsumer
from tracefold.news.pipeline.triage import TriageConsumer

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_dsn")]

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "news_v3_hits_sample.json"
WATCHLIST = frozenset({"BTC", "NVDA", "ETH"})
PROGRAM_VERSION = "news_semantic_program_test_v1"
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


class ExplodingJudge:
    """A model seam that records an accidental call before making the test fail."""

    def __init__(self) -> None:
        self.calls = 0

    async def judge(self, _context: Any) -> Any:
        self.calls += 1
        raise AssertionError("strategy 1019 must not call a model")


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
    return DeduperConsumer(bus=bus, db=FakeWorkerDatabase(conn), watchlist_symbols=WATCHLIST)


def _triage(conn: Any, bus: FakeBus, *, judge: Any = None) -> TriageConsumer:
    return TriageConsumer(
        bus=bus,
        db=FakeWorkerDatabase(conn),
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


def _deliverer(conn: Any, bus: FakeBus) -> DelivererConsumer:
    deliverer = DelivererConsumer(
        bus=bus,
        db=FakeWorkerDatabase(conn),
        sender=None,
        finite_operations=InlineFiniteOperations(),
        min_interval_seconds=0.0,
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
        "SELECT event_id, admission, published_at_ms, family, queue_priority FROM news_events WHERE event_id = ANY(%s)",
        (published_ids,),
    ).fetchall()
    assert len(rows) == len(published_ids)
    # Both admitted admissions route to Triage: `candidate` and `listing_deterministic` (#72 — listing frames used
    # to be stored, placed on the high scheduling lane, and then silently dropped instead of published).
    assert all(row["admission"] in ADMITTED_ADMISSIONS and row["published_at_ms"] is not None for row in rows)
    for row in rows:
        assert f"event.{row['family']}.{row['queue_priority']}" in bus.routing_keys()
    unpublished_admitted = conn.execute(
        "SELECT count(*) AS n FROM news_events WHERE admission = ANY(%s) AND published_at_ms IS NULL",
        (sorted(ADMITTED_ADMISSIONS),),
    ).fetchone()["n"]
    assert unpublished_admitted == 0
    suppressed = conn.execute(
        "SELECT count(*) AS n FROM news_events WHERE NOT (admission = ANY(%s))",
        (sorted(ADMITTED_ADMISSIONS),),
    ).fetchone()["n"]
    assert suppressed > 0  # suppressed admissions are stored but never routed to Triage

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


def test_triage_without_model_is_fail_closed_and_only_objective_guards_push(conn) -> None:
    bus = FakeBus()
    triage = _triage(conn, bus)
    strong = conn.execute(
        """
        SELECT event_id, queue_priority, leader_title FROM news_events
         WHERE admission = 'candidate'
           AND jsonb_array_length(grounded_assets) > 0
           AND jsonb_array_length(watchlist_hits) > 0
           AND event_id NOT IN (SELECT event_id FROM news_verdicts)
         ORDER BY opened_at_ms DESC, event_id LIMIT 1
        """
    ).fetchone()
    weak = conn.execute(
        """
        SELECT event_id, queue_priority, provider_score_max FROM news_events
         WHERE admission = 'candidate'
           AND jsonb_array_length(watchlist_hits) = 0
           AND event_id NOT IN (SELECT event_id FROM news_verdicts)
         ORDER BY (queue_priority = 'high') DESC, provider_score_max DESC NULLS LAST, opened_at_ms DESC, event_id
         LIMIT 1
        """
    ).fetchone()
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
        strong_priority = 5 if strong["queue_priority"] == "high" else 0
        weak_priority = 5 if weak["queue_priority"] == "high" else 0
        await triage.handle(_message(str(strong["event_id"]), priority=strong_priority))
        await triage.handle(_message(str(weak["event_id"]), priority=weak_priority))
        # replay both: existing verdicts are honoured, nothing is re-published
        await triage.handle(_message(str(strong["event_id"]), priority=strong_priority))
        await triage.handle(_message(str(weak["event_id"]), priority=weak_priority))
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
        assert row["error_code"] == "news_semantic_program_unconfigured"
        assert row["model_decision"] is None and row["model"] is None
        assert row["verdict"]["headline_zh"] and "模型不可用" not in row["verdict"]["headline_zh"]  # the wire headline
    strong_row = verdicts[strong["event_id"]]
    weak_row = verdicts[weak["event_id"]]
    assert strong_row["rule_baseline_decision"] == "push"
    # #77: a degraded card carries no model judgment, so it pushes but never wears the ⚡ header.
    assert strong_row["final_decision"] == "push"
    assert strong_row["published_at_ms"] is not None
    assert weak_row["rule_baseline_decision"] == "drop"
    assert weak_row["final_decision"] == "drop"
    assert weak_row["published_at_ms"] is None

    routing = [(m.routing_key, m.payload["event_id"]) for m in bus.published]
    assert routing == [(RK_VERDICT_PUSH, strong["event_id"])]  # one push lane; escalate is loudness, not a second lane
    assert bus.published[0].message_id == f"push:{strong['event_id']}"
    assert bus.published[0].payload["kind"] == "first"
    context = conn.execute(
        "SELECT context_line, storyline_key FROM news_events WHERE event_id = %s", (strong["event_id"],)
    ).fetchone()
    assert " ".join(strong["leader_title"].split())[:60] in context["context_line"]  # the wire headline, not an apology
    assert "→ push·degraded_watchlist_objective" in context["context_line"]
    # Triage wrote the final storyline key back and recorded it in the replayable trace.
    assert context["storyline_key"] == strong_row["trace"]["storyline_key"]
    assert conn.execute("SELECT count(*) AS n FROM news_verdicts").fetchone()["n"] == 2


def test_strategy_1019_parse_failure_is_persisted_and_counted_without_a_model(
    conn,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stamp = now_ms()
    bus = FakeBus()
    judge = ExplodingJudge()
    telemetry_logger = logging.getLogger("tracefold.news")
    monkeypatch.setattr(telemetry_logger, "disabled", False)
    monkeypatch.setattr(telemetry_logger, "propagate", True)
    caplog.set_level(logging.WARNING, logger="tracefold.news")
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
    event_message = bus.published[-1]
    event_id = str(event_message.payload["event_id"])
    asyncio.run(_triage(conn, bus, judge=judge).handle(event_message))
    conn.commit()

    verdict = conn.execute(
        "SELECT final_decision, override_rule, error_code, program_version, trace "
        "FROM news_verdicts WHERE event_id = %s",
        (event_id,),
    ).fetchone()
    assert verdict is not None
    assert (verdict["final_decision"], verdict["override_rule"], verdict["error_code"]) == (
        "drop",
        "oi_parse_failed",
        "oi_parse_failed",
    )
    assert verdict["program_version"] == "news_oi_signal_v1"
    assert verdict["trace"]["oi_signal"] == {
        "parsed": False,
        "rule": "oi_parse_failed",
        "strategy_id": "1019",
        "provider": "opennews",
        "provider_source": "binance",
        "title_sha256": "3cbadc25cd510bd837cfcfae89b6c48040cfd9ffd18cafd13031cc660ee8786b",
        "parser_version": "oi_signal_parser_v1",
        "source_classifier_version": "opennews_source_classifier_v1",
        "failure_stage": "source_contract_drift",
    }
    assert conn.execute("SELECT 1 FROM news_oi_signals WHERE event_id = %s", (event_id,)).fetchone() is None
    assert judge.calls == 0
    assert "news_oi_parse_failed" in caplog.text and "provider format changed" not in caplog.text
    assert not [message for message in bus.published if message.routing_key == RK_VERDICT_PUSH]

    pipeline = repositories_for_connection(conn).news.status_snapshot(now_ms=stamp + 1)["pipeline"]
    assert pipeline["telemetry_received_24h"] >= 1
    assert pipeline["telemetry_parse_failed_24h"] >= 1
    assert pipeline["dropped_by_rule"]["oi_parse_failed"] >= 1


def test_verified_liquidation_reconciliation_focuses_the_typed_cross_item_fact(conn) -> None:
    stamp = now_ms()
    bus = FakeBus()
    judge = ExplodingJudge()

    def _raw(record_id: int) -> BusMessage:
        return BusMessage(
            kind="raw",
            message_id=f"raw:{record_id}",
            routing_key=RK_RAW_LIVE.format(strategy_id="2000"),
            payload={
                "params": {
                    "id": record_id,
                    "engineType": "market",
                    "text": "SPCX Large Short Liquidation 202.71K at $137.01",
                    "source": "binance",
                    "ts": stamp,
                    "strategy": {"id": 2000, "name": "实时清算", "sourceType": "market"},
                },
                "strategy_id": "2000",
                "ingest_mode": "live",
                "observed_at_ms": stamp,
            },
            trace_id=new_trace_id(),
            occurred_at_ms=stamp,
        )

    asyncio.run(_deduper(conn, bus).handle(_raw(2_880_451)))
    event_message = bus.published[-1]
    event_id = str(event_message.payload["event_id"])
    leader = conn.execute("SELECT leader_item_id FROM news_events WHERE event_id = %s", (event_id,)).fetchone()
    assert leader is not None

    # Model the 0315 migration residual: the old Event has no typed fact and is
    # explicitly unverified. The next exact provider Item is the first current
    # parser proof and must become Triage's evidence focus.
    conn.execute("DELETE FROM news_market_liquidations WHERE item_id = %s", (leader["leader_item_id"],))
    conn.execute(
        "UPDATE news_events SET source_contract_reason = 'source_contract_unverified', admission = 'candidate' "
        "WHERE event_id = %s",
        (event_id,),
    )
    conn.commit()

    published_before = len(bus.published)
    asyncio.run(_deduper(conn, bus).handle(_raw(2_880_452)))
    conn.commit()
    assert len(bus.published) == published_before

    repos = repositories_for_connection(conn)
    card = repos.news.event_card(event_id)
    current_item = conn.execute("SELECT item_id FROM news_items WHERE source_item_key = '2880452'").fetchone()
    assert card is not None and current_item is not None
    assert card["leader_item_id"] == current_item["item_id"]
    assert (
        repos.news.market_liquidation(
            item_id=str(card["leader_item_id"]),
            fact_id=str(card["focus_fact_id"]),
            parser_version="liquidation_parser_v1",
        )
        is not None
    )
    assert (
        conn.execute("SELECT source_contract_reason FROM news_events WHERE event_id = %s", (event_id,)).fetchone()[
            "source_contract_reason"
        ]
        is None
    )

    asyncio.run(_triage(conn, bus, judge=judge).handle(event_message))
    conn.commit()
    verdict = conn.execute(
        "SELECT program_version, override_rule, error_code FROM news_verdicts WHERE event_id = %s",
        (event_id,),
    ).fetchone()
    assert verdict == {
        "program_version": "news_liquidation_fact_v1",
        "override_rule": "liquidation_deterministic",
        "error_code": None,
    }
    assert judge.calls == 0


def test_deliverer_without_sender_settles_terminal_delivery_unavailable(conn) -> None:
    bus = FakeBus()
    deliverer = _deliverer(conn, bus)
    row = conn.execute(
        # Any delivering decision: #77 made the fixture's high-priority verdict a `push` rather than an
        # `escalate`, and the Deliverer treats both identically — escalate is loudness, not a second lane.
        "SELECT event_id FROM news_verdicts WHERE stage = 'triage' AND final_decision IN ('push', 'escalate') LIMIT 1"
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
    detail = repos.news.event_detail(event_id)
    assert detail is not None and detail["deliveries"][0]["state"] == "terminal"
