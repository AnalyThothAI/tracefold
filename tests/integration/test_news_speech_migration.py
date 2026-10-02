"""The forward-only speech conversion preserves adopted and sent fact identities."""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import closing

import pytest
from alembic import command
from psycopg.types.json import Jsonb

from tests.postgres_test_utils import connect_postgres_test, postgres_migration_test_dsn
from tests.support.news_update_pg import EVENT, Sender, adopted_head, notifications, store
from tracefold.news.updates.contracts import EventUpdate, Extraction
from tracefold.platform.postgres.migrations import alembic_config

pytestmark = [pytest.mark.integration, pytest.mark.migration, pytest.mark.usefixtures("postgres_migration_dsn")]


def test_speech_conversion_preserves_sources_refs_receipts_and_restores_guards(postgres_migration_dsn):
    config = alembic_config()
    config.attributes["database_url"] = postgres_migration_test_dsn()
    command.upgrade(config, "20261002_0426")
    stores, _, clock = store()
    head = adopted_head(stores.semantic, clock)
    assert asyncio.run(notifications(stores.notifications, clock, Sender()).process(EVENT, "news")) == "sent"
    with closing(connect_postgres_test()) as conn, conn.transaction():
        original = conn.execute(
            "SELECT document,understanding FROM news_analyses WHERE document IS NOT NULL"
        ).fetchone()
        old_document = original["document"]
        old_document["claims"][0]["fields"]["mode"] = "conditional_threat"
        old_understanding = original["understanding"]
        old_understanding["claims"][0]["fields"]["mode"] = "commentary"
        # Fixture setup supplies genuinely retired stored values without adding a live parser alias.
        conn.execute("ALTER TABLE news_analyses DISABLE TRIGGER news_analyses_immutable")
        conn.execute("ALTER TABLE news_notifications DISABLE TRIGGER news_notifications_guard")
        conn.execute(
            "UPDATE news_analyses SET document=%s,understanding=%s WHERE document IS NOT NULL",
            (Jsonb(old_document), Jsonb(old_understanding)),
        )
        conn.execute(
            "UPDATE news_notifications SET sent_claims=%s,input_snapshot=%s WHERE state='sent'",
            (Jsonb(old_document["claims"]), Jsonb({"historical_claim": old_understanding["claims"][0]})),
        )
        conn.execute(
            "INSERT INTO news_judgment_cache(cache_key,answer,created_at_ms) VALUES ('speech-checkpoint',%s,1)",
            (Jsonb(old_understanding),),
        )
        conn.execute("UPDATE news_jobs SET detail=detail || %s", (Jsonb({"reading": {"mode": "commentary"}}),))
        conn.execute(
            "UPDATE news_trade_events SET payload=payload || %s",
            (Jsonb({"historical_reading": {"mode": "conditional_threat"}}),),
        )
        conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
        conn.execute("ALTER TABLE news_notifications ENABLE TRIGGER news_notifications_guard")
        conn.execute("ALTER TABLE news_analyses ENABLE TRIGGER news_analyses_immutable")
        identities = conn.execute(
            "SELECT analysis_id,update_ref,content_revision,input_revision,input_sha256,program_identity,read_refs "
            "FROM news_analyses ORDER BY analysis_id"
        ).fetchall()
        receipts = conn.execute(
            "SELECT notification_id,intent_id,card,receipt,settlement,claim_refs FROM news_notifications "
            "ORDER BY notification_id"
        ).fetchall()
    command.upgrade(config, "head")
    with closing(connect_postgres_test()) as conn:
        current = conn.execute("SELECT document,understanding FROM news_analyses WHERE document IS NOT NULL").fetchone()
        migrated = EventUpdate.model_validate(current["document"])
        assert migrated.ref == head.ref and migrated.content_revision == head.content_revision
        assert migrated.claims[0].ref == head.claims[0].ref
        assert migrated.evidence == head.evidence and migrated.claims[0].citations == head.claims[0].citations
        assert migrated.claims[0].fields.mode == "threat"
        assert Extraction.model_validate(current["understanding"]).claims[0].fields.mode == "unknown"
        assert (
            conn.execute(
                "SELECT analysis_id,update_ref,content_revision,input_revision,input_sha256,program_identity,read_refs "
                "FROM news_analyses ORDER BY analysis_id"
            ).fetchall()
            == identities
        )
        assert (
            conn.execute(
                "SELECT notification_id,intent_id,card,receipt,settlement,claim_refs FROM news_notifications "
                "ORDER BY notification_id"
            ).fetchall()
            == receipts
        )
        sent = conn.execute("SELECT sent_claims,input_snapshot FROM news_notifications WHERE state='sent'").fetchone()
        assert sent["sent_claims"][0]["fields"]["mode"] == "threat"
        assert sent["input_snapshot"]["historical_claim"]["fields"]["mode"] == "unknown"
        checkpoint = conn.execute(
            "SELECT answer FROM news_judgment_cache WHERE cache_key='speech-checkpoint'"
        ).fetchone()
        assert Extraction.model_validate(checkpoint["answer"]).claims[0].fields.mode == "unknown"
        assert all(r["detail"]["reading"]["mode"] == "unknown" for r in conn.execute("SELECT detail FROM news_jobs"))
        for row in conn.execute("SELECT payload,payload_sha256 FROM news_trade_events"):
            assert row["payload"]["historical_reading"]["mode"] == "threat"
            encoded = json.dumps(row["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            assert row["payload_sha256"] == hashlib.sha256(encoded).hexdigest()
        guards = conn.execute(
            "SELECT tgname,tgenabled FROM pg_trigger "
            "WHERE tgname IN ('news_analyses_immutable','news_notifications_guard')"
        ).fetchall()
        assert len(guards) == 2 and all(r["tgenabled"] == "O" for r in guards)
    with pytest.raises(RuntimeError, match="forward-only"):
        command.downgrade(config, "20261002_0426")
