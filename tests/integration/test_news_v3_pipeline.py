"""News V3 integration: migration shape, Deduper transaction, storyline status, verdict/delivery idempotency."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from psycopg.errors import RaiseException

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.repositories import repositories_for_connection
from tracefold.news.events import admit_frame, admit_item
from tracefold.news.models import TRIAGE_POLICY_VERSION, TriageVerdict
from tracefold.news.opennews import parse_opennews_message
from tracefold.news.triage_rules import DecidePolicy, GateFacts, decide, storyline_status

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
    "news_deliveries",
    "news_control_state",
    "news_reviews",
    "news_external_miss_snapshots",
    "news_learning_artifacts",
    "news_learning_cases",
    "news_model_recordings",
    "news_canary_activations",
    "news_agent_assignments",
    "news_agent_runtime_manifests",
    "news_learning_retention_state",
    "news_review_task_source_v1",
    # #112 security-barrier views expose only the rows each runtime role is
    # allowed to read; information_schema reports views in this relation too.
    "news_review_records_v1",
    "news_review_external_source_v1",
    "news_review_pairwise_tasks_v1",
    "news_review_active_agent_v1",
    # #75 instrument universe: a News-owned provider fact table plus its alias map.
    "news_market_instruments",
    "news_symbol_aliases",
    # #88 price review plane: latest-only current quotes, versioned deterministic Event Reactions.
    "news_quote_snapshots",
    "news_event_reactions",
    # #112 immutable evidence actually read by the SemanticJudge.
    "news_event_evidence_snapshots",
}


@pytest.fixture(scope="module")
def conn():
    connection = connect_postgres_test(read_only=False)
    migrate(connection)
    yield connection
    connection.close()


def _events():
    hits = json.loads(FIXTURE.read_text(encoding="utf-8"))
    out = []
    for hit in sorted(hits, key=lambda h: str(h.get("ts") or "")):
        event = parse_opennews_message({"method": "strategy.triggered", "params": hit})
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
        "news_title_presentations",
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


def test_reader_ledger_and_verdict_idempotency(conn) -> None:
    repos = repositories_for_connection(conn)
    row = conn.execute(
        "SELECT event_id, storyline_key, grounded_assets, provider_score_max, priority, admission, opened_at_ms"
        " FROM news_events WHERE admission='candidate' AND priority='normal' ORDER BY opened_at_ms LIMIT 1"
    ).fetchone()
    assert row is not None
    now_ms = int(row["opened_at_ms"]) + 60_000
    verdict = TriageVerdict(
        novelty="new_fact",
        event_type="macro",
        assets=[],
        direction="bullish",
        scope="macro",
        magnitude=2,
        actionable=True,
        confidence=0.7,
        decision="push",
        headline_zh="测试",
        why_zh="",
    )
    facts = GateFacts(
        grounded_assets=tuple(row["grounded_assets"] or []),
        watchlist_symbols=frozenset(),
        provider_score=row["provider_score_max"],
        priority=row["priority"],
        admission=row["admission"],
    )
    status0 = storyline_status(row["storyline_key"])
    first = decide(verdict, facts, status0)
    assert first.final == "push"
    evidence = repos.news.latest_evidence_snapshot(row["event_id"])
    assert evidence is not None
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
            evidence_version=int(evidence["evidence_version"]),
            evidence_sha256=str(evidence["evidence_sha256"]),
            focus_fact_id=str(evidence["focus_fact_id"]),
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
            evidence_version=int(evidence["evidence_version"]),
            evidence_sha256=str(evidence["evidence_sha256"]),
            focus_fact_id=str(evidence["focus_fact_id"]),
            now_ms=now_ms,
        )
        assert again is False
    status1 = storyline_status(row["storyline_key"])
    second = decide(verdict, facts, status1, policy=DecidePolicy(similarity_max=0.0))
    assert second.final == "push" and second.throttled_by is None
    seen = storyline_status(
        row["storyline_key"],
        seen=[{"event_id": row["event_id"], "headline_zh": verdict.headline_zh}],
    )
    repeated = decide(verdict, facts, seen)
    assert repeated.final == "throttled" and repeated.throttled_by.endswith(":seen")
    distinct = decide(verdict.model_copy(update={"headline_zh": "另一件完全不同的事情"}), facts, seen)
    assert distinct.final == "push" and distinct.override_rule == "model_push_actionable"
    # A decision is only a reservation.  With no settled first delivery there
    # is no ReaderReceipt and therefore no semantic told memory.
    assert repos.news.told_ledger(now_ms=now_ms + 1000, window_ms=4 * 3600_000, limit=12) == []
    assert repos.news.told_ledger(now_ms=now_ms + 5 * 3600_000, window_ms=4 * 3600_000, limit=12) == []
    # A card the reader never received (first delivery terminalised) leaves the ledger; a sent one stays; and the
    # preliminary storyline's own cards are fetched even when the global limit would not reach them.
    later = now_ms + 1000
    window = 4 * 3600_000
    with repos.transaction():
        assert repos.news.begin_delivery(event_id=row["event_id"], kind="first", card={}, now_ms=now_ms + 10) == "new"
        assert repos.news.settle_delivery(
            event_id=row["event_id"],
            kind="first",
            state="terminal",
            receipt=None,
            error_code="delivery_unavailable",
            now_ms=now_ms + 20,
        )
    assert repos.news.told_ledger(now_ms=later, window_ms=window, limit=12) == []
    with repos.transaction():
        conn.execute("DELETE FROM news_deliveries WHERE event_id = %s", (row["event_id"],))
        assert repos.news.begin_delivery(event_id=row["event_id"], kind="first", card={}, now_ms=now_ms + 10) == "new"
        assert repos.news.settle_delivery(
            event_id=row["event_id"],
            kind="first",
            state="sent",
            receipt={"ok": True},
            error_code=None,
            now_ms=now_ms + 20,
        )
    told = repos.news.told_ledger(now_ms=later, window_ms=window, limit=12)
    assert [t["event_id"] for t in told] == [row["event_id"]]
    assert told[0]["headline_zh"] == "测试" and told[0]["magnitude"] == 2 and told[0]["direction"] == "bullish"
    assert told[0]["storyline_key"] == row["storyline_key"] and told[0]["at_ms"] == now_ms + 20
    preferred = repos.news.told_ledger(now_ms=later, window_ms=window, limit=1, prefer_key=row["storyline_key"])
    assert [t["event_id"] for t in preferred] == [row["event_id"]]
    assert len(repos.news.told_ledger(now_ms=later, window_ms=window, limit=12, prefer_key="asset:NOPE")) == 1
    with repos.transaction():
        conn.execute("DELETE FROM news_deliveries WHERE event_id = %s", (row["event_id"],))
    # A grounded restatement of that card drops, and the storyline lock is a plain transaction-scoped advisory lock.
    told_status = storyline_status(
        row["storyline_key"],
        told=[{"i": 0, "dir": t["direction"], "headline_zh": t["headline_zh"]} for t in told],
    )
    restated = decide(verdict.model_copy(update={"novelty": "restatement", "restates": 0}), facts, told_status)
    assert restated.final == "drop" and restated.override_rule == "restatement"
    held_sql = (
        "SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' AND classid = %s AND pid = pg_backend_pid()"
    )
    with repos.transaction():
        repos.news.lock_storyline(row["storyline_key"])
        held = conn.execute(held_sql, (0x4E455753,)).fetchone()
        assert held is not None and int(held["n"]) == 1
    released = conn.execute(held_sql, (0x4E455753,)).fetchone()
    assert released is not None and int(released["n"]) == 0
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
    delivered_row = next(e for e in feed["events"] if e["event_id"] == event_id)
    assert delivered_row["outcome"]["kind"] == "delivered" and delivered_row["outcome"]["group"] == "pushed"
    assert detail["outcome"]["kind"] == "delivered"
    # This test writes the delivery without a verdict, so the timeline has no triage/decide steps.
    assert [step["stage"] for step in detail["timeline"]] == ["received", "gate", "delivery"]

    def _feed(**over):
        base = dict(
            family=None,
            admission=None,
            priority=None,
            decision=None,
            symbol=None,
            q=None,
            sort="latest",
            limit=10_000,
            cursor=None,
        )
        base.update(over)
        return repos.news.list_feed(**base)

    pushed_ids = {e["event_id"] for e in _feed(outcome="pushed")["events"]}
    held_ids = {e["event_id"] for e in _feed(outcome="held")["events"]}
    pending_ids = {e["event_id"] for e in _feed(outcome="pending")["events"]}
    everything = _feed()["events"]
    all_ids = {e["event_id"] for e in everything}
    assert event_id in pushed_ids and pushed_ids.isdisjoint(held_ids) and pushed_ids.isdisjoint(pending_ids)
    assert held_ids.isdisjoint(pending_ids) and pushed_ids | held_ids | pending_ids == all_ids
    for group, ids in (("pushed", pushed_ids), ("held", held_ids), ("pending", pending_ids)):
        assert all(e["outcome"]["group"] == group for e in everything if e["event_id"] in ids)
    # The tab counts describe the whole filtered set, so they must agree with the per-group listings and stay
    # unchanged when the reader picks one tab.
    counts = _feed()["counts"]
    assert counts == {
        "total": len(all_ids),
        "pushed": len(pushed_ids),
        "held": len(held_ids),
        "pending": len(pending_ids),
    }
    assert counts["total"] == counts["pushed"] + counts["held"] + counts["pending"]
    assert _feed(outcome="held")["counts"] == counts
    # A window the reader narrows narrows the counts with it; a paged request reuses the first page's.
    assert _feed(hours=1, now_ms=10_000_000_000_000)["counts"] == {
        "total": 0,
        "pushed": 0,
        "held": 0,
        "pending": 0,
    }
    first = _feed(limit=1)
    assert first["counts"] == counts and first["next_cursor"]
    assert _feed(limit=1, cursor=first["next_cursor"])["counts"] is None
    assert _feed(hours=1, now_ms=10_000_000_000_000)["events"] == []
    status = repos.news.status_snapshot(now_ms=10_000_000_000_000)
    assert status["delivery"]["sent_24h"] >= 0 and "pipeline" in status
    assert status["learning_retention"]["eligible_recordings"] == 0
    conn.commit()


def test_reader_receipt_uses_actual_degraded_card_and_keeps_ambiguous_unknown(conn) -> None:
    repos = repositories_for_connection(conn)
    candidates = conn.execute(
        """
        SELECT e.event_id FROM news_events e
         WHERE NOT EXISTS (SELECT 1 FROM news_verdicts v WHERE v.event_id = e.event_id)
         ORDER BY e.opened_at_ms LIMIT 2
        """
    ).fetchall()
    assert len(candidates) == 2
    sent_event, ambiguous_event = str(candidates[0]["event_id"]), str(candidates[1]["event_id"])
    evidence = repos.news.latest_evidence_snapshot(sent_event)
    assert evidence is not None
    verdict = TriageVerdict(
        novelty="new_fact",
        event_type="macro",
        assets=[],
        direction="unclear",
        scope="macro",
        magnitude=2,
        actionable=True,
        confidence=0.0,
        decision="push",
        headline_zh="模型占位文字",
        why_zh="",
    )
    degraded_card = {"header": {"title": {"tag": "plain_text", "content": "实际降级卡片"}}}
    with repos.transaction():
        assert repos.news.insert_verdict(
            event_id=sent_event,
            stage="triage",
            policy_version="news_triage_policy_receipt_test",
            model_decision=None,
            rule_baseline_decision="push",
            final_decision="push",
            override_rule="rule_baseline",
            throttled_by=None,
            verdict=verdict.model_dump(),
            model=None,
            prompt_version=None,
            degraded=True,
            error_code="news_triage_timeout",
            trace={},
            evidence_version=int(evidence["evidence_version"]),
            evidence_sha256=str(evidence["evidence_sha256"]),
            focus_fact_id=str(evidence["focus_fact_id"]),
            now_ms=10_000,
        )
        assert repos.news.begin_delivery(event_id=sent_event, kind="first", card=degraded_card, now_ms=10_100) == "new"
    # A sending reservation is not a receipt.
    assert repos.news.told_ledger(now_ms=10_150, window_ms=3600_000, limit=12) == []
    with repos.transaction():
        assert repos.news.settle_delivery(
            event_id=sent_event, kind="first", state="sent", receipt={"ok": True}, error_code=None, now_ms=10_200
        )
        assert repos.news.begin_delivery(event_id=ambiguous_event, kind="first", card={}, now_ms=20_000) == "new"
        assert repos.news.terminalize_interrupted_deliveries(now_ms=81_001) == 1

    told = repos.news.told_ledger(now_ms=10_300, window_ms=3600_000, limit=12)
    assert len(told) == 1 and told[0]["event_id"] == sent_event and told[0]["headline_zh"] == "实际降级卡片"
    sent_detail = repos.news.event_detail(sent_event)
    ambiguous_detail = repos.news.event_detail(ambiguous_event)
    assert sent_detail is not None and sent_detail["reader_receipt"]["state"] == "received"
    assert sent_detail["reader_receipt"]["rendered_card"] == degraded_card
    assert ambiguous_detail is not None and ambiguous_detail["reader_receipt"]["state"] == "unknown"
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
        repos.news.update_broker_snapshot(
            snapshot={"connected": True, "queues": {"news.raw": {"messages": 0, "consumers": 1}}}, now_ms=7
        )
    status = repos.news.status_snapshot(now_ms=10)
    assert status["broker"]["connected"] is True and status["broker"]["queues"]["news.raw"]["consumers"] == 1
    conn.commit()


def _hit(*, hit_id: int, text: str, engine: str, score: int, coins: list[dict], source: str, ts: str) -> dict:
    return {
        "id": hit_id,
        "text": text,
        "link": f"https://example.test/{hit_id}",
        "source": source,
        "newsType": "twitter" if engine == "meme" else "news",
        "engineType": engine,
        "ts": ts,
        "aiRating": {"score": score, "signal": "long", "status": "done"},
        "coins": [
            {"expired": False, "grade": c.get("grade"), "market_type": "cex", "score": score, "symbol": c["symbol"]}
            for c in coins
        ],
        "strategy": {"id": 1018, "name": "News Score > 70", "engine_type": engine, "source_type": "news"},
    }


def test_explicit_multi_fact_item_creates_one_focused_event_per_fact(conn) -> None:
    repos = repositories_for_connection(conn)
    raw = (
        "市场快讯：<br/>1. 商务部反对欧方打压中国企业并要求纠正。<br/>"
        "2. Moderna 下调全年指引，盘前下跌 13%。<br/>"
        "3. 沃尔玛上调全年销售预期至 4.8%。"
    )
    event = parse_opennews_message(
        {
            "method": "strategy.triggered",
            "params": _hit(
                hit_id=920001,
                text=raw,
                engine="news",
                score=80,
                coins=[],
                source="wire",
                ts="2026-08-18T21:00:00+08:00",
            ),
        },
    )
    assert event is not None
    stamp = int(event.entry.published_at_ms or 0) + 1000
    with repos.transaction():
        batch = admit_frame(
            repos,
            event=event,
            ingest_mode="live",
            observed_at_ms=stamp,
            trace_id="fact-batch",
            watchlist_symbols=frozenset(),
            now_ms=stamp,
        )
    assert batch.item_inserted and len(batch.results) == 3
    assert len({result.event_id for result in batch.results}) == 3
    rows = conn.execute(
        """
        SELECT e.event_id, e.focus_fact_id, e.focus_fact_text, e.focus_fact_method,
               s.evidence_version, s.snapshot #>> '{card,leader_description}' AS content
          FROM news_events e
          JOIN news_event_evidence_snapshots s
            ON s.event_id = e.event_id AND s.evidence_version = 1
         WHERE e.leader_item_id = %s ORDER BY e.focus_fact_text
        """,
        (batch.item_id,),
    ).fetchall()
    assert len(rows) == 3
    assert {row["focus_fact_method"] for row in rows} == {"explicit_numbered"}
    assert {int(row["evidence_version"]) for row in rows} == {1}
    assert all(row["content"] == "市场快讯：" for row in rows)
    assert any(str(row["focus_fact_text"]).startswith("Moderna") for row in rows)
    conn.commit()


def test_a_stronger_member_regates_a_suppressed_event_and_publishes_it_once(conn) -> None:
    """A low-score ungrounded post opens a suppressed Event (low-signal switch on); the same headline arriving from a
    news source with score 85 and a grade-A tag re-gates the Event to candidate and reports it as publishable once."""

    repos = repositories_for_connection(conn)
    text = "SafePal discloses a security breach exposing personal data of nearly 40,000 customers"
    first = parse_opennews_message(
        {
            "method": "strategy.triggered",
            "params": _hit(
                hit_id=910001,
                text=text,
                engine="meme",
                score=60,
                coins=[],
                source="someone",
                ts="2026-08-18T20:00:00+08:00",
            ),
        },
    )
    second = parse_opennews_message(
        {
            "method": "strategy.triggered",
            "params": _hit(
                hit_id=910002,
                text=text,
                engine="news",
                score=85,
                coins=[{"symbol": "SFP", "grade": "A"}],
                source="CoinDesk",
                ts="2026-08-18T20:01:00+08:00",
            ),
        },
    )
    assert first is not None and second is not None
    now_ms = int(second.entry.published_at_ms or 0) + 1000
    with repos.transaction():
        opened = admit_item(
            repos,
            event=first,
            ingest_mode="live",
            observed_at_ms=now_ms,
            trace_id="t",
            watchlist_symbols=frozenset(),
            now_ms=now_ms,
            suppress_low_signal=True,
        )
        assert opened.event_created and opened.admission == "suppressed_low_signal"
        first_evidence = repos.news.latest_evidence_snapshot(opened.event_id)
        assert first_evidence is not None and int(first_evidence["evidence_version"]) == 1
        joined = admit_item(
            repos,
            event=second,
            ingest_mode="live",
            observed_at_ms=now_ms,
            trace_id="t",
            watchlist_symbols=frozenset(),
            now_ms=now_ms,
            suppress_low_signal=True,
        )
        assert joined.event_id == opened.event_id and joined.match_kind == "exact"
        assert joined.admission == "candidate" and joined.event_created is True  # publish once, like a new candidate
        again = admit_item(
            repos,
            event=second,
            ingest_mode="live",
            observed_at_ms=now_ms,
            trace_id="t",
            watchlist_symbols=frozenset(),
            now_ms=now_ms,
            suppress_low_signal=True,
        )
        assert again.event_created is False and again.admission == "candidate"  # idempotent replay
        latest_evidence = repos.news.latest_evidence_snapshot(opened.event_id)
        assert latest_evidence is not None and int(latest_evidence["evidence_version"]) == 2
        assert len(latest_evidence["snapshot"]["members"]) == 2
        assert latest_evidence["snapshot"]["card"]["provider_score_max"] == 85.0
        assert latest_evidence["snapshot"]["focus_fact"]["text"] == text
        assert latest_evidence["snapshot"]["card"]["reporting_origin"] == "coindesk"
        assert latest_evidence["snapshot"]["card"]["leader_item_id"] == joined.item_id
        assert repos.news.event_card(opened.event_id)["reporting_origin"] == "coindesk"
    row = conn.execute(
        "SELECT admission, grounded_assets, member_count, provider_score_max FROM news_events WHERE event_id = %s",
        (opened.event_id,),
    ).fetchone()
    assert row["admission"] == "candidate" and list(row["grounded_assets"]) == ["SFP"]
    assert row["member_count"] == 2 and float(row["provider_score_max"]) == 85.0
    assets = conn.execute("SELECT symbol FROM news_event_assets WHERE event_id = %s", (opened.event_id,)).fetchall()
    assert {r["symbol"] for r in assets} == {"SFP"}
    conn.commit()


def test_evidence_snapshots_are_append_only_and_outlive_event_retention(conn) -> None:
    repos = repositories_for_connection(conn)
    event = parse_opennews_message(
        {
            "method": "strategy.triggered",
            "params": _hit(
                hit_id=910099,
                text="Micron says DRAM contract prices rose again in August",
                engine="news",
                score=82,
                coins=[],
                source="Reuters",
                ts="2026-08-18T21:00:00+08:00",
            ),
        },
    )
    assert event is not None
    now_ms = int(event.entry.published_at_ms or 0) + 1000
    with repos.transaction():
        opened = admit_item(
            repos,
            event=event,
            ingest_mode="live",
            observed_at_ms=now_ms,
            trace_id="retention-evidence",
            watchlist_symbols=frozenset(),
            now_ms=now_ms,
        )
    snapshot = repos.news.latest_evidence_snapshot(opened.event_id)
    assert snapshot is not None

    conn.execute("BEGIN")
    conn.execute("SAVEPOINT evidence_mutation")
    with pytest.raises(RaiseException, match="news_event_evidence_append_only"):
        conn.execute(
            "UPDATE news_event_evidence_snapshots SET release_eligible = false "
            "WHERE event_id = %s AND evidence_version = %s",
            (opened.event_id, snapshot["evidence_version"]),
        )
    conn.execute("ROLLBACK TO SAVEPOINT evidence_mutation")
    conn.execute("RELEASE SAVEPOINT evidence_mutation")

    conn.execute("DELETE FROM news_items WHERE item_id = %s", (opened.item_id,))
    assert conn.execute("SELECT 1 FROM news_events WHERE event_id = %s", (opened.event_id,)).fetchone() is None
    retained = conn.execute(
        "SELECT provenance, release_eligible FROM news_event_evidence_snapshots WHERE event_id = %s",
        (opened.event_id,),
    ).fetchone()
    assert retained == {"provenance": "observed", "release_eligible": True}
    conn.commit()


def test_explain_event_prints_the_chain_with_a_one_line_outcome(conn) -> None:
    from tracefold.news.eval.why import explain_event

    repos = repositories_for_connection(conn)
    with_verdict = conn.execute(
        "SELECT event_id FROM news_verdicts WHERE stage = 'triage' ORDER BY created_at_ms LIMIT 1"
    ).fetchone()
    suppressed = conn.execute(
        "SELECT event_id FROM news_events WHERE admission = 'suppressed_pr_template' ORDER BY opened_at_ms LIMIT 1"
    ).fetchone()
    assert with_verdict is not None and suppressed is not None
    explained = explain_event(repos, with_verdict["event_id"])
    assert explained is not None
    stages = [step["stage"] for step in explained["chain"]]
    assert stages[:4] == ["item", "gate", "triage", "decide"]
    assert explained["chain"][0]["provider_coins"] is not None
    assert explained["outcome"]
    held = explain_event(repos, suppressed["event_id"])
    assert held is not None and held["outcome"] == "未送审：律所推广模板，规则直接拦截"
    assert held["outcome_kind"] == "held_gate" and [s["stage"] for s in held["timeline"]] == ["received", "gate"]
    assert [step["stage"] for step in held["chain"]] == ["item", "gate"]
    assert explain_event(repos, "does-not-exist") is None
