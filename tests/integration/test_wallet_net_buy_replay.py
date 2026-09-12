"""Executed hot-token plans and bounded synthetic receipt replay for #641."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest

from tests.integration.test_wallet_net_buy import (
    CHAIN_ID,
    NOW,
    TOKEN,
    Db,
    Sender,
    add_facts,
    events,
    fill,
    run,
    seed,
)
from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.market_notifications import MarketNotificationLoop
from tracefold.news.storage.wallet_events import NET_BUY_WINDOW_SQL, WALLET_PENDING_RECEIPTS_SQL

pytestmark = pytest.mark.integration


@pytest.fixture()
def conn(postgres_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
    yield connection
    connection.close()


def _scanned(plan):
    total = 0
    if plan["Node Type"] in {"Index Scan", "Index Only Scan", "Bitmap Heap Scan", "Seq Scan"}:
        total = (plan.get("Actual Rows", 0) + plan.get("Rows Removed by Filter", 0)) * plan.get("Actual Loops", 1)
    return total + sum(_scanned(child) for child in plan.get("Plans", []))


def test_hot_token_plan_bounded_replay_and_unchanged_snapshot_have_measured_evidence(conn):
    context = [fill(i, at=NOW - 7200000) for i in range(1, 10001)]
    seed(conn, context)
    conn.execute("UPDATE news_market_wallet_fills SET derived_at_ms=classified_at_ms,derived_reason='replay_context'")
    stale_token = "0x" + "bb" * 20
    net_zero_token = "0x" + "cc" * 20
    unknown_token = "0x" + "dd" * 20
    stale = [replace(fill(10000 + i, wallet=i, at=NOW - 100000), token=stale_token) for i in range(1, 4)]
    hot = [fill(10003 + i, wallet=(i - 1) % 6 + 1, usd="100", raw=100) for i in range(1, 201)]
    negative = []
    for i in range(1, 4):
        negative.extend(
            [
                replace(fill(10203 + i, wallet=i, usd="2000", raw=2000, log=1), token=net_zero_token),
                replace(fill(10203 + i, wallet=i, kind="sell", usd="1800", raw=2000, log=2), token=net_zero_token),
            ]
        )
    unknown = [replace(fill(10206 + i, wallet=i), token=unknown_token) for i in range(1, 4)]
    unknown.append(replace(fill(10209, wallet=3, kind="sell", usd=None, raw=1, log=2), token=unknown_token))
    transfer_token = "0x" + "ee" * 20
    transferred = [replace(fill(10209 + i, wallet=i), token=transfer_token) for i in range(1, 4)]
    transferred.append(
        replace(fill(10212, wallet=3, kind="transfer_out", usd=None, raw=1, log=2), token=transfer_token)
    )
    pending = stale + hot + negative + unknown + transferred
    add_facts(conn, pending, stamp=NOW)
    conn.execute("ANALYZE news_market_wallet_fills")
    parameters = dict(chain_id=CHAIN_ID, token=TOKEN, from_ms=NOW - 1800000, to_ms=NOW, block=20000, log=2147483647)
    window_plan = conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + NET_BUY_WINDOW_SQL, parameters).fetchone()[
        "QUERY PLAN"
    ][0]
    pending_plan = conn.execute(
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + WALLET_PENDING_RECEIPTS_SQL, (20,)
    ).fetchone()["QUERY PLAN"][0]
    assert window_plan["Plan"]["Actual Rows"] == 200
    assert _scanned(window_plan["Plan"]) < 1000
    assert _scanned(pending_plan["Plan"]) < 1000
    durations = []
    receipt_count = opened = 0
    replay_started = time.perf_counter()
    while True:
        started = time.perf_counter()
        result = run(conn)
        durations.append((time.perf_counter() - started) * 1000)
        receipt_count += result.receipts
        opened += result.opened
        assert result.receipts <= 20 and not result.errors
        if result.receipts == 0:
            break
        assert len(durations) < 30
    assert receipt_count == 212 and opened == 1
    assert len(events(conn)) == 1 and events(conn)[0]["token"] == TOKEN
    assert events(conn)[0]["initial_snapshot"]["fast"]["qualified_n"] == 3
    assert events(conn)[0]["latest_snapshot"]["fast"]["qualified_n"] == 6
    before = conn.execute("SELECT xmin::text AS version FROM news_market_wallet_events").fetchone()
    assert run(conn).updated == 0
    assert conn.execute("SELECT xmin::text AS version FROM news_market_wallet_events").fetchone() == before
    repos = repositories_for_connection(conn)
    with repos.transaction():
        assert repos.news.chain_tape_record_fills(pending) == 0
    assert run(conn).receipts == 0
    sender = Sender()
    asyncio.run(MarketNotificationLoop(db=Db(conn), sender=sender, clock=lambda: NOW).advance())
    assert len(sender.cards) == 1
    reasons = conn.execute("""
        SELECT derived_reason,count(DISTINCT (chain_id,tx_hash,token)) AS count
        FROM news_market_wallet_fills WHERE derived_reason<>'replay_context'
        GROUP BY derived_reason ORDER BY derived_reason
    """).fetchall()
    report = {
        "scope": "synthetic replay on isolated PostgreSQL; not live frequency, profitability or SLO evidence",
        "source_interval_ms": [NOW - 7200000, NOW],
        "known_at_roster": "seeded one hour before trigger; 10 quality members; six hot-token participants",
        "context_fills": 10000,
        "evaluated_fills": len(pending),
        "complete_receipts": receipt_count,
        "episodes": opened,
        "logical_first_intents": len(sender.cards),
        "duplicate_new_fills": 0,
        "unchanged_snapshot_writes": 0,
        "unpriced_trades": 1,
        "net_zero_wallets": 3,
        "wallets_excluded_by_transfer_out": 1,
        "stale_trigger_receipts": 3,
        "missing_baselines": 1,
        "comparable_price_outcomes": 0,
        "derivation_reasons": reasons,
        "replay_to_first_send_ms": (time.perf_counter() - replay_started) * 1000,
        "clock_scope": "chain/receive/detect/intent/attempt clocks are controlled, not measured network latency",
        "turn_ms": {"count": len(durations), "max": max(durations), "total": sum(durations)},
        "window_plan": window_plan,
        "pending_plan": pending_plan,
    }
    output = os.environ.get("TRACEFOLD_WALLET_REPLAY_REPORT")
    if output:
        Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if not k.endswith("_plan")}, ensure_ascii=False))
