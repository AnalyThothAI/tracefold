"""News V3 integration: migration shape, Deduper transaction, storyline status, verdict/delivery idempotency."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.repositories import repositories_for_connection
from tracefold.news.events import admit_item
from tracefold.news.models import TRIAGE_POLICY_VERSION, TriageVerdict
from tracefold.news.opennews import parse_opennews_message
from tracefold.news.triage_rules import GateFacts, decide, storyline_status_from_row

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "news_v3_hits_sample.json"
NEWS_TABLES = {
    "news_ingest_state",
    "news_opennews_incidents",
    "news_items",
    "news_events",
    "news_event_members",
    "news_event_bands",
    "news_event_assets",
    "news_verdicts",
    "news_title_presentations",
    "news_deliveries",
    "news_control_state",
    "news_event_market_marks",
    "news_event_labels",
}


@pytest.fixture(scope="module")
def conn():
    connection = connect_postgres_test(read_only=False)
    migrate(connection)
    yield connection
    connection.close()


def _events(strategy_ids=("1018", "1352", "1353")):
    hits = json.loads(FIXTURE.read_text(encoding="utf-8"))
    out = []
    for hit in sorted(hits, key=lambda h: str(h.get("ts") or "")):
        event = parse_opennews_message(
            {"method": "strategy.triggered", "params": hit}, strategy_ids=frozenset(strategy_ids)
        )
        if event is not None:
            out.append(event)
    return out


def test_migration_creates_exact_news_tables_and_drops_legacy(conn) -> None:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name LIKE 'news_%'"
    ).fetchall()
    names = {r["table_name"] for r in rows}
    assert names == NEWS_TABLES
    for legacy in (
        "news_stories",
        "news_push_state",
        "news_brief_current",
        "news_sources",
        "news_item_title_presentations",
    ):
        assert legacy not in names


def test_deduper_is_idempotent_and_merges_exact_and_near(conn) -> None:
    repos = repositories_for_connection(conn)
    events = _events()
    now_ms = max(int(e.entry.published_at_ms or 0) for e in events) + 60_000
    created = 0
    with repos.transaction():
        for event in events:
            result = admit_item(
                repos,
                event=event,
                ingest_mode="live",
                observed_at_ms=now_ms,
                trace_id="t",
                watchlist_symbols=frozenset({"BTC", "NVDA"}),
                now_ms=now_ms,
            )
            created += int(result.event_created)
    # replay the same frames: zero new items, zero new events
    with repos.transaction():
        for event in events:
            result = admit_item(
                repos,
                event=event,
                ingest_mode="live",
                observed_at_ms=now_ms,
                trace_id="t",
                watchlist_symbols=frozenset(),
                now_ms=now_ms,
            )
            assert result.item_inserted is False and result.event_created is False
    counts = conn.execute(
        "SELECT (SELECT count(*) FROM news_items) AS items, (SELECT count(*) FROM news_events) AS events,"
        " (SELECT count(*) FROM news_event_members) AS members"
    ).fetchone()
    assert counts["items"] == len(events) and counts["events"] == created and counts["members"] == len(events)
    cfx = conn.execute(
        "SELECT member_count FROM news_events WHERE leader_title ILIKE '%Conflux Network (CFX)%'"
    ).fetchall()
    assert len(cfx) == 1 and cfx[0]["member_count"] >= 5
    conn.commit()


def test_storyline_status_and_verdict_idempotency(conn) -> None:
    repos = repositories_for_connection(conn)
    row = conn.execute(
        "SELECT event_id, storyline_key, grounded_assets, provider_score_max, priority, admission, opened_at_ms"
        " FROM news_events WHERE admission='candidate' AND priority='normal' ORDER BY opened_at_ms LIMIT 1"
    ).fetchone()
    assert row is not None
    now_ms = int(row["opened_at_ms"]) + 60_000
    verdict = TriageVerdict(
        event_type="macro",
        assets=[],
        direction="bullish",
        scope="macro",
        magnitude=2,
        actionable=True,
        confidence=0.7,
        decision="push",
        headline_zh="测试",
        rationale="",
    )
    facts = GateFacts(
        grounded_assets=tuple(row["grounded_assets"] or []),
        watchlist_symbols=frozenset(),
        provider_score=row["provider_score_max"],
        priority=row["priority"],
        admission=row["admission"],
    )
    status0 = storyline_status_from_row(
        repos.news.event_status(storyline_key=row["storyline_key"], now_ms=now_ms), row["storyline_key"]
    )
    first = decide(verdict, facts, status0)
    assert first.final == "push"
    with repos.transaction():
        inserted = repos.news.insert_verdict(
            event_id=row["event_id"],
            stage="triage",
            policy_version=TRIAGE_POLICY_VERSION,
            model_decision="push",
            rule_baseline_decision=first.rule_baseline,
            final_decision=first.final,
            override_rule=first.override_rule,
            throttled_by=None,
            verdict=verdict.model_dump(),
            model="test",
            prompt_version="p",
            degraded=False,
            error_code=None,
            trace={"latency_ms": 5},
            now_ms=now_ms,
        )
        assert inserted is True
        again = repos.news.insert_verdict(
            event_id=row["event_id"],
            stage="triage",
            policy_version=TRIAGE_POLICY_VERSION,
            model_decision="push",
            rule_baseline_decision=first.rule_baseline,
            final_decision=first.final,
            override_rule=first.override_rule,
            throttled_by=None,
            verdict=verdict.model_dump(),
            model="test",
            prompt_version="p",
            degraded=False,
            error_code=None,
            trace={},
            now_ms=now_ms,
        )
        assert again is False
    status1 = storyline_status_from_row(
        repos.news.event_status(storyline_key=row["storyline_key"], now_ms=now_ms + 1000), row["storyline_key"]
    )
    assert status1.pushed_2h == 1 and status1.max_magnitude_2h == 2
    second = decide(verdict, facts, status1)
    assert second.final == "throttled" and second.throttled_by == f"storyline:{row['storyline_key']}"
    conn.commit()


def test_delivery_begin_settle_and_ambiguous_after_crash(conn) -> None:
    repos = repositories_for_connection(conn)
    row = conn.execute("SELECT event_id FROM news_events ORDER BY opened_at_ms DESC LIMIT 1").fetchone()
    event_id = row["event_id"]
    with repos.transaction():
        assert repos.news.begin_delivery(event_id=event_id, kind="first", card={"x": 1}, now_ms=1_000) == "new"
        assert repos.news.begin_delivery(event_id=event_id, kind="first", card={"x": 1}, now_ms=1_000) == "sending"
        assert repos.news.settle_delivery(
            event_id=event_id, kind="first", state="sent", receipt={"code": 0}, error_code=None, now_ms=2_000
        )
        assert repos.news.begin_delivery(event_id=event_id, kind="first", card={}, now_ms=3_000) == "sent"
        assert repos.news.sent_count_since(since_ms=0) == 1
    detail = repos.news.event_detail(event_id)
    assert detail is not None and detail["deliveries"][0]["state"] == "sent"
    feed = repos.news.list_feed(
        family=None,
        admission=None,
        priority=None,
        decision=None,
        symbol=None,
        q=None,
        sort="latest",
        limit=100,
        cursor=None,
    )
    assert feed["events"] and any(e["event_id"] == event_id for e in feed["events"])
    status = repos.news.status_snapshot(now_ms=10_000_000_000_000)
    assert status["delivery"]["sent_24h"] >= 0 and "pipeline" in status
    conn.commit()


def test_control_state_and_incidents(conn) -> None:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        planned = repos.news.open_incident(cause_class="planned_shutdown", now_ms=50, planned=True)
        incident = repos.news.open_incident(cause_class="network_connect", now_ms=100)
        assert repos.news.open_incident(cause_class="network_connect", now_ms=200) == incident
        assert repos.news.close_open_incidents(cause_classes=["network_connect", "planned_shutdown"], now_ms=300) == 2
        assert planned != incident
        pending = repos.news.pending_recovery_incidents()
        assert any(int(p["incident_id"]) == incident for p in pending)
        repos.news.write_control(
            paused=True, mutes=[{"kind": "theme", "key": "rates", "until_ms": 999_999_999_999_999}], now_ms=1
        )
    control = repos.news.read_control(now_ms=5)
    assert control["paused"] is True and control["mutes"][0]["key"] == "rates"
    with repos.transaction():
        repos.news.update_broker_snapshot(snapshot={"connected": True, "queues": {"news.raw": {"messages": 0, "consumers": 1}}}, now_ms=7)
    status = repos.news.status_snapshot(now_ms=10)
    assert status["broker"]["connected"] is True and status["broker"]["queues"]["news.raw"]["consumers"] == 1
    conn.commit()
