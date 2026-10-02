"""Scope repair is an atomic append-only head correction on isolated PostgreSQL."""

from __future__ import annotations

import asyncio

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_0424_sql import (
    ANALYSES_SQL,
    ANALYSIS_HEADS_SQL,
    NOTIFY_JOBS_SQL,
    SEMANTIC_RESULTS_SQL,
    UPDATE_RECEIPTS_SQL,
)
from tests.support.news_head_scope import BODY, historical_head
from tests.support.news_update_pg import STAMP, Clock, ThreadedDb, notify_plan, save_card, seed_event, sql
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.events.facts import extract_fact_units
from tracefold.news.notifications.contracts import FrozenCard
from tracefold.news.notifications.ports import SendOutcome
from tracefold.news.storage.head_scope_repairs import audit_scope_rows
from tracefold.news.storage.notification_jobs import NotificationJobDetail
from tracefold.news.storage.notification_store import PgNotificationStore
from tracefold.news.storage.update_commit import lock_event
from tracefold.news.updates.identity import digest

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def _seed_numbered_head(notification_state: str = "pending"):
    event_id = "event-alpha"
    attempts = 3 if notification_state == "failed" else 0
    item_id = f"it-{event_id}"
    seed_event(event_id, text=BODY, title="Digest")
    head, _ = historical_head(item_id)
    unit = extract_fact_units(item_id=item_id, raw_text=BODY, fallback_title="Digest")[0]
    sql(
        """UPDATE news_events SET focus_fact_id=%s,focus_fact_text=%s,
             focus_fact_method='explicit_numbered' WHERE event_id=%s""",
        (unit.fact_id, unit.text, event_id),
    )
    sql(
        "UPDATE news_event_members SET fact_id=%s,fact_text=%s WHERE event_id=%s",
        (unit.fact_id, unit.text, event_id),
    )
    sql(
        """INSERT INTO news_analyses(analysis_id,origin,work_id,event_id,input_revision,input_sha256,
             program_identity,completed_at_ms,understanding,content_revision,update_ref,adopted_at_ms,document)
           VALUES ('historical-result','semantic','historical-work',%s,2,%s,'historical-test',
                   %s,'{}',%s,%s,%s,%s::jsonb)""",
        (
            event_id,
            digest("historical-input"),
            STAMP,
            head.content_revision,
            head.ref,
            STAMP + 1,
            head.model_dump_json(),
        ),
    )
    sql("UPDATE news_events SET current_analysis_id='historical-result' WHERE event_id=%s", (event_id,))
    decision_ref = None
    if notification_state == "done":
        decision_ref = "historical-decision"
        sql(
            """INSERT INTO news_notifications(notification_id,event_id,update_ref,kind,input_snapshot,plan,origin,
                 decided_at_ms,state,created_at_ms,updated_at_ms)
               VALUES (%s,%s,%s,'update','{}'::jsonb,'{}'::jsonb,'legacy_work_plan',%s,'decided',%s,%s)""",
            (decision_ref, event_id, head.ref, STAMP + 1, STAMP + 1, STAMP + 1),
        )
    detail = NotificationJobDetail(content_revision=head.content_revision, decision_ref=decision_ref)
    sql(
        """INSERT INTO news_jobs(job_kind,subject_id,detail,state,attempts,last_error_code,next_attempt_at_ms,
           created_at_ms,updated_at_ms) VALUES ('notify',%s,%s::jsonb,%s,%s,%s,%s,%s,%s)""",
        (
            event_id,
            detail.model_dump_json(),
            notification_state,
            attempts,
            "news_notification_plan:KeyError" if notification_state == "failed" else None,
            STAMP + 1,
            STAMP + 1,
            STAMP + 1,
        ),
    )
    return head


@pytest.mark.parametrize("notification_state", ("pending", "done", "failed"))
def test_scope_repair_cas_keeps_observation_separate_and_dispatches_retirement(notification_state: str) -> None:
    event_id = "event-alpha"
    decision_ref = "historical-decision" if notification_state == "done" else None
    head = _seed_numbered_head(notification_state)
    conn = connect_postgres_test(read_only=False)
    try:
        repos = repositories_for_connection(conn)
        with conn.transaction():
            report = audit_scope_rows(repos.news.head_scope_repairs.head_scope_material())
            assert report["affected_heads"] == report["outside_active_claims"] == 1
            proof = report["events"][0]
            revision = repos.news.head_scope_repairs.adopt_head_scope_repair(
                expected_head=head.content_revision, proof=proof, now_ms=STAMP + 2
            )
        assert revision != head.content_revision
        with pytest.raises(ValueError, match="news_scope_repair_head_changed"), conn.transaction():
            repos.news.head_scope_repairs.adopt_head_scope_repair(
                expected_head=head.content_revision, proof=proof, now_ms=STAMP + 3
            )
    finally:
        conn.close()
    revisions = sql(
        f"""SELECT content_revision,observation_result_id,scope_repair_id
             FROM ({ANALYSES_SQL}) WHERE event_id=%s ORDER BY adopted_at_ms""",
        (event_id,),
    )
    assert len(revisions) == 2
    assert revisions[0]["observation_result_id"] == "historical-result"
    assert revisions[0]["scope_repair_id"] is None
    assert revisions[1]["observation_result_id"] is None
    assert revisions[1]["scope_repair_id"]
    assert (
        sql(f"SELECT content_revision FROM ({ANALYSIS_HEADS_SQL}) WHERE event_id=%s", (event_id,))[0][
            "content_revision"
        ]
        == revision
    )
    assert sql("SELECT count(*) AS n FROM news_analyses WHERE origin='scope_repair'")[0]["n"] == 1
    (outbox,) = sql(
        """SELECT kind,source_fact_key,source_revision,payload,payload_sha256
             FROM news_trade_events WHERE source_revision=%s""",
        (revision,),
    )
    assert outbox["kind"] == "source_update"
    assert len(outbox["payload"]["retired_claim_refs"]) == 1
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            result = repositories_for_connection(conn).trading.receive_source_update(
                update_id=outbox["payload"]["update_id"],
                source_fact_key=outbox["source_fact_key"],
                content_revision=outbox["source_revision"],
                affected_claim_refs=outbox["payload"]["affected_claim_refs"],
                retired_claim_refs=outbox["payload"]["retired_claim_refs"],
                payload=outbox["payload"],
                payload_sha256=outbox["payload_sha256"],
                now_ms=STAMP + 3,
            )
        assert result == "accepted"
    finally:
        conn.close()
    assert sql("SELECT count(*) AS n FROM trading_inputs WHERE kind='source_update'")[0]["n"] == 1
    assert sql(f"SELECT count(*) AS n FROM ({SEMANTIC_RESULTS_SQL})")[0]["n"] == 1
    # Work still owed follows the repaired head -- pending, or failed and waiting for a retry of exactly that
    # head -- without its budget replenished; completed work stays with the head it completed.
    assert sql(f"SELECT content_revision,state,decision_ref FROM ({NOTIFY_JOBS_SQL})")[0] == {
        "content_revision": head.content_revision if notification_state == "done" else revision,
        "state": notification_state,
        "decision_ref": decision_ref if notification_state == "done" else None,
    }
    if notification_state == "failed":
        assert sql(f"SELECT attempts,last_error_code FROM ({NOTIFY_JOBS_SQL})")[0] == {
            "attempts": 3,
            "last_error_code": "news_notification_plan:KeyError",
        }
    if notification_state == "done":
        reader = connect_postgres_test(read_only=True)
        try:
            detail = repositories_for_connection(reader).news.event_detail(event_id)
        finally:
            reader.close()
        assert detail is not None and detail["processing"]["notification"] is None
    assert sql(f"SELECT count(*) AS n FROM ({UPDATE_RECEIPTS_SQL})")[0]["n"] == 0


def _prepared_intent(head):
    clock = Clock(STAMP + 2)
    store = PgNotificationStore(ThreadedDb(), clock=clock)
    snapshot = asyncio.run(store.notification_snapshot(head.event_id, "news"))
    assert snapshot is not None
    plan = notify_plan(head, snapshot.reader.revision)
    committed = asyncio.run(store.atomic_record_plan(plan))
    assert committed.status == "committed" and committed.lease is not None
    lease = committed.lease
    body = "范围\n\n" + "\n\n".join("命题" for _ in plan.selected_claim_refs)
    card = FrozenCard(
        intent_id=lease.intent_id,
        claim_refs=plan.selected_claim_refs,
        headline_zh="范围",
        body=body,
        payload_sha256=digest(body),
    )
    asyncio.run(save_card(store, lease, card))
    return store, lease, card


def _repair(head) -> str:
    conn = connect_postgres_test(read_only=False)
    try:
        repos = repositories_for_connection(conn)
        with conn.transaction():
            proof = audit_scope_rows(repos.news.head_scope_repairs.head_scope_material())["events"][0]
            return repos.news.head_scope_repairs.adopt_head_scope_repair(
                expected_head=head.content_revision, proof=proof, now_ms=STAMP + 3
            )
    finally:
        conn.close()


def test_repair_before_begin_refuses_the_old_frozen_send() -> None:
    head = _seed_numbered_head()
    store, lease, card = _prepared_intent(head)
    revision = _repair(head)

    assert asyncio.run(store.atomic_begin_send(lease, card)) == "head_changed"
    assert sql(f"SELECT intent_id FROM ({UPDATE_RECEIPTS_SQL})") == []
    assert sql(f"SELECT content_revision,attempts FROM ({NOTIFY_JOBS_SQL})") == [
        {"content_revision": revision, "attempts": 0}
    ]


def test_begin_waiting_for_event_lock_rechecks_head_after_repair_commit() -> None:
    head = _seed_numbered_head()
    store, lease, card = _prepared_intent(head)

    async def race() -> str:
        conn = connect_postgres_test(read_only=False)
        try:
            repos = repositories_for_connection(conn)
            with conn.transaction():
                lock_event(conn, head.event_id)
                begin = asyncio.create_task(store.atomic_begin_send(lease, card))
                for _ in range(100):
                    waiting = sql(
                        "SELECT 1 FROM pg_stat_activity WHERE datname=current_database() "
                        "AND wait_event_type='Lock' "
                        "AND query LIKE 'SELECT event_id FROM news_events%FOR NO KEY UPDATE%' LIMIT 1"
                    )
                    if waiting:
                        break
                    await asyncio.sleep(0.02)
                else:
                    begin.cancel()
                    raise AssertionError("begin did not reach the Event lock")
                proof = audit_scope_rows(repos.news.head_scope_repairs.head_scope_material())["events"][0]
                repos.news.head_scope_repairs.adopt_head_scope_repair(
                    expected_head=head.content_revision, proof=proof, now_ms=STAMP + 3
                )
            return await asyncio.wait_for(begin, 5)
        finally:
            conn.close()

    assert asyncio.run(race()) == "head_changed"
    assert sql(f"SELECT intent_id FROM ({UPDATE_RECEIPTS_SQL})") == []


def test_begin_before_repair_keeps_the_old_send_result_owned_by_its_intent() -> None:
    head = _seed_numbered_head()
    store, lease, card = _prepared_intent(head)
    assert asyncio.run(store.atomic_begin_send(lease, card)) == "begun"
    revision = _repair(head)

    assert sql(f"SELECT state,content_revision FROM ({UPDATE_RECEIPTS_SQL})") == [
        {"state": "sending", "content_revision": head.content_revision}
    ]
    result = asyncio.run(
        store.settle_send(
            lease,
            card,
            SendOutcome(state="sent", payload_sha256=card.payload_sha256, message_id="received"),
            settled_at_ms=STAMP + 4,
        )
    )
    assert result == "sent"
    assert sql(f"SELECT state,content_revision FROM ({UPDATE_RECEIPTS_SQL})") == [
        {"state": "sent", "content_revision": head.content_revision}
    ]
    assert sql(f"SELECT state,content_revision FROM ({NOTIFY_JOBS_SQL})") == [
        {"state": "pending", "content_revision": revision}
    ]
