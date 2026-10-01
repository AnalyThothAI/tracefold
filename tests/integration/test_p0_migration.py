"""P0 upgrades preserve membership and receipt projections and refuse unsafe source data."""

from __future__ import annotations

import json
from contextlib import closing

import pytest
from alembic import command
from psycopg.errors import CheckViolation

from tests.fixtures.news_semantic_0422 import persist_update, seed_event
from tests.postgres_test_utils import (
    connect_postgres_test,
    postgres_migration_test_dsn,
    prepare_test_migration_database,
)
from tests.support.news_event_updates import first_update
from tests.support.news_update_pg import EVENT
from tracefold.platform.postgres.migrations import alembic_config

pytestmark = [pytest.mark.integration, pytest.mark.migration, pytest.mark.usefixtures("postgres_migration_dsn")]
SOURCE, TARGET = "20261001_0419", "20261001_0420"
WALLET, TOKEN, TX = "0x" + "1" * 40, "0x" + "2" * 40, "0x" + "3" * 64


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


def historical_head():
    # Literal predecessor facts: the current runtime now writes the P2 notification tables.
    seed_event()
    head = first_update(EVENT)
    with closing(connect_postgres_test()) as conn, conn.transaction():
        persist_update(conn, head)
    return head


def roster(conn, *, inconsistent=False):
    for version, members in [(1, [WALLET]), (2, [WALLET]), (3, [WALLET, TOKEN])]:
        for index, wallet in enumerate(members):
            conn.execute(
                """INSERT INTO news_market_wallet_roster
                   (roster_version,taken_at_ms,wallet,handle,known_at_ms,monitoring_from_ms)
                   VALUES (%s,%s,%s,'member',%s,1000)""",
                (version, version * 1000, wallet, version * 1000 + (index if inconsistent else 0)),
            )


def receipt(conn, head):
    claims = [claim.model_dump(mode="json") for claim in head.claims]
    conn.execute(
        """INSERT INTO news_deliveries(intent_id,event_id,kind,state,card,receipt,attempted_at_ms,
             settled_at_ms,created_at_ms,content_revision,claim_refs,body,payload_sha256,plan_key)
           VALUES ('intent:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                   %s,'update','sent','{}','{}',1,2,1,%s,%s::jsonb,'sent',news_text_digest('sent'),false)""",
        (head.event_id, head.content_revision, json.dumps([claim["ref"] for claim in claims])),
    )
    return claims


def test_populated_upgrade_preserves_members_fills_cursor_and_sent_claims(source):
    head = historical_head()
    with closing(connect_postgres_test()) as conn:
        roster(conn)
        expected = receipt(conn, head)
        conn.execute(
            """INSERT INTO news_market_wallet_tape_state(state_id,roster_version,updated_at_ms)
               VALUES ('chain_tape',1,1000)"""
        )
        conn.execute(
            """INSERT INTO news_market_wallet_fills(chain_id,tx_hash,log_index,block_number,block_hash,
                 wallet,token,kind,amount_raw,event_at_ms,received_at_ms,classified_at_ms,roster_version)
               VALUES (4663,%s,0,100,%s,%s,%s,'transfer_out',1,1000,1000,1000,1)""",
            (TX, TX, WALLET, TOKEN),
        )
        conn.commit()
    command.upgrade(source, TARGET)
    with closing(connect_postgres_test()) as conn:
        rows = conn.execute(
            "SELECT roster_version,wallet,known_at_ms FROM news_market_wallet_roster ORDER BY 1,2"
        ).fetchall()
        assert [(r["roster_version"], r["wallet"], r["known_at_ms"]) for r in rows] == [
            (2, WALLET, 1000),
            (3, WALLET, 3000),
            (3, TOKEN, 3000),
        ]
        assert conn.execute("SELECT roster_version FROM news_market_wallet_fills").fetchone()["roster_version"] == 2
        tape = conn.execute("SELECT roster_version FROM news_market_wallet_tape_state").fetchone()
        assert tape["roster_version"] == 2
        assert conn.execute("SELECT sent_claims FROM news_deliveries").fetchone()["sent_claims"] == expected
        conn.execute("""INSERT INTO news_market_wallet_roster
            (roster_version,taken_at_ms,wallet,known_at_ms,monitoring_from_ms)
            SELECT 4,4000,'0x' || lpad(to_hex(n),40,'0'),4000,4000
            FROM generate_series(1,10000) n""")
        conn.execute("ANALYZE news_market_wallet_roster")
        plan = conn.execute(
            "EXPLAIN UPDATE news_market_wallet_roster SET monitoring_from_ms=2000 WHERE monitoring_from_ms IS NULL"
        ).fetchall()
        assert "news_market_wallet_roster_unmonitored" in str(plan)
        for table in (
            "trading_trigger_conflicts",
            "news_evidence_documents",
            "news_market_wallet_archive",
            "news_market_instrument_listing_events",
        ):
            assert conn.execute("SELECT to_regclass(%s) AS relation", (table,)).fetchone()["relation"] is None


@pytest.mark.parametrize(
    "poison, error",
    [
        ("roster", "p0_roster_known_at_not_constant_per_version"),
        ("conflict", "p0_trading_trigger_conflicts_not_empty"),
        ("evidence", "p0_news_evidence_documents_not_empty"),
        ("delete", "p0_news_deliveries_delete_state_present"),
        ("document", "p0_event_update_document_invalid"),
        ("plan", "p0_decision_contract_violations"),
        ("writer", "p0_trading_writers_connected"),
    ],
)
def test_upgrade_rejects_invalid_source_and_rolls_back(source, poison, error):
    head = historical_head()
    with closing(connect_postgres_test()) as conn:
        if poison == "roster":
            roster(conn, inconsistent=True)
        elif poison == "conflict":
            conn.execute("INSERT INTO trading_trigger_conflicts VALUES ('oi','key','v1','attempt','original',1)")
        elif poison == "evidence":
            conn.execute("""INSERT INTO news_evidence_documents
                (document_id,requested_url,final_url,normalized_url,response_sha256,extracted_text_sha256,
                 extractor_version,extracted_text,observed_at_ms,available_at_ms,content_type,extraction_status)
                VALUES ('old','https://archive.invalid','https://archive.invalid','https://archive.invalid',
                        'response','text','old','Archived text',1,1,'text/plain','success')""")
        elif poison == "delete":
            receipt(conn, head)
            conn.execute("""UPDATE news_deliveries SET delete_state='deleted',delete_evidence='{}',
                delete_reason='test',delete_attempted_at_ms=1,delete_settled_at_ms=2""")
        elif poison == "document":
            # Updates are append-only; a second malformed version demonstrates the old NULL loophole.
            conn.execute(
                """INSERT INTO news_event_updates(event_id,content_revision,input_revision,
                adopted_at_ms,observation_result_id,document)
                SELECT event_id,%s,input_revision,adopted_at_ms,observation_result_id,'{}'
                FROM news_event_updates LIMIT 1""",
                ("a" * 64,),
            )
        elif poison == "plan":
            conn.execute(
                """INSERT INTO news_notification_decisions
                (decision_ref,event_id,update_ref,channel,input_digest,input_snapshot,plan,origin,created_at_ms)
                VALUES ('bad',%s,'update:bad','news','bad','{}','{}','reader_v2',1)""",
                (head.event_id,),
            )
        else:
            conn.execute("SET application_name='tracefold_executor'")
        conn.commit()
        with pytest.raises(Exception, match=error):
            command.upgrade(source, TARGET)
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == SOURCE
        assert conn.execute("SELECT to_regclass('trading_trigger_conflicts') AS relation").fetchone()["relation"]


def test_empty_documents_and_plans_are_rejected_at_target(source):
    head = historical_head()
    command.upgrade(source, TARGET)
    with closing(connect_postgres_test()) as conn:
        with pytest.raises(CheckViolation), conn.transaction():
            conn.execute(
                """INSERT INTO news_event_updates(event_id,content_revision,input_revision,
                adopted_at_ms,observation_result_id,document)
                SELECT event_id,%s,input_revision,adopted_at_ms,observation_result_id,'{}'
                FROM news_event_updates LIMIT 1""",
                ("a" * 64,),
            )
        with pytest.raises(CheckViolation), conn.transaction():
            conn.execute(
                """INSERT INTO news_notification_decisions
                (decision_ref,event_id,update_ref,channel,input_digest,input_snapshot,plan,origin,created_at_ms)
                VALUES ('bad',%s,'update:bad','news','bad','{}','{}','reader_v2',1)""",
                (head.event_id,),
            )
