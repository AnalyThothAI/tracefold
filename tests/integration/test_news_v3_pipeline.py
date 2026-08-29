"""News V3 integration: migration shape, Deduper transaction, storyline status, verdict/delivery idempotency."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from psycopg.errors import CheckViolation, RaiseException

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_judgment import scored_judgment
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.models import TRIAGE_POLICY_VERSION, TriageVerdict
from tracefold.news.oi_signals import PROGRAM_VERSION as OI_PROGRAM_VERSION
from tracefold.news.oi_signals import OiPolicy, OiSignal, evaluate_oi, oi_parse_failure, oi_source_contract
from tracefold.news.opennews import parse_opennews_message, source_artifact_identity
from tracefold.news.pipeline.admission import admit_frame, admit_item
from tracefold.news.program.runtime import PROGRAM_VERSION as SEMANTIC_PROGRAM_VERSION
from tracefold.news.search import compile_news_search
from tracefold.news.storage.operations import RECOVERY_BACKLOG_LIMIT
from tracefold.news.triage_rules import DecidePolicy, GateFacts, decide, fallback_verdict, storyline_status

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "news_v3_hits_sample.json"
NEWS_TABLES = {
    "news_ingest_state",
    "news_opennews_incidents",
    "news_items",
    "news_events",
    "news_current_events_v1",
    "news_event_members",
    "news_event_bands",
    "news_event_assets",
    "news_verdicts",
    "news_deliveries",
    "news_reviews",
    "news_external_miss_snapshots",
    "news_learning_artifacts",
    "news_learning_epochs",
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
    "news_market_instrument_listing_events",
    "news_symbol_aliases",
    # #88 price review plane: latest-only current quotes, versioned deterministic Event Reactions.
    "news_quote_snapshots",
    "news_event_reactions",
    "news_oi_signals",
    "news_market_liquidations",
    # #112 immutable evidence actually read by the SemanticJudge.
    "news_event_evidence_snapshots",
}


@pytest.fixture(scope="module")
def conn(postgres_module_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
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
        "SELECT event_kind, member_count FROM news_events WHERE leader_title ILIKE '%Conflux Network (CFX)%'"
    ).fetchall()
    assert {row["event_kind"] for row in cfx} == {"news", "listing"}
    assert all(row["member_count"] >= 4 for row in cfx)
    conn.commit()


def test_reader_ledger_and_verdict_idempotency(conn) -> None:
    repos = repositories_for_connection(conn)
    row = conn.execute(
        "SELECT event_id, storyline_key, grounded_assets, admission, opened_at_ms"
        " FROM news_events WHERE admission='candidate' AND queue_priority='normal' ORDER BY opened_at_ms LIMIT 1"
    ).fetchone()
    assert row is not None
    now_ms = int(row["opened_at_ms"]) + 60_000
    verdict = TriageVerdict(
        novelty="new_fact",
        assets=[],
        direction="bullish",
        scope="macro",
        magnitude=2,
        confidence=0.7,
        headline_zh="测试",
        why_zh="",
    )
    judgment = scored_judgment(verdict)
    facts = GateFacts(
        grounded_assets=tuple(row["grounded_assets"] or []),
        watchlist_symbols=frozenset(),
        admission=row["admission"],
    )
    status0 = storyline_status(row["storyline_key"])
    first = decide(judgment, facts, status0)
    assert first.final == "push"
    evidence = repos.news.latest_evidence_snapshot(row["event_id"])
    assert evidence is not None
    runtime_manifest_sha = "b" * 64
    trace = {
        "judgment_contract_version": judgment.judgment_contract_version,
        "judgment_origin": "model",
        "judgment_sha256": judgment.scored_judgment_sha256,
        "verdict_sha256": canonical_sha(verdict.model_dump(mode="json")),
        "editorial_sha256": judgment.editorial.editorial_sha256,
        "runtime_manifest_sha": runtime_manifest_sha,
        "program_version": SEMANTIC_PROGRAM_VERSION,
        "program_sha256": "a" * 64,
        "evidence_version": int(evidence["evidence_version"]),
        "evidence_sha256": str(evidence["evidence_sha256"]),
        "focus_fact_id": str(evidence["focus_fact_id"]),
        "told": [],
        "told_count": 0,
    }
    with repos.transaction():
        inserted = repos.news.insert_verdict(
            event_id=row["event_id"],
            stage="triage",
            policy_version=TRIAGE_POLICY_VERSION,
            judgment_contract_version=judgment.judgment_contract_version,
            judgment_origin="model",
            rule_baseline_decision=first.rule_baseline,
            final_decision=first.final,
            override_rule=first.override_rule,
            throttled_by=None,
            verdict=verdict.model_dump(),
            model_editorial=judgment.editorial.model_dump(mode="json"),
            judgment_sha256=judgment.scored_judgment_sha256,
            runtime_manifest_sha=runtime_manifest_sha,
            model="test",
            program_version=SEMANTIC_PROGRAM_VERSION,
            program_sha256="a" * 64,
            degraded=False,
            error_code=None,
            trace={**trace, "latency_ms": 5},
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
            judgment_contract_version=judgment.judgment_contract_version,
            judgment_origin="model",
            rule_baseline_decision=first.rule_baseline,
            final_decision=first.final,
            override_rule=first.override_rule,
            throttled_by=None,
            verdict=verdict.model_dump(),
            model_editorial=judgment.editorial.model_dump(mode="json"),
            judgment_sha256=judgment.scored_judgment_sha256,
            runtime_manifest_sha=runtime_manifest_sha,
            model="test",
            program_version=SEMANTIC_PROGRAM_VERSION,
            program_sha256="a" * 64,
            degraded=False,
            error_code=None,
            trace=trace,
            evidence_version=int(evidence["evidence_version"]),
            evidence_sha256=str(evidence["evidence_sha256"]),
            focus_fact_id=str(evidence["focus_fact_id"]),
            now_ms=now_ms,
        )
        assert again is False
    status1 = storyline_status(row["storyline_key"])
    second = decide(judgment, facts, status1, policy=DecidePolicy(similarity_max=0.0))
    assert second.final == "push" and second.throttled_by is None
    seen = storyline_status(
        row["storyline_key"],
        seen=[{"event_id": row["event_id"], "headline_zh": verdict.headline_zh}],
    )
    repeated = decide(judgment, facts, seen)
    assert repeated.final == "throttled" and repeated.throttled_by.endswith(":seen")
    distinct_judgment = scored_judgment(verdict.model_copy(update={"headline_zh": "另一件完全不同的事情"}))
    distinct = decide(distinct_judgment, facts, seen)
    assert distinct.final == "push" and distinct.override_rule == "trade_relevance_realtime"
    # A decision is only a reservation.  With no settled first delivery there
    # is no ReaderReceipt and therefore no semantic told memory.
    assert not repos.news.reader_history(
        event_id="candidate-reader-history", now_ms=now_ms + 1000, include_targeted=False
    ).recent_seen_rows
    assert not repos.news.reader_history(
        event_id="candidate-reader-history", now_ms=now_ms + 5 * 3600_000, include_targeted=False
    ).recent_seen_rows
    # A card the reader never received (first delivery terminalised) leaves the ledger; a sent one stays; and the
    # preliminary storyline's own cards are fetched even when the global limit would not reach them.
    later = now_ms + 1000
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
    assert not repos.news.reader_history(
        event_id="candidate-reader-history", now_ms=later, include_targeted=False
    ).recent_seen_rows
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
    told = [
        row.as_told_row()
        for row in repos.news.reader_history(
            event_id="candidate-reader-history", now_ms=later, include_targeted=False
        ).recent_seen_rows
    ]
    assert [t["event_id"] for t in told] == [row["event_id"]]
    assert told[0]["headline_zh"] == "测试" and told[0]["magnitude"] == 2 and told[0]["direction"] == "bullish"
    assert told[0]["storyline_key"] == row["storyline_key"] and told[0]["at_ms"] == now_ms + 20
    # The projection is the selector's input contract: everything it ranks on comes from this one query.
    assert told[0]["comparison_title"] and told[0]["dedupe_family"] == "general"
    assert list(told[0]["grounded_assets"]) == list(row["grounded_assets"])
    with repos.transaction():
        conn.execute("DELETE FROM news_deliveries WHERE event_id = %s", (row["event_id"],))
    # A grounded restatement of that card drops, and the storyline lock is a plain transaction-scoped advisory lock.
    told_status = storyline_status(
        row["storyline_key"],
        told=[{"i": 0, "direction": t["direction"], "headline_zh": t["headline_zh"]} for t in told],
    )
    restated_judgment = scored_judgment(verdict.model_copy(update={"novelty": "restatement", "restates": 0}))
    restated = decide(restated_judgment, facts, told_status)
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
    target_sha256 = "a" * 64
    initial_receipt = {
        "provider": "telegram",
        "message_id": 42,
        "pushed_at_ms": 1_500,
        "target_sha256": target_sha256,
    }
    updated_receipt = {**initial_receipt, "edited_at_ms": 2_500}
    with repos.transaction():
        assert repos.news.begin_delivery(event_id=event_id, kind="first", card={"x": 1}, now_ms=1_000) == "new"
        assert repos.news.begin_delivery(event_id=event_id, kind="first", card={"x": 1}, now_ms=1_000) == "sending"
        assert repos.news.settle_delivery(
            event_id=event_id,
            kind="first",
            state="sent",
            receipt=initial_receipt,
            error_code=None,
            now_ms=2_000,
        )
        assert not repos.news.begin_delivery_edit(
            event_id=event_id,
            kind="first",
            card={"x": 99},
            receipt={**initial_receipt, "source_url": "https://should-not-persist.test"},
            now_ms=2_100,
        )
        assert repos.news.begin_delivery_edit(
            event_id=event_id,
            kind="first",
            card={"x": 2, "market_data_state": "ready"},
            receipt=initial_receipt,
            now_ms=2_200,
        )
        editing = repos.news.delivery(event_id=event_id, kind="first")
        assert editing is not None
        assert editing["card"] == {"x": 1}
        assert editing["pending_card"] == {"x": 2, "market_data_state": "ready"}
        assert editing["edit_state"] == "editing"
        assert not repos.news.settle_delivery_edit(
            event_id=event_id,
            kind="first",
            receipt={**updated_receipt, "pushed_at_ms": 1_501},
            now_ms=2_400,
        )
        assert not repos.news.settle_delivery_edit(
            event_id=event_id,
            kind="first",
            receipt={**updated_receipt, "provider_response": "untrusted"},
            now_ms=2_400,
        )
        assert not repos.news.settle_delivery_edit(
            event_id=event_id,
            kind="first",
            receipt={**updated_receipt, "edited_at_ms": 1_499},
            now_ms=2_400,
        )
        assert repos.news.settle_delivery_edit(
            event_id=event_id,
            kind="first",
            receipt=updated_receipt,
            now_ms=2_500,
        )
        assert repos.news.begin_delivery(event_id=event_id, kind="first", card={}, now_ms=3_000) == "sent"
    delivery = repos.news.delivery(event_id=event_id, kind="first")
    assert delivery is not None
    assert delivery["card"] == {"x": 2, "market_data_state": "ready"}
    assert delivery["receipt"] == updated_receipt
    assert delivery["pending_card"] is None
    assert delivery["edit_state"] == "edited"
    with repos.transaction():
        assert repos.news.begin_delivery_edit(
            event_id=event_id,
            kind="first",
            card={"x": 3, "market_data_state": "newer"},
            receipt=updated_receipt,
            now_ms=3_000,
        )
        # A new process owns no edit task yet, so even a just-written inherited intent is unambiguously interrupted.
        assert repos.news.terminalize_interrupted_delivery_edits(now_ms=3_001) == 1
    interrupted = repos.news.delivery(event_id=event_id, kind="first")
    assert interrupted is not None
    assert interrupted["card"] == {"x": 2, "market_data_state": "ready"}
    assert interrupted["pending_card"] == {"x": 3, "market_data_state": "newer"}
    assert interrupted["edit_state"] == "ambiguous"
    assert interrupted["edit_error_code"] == "edit_ambiguous_after_crash"
    with pytest.raises(CheckViolation), repos.transaction():
        conn.execute(
            """
            UPDATE news_deliveries
               SET edit_state = NULL, pending_card = '{}'::jsonb,
                   edit_error_code = NULL, edit_attempted_at_ms = 4_000,
                   edit_settled_at_ms = NULL
             WHERE event_id = %s AND kind = 'first'
            """,
            (event_id,),
        )
    with repos.transaction():
        conn.execute(
            """
            UPDATE news_deliveries
               SET edit_state = 'edited', pending_card = NULL, edit_error_code = NULL,
                   edit_attempted_at_ms = 4_000, edit_settled_at_ms = 4_001
             WHERE event_id = %s AND kind = 'first'
            """,
            (event_id,),
        )
        assert repos.news.begin_delivery_edit(
            event_id=event_id,
            kind="first",
            card={"x": 4, "market_data_state": "stale-settlement"},
            receipt=updated_receipt,
            now_ms=5_000,
        )
        assert repos.news.terminalize_stale_delivery_edits(now_ms=65_001) == 1
    stale = repos.news.delivery(event_id=event_id, kind="first")
    assert stale is not None
    assert stale["edit_state"] == "ambiguous"
    assert stale["edit_error_code"] == "edit_settlement_unavailable"
    detail = repos.news.event_detail(event_id)
    assert detail is not None and detail["deliveries"][0]["state"] == "sent"
    feed = repos.news.list_feed(
        event_family=None,
        change_state=None,
        assertion_status=None,
        source_authority=None,
        subject_code=None,
        admission=None,
        final_decision=None,
        event_kind=None,
        search=None,
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
            event_family=None,
            change_state=None,
            assertion_status=None,
            source_authority=None,
            subject_code=None,
            admission=None,
            final_decision=None,
            event_kind=None,
            search=None,
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
    assert {"e2e_p50_ms", "e2e_p95_ms"} <= status["delivery"].keys()
    assert status["learning_retention"]["eligible_recordings"] == 0
    conn.commit()


def test_delivery_delete_requires_durable_five_venue_evidence_and_exact_receipt(conn) -> None:
    repos = repositories_for_connection(conn)
    row = conn.execute(
        """
        SELECT e.event_id
          FROM news_events e
          LEFT JOIN news_deliveries d ON d.event_id = e.event_id AND d.kind = 'first'
         WHERE d.event_id IS NULL
         ORDER BY e.opened_at_ms DESC
         LIMIT 1
        """
    ).fetchone()
    assert row is not None
    event_id = row["event_id"]
    receipt = {
        "provider": "telegram",
        "message_id": 84,
        "pushed_at_ms": 5_000,
        "target_sha256": "b" * 64,
    }
    evidence = {
        "tradability_review": {
            "state": "absent",
            "checked_venues": ["binance", "hyperliquid", "okx", "lighter", "bitget"],
            "failed_venues": [],
            "matches": [],
        }
    }
    reason = "Binance、Hyperliquid、OKX、Lighter、Bitget 均未发现可交易合约。"
    with repos.transaction():
        assert repos.news.begin_delivery(event_id=event_id, kind="first", card={"x": 1}, now_ms=4_000) == "new"
        assert repos.news.settle_delivery(
            event_id=event_id,
            kind="first",
            state="sent",
            receipt=receipt,
            error_code=None,
            now_ms=5_000,
        )
        assert not repos.news.begin_delivery_delete(
            event_id=event_id,
            kind="first",
            evidence=evidence,
            reason=reason,
            receipt={**receipt, "provider_response": "untrusted"},
            now_ms=5_100,
        )
        assert repos.news.begin_delivery_delete(
            event_id=event_id,
            kind="first",
            evidence=evidence,
            reason=reason,
            receipt=receipt,
            now_ms=5_100,
        )
        assert not repos.news.settle_delivery_delete(
            event_id=event_id,
            kind="first",
            receipt={**receipt, "deleted_at_ms": 4_999},
            now_ms=5_200,
        )
        deleted_receipt = {**receipt, "deleted_at_ms": 5_200}
        assert repos.news.settle_delivery_delete(
            event_id=event_id,
            kind="first",
            receipt=deleted_receipt,
            now_ms=5_200,
        )
    delivery = repos.news.delivery(event_id=event_id, kind="first")
    assert delivery is not None
    assert delivery["state"] == "sent"
    assert delivery["delete_state"] == "deleted"
    assert delivery["delete_evidence"] == evidence
    assert delivery["delete_reason"] == reason
    assert delivery["receipt"] == deleted_receipt
    with pytest.raises(CheckViolation), repos.transaction():
        conn.execute(
            """
            UPDATE news_deliveries
               SET delete_state = NULL, delete_evidence = '{}'::jsonb,
                   delete_reason = NULL, delete_error_code = NULL,
                   delete_attempted_at_ms = NULL, delete_settled_at_ms = NULL
             WHERE event_id = %s AND kind = 'first'
            """,
            (event_id,),
        )


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
    degraded_judgment = fallback_verdict(
        GateFacts(
            grounded_assets=("BTC",),
            watchlist_symbols=frozenset({"BTC"}),
            admission="candidate",
        ),
        error_code="news_program_route_deadline",
        title="模型占位文字",
    )
    verdict = degraded_judgment.verdict
    runtime_manifest_sha = "b" * 64
    trace = {
        "judgment_contract_version": degraded_judgment.judgment_contract_version,
        "judgment_origin": "degraded",
        "judgment_sha256": degraded_judgment.judgment_sha256,
        "verdict_sha256": canonical_sha(verdict.model_dump(mode="json")),
        "runtime_manifest_sha": runtime_manifest_sha,
        "program_version": SEMANTIC_PROGRAM_VERSION,
        "program_sha256": "a" * 64,
        "evidence_version": int(evidence["evidence_version"]),
        "evidence_sha256": str(evidence["evidence_sha256"]),
        "focus_fact_id": str(evidence["focus_fact_id"]),
        "told": [],
        "told_count": 0,
        "judgment": degraded_judgment.judgment_atom,
    }
    degraded_card = {"header": {"title": {"tag": "plain_text", "content": "实际降级卡片"}}}
    with repos.transaction():
        assert repos.news.insert_verdict(
            event_id=sent_event,
            stage="triage",
            policy_version=TRIAGE_POLICY_VERSION,
            judgment_contract_version=degraded_judgment.judgment_contract_version,
            judgment_origin="degraded",
            rule_baseline_decision=degraded_judgment.decision.rule_baseline,
            final_decision=degraded_judgment.decision.final,
            override_rule=degraded_judgment.decision.override_rule,
            throttled_by=degraded_judgment.decision.throttled_by,
            verdict=verdict.model_dump(),
            model_editorial=None,
            judgment_sha256=degraded_judgment.judgment_sha256,
            runtime_manifest_sha=runtime_manifest_sha,
            model=None,
            program_version=SEMANTIC_PROGRAM_VERSION,
            program_sha256="a" * 64,
            degraded=True,
            error_code="news_program_route_deadline",
            trace=trace,
            evidence_version=int(evidence["evidence_version"]),
            evidence_sha256=str(evidence["evidence_sha256"]),
            focus_fact_id=str(evidence["focus_fact_id"]),
            now_ms=10_000,
        )
        assert repos.news.begin_delivery(event_id=sent_event, kind="first", card=degraded_card, now_ms=10_100) == "new"
    # A sending reservation is not a receipt.
    assert not repos.news.reader_history(
        event_id="candidate-reader-history", now_ms=10_150, include_targeted=False
    ).recent_seen_rows
    with repos.transaction():
        assert repos.news.settle_delivery(
            event_id=sent_event, kind="first", state="sent", receipt={"ok": True}, error_code=None, now_ms=10_200
        )
        assert repos.news.begin_delivery(event_id=ambiguous_event, kind="first", card={}, now_ms=20_000) == "new"
        assert repos.news.terminalize_interrupted_deliveries(now_ms=81_001) == 1

    told = [
        row.as_told_row()
        for row in repos.news.reader_history(
            event_id="candidate-reader-history", now_ms=10_300, include_targeted=False
        ).recent_seen_rows
    ]
    assert len(told) == 1 and told[0]["event_id"] == sent_event and told[0]["headline_zh"] == "实际降级卡片"
    sent_detail = repos.news.event_detail(sent_event)
    ambiguous_detail = repos.news.event_detail(ambiguous_event)
    assert sent_detail is not None and sent_detail["reader_receipt"]["state"] == "received"
    assert sent_detail["reader_receipt"]["rendered_card"] == degraded_card
    assert ambiguous_detail is not None and ambiguous_detail["reader_receipt"]["state"] == "unknown"
    conn.commit()


def test_incidents_and_broker_snapshot(conn) -> None:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        planned = repos.news.open_incident(cause_class="planned_shutdown", now_ms=50, planned=True)
        incident = repos.news.open_incident(cause_class="network_connect", now_ms=100)
        backpressure = repos.news.open_incident(cause_class="broker_backpressure", now_ms=110)
        assert repos.news.open_incident(cause_class="network_connect", now_ms=200) == incident
        summary = {row["cause_class"]: row for row in repos.news.open_incident_summary()}
        assert summary["network_connect"]["count"] == 1
        assert summary["network_connect"]["oldest_opened_at_ms"] == 100
        assert (
            repos.news.close_open_incidents(
                cause_classes=["network_connect", "planned_shutdown", "broker_backpressure"], now_ms=300
            )
            == 3
        )
        assert len({planned, incident, backpressure}) == 3
        pending = repos.news.pending_recovery_incidents()
        assert any(int(p["incident_id"]) == incident for p in pending)
        assert any(int(p["incident_id"]) == backpressure for p in pending)
        assert repos.news.record_recovery_error(
            incident_id=backpressure, error_code="opennews_history_rate_limited", now_ms=350
        )
        backlog = repos.news.recovery_backlog()
        assert backlog["pending_count"] >= 3
        assert backlog["oldest_opened_at_ms"] == 50
        assert backlog["last_error_code"] == "opennews_history_rate_limited"
        assert backlog["reason"] == "recovery_transient"
        assert repos.news.complete_recovery(
            incident_id=backpressure,
            status="recovered",
            recovered_count=2,
            error_code=None,
            recovery_from_at_ms=80,
            recovery_to_at_ms=300,
            now_ms=400,
        )
        assert not repos.news.complete_recovery(
            incident_id=backpressure,
            status="recovered",
            recovered_count=2,
            error_code=None,
            recovery_from_at_ms=80,
            recovery_to_at_ms=300,
            now_ms=500,
        )
    with repos.transaction():
        repos.news.update_broker_snapshot(
            snapshot={"connected": True, "queues": {"news.raw": {"messages": 0, "consumers": 1}}}, now_ms=7
        )
    status = repos.news.status_snapshot(now_ms=10)
    assert status["broker"]["connected"] is True and status["broker"]["queues"]["news.raw"]["consumers"] == 1
    assert status["ingest"]["recovery"]["pending_count"] >= 2
    assert status["ingest"]["recovery"]["reason"] == "recovery_pending"
    conn.commit()


def test_recovery_backlog_is_the_same_bounded_batch_recovery_will_process(conn) -> None:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        for offset in range(RECOVERY_BACKLOG_LIMIT + 5):
            incident_id = repos.news.open_incident(cause_class="unknown", now_ms=1_000 + offset * 2)
            assert repos.news.close_open_incidents(cause_classes=["unknown"], now_ms=1_001 + offset * 2) == 1
            if offset == RECOVERY_BACKLOG_LIMIT + 4:
                assert repos.news.record_recovery_error(
                    incident_id=incident_id,
                    error_code="outside_next_recovery_batch",
                    now_ms=2_000,
                )

        pending = repos.news.pending_recovery_incidents(limit=RECOVERY_BACKLOG_LIMIT)
        backlog = repos.news.recovery_backlog()

    assert len(pending) == RECOVERY_BACKLOG_LIMIT
    assert backlog == {
        "pending_count": len(pending),
        "oldest_opened_at_ms": min(int(row["opened_at_ms"]) for row in pending),
        "last_error_code": None,
        "reason": "recovery_pending",
    }
    assert [row["incident_id"] for row in pending] == sorted(row["incident_id"] for row in pending)


def _hit(
    *,
    hit_id: int,
    text: str,
    engine: str,
    score: int | None,
    coins: list[dict],
    source: str,
    ts: str,
    strategy_id: int = 1018,
    strategy_name: str = "News Score > 70",
    source_type: str = "news",
) -> dict:
    hit = {
        "id": hit_id,
        "text": text,
        "link": f"https://example.test/{hit_id}",
        "source": source,
        "newsType": "twitter" if engine == "meme" else "news",
        "engineType": engine,
        "ts": ts,
        "coins": [
            {"expired": False, "grade": c.get("grade"), "market_type": "cex", "score": score, "symbol": c["symbol"]}
            for c in coins
        ],
        "strategy": {"id": strategy_id, "name": strategy_name, "sourceType": source_type},
    }
    if score is not None:
        hit["aiRating"] = {"score": score, "signal": "long", "status": "done"}
    return hit


def _admit_test_events(conn, *, hit_base: int, titles: tuple[str, ...], hour: int) -> list[str]:
    repos = repositories_for_connection(conn)
    event_ids: list[str] = []
    for offset, title in enumerate(titles, start=1):
        event = parse_opennews_message(
            {
                "method": "strategy.triggered",
                "params": _hit(
                    hit_id=hit_base + offset,
                    text=title,
                    engine="news",
                    score=80,
                    coins=[],
                    source="wire",
                    ts=f"2026-08-24T{hour:02d}:{offset:02d}:00+08:00",
                ),
            }
        )
        assert event is not None
        stamp = int(event.entry.published_at_ms or 0)
        with repos.transaction():
            admitted = admit_item(
                repos,
                event=event,
                ingest_mode="live",
                observed_at_ms=stamp,
                trace_id=f"test-{hit_base}-{offset}",
                watchlist_symbols=frozenset(),
                now_ms=stamp,
            )
        event_ids.append(admitted.event_id)
    return event_ids


def _insert_test_verdict(
    repos,
    *,
    event_id: str,
    direction: str,
    now_ms: int,
    final_decision: str = "drop",
) -> None:
    verdict = TriageVerdict(
        novelty="new_fact",
        assets=[],
        direction=direction,
        scope="single_name",
        magnitude=1,
        confidence=0.5,
        headline_zh="筛选测试",
        why_zh="",
    )
    judgment = scored_judgment(verdict)
    evidence = repos.news.latest_evidence_snapshot(event_id)
    assert evidence is not None
    runtime_manifest_sha = "c" * 64
    trace = {
        "judgment_contract_version": judgment.judgment_contract_version,
        "judgment_origin": "model",
        "judgment_sha256": judgment.scored_judgment_sha256,
        "verdict_sha256": canonical_sha(verdict.model_dump(mode="json")),
        "editorial_sha256": judgment.editorial.editorial_sha256,
        "runtime_manifest_sha": runtime_manifest_sha,
        "program_version": SEMANTIC_PROGRAM_VERSION,
        "program_sha256": "d" * 64,
        "evidence_version": int(evidence["evidence_version"]),
        "evidence_sha256": str(evidence["evidence_sha256"]),
        "focus_fact_id": str(evidence["focus_fact_id"]),
        "told": [],
        "told_count": 0,
    }
    assert repos.news.insert_verdict(
        event_id=event_id,
        stage="triage",
        policy_version=TRIAGE_POLICY_VERSION,
        judgment_contract_version=judgment.judgment_contract_version,
        judgment_origin="model",
        rule_baseline_decision=final_decision,
        final_decision=final_decision,
        override_rule="trade_relevance_realtime" if final_decision == "push" else "reader_value_none",
        throttled_by=None,
        verdict=verdict.model_dump(),
        model_editorial=judgment.editorial.model_dump(mode="json"),
        judgment_sha256=judgment.scored_judgment_sha256,
        runtime_manifest_sha=runtime_manifest_sha,
        model="test",
        program_version=SEMANTIC_PROGRAM_VERSION,
        program_sha256="d" * 64,
        degraded=False,
        error_code=None,
        trace=trace,
        evidence_version=int(evidence["evidence_version"]),
        evidence_sha256=str(evidence["evidence_sha256"]),
        focus_fact_id=str(evidence["focus_fact_id"]),
        now_ms=now_ms,
    )


def test_oi_rank_ignores_ineligible_frames_in_the_same_window(conn) -> None:
    """#179: low-concentration frames remain auditable without spending a later signal's rank."""

    repos = repositories_for_connection(conn)
    titles = (
        "Alpha expands capacity",
        "Beta opens a new facility",
        "Gamma names a director",
        "Delta starts production",
        "Epsilon updates guidance",
    )
    event_ids = _admit_test_events(conn, hit_base=1_790_000, titles=titles, hour=10)

    observed_at_ms = 1_777_000_000_000
    rows = (
        (event_ids[0], "BTC", 4_000, 500, observed_at_ms - 20 * 60_000),
        (event_ids[1], "BTC", 6_000, 500, observed_at_ms - 10 * 60_000),
        # The lower edge is open: a qualifying frame exactly at the cutoff has aged out.
        (event_ids[2], "BTC", 9_100, 500, observed_at_ms - 4 * 3_600_000),
        # A different symbol owns a different rank window.
        (event_ids[3], "ETH", 9_100, 500, observed_at_ms - 5 * 60_000),
        # With a positive change floor this row is ineligible too.
        (event_ids[4], "BTC", 9_100, 99, observed_at_ms - 5 * 60_000),
    )
    with repos.transaction():
        for event_id, symbol, ratio_bps, change_bps, at_ms in rows:
            repos.news.insert_oi_signal(
                event_id=event_id,
                metric_version="oi_issue_179_repro",
                symbol=symbol,
                direction="rise",
                oi_change_bps=change_bps,
                oi_value_usd=10_000_000,
                whale_long_profit_bps=9_000,
                whale_oi_ratio_bps=ratio_bps,
                observed_at_ms=at_ms,
                rank_in_window=99,
                now_ms=observed_at_ms,
            )

    earlier_eligible_count = repos.news.count_recent_eligible_oi_signals(
        symbol="BTC",
        metric_version="oi_issue_179_repro",
        since_ms=observed_at_ms - 4 * 3_600_000,
        before_ms=observed_at_ms,
        whale_oi_ratio_above_bps=8_000,
        oi_change_at_least_bps=100,
    )
    judgment = evaluate_oi(
        OiSignal(
            symbol="BTC",
            direction="rise",
            oi_change_bps=500,
            oi_value_usd=10_000_000,
            whale_long_profit_bps=9_000,
            whale_oi_ratio_bps=9_100,
        ),
        earlier_eligible_count=earlier_eligible_count,
        policy=OiPolicy(oi_change_at_least_bps=100),
    )

    assert earlier_eligible_count == 0
    assert (judgment.rank_in_window, judgment.decision.final) == (1, "push")
    assert conn.execute(
        "SELECT count(*) AS n FROM news_oi_signals WHERE metric_version = 'oi_issue_179_repro'"
    ).fetchone()["n"] == len(rows), "every parsed frame remains auditable"
    assert repos.news.status_snapshot(now_ms=observed_at_ms + 1)["pipeline"]["telemetry_parsed_24h"] >= len(rows)


def test_same_symbol_concurrency_serializes_eligible_rank_and_caps_pushes(conn) -> None:
    event_ids = _admit_test_events(
        conn,
        hit_base=1_792_000,
        titles=(
            "Northwind expands its logistics network",
            "Contoso appoints a new finance chief",
            "Fabrikam opens a manufacturing site",
        ),
        hour=12,
    )
    observed_at_ms = 1_777_100_000_000

    def judge(event_id: str) -> str:
        connection = connect_postgres_test(read_only=False)
        try:
            repos = repositories_for_connection(connection)
            with repos.transaction():
                repos.news.lock_storyline("asset:BTC")
                count = repos.news.count_recent_eligible_oi_signals(
                    symbol="BTC",
                    metric_version="oi_issue_179_concurrency",
                    since_ms=observed_at_ms - 4 * 3_600_000,
                    before_ms=observed_at_ms,
                    whale_oi_ratio_above_bps=8_000,
                    oi_change_at_least_bps=0,
                    exclude_event_id=event_id,
                )
                judgment = evaluate_oi(
                    OiSignal(
                        symbol="BTC",
                        direction="rise",
                        oi_change_bps=500,
                        oi_value_usd=10_000_000,
                        whale_long_profit_bps=9_000,
                        whale_oi_ratio_bps=9_100,
                    ),
                    earlier_eligible_count=count,
                )
                repos.news.insert_oi_signal(
                    event_id=event_id,
                    metric_version="oi_issue_179_concurrency",
                    symbol="BTC",
                    direction="rise",
                    oi_change_bps=500,
                    oi_value_usd=10_000_000,
                    whale_long_profit_bps=9_000,
                    whale_oi_ratio_bps=9_100,
                    observed_at_ms=observed_at_ms,
                    rank_in_window=judgment.rank_in_window,
                    now_ms=observed_at_ms,
                )
            return judgment.decision.final
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=3) as pool:
        decisions = list(pool.map(judge, event_ids))

    assert sorted(decisions) == ["drop", "push", "push"]
    ranks = conn.execute(
        "SELECT rank_in_window FROM news_oi_signals "
        "WHERE metric_version = 'oi_issue_179_concurrency' ORDER BY rank_in_window"
    ).fetchall()
    assert [row["rank_in_window"] for row in ranks] == [1, 2, 3]


def test_strategy_1019_format_drift_reaches_triage_instead_of_near_duplicate_merging(conn) -> None:
    repos = repositories_for_connection(conn)
    event_ids: list[str] = []
    base = "BTC OI Rise 4.55%, OI Value 32.17M, Whale Long Profit 80.21%, Whale/OI Ratio 100.71%"
    for offset, suffix in enumerate(("format drift", "provider format drift"), start=1):
        params = _hit(
            hit_id=1_791_000 + offset,
            text=f"{base} {suffix}",
            engine="market",
            score=90,
            coins=[],
            source="binance",
            ts=f"2026-08-24T11:{offset:02d}:00+08:00",
        )
        params["strategy"] = {
            "id": 1019,
            "name": "OI Event Monitor",
            "sourceType": "market",
        }
        event = parse_opennews_message({"method": "strategy.triggered", "params": params})
        assert event is not None
        stamp = int(event.entry.published_at_ms or 0)
        with repos.transaction():
            admitted = admit_item(
                repos,
                event=event,
                ingest_mode="live",
                observed_at_ms=stamp,
                trace_id=f"oi-format-drift-{offset}",
                watchlist_symbols=frozenset(),
                now_ms=stamp,
            )
        assert admitted.event_kind == "oi"
        assert admitted.admission == "telemetry_deterministic" and admitted.match_kind == "leader"
        event_ids.append(admitted.event_id)

    assert len(set(event_ids)) == 2


@pytest.mark.parametrize(
    ("hit_id", "symbol", "order"),
    [
        (2_881_001, "NFIRST", ("news", "oi", "drift")),
        (2_881_002, "OFIRST", ("oi", "news", "drift")),
    ],
)
def test_same_provider_fact_keeps_one_event_per_kind_in_either_strategy_order(
    conn, hit_id: int, symbol: str, order: tuple[str, ...]
) -> None:
    repos = repositories_for_connection(conn)
    title = f"{symbol} OI Rise 4.55%, OI Value 32.17M, Whale Long Profit 80.21%, Whale/OI Ratio 100.71%"
    contracts = {
        "news": (1018, "News Score > 70", "news", "news"),
        "oi": (1019, "OI Event Monitor", "market", "market"),
        "drift": (1019, "wrong OI monitor", "market", "market"),
    }
    frames = {}
    for label, (strategy_id, strategy_name, source_type, engine) in contracts.items():
        params = _hit(
            hit_id=hit_id,
            text=title,
            engine=engine,
            score=90,
            coins=[],
            source="binance",
            ts=f"2026-08-{24 if symbol == 'NFIRST' else 25}T11:30:00+08:00",
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            source_type=source_type,
        )
        frames[label] = parse_opennews_message({"method": "strategy.triggered", "params": params})
        assert frames[label] is not None

    first_results = []
    with repos.transaction():
        for label in order:
            event = frames[label]
            assert event is not None
            first_results.append(
                admit_item(
                    repos,
                    event=event,
                    ingest_mode="live",
                    observed_at_ms=int(event.entry.published_at_ms or 0),
                    trace_id=f"same-record-{label}",
                    watchlist_symbols=frozenset(),
                    now_ms=int(event.entry.published_at_ms or 0),
                )
            )
    assert all(result.event_created for result in first_results)
    item_id = first_results[0].item_id

    with repos.transaction():
        redelivered = [
            admit_item(
                repos,
                event=frames[label],
                ingest_mode="live",
                observed_at_ms=int(frames[label].entry.published_at_ms or 0),
                trace_id=f"same-record-{label}-redelivery",
                watchlist_symbols=frozenset(),
                now_ms=int(frames[label].entry.published_at_ms or 0),
            )
            for label in order
        ]
    assert all(not result.item_inserted and not result.event_created for result in redelivered)

    rows = conn.execute(
        """
        SELECT e.event_id, e.event_kind, e.admission, e.source_contract_reason
          FROM news_event_members m
          JOIN news_events e ON e.event_id = m.event_id
         WHERE m.item_id = %s
         ORDER BY e.event_kind
        """,
        (item_id,),
    ).fetchall()
    assert {(row["event_kind"], row["admission"]) for row in rows} == {
        ("news", "candidate"),
        ("oi", "telemetry_deterministic"),
        ("unsupported_market", "unsupported_market_contract"),
    }
    assert (
        next(row for row in rows if row["event_kind"] == "unsupported_market")["source_contract_reason"]
        == "source_contract_drift"
    )
    ids = {row["event_kind"]: row["event_id"] for row in rows}
    assert ids["news"] != item_id
    assert len(set(ids.values())) == 3
    oi_card = repos.news.event_card(ids["oi"])
    assert oi_card is not None
    source = oi_source_contract(oi_card["provider_metadata"])
    assert source is not None and (source.strategy_id, source.measurement_window_ms) == ("1019", 300_000)

    # `news_items`, not the original provider frames, is material truth. Its
    # first-seen Strategy union must therefore recreate the same Event kinds.
    merged_metadata = conn.execute(
        "SELECT provider_metadata FROM news_items WHERE item_id = %s", (item_id,)
    ).fetchone()["provider_metadata"]
    conn.execute("DELETE FROM news_events WHERE leader_item_id = %s", (item_id,))
    rebuilt_event = replace(frames[order[0]], provider_metadata=dict(merged_metadata))
    with repos.transaction():
        rebuilt = admit_frame(
            repos,
            event=rebuilt_event,
            ingest_mode="live",
            observed_at_ms=int(rebuilt_event.entry.published_at_ms or 0),
            trace_id="same-record-material-rebuild",
            watchlist_symbols=frozenset(),
            now_ms=int(rebuilt_event.entry.published_at_ms or 0),
        )
    assert not rebuilt.item_inserted
    assert {result.event_kind for result in rebuilt.results} == {"news", "oi", "unsupported_market"}
    assert {result.event_kind: result.event_id for result in rebuilt.results} == ids


def test_strategy_2000_liquidations_are_typed_and_idempotent_for_live_and_recovery(conn) -> None:
    repos = repositories_for_connection(conn)

    def admit(hit_id: int, *, ingest_mode: str, side: str, venue: str) -> None:
        params = _hit(
            hit_id=hit_id,
            text=f"SPCX Large {side} Liquidation 202.71K at $137.01",
            engine="market",
            score=90,
            coins=[],
            source=venue,
            ts="2026-08-24T12:00:00+08:00",
        )
        params["strategy"] = {"id": 2000, "name": "实时清算", "sourceType": "market"}
        event = parse_opennews_message({"method": "strategy.triggered", "params": params})
        assert event is not None
        stamp = int(event.entry.published_at_ms or 0) + 1_000
        with repos.transaction():
            result = admit_item(
                repos,
                event=event,
                ingest_mode=ingest_mode,
                observed_at_ms=stamp,
                trace_id=f"liquidation-{hit_id}",
                watchlist_symbols=frozenset(),
                now_ms=stamp,
            )
        assert result.event_kind == "liquidation"
        assert result.admission == ("recovery" if ingest_mode == "recovery" else "liquidation_deterministic")

    admit(2_000_001, ingest_mode="live", side="Short", venue="binance")
    admit(2_000_001, ingest_mode="live", side="Short", venue="binance")
    admit(2_000_002, ingest_mode="recovery", side="Long", venue="hyperliquid")

    rows = conn.execute(
        "SELECT source_key, ingest_mode, venue, liquidated_position_side, forced_order_side, notional_usd, quantity, "
        "provider_record_identity, symbol_contract_identity, position_side_semantics, "
        "quantity_semantics, notional_semantics, price_semantics, completeness_assumption, "
        "throttle_assumption, source_contract_version, source_contract_complete "
        "FROM news_market_liquidations WHERE symbol = 'SPCX' ORDER BY venue"
    ).fetchall()
    assert [
        (row["venue"], row["liquidated_position_side"], row["forced_order_side"], row["notional_usd"], row["quantity"])
        for row in rows
    ] == [
        ("binance", "short", "buy", 202_710, None),
        ("hyperliquid", "long", "sell", 202_710, None),
    ]
    assert all(row["provider_record_identity"] for row in rows)
    assert all(str(row["symbol_contract_identity"]).startswith("unresolved:") for row in rows)
    assert all(row["position_side_semantics"] for row in rows)
    assert all(row["quantity_semantics"] == "not_provided" for row in rows)
    assert all(row["notional_semantics"] == "provider_reported_usd_notional" for row in rows)
    assert all(row["price_semantics"] == "provider_reported_unspecified_price" for row in rows)
    assert all(row["completeness_assumption"] and row["throttle_assumption"] for row in rows)
    assert all(row["source_contract_version"] == "opennews_liquidation_source_v1" for row in rows)
    assert all(row["source_contract_complete"] is False for row in rows)
    assert {row["venue"]: row["ingest_mode"] for row in rows} == {
        "binance": "live",
        "hyperliquid": "recovery",
    }

    # Raw Item retention may remove the provider envelope, but the normalized
    # immutable replay fact is a separate durable research source.
    retained = conn.execute(
        "SELECT source_key, item_id FROM news_market_liquidations WHERE venue = 'binance'"
    ).fetchone()
    assert retained is not None
    conn.execute("DELETE FROM news_items WHERE item_id = %s", (retained["item_id"],))
    conn.commit()
    assert (
        conn.execute(
            "SELECT source_key FROM news_market_liquidations WHERE source_key = %s", (retained["source_key"],)
        ).fetchone()
        is not None
    )
    # The typed fact stays a durable News research source and is published to nobody (#331): the
    # liquidation projection existed only for a Trading consumer that no longer exists, and a
    # projection with no reader is an invitation for the capital lane to grow a second trigger.
    assert not hasattr(repos.news, "trade_candidate_liquidation_rows")
    assert not hasattr(repos.news, "trade_candidate_news_rows")


@pytest.mark.parametrize(
    ("hit_id", "strategy_id", "strategy_name", "text", "event_kind"),
    [
        (2_880_101, 1019, "OI Event Monitor", "BTC OI provider template changed", "oi"),
        (2_880_200, 2000, "实时清算", "BTC liquidation provider fields changed", "liquidation"),
    ],
)
def test_recovery_runs_the_same_strict_market_parser_and_persists_drift_without_live_facts(
    conn,
    hit_id: int,
    strategy_id: int,
    strategy_name: str,
    text: str,
    event_kind: str,
) -> None:
    repos = repositories_for_connection(conn)
    params = _hit(
        hit_id=hit_id,
        text=text,
        engine="market",
        score=90,
        coins=[],
        source="binance",
        ts="2026-08-24T12:20:00+08:00",
        strategy_id=strategy_id,
        strategy_name=strategy_name,
        source_type="market",
    )
    event = parse_opennews_message({"method": "strategy.triggered", "params": params})
    assert event is not None
    stamp = int(event.entry.published_at_ms or 0)

    with repos.transaction():
        result = admit_item(
            repos,
            event=event,
            ingest_mode="recovery",
            observed_at_ms=stamp,
            trace_id=f"recovery-drift-{event_kind}",
            watchlist_symbols=frozenset(),
            now_ms=stamp,
        )

    row = conn.execute(
        "SELECT event_kind, admission, source_contract_reason FROM news_events WHERE event_id = %s",
        (result.event_id,),
    ).fetchone()
    assert row == {
        "event_kind": event_kind,
        "admission": "recovery",
        "source_contract_reason": "source_contract_drift",
    }
    downstream = conn.execute(
        """
        SELECT
          (SELECT count(*) FROM news_verdicts WHERE event_id = %s) AS verdicts,
          (SELECT count(*) FROM news_deliveries WHERE event_id = %s) AS deliveries,
          (SELECT count(*) FROM news_oi_signals WHERE event_id = %s) AS oi_signals,
          (SELECT count(*) FROM news_market_liquidations WHERE item_id = %s) AS liquidations
        """,
        (result.event_id, result.event_id, result.event_id, result.item_id),
    ).fetchone()
    assert downstream == {"verdicts": 0, "deliveries": 0, "oi_signals": 0, "liquidations": 0}


@pytest.mark.parametrize(
    ("hit_id", "strategy_id", "strategy_name", "source_type", "text"),
    [
        (2_026_001, 2026, "聪明钱监控", "wallet", "Wallet activity contract awaiting a typed schema"),
        (
            2_083_001,
            2083,
            "Large-scale liquidation",
            "market",
            "Aster liquidation contract awaiting venue semantics",
        ),
    ],
)
def test_unsupported_market_contracts_persist_once_without_downstream_facts(
    conn,
    hit_id: int,
    strategy_id: int,
    strategy_name: str,
    source_type: str,
    text: str,
) -> None:
    repos = repositories_for_connection(conn)
    params = _hit(
        hit_id=hit_id,
        text=text,
        engine="market",
        score=None,
        coins=[],
        source="opennews",
        ts="2026-08-24T12:30:00+08:00",
        strategy_id=strategy_id,
        strategy_name=strategy_name,
        source_type=source_type,
    )
    event = parse_opennews_message({"method": "strategy.triggered", "params": params})
    assert event is not None
    stamp = int(event.entry.published_at_ms or 0)

    with repos.transaction():
        first = admit_item(
            repos,
            event=event,
            ingest_mode="live",
            observed_at_ms=stamp,
            trace_id=f"unsupported-{strategy_id}",
            watchlist_symbols=frozenset(),
            now_ms=stamp,
        )
        redelivered = admit_item(
            repos,
            event=event,
            ingest_mode="live",
            observed_at_ms=stamp,
            trace_id=f"unsupported-{strategy_id}-redelivery",
            watchlist_symbols=frozenset(),
            now_ms=stamp,
        )

    assert (first.event_kind, first.admission, first.event_created) == (
        "unsupported_market",
        "unsupported_market_contract",
        True,
    )
    assert redelivered.event_id == first.event_id
    assert redelivered.item_inserted is False and redelivered.event_created is False

    row = conn.execute(
        """
        SELECT e.event_kind, e.admission, e.source_contract_reason, i.provider_metadata
          FROM news_events e
          JOIN news_items i ON i.item_id = e.leader_item_id
         WHERE e.event_id = %s
        """,
        (first.event_id,),
    ).fetchone()
    assert row is not None
    strategy = row["provider_metadata"]["strategies"][0]
    assert (row["event_kind"], row["admission"]) == (
        "unsupported_market",
        "unsupported_market_contract",
    )
    assert row["source_contract_reason"] == "unsupported_market_contract"
    assert (
        strategy["id"],
        strategy["name"],
        strategy["source_type"],
        strategy["engine_type"],
    ) == (str(strategy_id), strategy_name, source_type, "market")
    downstream = conn.execute(
        """
        SELECT
          (SELECT count(*) FROM news_event_members WHERE event_id = %s) AS members,
          (SELECT count(*) FROM news_verdicts WHERE event_id = %s) AS verdicts,
          (SELECT count(*) FROM news_deliveries WHERE event_id = %s) AS deliveries,
          (SELECT count(*) FROM news_oi_signals WHERE event_id = %s) AS oi_signals,
          (SELECT count(*) FROM news_market_liquidations WHERE item_id = %s) AS liquidations
        """,
        (first.event_id, first.event_id, first.event_id, first.event_id, first.item_id),
    ).fetchone()
    assert downstream == {
        "members": 1,
        "verdicts": 0,
        "deliveries": 0,
        "oi_signals": 0,
        "liquidations": 0,
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
               s.evidence_version, s.snapshot #>> '{card,leader_description}' AS content,
               s.snapshot #>> '{card,leader_title}' AS model_title,
               s.snapshot #>> '{card,raw_first_line}' AS model_raw_first_line
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
    # #152: the parent's first line here is the header, which `content` already carries; `leader_title` is the
    # bullet's own unnormalized text, so `raw_first_line` has nothing left to recover and is blanked.
    assert all(row["model_raw_first_line"] == "" for row in rows)
    assert {row["model_title"] for row in rows} == {str(row["focus_fact_text"]) for row in rows}
    conn.commit()


def test_a_bare_numbered_digest_never_grounds_one_bullet_on_another(conn) -> None:
    """#152: with no preamble the parent's first line *is* bullet 1.

    The Gate reads `title + raw_first_line` for cashtags and for the macro/energy/PR lexicons, so bullet 1's
    `$TSLA` used to ground every sibling. There is no shared lead in this shape, so each bullet is judged and
    grounded on itself alone.

    The tag is graded `C` on purpose: a B+/A/A+ tag grounds the whole Item by design and would hide the leak.
    """

    repos = repositories_for_connection(conn)
    raw = (
        "1. 特斯拉 $TSLA 盘前上涨 6%，公司上调全年交付指引。<br/>"
        "2. 美国航空航天局称将于下周向宇航员颁发荣誉勋章。<br/>"
        "3. 印度政府表示正密切关注国内糖价上涨的情况。"
    )
    event = parse_opennews_message(
        {
            "method": "strategy.triggered",
            "params": _hit(
                hit_id=920002,
                text=raw,
                engine="news",
                score=80,
                coins=[{"symbol": "TSLA", "market_type": "cex", "grade": "C", "score": 10}],
                source="wire",
                ts="2026-08-18T22:00:00+08:00",
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
            trace_id="bare-digest",
            watchlist_symbols=frozenset(),
            now_ms=stamp,
        )
    assert len(batch.results) == 3
    rows = conn.execute(
        """
        SELECT e.focus_fact_text, e.focus_fact_context, e.grounded_assets,
               s.snapshot #>> '{card,raw_first_line}' AS model_raw_first_line
          FROM news_events e
          JOIN news_event_evidence_snapshots s
            ON s.event_id = e.event_id AND s.evidence_version = 1
         WHERE e.leader_item_id = %s ORDER BY e.focus_fact_text
        """,
        (batch.item_id,),
    ).fetchall()
    assert len(rows) == 3
    # No preamble exists, so no unit gets one — and the first bullet is not promoted into that role.
    assert all(row["focus_fact_context"] == "" for row in rows)
    assert all(row["model_raw_first_line"] == "" for row in rows)
    by_text = {str(row["focus_fact_text"])[:4]: row for row in rows}
    assert list(by_text["特斯拉 $TS"[:4]]["grounded_assets"]) == ["TSLA"]
    for other in ("美国航空航天局", "印度政府表示"):
        assert list(by_text[other[:4]]["grounded_assets"]) == []
    conn.commit()


def _artifact_frame(*, hit_id: int, text: str, url: str, ts: str):
    event = parse_opennews_message(
        {
            "method": "strategy.triggered",
            "params": {
                **_hit(hit_id=hit_id, text=text, engine="news", score=80, coins=[], source="wire", ts=ts),
                "link": url,
            },
        }
    )
    assert event is not None
    return event


def test_the_same_source_artifact_joins_one_event_across_url_spellings_and_windows(conn) -> None:
    """#154, the three cases measured over 30 days of production.

    The text-derived path cannot reach any of them: `What a coincidence!` scores below the three-token
    `shareable` floor so no fingerprint lookup ever runs, and the other two arrive after the 12 h dedupe window
    has closed. All three are the same tweet.
    """

    repos = repositories_for_connection(conn)
    cases = (
        # Two URL spellings, four seconds apart. The reader got two cards for one whale trade.
        (
            "What a coincidence!",
            3654362,
            3654363,
            "https://twitter.com/lookonchain/status/2090239389318410280",
            "https://x.com/lookonchain/status/2090239389318410280",
            "2026-08-20T08:48:00+08:00",
            "2026-08-20T08:48:04+08:00",
        ),
        # Same spelling, 16.2 h apart: past the 12 h `general` window, inside the 7 d artifact window.
        (
            "Kraken launches U.S. stock trading for European customers on one regulated platform",
            3700001,
            3700002,
            "https://x.com/CoinDesk/status/2089761853727490268",
            "https://x.com/CoinDesk/status/2089761853727490268",
            "2026-08-19T01:10:00+08:00",
            "2026-08-19T17:22:00+08:00",
        ),
        # 88.7 h apart: the frame that started #154.
        (
            "Nvidia has agreed to provide a more than 100 billion dollar backstop for an OpenAI data center",
            3641794,
            3700789,
            "https://x.com/soon_svm/status/2089994673804939740",
            "https://x.com/soon_svm/status/2089994673804939740",
            "2026-08-19T16:35:36+08:00",
            "2026-08-23T09:16:24+08:00",
        ),
    )
    for text, first_id, second_id, first_url, second_url, first_ts, second_ts in cases:
        first = _artifact_frame(hit_id=first_id, text=text, url=first_url, ts=first_ts)
        second = _artifact_frame(hit_id=second_id, text=text, url=second_url, ts=second_ts)
        stamp = int(second.entry.published_at_ms or 0) + 1000
        with repos.transaction():
            opened = admit_item(
                repos,
                event=first,
                ingest_mode="live",
                observed_at_ms=int(first.entry.published_at_ms or 0),
                trace_id="artifact-1",
                watchlist_symbols=frozenset(),
                now_ms=stamp,
            )
            rejoined = admit_item(
                repos,
                event=second,
                ingest_mode="live",
                observed_at_ms=stamp,
                trace_id="artifact-2",
                watchlist_symbols=frozenset(),
                now_ms=stamp,
            )
        assert opened.event_created is True, text
        assert rejoined.event_created is False, text
        assert rejoined.event_id == opened.event_id, text
        assert rejoined.match_kind == "exact", text
    conn.commit()


def test_a_repeated_digest_keeps_one_event_per_bullet(conn) -> None:
    """The artifact key alone would collapse a split digest, because all four units share one tweet.

    The lookup pairs it with the fingerprint, so unit *k* can only rejoin unit *k*.
    """

    repos = repositories_for_connection(conn)
    text = (
        "BREAKING: details include:<br/>1. 商务部反对欧方打压中国企业并要求纠正。<br/>"
        "2. Moderna 下调全年指引，盘前下跌 13%。<br/>3. 沃尔玛上调全年销售预期至 4.8%。"
    )
    url = "https://x.com/wire/status/2089994673804939999"
    first = _artifact_frame(hit_id=930001, text=text, url=url, ts="2026-08-19T10:00:00+08:00")
    second = _artifact_frame(hit_id=930002, text=text, url=url, ts="2026-08-21T10:00:00+08:00")
    stamp = int(second.entry.published_at_ms or 0) + 1000
    with repos.transaction():
        opened = admit_frame(
            repos,
            event=first,
            ingest_mode="live",
            observed_at_ms=int(first.entry.published_at_ms or 0),
            trace_id="digest-1",
            watchlist_symbols=frozenset(),
            now_ms=stamp,
        )
        rejoined = admit_frame(
            repos,
            event=second,
            ingest_mode="live",
            observed_at_ms=stamp,
            trace_id="digest-2",
            watchlist_symbols=frozenset(),
            now_ms=stamp,
        )
    assert len(opened.results) == 3 and all(r.event_created for r in opened.results)
    assert len(rejoined.results) == 3 and not any(r.event_created for r in rejoined.results)
    # Three Events in, three Events out — each bullet rejoined its own, none collapsed into a sibling.
    assert [r.event_id for r in rejoined.results] == [r.event_id for r in opened.results]
    assert len({r.event_id for r in rejoined.results}) == 3
    conn.commit()


def test_source_artifact_backfill_matches_the_parser(conn) -> None:
    """The migration computes `source_artifact_id` in SQL; `opennews` computes it in Python. One rule."""

    rows = conn.execute(
        """
        SELECT canonical_url,
               CASE WHEN canonical_url ~* '(?i)^https?://(www\\.)?(x|twitter)\\.com/[^/]+/status(es)?/[0-9]{5,25}([/?#]|$)'
                    THEN 'x:' || substring(canonical_url from '(?i)/status(?:es)?/([0-9]{5,25})')
                    ELSE '' END AS sql_id
          FROM (VALUES
            ('https://x.com/soon_svm/status/2089994673804939740'),
            ('https://twitter.com/soon_svm/status/2089994673804939740'),
            ('https://www.twitter.com/soon_svm/statuses/2089994673804939740'),
            ('https://x.com/soon_svm/status/2089994673804939740?s=20'),
            ('https://www.zerohedge.com/markets/story'),
            ('https://x.com/soon_svm'),
            -- Case in both the handle and the path segment: `~*` matches but `substring` would not.
            ('https://X.com/SOON_SVM/Status/2089994673804939740'),
            ('https://twitter.com/CoinDesk/STATUSES/2089761853727490268')
          ) AS t(canonical_url)
        """
    ).fetchall()
    assert rows
    for row in rows:
        assert row["sql_id"] == source_artifact_identity(str(row["canonical_url"]))[0], row["canonical_url"]
    conn.commit()


def test_delivery_timing_uses_original_tweet_time_and_first_local_observation(conn) -> None:
    repos = repositories_for_connection(conn)
    hit = _hit(
        hit_id=9_109_901,
        text="Bitcoin ETF inflows accelerate",
        engine="news",
        score=90,
        coins=[{"symbol": "BTC", "grade": "A"}],
        source="serenity",
        ts="2026-08-19T18:10:00+08:00",
    )
    hit["link"] = "https://x.com/serenity/status/2089761853727490268"
    event = parse_opennews_message({"method": "strategy.triggered", "params": hit})
    assert event is not None
    observed_at_ms = int(event.entry.published_at_ms or 0) + 4_321
    with repos.transaction():
        admitted = admit_item(
            repos,
            event=event,
            ingest_mode="live",
            observed_at_ms=observed_at_ms,
            trace_id="delivery-timing",
            watchlist_symbols=frozenset({"BTC"}),
            now_ms=observed_at_ms,
        )

    assert repos.news.event_delivery_timing(admitted.event_id) == {
        "news_at_ms": 1_787_073_026_483,
        "reaction_anchor_at_ms": int(event.entry.published_at_ms or 0),
        "observed_at_ms": observed_at_ms,
    }
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


def test_the_oi_filter_only_reaches_the_lane_that_can_write_the_key(conn) -> None:
    """`?oi=` names the deterministic lane's gate, so it must not answer for any other admission.

    `triage.py` enters its OI branch under `admission = 'telemetry_deterministic'` and nothing else writes
    `trace['oi_signal']`, so pairing the two costs no rows. It is what keeps the filter off the whole
    retention's TOAST: without the admission the planner reads `trace` — 26 MB over 24 h of verdicts — for
    every candidate row before it can tell whether the rule matches, and a rare rule finds nothing to stop
    early on. Measured live on 2026-08-25, unbounded `?oi=parse_failed` was a 500 at 1.24 s against the serve
    role's 1 s statement timeout, and 0.44 s once the admission came with it.
    """

    repos = repositories_for_connection(conn)
    telemetry_id, other_id = _admit_test_events(
        conn,
        hit_base=1_795_000,
        titles=("Zeta OI Rise 4.55 percent", "Eta publishes an unrelated update"),
        hour=11,
    )
    judgment, lane_trace = oi_parse_failure("Zeta OI invalid frame", provider_source="opennews")
    with repos.transaction():
        conn.execute(
            "UPDATE news_events SET admission = 'telemetry_deterministic' WHERE event_id = %s",
            (telemetry_id,),
        )
        # The same trace key on both rows: the filter must separate them by lane, not by what the trace holds.
        for offset, event_id in enumerate((telemetry_id, other_id)):
            evidence = repos.news.latest_evidence_snapshot(event_id)
            assert evidence is not None
            runtime_manifest_sha = "c" * 64
            trace = {
                "oi_signal": lane_trace,
                "judgment": judgment.judgment_atom,
                "judgment_contract_version": judgment.judgment_contract_version,
                "judgment_origin": "oi",
                "judgment_sha256": judgment.judgment_sha256,
                "verdict_sha256": canonical_sha(judgment.verdict.model_dump(mode="json")),
                "runtime_manifest_sha": runtime_manifest_sha,
                "program_version": OI_PROGRAM_VERSION,
                "program_sha256": "d" * 64,
                "evidence_version": int(evidence["evidence_version"]),
                "evidence_sha256": str(evidence["evidence_sha256"]),
                "focus_fact_id": str(evidence["focus_fact_id"]),
                "told": [],
                "told_count": 0,
            }
            repos.news.insert_verdict(
                event_id=event_id,
                stage="triage",
                policy_version=TRIAGE_POLICY_VERSION,
                judgment_contract_version=judgment.judgment_contract_version,
                judgment_origin="oi",
                rule_baseline_decision=judgment.decision.rule_baseline,
                final_decision=judgment.decision.final,
                override_rule=judgment.decision.override_rule,
                throttled_by=judgment.decision.throttled_by,
                verdict=judgment.verdict.model_dump(mode="json"),
                model_editorial=None,
                judgment_sha256=judgment.judgment_sha256,
                runtime_manifest_sha=runtime_manifest_sha,
                model=None,
                program_version=OI_PROGRAM_VERSION,
                program_sha256="d" * 64,
                degraded=False,
                error_code="oi_parse_failed",
                trace=trace,
                evidence_version=int(evidence["evidence_version"]),
                evidence_sha256=str(evidence["evidence_sha256"]),
                focus_fact_id=str(evidence["focus_fact_id"]),
                now_ms=1_790_000_000_000 + offset,
            )

    served = repos.news.list_feed(
        event_family=None,
        change_state=None,
        assertion_status=None,
        source_authority=None,
        subject_code=None,
        admission=None,
        final_decision=None,
        event_kind=None,
        search=None,
        limit=10_000,
        cursor=None,
        oi="parse_failed",
    )
    served_ids = {event["event_id"] for event in served["events"]}
    assert telemetry_id in served_ids
    assert other_id not in served_ids
    conn.commit()


def test_feed_direction_and_event_kind_filters_compose_over_the_authoritative_query(conn) -> None:
    repos = repositories_for_connection(conn)
    sentinel = "direction-channel-filter-sentinel"
    bullish_id, bearish_oi_id = _admit_test_events(
        conn,
        hit_base=1_795_200,
        titles=(
            f"{sentinel} semiconductor orders accelerate after a capacity expansion",
            f"{sentinel} leveraged open interest unwinds across crypto perpetuals",
        ),
        hour=11,
    )
    assert bullish_id != bearish_oi_id
    with repos.transaction():
        conn.execute(
            "UPDATE news_events SET event_kind = 'oi' WHERE event_id = %s",
            (bearish_oi_id,),
        )
        for offset, (event_id, direction) in enumerate(((bullish_id, "bullish"), (bearish_oi_id, "bearish"))):
            _insert_test_verdict(
                repos,
                event_id=event_id,
                direction=direction,
                now_ms=1_790_000_100_000 + offset,
            )

    def ids(**filters):
        params = dict(
            event_family=None,
            change_state=None,
            assertion_status=None,
            source_authority=None,
            subject_code=None,
            admission=None,
            final_decision=None,
            event_kind=None,
            # The integration database is intentionally shared across this module. Scope the assertion to
            # this test's Events so unrelated, valid verdicts cannot make an exact-set assertion flaky.
            search=compile_news_search(q=sentinel, symbol=None, instruments=repos.instruments),
            limit=10,
            cursor=None,
        )
        params.update(filters)
        page = repos.news.list_feed(**params)
        return {event["event_id"] for event in page["events"]}

    assert ids(directions=("bullish",)) == {bullish_id}
    assert ids(directions=("bearish",)) == {bearish_oi_id}
    assert ids(event_kind=("news",)) == {bullish_id}
    assert ids(event_kind=("oi",)) == {bearish_oi_id}
    assert ids(event_kind=("news", "oi")) == {bullish_id, bearish_oi_id}
    assert ids(directions=("bullish",), event_kind=("oi",)) == set()
    conn.commit()


def test_event_feed_funnel_tracks_one_opened_event_cohort_across_durable_stages(conn) -> None:
    repos = repositories_for_connection(conn)
    old_event, current_event = _admit_test_events(
        conn,
        hit_base=1_795_400,
        titles=(
            "An older event completes delivery after the window boundary",
            "A current event completes delivery inside its intake cohort",
        ),
        hour=11,
    )
    now_ms = 2_000_000_000_000
    with repos.transaction():
        conn.execute(
            "UPDATE news_events SET opened_at_ms = %s WHERE event_id = %s",
            (now_ms - 25 * 3600_000, old_event),
        )
        conn.execute(
            "UPDATE news_events SET opened_at_ms = %s WHERE event_id = %s",
            (now_ms - 3600_000, current_event),
        )
        for offset, event_id in enumerate((old_event, current_event)):
            _insert_test_verdict(
                repos,
                event_id=event_id,
                direction="bullish",
                final_decision="push",
                now_ms=now_ms - 10 * 60_000 + offset,
            )
            assert (
                repos.news.begin_delivery(
                    event_id=event_id,
                    kind="first",
                    card={"event_id": event_id},
                    now_ms=now_ms - 5 * 60_000 + offset,
                )
                == "new"
            )
            assert repos.news.settle_delivery(
                event_id=event_id,
                kind="first",
                state="sent",
                receipt={"ok": True},
                error_code=None,
                now_ms=now_ms - 4 * 60_000 + offset,
            )

    status = repos.news.status_snapshot(now_ms=now_ms)
    pipeline = status["pipeline"]
    # Throughput ledgers keep their own 24 h clocks and therefore see both late completions.
    assert pipeline["triage_24h"] == 2
    assert status["delivery"]["sent_24h"] == 2
    # The reader funnel follows only Events opened in its one intake cohort, so every stage remains monotonic.
    assert {
        key: pipeline[key]
        for key in (
            "funnel_received_24h",
            "funnel_admitted_24h",
            "funnel_triaged_24h",
            "funnel_delivered_24h",
        )
    } == {
        "funnel_received_24h": 1,
        "funnel_admitted_24h": 1,
        "funnel_triaged_24h": 1,
        "funnel_delivered_24h": 1,
    }
    conn.commit()


def test_the_symbol_filter_names_an_identity_rather_than_one_spelling(conn) -> None:
    """#87/#207 PR-W1: the asset chip renders the collapsed base, so the filter behind it has to match it.

    `news_event_assets` stores the normalized provider tag. An Event grounded on `SKHX` carries `SKHX`, the
    chip renders `SKHY` because that is the identity the aliases collapse to, and the token page is keyed on
    `SKHY` — so matching the tag exactly left the Event that supplied the link missing from its own page.
    """

    repos = repositories_for_connection(conn)
    tagged_id, plain_id = _admit_test_events(
        conn,
        hit_base=1_796_000,
        titles=("SK Hynix raises its capex guidance", "An unrelated company reports"),
        hour=12,
    )
    with repos.transaction():
        conn.execute(
            "INSERT INTO news_symbol_aliases (alias, base_symbol, source, updated_at_ms)"
            " VALUES (%s, %s, 'seed', 0) ON CONFLICT (alias) DO NOTHING",
            ("SKHX", "SKHY"),
        )
        for event_id, tag in ((tagged_id, "SKHX"), (plain_id, "ZZTOP")):
            conn.execute(
                "INSERT INTO news_event_assets (event_id, symbol, opened_at_ms) "
                "SELECT %s, %s, opened_at_ms FROM news_events WHERE event_id = %s"
                " ON CONFLICT DO NOTHING",
                (event_id, tag, event_id),
            )

    def _served(symbol: str) -> set[str]:
        page = repos.news.list_feed(
            event_family=None,
            change_state=None,
            assertion_status=None,
            source_authority=None,
            subject_code=None,
            admission=None,
            final_decision=None,
            event_kind=None,
            search=compile_news_search(q=None, symbol=symbol, instruments=repos.instruments),
            limit=10_000,
            cursor=None,
        )
        return {event["event_id"] for event in page["events"]}

    # The canonical base finds the Event tagged with an alias of it...
    assert tagged_id in _served("SKHY")
    # ...the alias itself still finds it, since a reader can arrive from either spelling...
    assert tagged_id in _served("SKHX")
    # ...and neither pulls in an Event that answers to a different identity.
    assert plain_id not in _served("SKHY")
    conn.commit()


def test_feed_search_hard_cuts_asset_identity_from_full_text(conn) -> None:
    repos = repositories_for_connection(conn)
    tagged_new, tagged_old, plain_id = _admit_test_events(
        conn,
        hit_base=1_796_200,
        titles=(
            "A semiconductor foundry raises advanced packaging capacity",
            "A semiconductor supplier expands advanced packaging output",
            "A tropical cyclone closes a regional airport",
        ),
        hour=12,
    )
    with repos.transaction():
        conn.execute(
            "INSERT INTO news_symbol_aliases (alias, base_symbol, source, updated_at_ms)"
            " VALUES (%s, %s, 'seed', 0) ON CONFLICT (alias) DO UPDATE"
            " SET base_symbol = EXCLUDED.base_symbol, source = EXCLUDED.source",
            ("QSEARCHALIAS", "QSEARCHBASE"),
        )
        conn.execute(
            "INSERT INTO news_market_instruments"
            " (venue, venue_symbol, base_symbol, instrument_class, quote_asset, status, last_seen_ms)"
            " VALUES (%s, %s, %s, 'crypto', 'USDT', 'trading', 0)"
            " ON CONFLICT (venue, venue_symbol) DO UPDATE SET base_symbol = EXCLUDED.base_symbol",
            ("qsearch.venue", "QSEARCHBASE-PERP", "QSEARCHBASE"),
        )
        for event_id in (tagged_new, tagged_old):
            conn.execute(
                "INSERT INTO news_event_assets (event_id, symbol, opened_at_ms)"
                " SELECT %s, %s, opened_at_ms FROM news_events WHERE event_id = %s ON CONFLICT DO NOTHING",
                (event_id, "QSEARCHALIAS", event_id),
            )
        conn.execute(
            "UPDATE news_items SET reporting_origin = 'qsearch-origin'"
            " WHERE item_id = (SELECT leader_item_id FROM news_events WHERE event_id = %s)",
            (tagged_new,),
        )
        _insert_test_verdict(
            repos,
            event_id=tagged_old,
            direction="neutral",
            final_decision="drop",
            now_ms=1_800_000_000_000,
        )
    as_of_ms = (
        int(
            conn.execute(
                "SELECT max(opened_at_ms) AS opened_at_ms FROM news_events WHERE event_id = ANY(%s)",
                ([tagged_new, tagged_old],),
            ).fetchone()["opened_at_ms"]
        )
        + 60_000
    )

    def page(
        *,
        q: str | None = None,
        symbol: str | None = None,
        outcome: str | None = None,
        limit: int = 10,
        cursor: str | None = None,
    ):
        return repos.news.list_feed(
            event_family=None,
            change_state=None,
            assertion_status=None,
            source_authority=None,
            subject_code=None,
            admission=None,
            final_decision=None,
            event_kind=None,
            search=compile_news_search(q=q, symbol=symbol, instruments=repos.instruments),
            limit=limit,
            cursor=cursor,
            outcome=outcome,
            now_ms=as_of_ms,
        )

    asset_page = page(symbol="QSEARCHBASE")
    expected = [event["event_id"] for event in asset_page["events"]]
    assert set(expected) == {tagged_new, tagged_old}
    assert asset_page["counts"] == {"total": 2, "pushed": 0, "held": 1, "pending": 1}
    for query in ("QSEARCHBASE", "qsearchalias", "QSEARCHBASE-PERP", "QSEARCHBASEUSDT", "$QSEARCHBASE"):
        query_page = page(q=query)
        assert [event["event_id"] for event in query_page["events"]] == expected
        assert query_page["counts"] == asset_page["counts"]

    for outcome in ("pushed", "held", "pending"):
        outcome_page = page(q="QSEARCHBASE", outcome=outcome)
        assert len(outcome_page["events"]) == asset_page["counts"][outcome]
        assert outcome_page["counts"] == asset_page["counts"]

    first = page(q="QSEARCHBASE", limit=1)
    second = page(q="QSEARCHBASE", limit=1, cursor=first["next_cursor"])
    assert [event["event_id"] for event in first["events"] + second["events"]] == expected
    assert second["next_cursor"] is None

    for query in ("qsearch-origin", "qsearch.venue", "BTCT", "ABTC", "%", "_"):
        assert not ({tagged_new, tagged_old} & {event["event_id"] for event in page(q=query)["events"]})
    text_page = page(q="advanced packaging")
    text_ids = {event["event_id"] for event in text_page["events"]}
    assert {tagged_new, tagged_old} <= text_ids
    assert plain_id not in text_ids
    assert text_page["counts"] == asset_page["counts"]
    conn.commit()
