"""Scope repair is an atomic append-only head correction on isolated PostgreSQL."""

from __future__ import annotations

import pytest

from tests.news.test_news_head_scope_repair import BODY, historical_head
from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_update_pg import STAMP, seed_event, sql
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.events.facts import extract_fact_units
from tracefold.news.storage.head_scope_repairs import audit_scope_rows
from tracefold.news.updates.identity import digest

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


@pytest.mark.parametrize("notification_state", ("pending", "done"))
def test_scope_repair_cas_keeps_observation_separate_and_dispatches_retirement(notification_state: str) -> None:
    event_id = "event-alpha"
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
        """INSERT INTO news_semantic_observations
             (result_id,work_id,event_id,input_revision,input_sha256,program_identity,
              completed_at_ms,understanding)
           VALUES ('historical-result','historical-work',%s,2,%s,'historical-test',%s,'{}'::jsonb)""",
        (event_id, digest("historical-input"), STAMP),
    )
    sql(
        """INSERT INTO news_event_updates
             (event_id,content_revision,input_revision,previous_content_revision,
              adopted_at_ms,observation_result_id,document)
           VALUES (%s,%s,2,NULL,%s,'historical-result',%s::jsonb)""",
        (event_id, head.content_revision, STAMP + 1, head.model_dump_json()),
    )
    sql(
        """INSERT INTO news_event_update_heads
             (event_id,content_revision,input_revision,update_ref,adopted_at_ms)
           VALUES (%s,%s,2,%s,%s)""",
        (event_id, head.content_revision, head.ref, STAMP + 1),
    )
    decision_ref = None
    if notification_state == "done":
        decision_ref = "historical-decision"
        sql(
            """INSERT INTO news_notification_decisions
                 (decision_ref,event_id,update_ref,channel,input_snapshot,plan,origin,created_at_ms)
               VALUES (%s,%s,%s,'news','{}'::jsonb,'{}'::jsonb,'legacy_work_plan',%s)""",
            (decision_ref, event_id, head.ref, STAMP + 1),
        )
    sql(
        """INSERT INTO news_notification_work
             (event_id,channel,content_revision,state,decision_ref,
              attempts,next_attempt_at_ms,updated_at_ms)
           VALUES (%s,'news',%s,%s,%s,0,%s,%s)""",
        (event_id, head.content_revision, notification_state, decision_ref, STAMP + 1, STAMP + 1),
    )
    conn = connect_postgres_test(read_only=False)
    try:
        repos = repositories_for_connection(conn)
        with conn.transaction():
            report = audit_scope_rows(repos.news.head_scope_material())
            assert report["affected_heads"] == report["outside_active_claims"] == 1
            proof = report["events"][0]
            revision = repos.news.adopt_head_scope_repair(
                expected_head=head.content_revision, proof=proof, now_ms=STAMP + 2
            )
        assert revision != head.content_revision
        with pytest.raises(ValueError, match="news_scope_repair_head_changed"), conn.transaction():
            repos.news.adopt_head_scope_repair(expected_head=head.content_revision, proof=proof, now_ms=STAMP + 3)
    finally:
        conn.close()
    revisions = sql(
        """SELECT content_revision,observation_result_id,scope_repair_id
             FROM news_event_updates WHERE event_id=%s ORDER BY adopted_at_ms""",
        (event_id,),
    )
    assert len(revisions) == 2
    assert revisions[0]["observation_result_id"] == "historical-result"
    assert revisions[0]["scope_repair_id"] is None
    assert revisions[1]["observation_result_id"] is None
    assert revisions[1]["scope_repair_id"]
    assert (
        sql("SELECT content_revision FROM news_event_update_heads WHERE event_id=%s", (event_id,))[0][
            "content_revision"
        ]
        == revision
    )
    assert sql("SELECT count(*) AS n FROM news_head_scope_repairs")[0]["n"] == 1
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
    assert sql("SELECT count(*) AS n FROM trading_source_amendments")[0]["n"] == 1
    assert sql("SELECT count(*) AS n FROM news_semantic_observations")[0]["n"] == 1
    assert sql("SELECT content_revision,state,decision_ref FROM news_notification_work")[0] == {
        "content_revision": revision if notification_state == "pending" else head.content_revision,
        "state": notification_state,
        "decision_ref": decision_ref if notification_state == "done" else None,
    }
    assert sql("SELECT count(*) AS n FROM news_deliveries")[0]["n"] == 0
