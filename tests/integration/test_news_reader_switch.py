"""The reviewed switch SQL drains reservations while preserving the ledger."""

from __future__ import annotations

import asyncio
from contextlib import closing
from pathlib import Path

import psycopg
import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_update_pg import EVENT, Sender, adopted_head, notifications, store

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn", "synthetic_reader_calibration")]
SWITCH_SQL = (Path(__file__).resolve().parents[2] / "scripts/news_reader_switch.sql").read_text("utf-8")


def prepared():
    stores, _, clock = store()
    adopted_head(stores.semantic, clock)
    turn = asyncio.run(notifications(stores.notifications, clock, Sender()).service.prepare(EVENT, "news"))
    assert turn.status == "ready"
    return stores, clock


def test_switch_clears_old_card_preserves_decision_and_wakes_fresh_plan() -> None:
    stores, clock = prepared()
    with closing(connect_postgres_test()) as conn:
        before = conn.execute(
            "SELECT notification_id,plan,input_snapshot,input_digest,card FROM news_notifications WHERE state='pending'"
        ).fetchone()
        assert before["card"]
        conn.execute("UPDATE news_notifications SET lease_until_ms=0 WHERE state='pending'")
        conn.commit()
        conn.execute(SWITCH_SQL)
        after = conn.execute(
            "SELECT * FROM news_notifications WHERE notification_id=%s", (before["notification_id"],)
        ).fetchone()
        assert (after["state"], after["intent_id"], after["card"], after["card_copy_document"]) == (
            "decided",
            None,
            None,
            None,
        )
        assert all(after[key] == before[key] for key in ("plan", "input_snapshot", "input_digest"))
        job = conn.execute("SELECT state,next_attempt_at_ms FROM news_jobs WHERE job_kind='notify'").fetchone()
        assert job["state"] == "pending" and job["next_attempt_at_ms"] > 0
        conn.commit()
        conn.execute(SWITCH_SQL)  # A second drain is a no-op, including for historical decisions.
    # Use the simulated clock again; the SQL made work due at the actual operator time.
    with closing(connect_postgres_test()) as conn:
        conn.execute("UPDATE news_jobs SET next_attempt_at_ms=%s WHERE job_kind='notify'", (clock(),))
        conn.commit()
    sender = Sender()
    assert asyncio.run(notifications(stores.notifications, clock, sender).process(EVENT, "news")) == "sent"
    assert len(sender.cards) == 1


@pytest.mark.parametrize("state,code", [("sending", "sending_not_drained"), ("pending", "pending_lease_active")])
def test_switch_refuses_unsettled_or_live_reserved_send(state: str, code: str) -> None:
    prepared()
    with closing(connect_postgres_test()) as conn:
        conn.execute(
            "UPDATE news_notifications SET state=%s,lease_until_ms=9223372036854775807 WHERE state='pending'", (state,)
        )
        conn.commit()
        before = conn.execute("SELECT to_jsonb(n) AS row FROM news_notifications n").fetchall()
        with pytest.raises(psycopg.errors.CheckViolation, match=code):
            conn.execute(SWITCH_SQL)
        conn.rollback()
        assert conn.execute("SELECT to_jsonb(n) AS row FROM news_notifications n").fetchall() == before


def test_switch_preserves_sent_delivery_evidence() -> None:
    stores, _, clock = store()
    adopted_head(stores.semantic, clock)
    assert asyncio.run(notifications(stores.notifications, clock, Sender()).process(EVENT, "news")) == "sent"
    with closing(connect_postgres_test()) as conn:
        before = conn.execute("SELECT to_jsonb(n) AS row FROM news_notifications n WHERE state='sent'").fetchall()
        conn.commit()
        conn.execute(SWITCH_SQL)
        after = conn.execute("SELECT to_jsonb(n) AS row FROM news_notifications n WHERE state='sent'").fetchall()
        assert after == before


def test_switch_refuses_retryable_not_sent_and_preserves_the_same_intent_budget() -> None:
    stores, _, clock = store()
    adopted_head(stores.semantic, clock)
    sender = Sender("not_sent")
    workflow = notifications(stores.notifications, clock, sender)
    assert asyncio.run(workflow.process(EVENT, "news")) == "not_sent"
    clock.now_ms += 30_001
    assert asyncio.run(workflow.process(EVENT, "news")) == "not_sent"
    with closing(connect_postgres_test()) as conn:
        before = conn.execute("SELECT to_jsonb(n) AS row FROM news_notifications n ORDER BY notification_id").fetchall()
        before_jobs = conn.execute("SELECT to_jsonb(j) AS row FROM news_jobs j ORDER BY job_kind,subject_id").fetchall()
        queued = next(result["row"] for result in before if result["row"]["state"] == "pending")
        assert queued["attempts"] == 2 and queued["settlement"]["state"] == "not_sent"
        conn.commit()
        with pytest.raises(psycopg.errors.CheckViolation, match="news_reader_switch_pending_attempt_history"):
            conn.execute(SWITCH_SQL)
        conn.rollback()
        assert (
            conn.execute("SELECT to_jsonb(n) AS row FROM news_notifications n ORDER BY notification_id").fetchall()
            == before
        )
        assert (
            conn.execute("SELECT to_jsonb(j) AS row FROM news_jobs j ORDER BY job_kind,subject_id").fetchall()
            == before_jobs
        )
    # The old workflow still owns the original bounded retry budget after a refused switch.
    clock.now_ms += 120_001
    assert asyncio.run(workflow.process(EVENT, "news")) == "not_sent"
    with closing(connect_postgres_test()) as conn:
        final = conn.execute(
            "SELECT intent_id,state,attempts,settlement FROM news_notifications WHERE intent_id=%s",
            (queued["intent_id"],),
        ).fetchone()
        assert final["state"] == "dead" and final["attempts"] == 3
        assert final["settlement"]["state"] == "not_sent"
        assert conn.execute("SELECT state FROM news_jobs WHERE job_kind='notify'").fetchone()["state"] == "failed"
