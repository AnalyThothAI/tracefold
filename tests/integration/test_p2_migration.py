"""Notification identities and every source projection survive the P2 forward cut."""

from __future__ import annotations

import json
from contextlib import closing

import pytest
from alembic import command

from tests.postgres_test_utils import (
    connect_postgres_test,
    postgres_migration_test_dsn,
    prepare_test_migration_database,
)
from tests.support.news_event_updates import first_update, notify_plan, persist_update
from tests.support.news_update_pg import STAMP, seed_event
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.notifications.contracts import FrozenCard
from tracefold.news.updates.identity import digest
from tracefold.platform.postgres.migrations import alembic_config

pytestmark = [pytest.mark.integration, pytest.mark.migration, pytest.mark.usefixtures("postgres_migration_dsn")]
SOURCE, TARGET = "20261001_0421", "20261001_0422"


@pytest.fixture
def source(postgres_migration_dsn):
    with closing(connect_postgres_test()) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
    prepare_test_migration_database(postgres_migration_dsn)
    config = alembic_config()
    config.attributes["database_url"] = postgres_migration_test_dsn()
    command.upgrade(config, SOURCE)
    return config


def legacy_notification(state: str, *, legacy: bool = False):
    event_id = f"p2-{state}-{'legacy' if legacy else 'judged'}"
    seed_event(event_id, fingerprint=event_id)
    update = first_update(event_id)
    plan = notify_plan(update)
    card = FrozenCard(
        intent_id=plan.intent_id,
        claim_refs=plan.selected_claim_refs,
        headline_zh="tariff",
        body="tariff",
        payload_sha256=digest("tariff"),
    )
    with closing(connect_postgres_test()) as conn, conn.transaction():
        persist_update(conn, update)
        if not legacy:
            conn.execute(
                """INSERT INTO news_notification_decisions
                   (decision_ref,event_id,update_ref,channel,input_digest,input_snapshot,plan,origin,created_at_ms)
                   VALUES (%s,%s,%s,'news',%s,'{}',%s::jsonb,'reader_v2',%s)""",
                (plan.record_ref, event_id, update.ref, plan.input_digest, plan.model_dump_json(), STAMP),
            )
        if state in ("pending", "dead", "sending", "terminal"):
            conn.execute(
                """INSERT INTO news_delivery_queue(intent_id,event_id,kind,state,attempts,enqueued_at_ms,
                   next_attempt_at_ms,updated_at_ms,content_revision,claim_refs,plan_key,frozen_card,lease_token,
                   decision_ref,settled_at_ms,error_code,last_settlement)
                   VALUES (%s,%s,'update',%s,1,%s,%s,%s,%s,%s::jsonb,false,%s::jsonb,%s,%s,%s,%s,%s::jsonb)""",
                (
                    plan.intent_id,
                    event_id,
                    "dead" if state in ("dead", "terminal") else "pending",
                    STAMP,
                    STAMP + 120000,
                    STAMP + 1,
                    update.content_revision,
                    json.dumps(list(plan.selected_claim_refs)),
                    card.model_dump_json(),
                    "lease" if state in ("pending", "sending") else None,
                    None if legacy else plan.record_ref,
                    STAMP + 2 if state in ("dead", "terminal") else None,
                    "old_retry" if state == "sending" else None,
                    json.dumps({"previous": "not_sent"}) if state == "sending" else None,
                ),
            )
        if state in ("sending", "sent", "ambiguous", "terminal"):
            conn.execute(
                """INSERT INTO news_deliveries(intent_id,event_id,kind,state,card,receipt,attempted_at_ms,
                   settled_at_ms,created_at_ms,content_revision,claim_refs,body,payload_sha256,plan_key,decision_ref)
                   VALUES (%s,%s,'update',%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s::jsonb,%s,%s,false,%s)""",
                (
                    plan.intent_id,
                    event_id,
                    state,
                    card.model_dump_json(),
                    "{}" if state == "sent" else None,
                    STAMP + 1,
                    None if state == "sending" else STAMP + 2,
                    STAMP,
                    update.content_revision,
                    json.dumps(list(plan.selected_claim_refs)),
                    card.body,
                    card.payload_sha256,
                    None if legacy else plan.record_ref,
                ),
            )
    return plan, card


def test_p2_empty_upgrade(source):
    command.upgrade(source, TARGET)
    with closing(connect_postgres_test()) as conn:
        assert conn.execute("SELECT count(*) AS n FROM pg_tables WHERE schemaname='public'").fetchone()["n"] == 45
        for table in ("news_jobs", "news_notifications"):
            assert conn.execute("SELECT reloptions FROM pg_class WHERE relname=%s", (table,)).fetchone()[
                "reloptions"
            ] == ["fillfactor=85"]


def test_p2_market_jobs_and_cards_preserve_all_states(source):
    seed_event("p2-market", fingerprint="p2-market")
    with closing(connect_postgres_test()) as conn, conn.transaction():
        # The P1 observation identity owns the trigger FK; this source bypasses the current writer.
        conn.execute(
            """INSERT INTO news_market_observations(observation_id,kind,source_id,source_item_key,
                 source_strategy_id,ingest_mode,event_at_ms,received_at_ms,created_at_ms,updated_at_ms,
                 title,raw_first_line,description,parse_status,parse_error,notify_state)
               VALUES ('p2-market','unknown_market','opennews','p2-market','2026','live',%s,%s,%s,%s,
                 'fixture','fixture','fixture','raw','unstructured','processed')""",
            (STAMP,) * 4,
        )
        for state in ("pending", "unavailable", "sending", "sent", "failed", "unknown"):
            conn.execute(
                """INSERT INTO news_market_tracks(group_key,market_kind,family,last_observed_at_ms,
                   last_observed_item_id,open_delivery_key,next_due_at_ms,round_started_at_ms,
                   created_at_ms,updated_at_ms) VALUES (%s,'oi','oi',%s,'p2-market',%s,%s,%s,%s,%s)""",
                (state, STAMP, state, STAMP + 20, STAMP - 20, STAMP, STAMP),
            )
            attempted = state not in ("pending", "unavailable")
            conn.execute(
                """INSERT INTO news_market_deliveries(delivery_key,group_key,market_kind,trigger_reason,
                   trigger_item_id,state,attempts,card,receipt,next_attempt_at_ms,first_attempt_at_ms,
                   settled_at_ms,created_at_ms,updated_at_ms)
                   VALUES (%s,%s,'oi','first','p2-market',%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s)""",
                (
                    state,
                    state,
                    state,
                    int(attempted),
                    '{"decimal":"0.000000000000000001"}' if attempted else "{}",
                    '{"message_id":"sent"}' if state == "sent" else None,
                    STAMP + 20,
                    STAMP if attempted else None,
                    STAMP + 1 if state in ("sent", "failed", "unknown") else None,
                    STAMP,
                    STAMP,
                ),
            )
    command.upgrade(source, TARGET)
    with closing(connect_postgres_test()) as conn:
        assert conn.execute("SELECT count(*) AS n FROM news_jobs WHERE job_kind='market_notify'").fetchone()["n"] == 6
        rows = conn.execute("SELECT notification_id,state FROM news_notifications WHERE kind='market'").fetchall()
        assert all(row["notification_id"] == row["state"] for row in rows) and len(rows) == 6


def test_p2_terminal_without_queue_and_immutable_boundaries(source):
    plan, card = legacy_notification("terminal", legacy=True)
    with closing(connect_postgres_test()) as conn, conn.transaction():
        conn.execute("DELETE FROM news_delivery_queue WHERE intent_id=%s", (plan.intent_id,))
    command.upgrade(source, TARGET)
    with closing(connect_postgres_test()) as conn:
        row = conn.execute("SELECT * FROM news_notifications WHERE intent_id=%s", (plan.intent_id,)).fetchone()
        assert row["reserved_at_ms"] is None and row["card"] == card.model_dump(mode="json")
        for statement, reason in (
            ("UPDATE news_notifications SET plan='{}'", "news_notification_decision_immutable"),
            ("UPDATE news_notifications SET card='{}'", "news_notification_settled_send_immutable"),
        ):
            with pytest.raises(Exception, match=reason), conn.transaction():
                conn.execute(statement)


def test_orphan_job_sweep_is_bounded_and_skips_another_owner(source):
    command.upgrade(source, TARGET)
    seed_event("p2-live", fingerprint="p2-live")
    with closing(connect_postgres_test()) as conn, conn.transaction():
        for subject in ("p2-live", "orphan-held", "orphan-free"):
            conn.execute(
                """INSERT INTO news_jobs(job_kind,subject_id,state,created_at_ms,updated_at_ms)
                   VALUES ('notify',%s,'pending',%s,%s)""",
                (subject, STAMP, STAMP),
            )
    with closing(connect_postgres_test()) as held, closing(connect_postgres_test()) as sweep:
        with held.transaction():
            held.execute("SELECT 1 FROM news_jobs WHERE subject_id='orphan-held' FOR UPDATE")
            with sweep.transaction():
                assert repositories_for_connection(sweep).news.sweep_orphan_jobs(limit=1) == 1
                assert sweep.execute("SELECT subject_id FROM news_jobs ORDER BY subject_id").fetchall() == [
                    {"subject_id": "orphan-held"},
                    {"subject_id": "p2-live"},
                ]
        with sweep.transaction():
            assert repositories_for_connection(sweep).news.sweep_orphan_jobs(limit=1) == 1
            assert repositories_for_connection(sweep).news.sweep_orphan_jobs(limit=1) == 0


def test_p2_all_editorial_states_preserve_identity_and_frozen_payload(source):
    plans = {
        state: legacy_notification(state)
        for state in ("decided", "pending", "dead", "sending", "sent", "ambiguous", "terminal")
    }
    legacy_notification("sent", legacy=True)
    legacy_notification("pending", legacy=True)
    command.upgrade(source, TARGET)
    with closing(connect_postgres_test()) as conn:
        rows = {r["notification_id"]: r for r in conn.execute("SELECT * FROM news_notifications").fetchall()}
        assert len(rows) == 9
        for state, (plan, card) in plans.items():
            row = rows[plan.record_ref]
            assert row["state"] == state and row["plan"] == plan.model_dump(mode="json")
            if state == "decided":
                assert row["intent_id"] is None
            else:
                assert row["intent_id"] == plan.intent_id and row["card"] == card.model_dump(mode="json")
        sending = rows[plans["sending"][0].record_ref]
        assert sending["error_code"] == "old_retry" and sending["settlement"] == {"previous": "not_sent"}
        assert sending["lease_until_ms"] == STAMP + 120000
        assert (
            conn.execute("SELECT count(*) AS n FROM news_notifications WHERE origin='legacy_delivery'").fetchone()["n"]
            == 2
        )


@pytest.mark.parametrize(
    "column,value,reason",
    [("body", "wrong", "p2_card_payload_mismatch"), ("frozen_card", "{}", "p2_queue_ledger_mismatch")],
)
def test_p2_rejects_mismatched_sources_without_partial_cut(source, column, value, reason):
    legacy_notification("sending")
    with closing(connect_postgres_test()) as conn, conn.transaction():
        if column == "body":
            # Fault injection: a source whose historical constraint was bypassed must fail closed.
            conn.execute("ALTER TABLE news_deliveries DROP CONSTRAINT news_deliveries_intent_check")
            conn.execute("UPDATE news_deliveries SET body=%s", (value,))
        else:
            conn.execute("UPDATE news_delivery_queue SET frozen_card=%s::jsonb", (value,))
    with pytest.raises(Exception, match=reason):
        command.upgrade(source, TARGET)
    with closing(connect_postgres_test()) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == SOURCE
        assert conn.execute("SELECT to_regclass('news_notifications') AS relation").fetchone()["relation"] is None
