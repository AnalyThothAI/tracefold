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

from tests.postgres_test_utils import connect_postgres_test, seed_current_news_evidence
from tracefold.app.repository_session import repositories_for_connection
from tracefold.app.workers.wiring.database import WorkerNewsDatabase
from tracefold.news.bus import (
    RK_RAW_LIVE,
    RK_RAW_RECOVERY,
    RK_VERDICT_PUSH,
    BusMessage,
    PermanentError,
    TransientError,
    new_trace_id,
    now_ms,
)
from tracefold.news.models import ADMITTED_ADMISSIONS, TRIAGE_POLICY_VERSION
from tracefold.news.pipeline.admission import DeduperConsumer
from tracefold.news.pipeline.delivery import DelivererConsumer
from tracefold.news.pipeline.maintenance import JanitorLoop
from tracefold.news.pipeline.triage import TriageConsumer
from tracefold.news.program.runtime import PROGRAM_VERSION
from tracefold.news.storage.decisions import (
    _HANDOFF_STATE_LIMIT as _VERDICT_HANDOFF_STATE_LIMIT,
)
from tracefold.news.storage.decisions import (
    _VERDICT_HANDOFF_STATE_SQL,
    UNPUBLISHED_VERDICT_CANDIDATES_SQL,
)
from tracefold.news.storage.events import (
    _EVENT_HANDOFF_STATE_SQL,
    UNPUBLISHED_EVENT_CANDIDATES_SQL,
)
from tracefold.news.storage.events import (
    _HANDOFF_STATE_LIMIT as _EVENT_HANDOFF_STATE_LIMIT,
)

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


class FailOnceWorkerDatabase(FakeWorkerDatabase):
    def __init__(self, conn: Any, *, fail_once: set[str]) -> None:
        super().__init__(conn)
        self.fail_once = set(fail_once)

    async def tx(self, name: str, fn: Any, *, timeout_seconds: float = 3.0) -> Any:
        if name in self.fail_once:
            self.fail_once.remove(name)
            raise TransientError(f"injected:{name}")
        return await super().tx(name, fn, timeout_seconds=timeout_seconds)


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


def _ensure_handoff_facts(conn: Any) -> tuple[str, str]:
    """Persist at least one admitted Event and one push Verdict through the production repositories."""

    deduper = _deduper(conn, FakeBus())

    async def _admit() -> None:
        for message in _raw_messages():
            await deduper.handle(message)

    asyncio.run(_admit())
    conn.commit()
    push = conn.execute(
        """
        SELECT v.event_id, v.policy_version
          FROM news_verdicts v
         WHERE v.stage = 'triage' AND v.final_decision IN ('push', 'escalate')
         ORDER BY v.created_at_ms DESC LIMIT 1
        """
    ).fetchone()
    if push is None:
        event = conn.execute(
            """
            SELECT event_id, queue_priority
              FROM news_events
             WHERE admission = 'candidate'
               AND jsonb_array_length(grounded_assets) > 0
               AND jsonb_array_length(watchlist_hits) > 0
               AND event_id NOT IN (SELECT event_id FROM news_verdicts)
             ORDER BY opened_at_ms DESC, event_id LIMIT 1
            """
        ).fetchone()
        assert event is not None
        priority = 5 if event["queue_priority"] == "high" else 0
        asyncio.run(
            _triage(conn, FakeBus()).handle(
                BusMessage(
                    kind="event",
                    message_id=f"event:{event['event_id']}",
                    routing_key=f"event.general.{event['queue_priority']}",
                    payload={"event_id": event["event_id"]},
                    trace_id="handoff-integration",
                    occurred_at_ms=now_ms(),
                    priority=priority,
                )
            )
        )
        conn.commit()
        push = conn.execute(
            """
            SELECT event_id, policy_version
              FROM news_verdicts
             WHERE event_id = %s AND stage = 'triage' AND final_decision IN ('push', 'escalate')
            """,
            (event["event_id"],),
        ).fetchone()
    assert push is not None
    return str(push["event_id"]), str(push["policy_version"])


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
        "SELECT event_id, admission, published_at_ms, dedupe_family, queue_priority"
        " FROM news_events WHERE event_id = ANY(%s)",
        (published_ids,),
    ).fetchall()
    assert len(rows) == len(published_ids)
    # Both admitted admissions route to Triage: `candidate` and `listing_deterministic` (#72 — listing frames used
    # to be stored, placed on the high scheduling lane, and then silently dropped instead of published).
    assert all(row["admission"] in ADMITTED_ADMISSIONS and row["published_at_ms"] is not None for row in rows)
    for row in rows:
        assert f"event.{row['dedupe_family']}.{row['queue_priority']}" in bus.routing_keys()
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


def test_recovery_raw_is_persisted_but_never_published_to_triage_or_delivery(conn) -> None:
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
          (SELECT count(*) FROM news_verdicts WHERE event_id = ANY(%s)) AS verdicts,
          (SELECT count(*) FROM news_deliveries WHERE event_id = ANY(%s)) AS deliveries
        """,
        (event_ids, event_ids),
    ).fetchone()
    assert downstream == {"verdicts": 0, "deliveries": 0}
    assert bus.published == []


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
        assert row["judgment_contract_version"] == "news_judgment_v2"
        assert row["judgment_origin"] == "degraded" and row["model"] is None
        assert row["scored_judgment_sha256"] == row["trace"]["judgment_sha256"]
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


def test_janitor_repairs_both_handoffs_after_confirmed_publish_marker_failure(conn) -> None:
    event_id, policy_version = _ensure_handoff_facts(conn)
    stamp = now_ms()
    conn.execute(
        "UPDATE news_events SET opened_at_ms = %s, published_at_ms = NULL WHERE event_id = %s",
        (stamp - 60_000, event_id),
    )
    conn.commit()

    first_event_bus = FakeBus()
    failing_event_db = FailOnceWorkerDatabase(conn, fail_once={"news_event_mark_published"})
    first_event = asyncio.run(
        JanitorLoop(db=failing_event_db, cold_db=failing_event_db, bus=first_event_bus).repair_event_handoffs()
    )
    assert first_event == 1
    assert (
        conn.execute("SELECT published_at_ms FROM news_events WHERE event_id = %s", (event_id,)).fetchone()[
            "published_at_ms"
        ]
        is None
    )

    second_event_bus = FakeBus()
    event_db = FakeWorkerDatabase(conn)
    second_event = asyncio.run(JanitorLoop(db=event_db, cold_db=event_db, bus=second_event_bus).repair_event_handoffs())
    assert second_event == 1
    assert first_event_bus.published[0].message_id == second_event_bus.published[0].message_id == f"event:{event_id}"
    event_marker = conn.execute("SELECT published_at_ms FROM news_events WHERE event_id = %s", (event_id,)).fetchone()[
        "published_at_ms"
    ]
    assert event_marker is not None

    conn.execute(
        """
        UPDATE news_verdicts SET created_at_ms = %s, published_at_ms = NULL
         WHERE event_id = %s AND stage = 'triage' AND policy_version = %s
        """,
        (stamp - 60_000, event_id, policy_version),
    )
    conn.commit()
    first_verdict_bus = FakeBus()
    failing_verdict_db = FailOnceWorkerDatabase(conn, fail_once={"news_triage_mark_published"})
    first_verdict = asyncio.run(
        JanitorLoop(
            db=failing_verdict_db,
            cold_db=failing_verdict_db,
            bus=first_verdict_bus,
        ).repair_verdict_handoffs()
    )
    assert first_verdict == 1
    assert (
        conn.execute(
            """
        SELECT published_at_ms FROM news_verdicts
         WHERE event_id = %s AND stage = 'triage' AND policy_version = %s
        """,
            (event_id, policy_version),
        ).fetchone()["published_at_ms"]
        is None
    )

    second_verdict_bus = FakeBus()
    verdict_db = FakeWorkerDatabase(conn)
    second_verdict = asyncio.run(
        JanitorLoop(db=verdict_db, cold_db=verdict_db, bus=second_verdict_bus).repair_verdict_handoffs()
    )
    assert second_verdict == 1
    assert first_verdict_bus.published[0].message_id == second_verdict_bus.published[0].message_id == f"push:{event_id}"
    assert (
        first_verdict_bus.published[0].payload
        == second_verdict_bus.published[0].payload
        == {
            "event_id": event_id,
            "kind": "first",
        }
    )
    verdict_marker = conn.execute(
        """
        SELECT published_at_ms FROM news_verdicts
         WHERE event_id = %s AND stage = 'triage' AND policy_version = %s
        """,
        (event_id, policy_version),
    ).fetchone()["published_at_ms"]
    assert verdict_marker is not None

    repos = repositories_for_connection(conn)
    with repos.transaction():
        assert repos.news.mark_event_published(event_id=event_id, now_ms=int(event_marker) + 1) is False
        assert (
            repos.news.mark_verdict_published(
                event_id=event_id,
                stage="triage",
                policy_version=policy_version,
                now_ms=int(verdict_marker) + 1,
            )
            is False
        )
    unchanged = conn.execute(
        """
        SELECT e.published_at_ms AS event_marker, v.published_at_ms AS verdict_marker
          FROM news_events e
          JOIN news_verdicts v ON v.event_id = e.event_id AND v.stage = 'triage' AND v.policy_version = %s
         WHERE e.event_id = %s
        """,
        (policy_version, event_id),
    ).fetchone()
    assert unchanged["event_marker"] == event_marker and unchanged["verdict_marker"] == verdict_marker


def test_handoff_candidate_and_state_plans_use_partial_indexes_at_history_scale(conn) -> None:
    event_id, policy_version = _ensure_handoff_facts(conn)
    stamp = now_ms()
    # Keep 20k table/cardinality evidence for the planner, but only 1,250 current rows: that is already
    # above the 1,000-row state cap. JSON/hash CHECK cost is covered separately under a native statement
    # timeout; validating 20,000 current verdicts made a read-plan fixture itself unbounded.
    conn.execute(
        """
        INSERT INTO news_events (
          event_id, leader_item_id, dedupe_family, event_kind, source_contract_reason,
          comparison_fingerprint, comparison_title, leader_title,
          focus_fact_id, focus_fact_text, focus_fact_context, focus_fact_method,
          focus_span_start, focus_span_end, opened_at_ms, last_member_at_ms, expires_at_ms,
          member_count, admission, queue_priority, provider_score_max, engine_type, asset_class,
          grounded_assets, watchlist_hits, macro_lexicon, storyline_key, context_line,
          published_at_ms, ingest_mode, trace_id, created_at_ms, updated_at_ms,
          current_contract_archive_only
        )
        SELECT 'handoff-scale-' || g.n, template.leader_item_id, template.dedupe_family, template.event_kind,
               template.source_contract_reason, md5('handoff-' || g.n) || md5('handoff-x-' || g.n),
               template.comparison_title, template.leader_title, template.focus_fact_id,
               template.focus_fact_text, template.focus_fact_context, template.focus_fact_method,
               template.focus_span_start, template.focus_span_end, %s - g.n, %s - g.n,
               template.expires_at_ms, 1, template.admission, template.queue_priority,
               template.provider_score_max, template.engine_type, template.asset_class,
               template.grounded_assets, template.watchlist_hits, template.macro_lexicon,
               template.storyline_key, template.context_line, %s, template.ingest_mode,
               template.trace_id, %s - g.n, %s - g.n, g.n > 1250
          FROM news_events template
          CROSS JOIN generate_series(1, 20000) AS g(n)
         WHERE template.event_id = %s
        ON CONFLICT DO NOTHING
        """,
        (
            stamp - 120_000,
            stamp - 120_000,
            stamp - 120_000,
            stamp - 120_000,
            stamp - 120_000,
            event_id,
        ),
    )
    seed_current_news_evidence(conn)
    conn.execute(
        """
        INSERT INTO news_verdicts (
          event_id, stage, policy_version, judgment_contract_version, judgment_origin,
          rule_baseline_decision, final_decision, override_rule, throttled_by, verdict, editorial,
          scored_judgment_sha256, runtime_manifest_sha, model, program_version, program_sha256,
          degraded, error_code, trace, published_at_ms, created_at_ms, evidence_version,
          evidence_sha256, focus_fact_id
        )
        SELECT e.event_id, template.stage, template.policy_version, template.judgment_contract_version,
               template.judgment_origin, template.rule_baseline_decision, template.final_decision,
               template.override_rule, template.throttled_by, template.verdict, template.editorial,
               template.scored_judgment_sha256, template.runtime_manifest_sha, template.model,
               template.program_version, template.program_sha256, template.degraded, template.error_code,
               template.trace || jsonb_build_object(
                 'evidence_version', evidence.evidence_version,
                 'evidence_sha256', evidence.evidence_sha256,
                 'focus_fact_id', evidence.focus_fact_id
               ),
               %s, %s, evidence.evidence_version, evidence.evidence_sha256, evidence.focus_fact_id
          FROM news_events e
          JOIN news_event_evidence_snapshots evidence ON evidence.event_id = e.event_id
          CROSS JOIN news_verdicts template
         WHERE e.event_id LIKE 'handoff-scale-%%'
           AND evidence.provenance = 'observed' AND evidence.release_eligible
           AND evidence.snapshot ->> 'schema_version' = 'news_event_evidence_v3'
           AND template.event_id = %s AND template.stage = 'triage' AND template.policy_version = %s
        ON CONFLICT DO NOTHING
        """,
        (stamp - 120_000, stamp - 120_000, event_id, policy_version),
    )
    conn.execute(
        """
        UPDATE news_events SET published_at_ms = NULL
         WHERE event_id LIKE 'handoff-scale-%%' AND created_at_ms >= %s
        """,
        (stamp - 120_000 - _EVENT_HANDOFF_STATE_LIMIT,),
    )
    conn.execute(
        """
        UPDATE news_verdicts verdict SET published_at_ms = NULL
          FROM news_events event
         WHERE event.event_id = verdict.event_id
           AND event.event_id LIKE 'handoff-scale-%%' AND event.created_at_ms >= %s
        """,
        (stamp - 120_000 - _VERDICT_HANDOFF_STATE_LIMIT,),
    )
    conn.execute(
        "UPDATE news_events SET opened_at_ms = %s, published_at_ms = NULL WHERE event_id = %s",
        (stamp - 60_000, event_id),
    )
    conn.execute(
        """
        UPDATE news_verdicts SET created_at_ms = %s, published_at_ms = NULL
         WHERE event_id = %s AND stage = 'triage' AND policy_version = %s
        """,
        (stamp - 60_000, event_id, policy_version),
    )
    conn.execute("ANALYZE news_events")
    conn.execute("ANALYZE news_event_evidence_snapshots")
    conn.execute("ANALYZE news_verdicts")
    conn.commit()

    event_plan = "\n".join(
        row["QUERY PLAN"]
        for row in conn.execute(
            "EXPLAIN (ANALYZE, BUFFERS) " + UNPUBLISHED_EVENT_CANDIDATES_SQL,
            (stamp - 15_000, stamp - 30 * 60_000, 50),
        ).fetchall()
    )
    verdict_plan = "\n".join(
        row["QUERY PLAN"]
        for row in conn.execute(
            "EXPLAIN (ANALYZE, BUFFERS) " + UNPUBLISHED_VERDICT_CANDIDATES_SQL,
            (stamp - 15_000, stamp - 30 * 60_000, 50),
        ).fetchall()
    )
    event_state_plan = "\n".join(
        row["QUERY PLAN"]
        for row in conn.execute(
            "EXPLAIN (ANALYZE, BUFFERS) " + _EVENT_HANDOFF_STATE_SQL,
            (
                stamp - 30 * 60_000,
                _EVENT_HANDOFF_STATE_LIMIT,
                stamp - 30 * 60_000,
                _EVENT_HANDOFF_STATE_LIMIT,
            ),
        ).fetchall()
    )
    verdict_state_plan = "\n".join(
        row["QUERY PLAN"]
        for row in conn.execute(
            "EXPLAIN (ANALYZE, BUFFERS) " + _VERDICT_HANDOFF_STATE_SQL,
            (
                stamp - 30 * 60_000,
                _VERDICT_HANDOFF_STATE_LIMIT,
                stamp - 30 * 60_000,
                _VERDICT_HANDOFF_STATE_LIMIT,
            ),
        ).fetchall()
    )
    repos = repositories_for_connection(conn)
    _, event_state = repos.news.event_handoff_scan(
        older_than_ms=stamp - 15_000,
        newer_than_ms=stamp - 30 * 60_000,
    )
    _, verdict_state = repos.news.verdict_handoff_scan(
        older_than_ms=stamp - 15_000,
        newer_than_ms=stamp - 30 * 60_000,
    )
    conn.execute("DELETE FROM news_verdicts WHERE event_id LIKE 'handoff-scale-%%'")
    conn.execute("ALTER TABLE news_event_evidence_snapshots DISABLE TRIGGER trg_news_event_evidence_append_only")
    conn.execute("DELETE FROM news_event_evidence_snapshots WHERE event_id LIKE 'handoff-scale-%%'")
    conn.execute("ALTER TABLE news_event_evidence_snapshots ENABLE TRIGGER trg_news_event_evidence_append_only")
    conn.execute("DELETE FROM news_events WHERE event_id LIKE 'handoff-scale-%%'")
    conn.execute(
        "UPDATE news_events SET published_at_ms = %s WHERE event_id = %s",
        (stamp, event_id),
    )
    conn.execute(
        """
        UPDATE news_verdicts SET published_at_ms = %s
         WHERE event_id = %s AND stage = 'triage' AND policy_version = %s
        """,
        (stamp, event_id, policy_version),
    )
    conn.commit()

    assert any(index in event_plan for index in ("ix_news_events_unpublished", "ix_news_events_current_opened")), (
        event_plan
    )
    assert any(index in verdict_plan for index in ("ix_news_verdicts_unpublished_delivery", "news_verdicts_pkey")), (
        verdict_plan
    )
    assert any(
        index in event_state_plan for index in ("ix_news_events_unpublished", "ix_news_events_current_opened")
    ), event_state_plan
    assert any(
        index in verdict_state_plan for index in ("ix_news_verdicts_unpublished_delivery", "news_verdicts_pkey")
    ), verdict_state_plan
    assert "Seq Scan on news_events" not in event_plan
    assert "Seq Scan on news_verdicts" not in verdict_plan
    assert "Seq Scan on news_events" not in event_state_plan
    assert "Seq Scan on news_verdicts" not in verdict_state_plan
    assert event_state["pending"] == _EVENT_HANDOFF_STATE_LIMIT
    assert verdict_state["pending"] == _VERDICT_HANDOFF_STATE_LIMIT


def test_handoff_scan_bounds_keep_the_deadline_in_pending_until_strict_expiry(conn) -> None:
    event_id, policy_version = _ensure_handoff_facts(conn)
    stamp = now_ms()
    min_age_boundary = stamp - 15_000
    deadline = stamp - 30 * 60_000
    repos = repositories_for_connection(conn)

    conn.execute(
        "UPDATE news_events SET opened_at_ms = %s, published_at_ms = NULL WHERE event_id = %s",
        (min_age_boundary + 1, event_id),
    )
    conn.commit()
    rows, state = repos.news.event_handoff_scan(
        older_than_ms=min_age_boundary,
        newer_than_ms=deadline,
        limit=1,
    )
    assert rows == [] and state["pending"] >= 1

    conn.execute("UPDATE news_events SET opened_at_ms = %s WHERE event_id = %s", (deadline, event_id))
    conn.commit()
    rows, state = repos.news.event_handoff_scan(
        older_than_ms=min_age_boundary,
        newer_than_ms=deadline,
        limit=1,
    )
    assert rows[0]["event_id"] == event_id and rows[0]["opened_at_ms"] == deadline
    assert set(rows[0]) == {"event_id", "dedupe_family", "queue_priority", "trace_id", "opened_at_ms"}
    assert state["pending"] >= 1

    conn.execute("UPDATE news_events SET opened_at_ms = %s WHERE event_id = %s", (deadline - 1, event_id))
    conn.commit()
    rows, state = repos.news.event_handoff_scan(
        older_than_ms=min_age_boundary,
        newer_than_ms=deadline,
        limit=1,
    )
    assert rows == [] and state["expired"] >= 1

    conn.execute(
        """
        UPDATE news_verdicts SET created_at_ms = %s, published_at_ms = NULL
         WHERE event_id = %s AND stage = 'triage' AND policy_version = %s
        """,
        (deadline, event_id, policy_version),
    )
    conn.commit()
    rows, state = repos.news.verdict_handoff_scan(
        older_than_ms=min_age_boundary,
        newer_than_ms=deadline,
        limit=1,
    )
    assert rows[0]["event_id"] == event_id
    assert rows[0]["policy_version"] == policy_version
    assert rows[0]["created_at_ms"] == deadline
    assert {"queue_priority", "trace_id"} <= rows[0].keys()
    assert state["pending"] >= 1

    conn.execute(
        """
        UPDATE news_verdicts SET created_at_ms = %s
         WHERE event_id = %s AND stage = 'triage' AND policy_version = %s
        """,
        (deadline - 1, event_id, policy_version),
    )
    conn.commit()
    rows, state = repos.news.verdict_handoff_scan(
        older_than_ms=min_age_boundary,
        newer_than_ms=deadline,
        limit=1,
    )
    assert rows == [] and state["expired"] >= 1
    conn.execute(
        "UPDATE news_events SET published_at_ms = %s WHERE event_id = %s",
        (stamp, event_id),
    )
    conn.execute(
        """
        UPDATE news_verdicts SET published_at_ms = %s
         WHERE event_id = %s AND stage = 'triage' AND policy_version = %s
        """,
        (stamp, event_id, policy_version),
    )
    conn.commit()


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
    assert verdict["program_version"] == "news_oi_signal_v2"
    assert verdict["trace"]["oi_signal"] == {
        "parsed": False,
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
