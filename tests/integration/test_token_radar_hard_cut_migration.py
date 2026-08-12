from __future__ import annotations

import hashlib
import importlib
import json
from decimal import Decimal
from typing import Any

import pytest
from alembic import command
from psycopg.errors import CheckViolation
from psycopg.types.json import Jsonb

from tests.factories import make_event
from tests.postgres_test_utils import connect_postgres_test, reset_postgres_schema
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.app.repositories import repositories_for_connection
from tracefold.market import MarketTick, event_to_row, market_tick_id
from tracefold.platform.postgres.postgres_migrations import alembic_config
from tracefold.platform.postgres.queue_terminal import terminalize_source_row

_FACT_IDENTITIES = {
    "events": "event_id",
    "token_intents": "intent_id",
    "token_intent_resolutions": "resolution_id",
    "market_ticks": "tick_id",
    "registry_assets": "asset_id",
    "cex_tokens": "cex_token_id",
    "price_feeds": "pricefeed_id",
}
_REPLAY_TABLES = (
    "token_intents",
    "token_intent_resolutions",
    "market_ticks",
    "registry_assets",
    "cex_tokens",
    "price_feeds",
)
_REPLAY_TRIGGERS = tuple(f"{table}_persisted_at_immutable" for table in _REPLAY_TABLES)
_REPLAY_CONSTRAINTS = tuple(f"{table}_persisted_at_ms_check" for table in _REPLAY_TABLES)
_LEGACY_RADAR_TABLES = (
    "token_radar_current_rows",
    "token_radar_publication_state",
    "token_radar_target_first_seen",
    "token_radar_target_features",
    "radar_source_edges",
    "radar_projection_frontiers",
)
_STOCKS_RADAR_TABLES = (
    "stock_attention_target_features",
    "stocks_radar_current_rows",
    "stocks_radar_publication_state",
)


def test_upgrade_from_0248_discards_only_legacy_radar_state() -> None:
    config, conn = _reset_to_0248()
    try:
        radar_terminal_id, radar_source_terminal_id, retained_terminal_id = _seed_nonempty_0248_state(conn)
        before = _fact_identities(conn)

        command.upgrade(config, "20260810_0249")

        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == {"version_num": "20260810_0249"}
        assert _fact_identities(conn) == before
        assert all(count == 1 for count, _ids in before.values())

        legacy_tables = conn.execute(
            """
            SELECT tablename
              FROM pg_tables
             WHERE schemaname = 'public'
               AND tablename = ANY(%s)
             ORDER BY tablename
            """,
            (list(_LEGACY_RADAR_TABLES),),
        ).fetchall()
        assert legacy_tables == []

        replay_columns = conn.execute(
            """
            SELECT table_name
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name = ANY(%s)
               AND column_name = 'persisted_at_ms'
             ORDER BY table_name
            """,
            (list(_REPLAY_TABLES),),
        ).fetchall()
        replay_triggers = conn.execute(
            """
            SELECT tgname
              FROM pg_trigger
             WHERE NOT tgisinternal
               AND tgname = ANY(%s)
             ORDER BY tgname
            """,
            (list(_REPLAY_TRIGGERS),),
        ).fetchall()
        replay_constraints = conn.execute(
            """
            SELECT conname
              FROM pg_constraint
             WHERE conname = ANY(%s)
             ORDER BY conname
            """,
            (list(_REPLAY_CONSTRAINTS),),
        ).fetchall()
        replay_function = conn.execute("SELECT to_regprocedure('enforce_fact_persisted_at_ms()') AS name").fetchone()
        assert replay_columns == []
        assert replay_triggers == []
        assert replay_constraints == []
        assert replay_function == {"name": None}

        terminals = conn.execute(
            """
            SELECT terminal_id, owner_key, source_table
              FROM queue_terminal_events
             WHERE terminal_id = ANY(%s)
             ORDER BY terminal_id
            """,
            ([radar_terminal_id, radar_source_terminal_id, retained_terminal_id],),
        ).fetchall()
        assert terminals == [
            {
                "terminal_id": retained_terminal_id,
                "owner_key": "event_anchor_backfill",
                "source_table": "event_anchor_backfill_jobs",
            }
        ]
        owner_constraint = conn.execute(
            """
            SELECT pg_get_constraintdef(oid) AS definition
              FROM pg_constraint
             WHERE conrelid = 'queue_terminal_events'::regclass
               AND conname = 'queue_terminal_events_owner_key_check'
            """
        ).fetchone()
        assert owner_constraint is not None
        assert "radar_projection" not in str(owner_constraint["definition"])

        singleton = conn.execute(
            """
            SELECT singleton_key, latest_attempt_status, served_payload
              FROM token_radar_current
            """
        ).fetchall()
        assert singleton == [
            {
                "singleton_key": True,
                "latest_attempt_status": "never",
                "served_payload": {
                    "schema_version": "token_radar_snapshot_v1",
                    "evidence_as_of_ms": 0,
                    "eligible_total": 0,
                    "items": [],
                },
            }
        ]
    finally:
        reset_postgres_schema(conn)
        conn.close()


def test_upgrade_from_0249_replaces_v1_with_top50_and_drops_stocks_only() -> None:
    config, conn = _reset_to_0248()
    try:
        _seed_nonempty_0248_state(conn)
        command.upgrade(config, "20260810_0249")
        before = _fact_identities(conn)
        fingerprint = "sha256:" + ("1" * 64)
        conn.execute(
            """
            UPDATE token_radar_current
               SET ruleset_version = 'token_radar_rules_v1',
                   ruleset_fingerprint = %s,
                   input_fingerprint = %s,
                   state_fingerprint = %s,
                   evidence_as_of_ms = 10,
                   evaluation_at_ms = 10,
                   input_rows = 1,
                   input_bytes = 1,
                   latest_attempt_status = 'ready',
                   served_payload = %s::jsonb,
                   updated_at_ms = 10
             WHERE singleton_key = true
            """,
            (
                fingerprint,
                fingerprint,
                fingerprint,
                '{"schema_version":"token_radar_snapshot_v1","evidence_as_of_ms":10,"eligible_total":1,"items":[{}]}',
            ),
        )
        conn.commit()

        command.upgrade(config, "20260810_0250")

        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == {"version_num": "20260810_0250"}
        assert _fact_identities(conn) == before
        assert (
            conn.execute(
                """
            SELECT tablename
              FROM pg_tables
             WHERE schemaname = 'public'
               AND tablename = ANY(%s)
             ORDER BY tablename
            """,
                (list(_STOCKS_RADAR_TABLES),),
            ).fetchall()
            == []
        )
        assert conn.execute(
            """
            SELECT schema_version, latest_attempt_status, served_payload
              FROM token_radar_current
             WHERE singleton_key = true
            """
        ).fetchone() == {
            "schema_version": "token_radar_snapshot_v2",
            "latest_attempt_status": "never",
            "served_payload": {
                "schema_version": "token_radar_snapshot_v2",
                "evidence_as_of_ms": 0,
                "eligible_total": 0,
                "items": [],
            },
        }
        constraint = conn.execute(
            """
            SELECT pg_get_constraintdef(oid) AS definition
              FROM pg_constraint
             WHERE conrelid = 'token_radar_current'::regclass
               AND conname = 'token_radar_current_schema_check'
            """
        ).fetchone()
        assert constraint is not None
        assert "token_radar_snapshot_v2" in constraint["definition"]
        assert "<= 50" in constraint["definition"]
    finally:
        reset_postgres_schema(conn)
        conn.close()


def test_upgrade_from_0253_hard_cuts_v2_current_to_v3_without_touching_facts() -> None:
    config, conn = _reset_to_0248()
    try:
        _seed_nonempty_0248_state(conn)
        command.upgrade(config, "20260811_0253")
        fingerprint = "sha256:" + ("2" * 64)
        conn.execute(
            """
            UPDATE token_radar_current
               SET ruleset_version = 'token_radar_rules_v1',
                   ruleset_fingerprint = %s,
                   input_fingerprint = %s,
                   state_fingerprint = %s,
                   evidence_as_of_ms = 20,
                   evaluation_at_ms = 21,
                   input_rows = 22,
                   input_bytes = 23,
                   latest_attempt_status = 'failed',
                   latest_error_code = 'old_v2_failure',
                   failure_count = 3,
                   served_payload = %s::jsonb,
                   created_at_ms = 10,
                   updated_at_ms = 21
             WHERE singleton_key = true
            """,
            (
                fingerprint,
                fingerprint,
                fingerprint,
                '{"schema_version":"token_radar_snapshot_v2","evidence_as_of_ms":20,"eligible_total":1,"items":[{}]}',
            ),
        )
        conn.commit()
        before_facts = _fact_identities(conn)
        before_tables = _public_tables(conn)
        before_columns = _table_columns(conn, "token_radar_current")

        command.upgrade(config, "20260811_0254")

        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == {"version_num": "20260811_0254"}
        assert _fact_identities(conn) == before_facts
        assert _public_tables(conn) == before_tables
        assert _table_columns(conn, "token_radar_current") == before_columns | {"state_changed_at_ms"}
        assert conn.execute(
            """
            SELECT data_type, is_nullable, column_default
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name = 'token_radar_current'
               AND column_name = 'state_changed_at_ms'
            """
        ).fetchone() == {
            "data_type": "bigint",
            "is_nullable": "NO",
            "column_default": "0",
        }
        assert conn.execute(
            """
            SELECT schema_version, ruleset_version, ruleset_fingerprint,
                   input_fingerprint, state_fingerprint, evidence_as_of_ms,
                   evaluation_at_ms, input_rows, input_bytes,
                   latest_attempt_status, latest_error_code, failure_count,
                   served_payload, state_changed_at_ms,
                   created_at_ms, updated_at_ms
              FROM token_radar_current
             WHERE singleton_key = true
            """
        ).fetchone() == {
            "schema_version": "token_radar_snapshot_v3",
            "ruleset_version": None,
            "ruleset_fingerprint": None,
            "input_fingerprint": None,
            "state_fingerprint": None,
            "evidence_as_of_ms": 0,
            "evaluation_at_ms": 0,
            "input_rows": 0,
            "input_bytes": 0,
            "latest_attempt_status": "never",
            "latest_error_code": None,
            "failure_count": 0,
            "served_payload": {
                "schema_version": "token_radar_snapshot_v3",
                "social_evidence_as_of_ms": 0,
                "eligible_total": 0,
                "items": [],
            },
            "state_changed_at_ms": 0,
            "created_at_ms": 0,
            "updated_at_ms": 0,
        }

        index = conn.execute(
            """
            SELECT pg_get_indexdef(indexrelid) AS definition,
                   pg_get_expr(indpred, indrelid) AS predicate
              FROM pg_index
             WHERE indexrelid = 'idx_events_token_radar_source_time'::regclass
            """
        ).fetchone()
        assert index is not None
        assert "USING btree (timestamp_ms, event_id)" in index["definition"]
        predicate = str(index["predicate"])
        for required in (
            "source_provider = 'gmgn'::text",
            "source_transport = 'direct_ws'::text",
            "coverage = 'public_stream'::text",
            "twitter_monitor_basic",
            "twitter_monitor_token",
            "twitter_monitor_translation",
            "twitter_monitor_express",
            "'tweet'::text",
            "'quote'::text",
            "'reply'::text",
            "'repost'::text",
        ):
            assert required in predicate

        current_constraint = conn.execute(
            """
            SELECT pg_get_constraintdef(oid) AS definition
              FROM pg_constraint
             WHERE conrelid = 'token_radar_current'::regclass
               AND conname = 'token_radar_current_schema_check'
            """
        ).fetchone()
        assert current_constraint is not None
        assert "octet_length" in str(current_constraint["definition"])
        assert "98304" not in str(current_constraint["definition"])
        assert "131072" in str(current_constraint["definition"])

        representative_items = [
            {f"field_{field_index:02d}": f"{item_index}-{field_index}" for field_index in range(24)}
            for item_index in range(50)
        ]
        representative_items[0]["padding"] = ""
        representative_payload = {
            "schema_version": "token_radar_snapshot_v3",
            "social_evidence_as_of_ms": 0,
            "eligible_total": 50,
            "items": representative_items,
        }
        canonical_size = len(
            json.dumps(
                representative_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        representative_items[0]["padding"] = "x" * (98_276 - canonical_size)
        assert (
            len(
                json.dumps(
                    representative_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            == 98_276
        )
        conn.execute(
            """
            UPDATE token_radar_current
               SET served_payload = %s
             WHERE singleton_key = true
            """,
            (Jsonb(representative_payload),),
        )
        stored_size = conn.execute(
            """
            SELECT octet_length(served_payload::text) AS bytes
              FROM token_radar_current
             WHERE singleton_key = true
            """
        ).fetchone()
        assert stored_size is not None
        assert 98_304 < int(stored_size["bytes"]) <= 131_072
        conn.rollback()

        with pytest.raises(CheckViolation):
            conn.execute(
                """
                UPDATE token_radar_current
                   SET served_payload = %s
                 WHERE singleton_key = true
                """,
                (
                    Jsonb(
                        {
                            "schema_version": "token_radar_snapshot_v3",
                            "social_evidence_as_of_ms": 0,
                            "eligible_total": 1,
                            "items": [{"padding": "x" * 132_000}],
                        }
                    ),
                ),
            )
        conn.rollback()

        with pytest.raises(CheckViolation):
            conn.execute(
                """
                UPDATE token_radar_current
                   SET served_payload =
                       '{"schema_version":"token_radar_snapshot_v2","evidence_as_of_ms":0,"eligible_total":0,"items":[]}'::jsonb
                 WHERE singleton_key = true
                """
            )
        conn.rollback()
    finally:
        reset_postgres_schema(conn)
        conn.close()


def test_0254_downgrade_is_explicitly_irreversible() -> None:
    migration = importlib.import_module(
        "tracefold.platform.postgres.alembic.versions.20260811_0254_token_radar_v3_hard_cut"
    )

    with pytest.raises(RuntimeError, match="irreversible Token Radar v3 hard cut"):
        migration.downgrade()


def test_upgrade_from_0254_hard_cuts_v3_current_to_v4_with_covering_indexes() -> None:
    config, conn = _reset_to_0248()
    try:
        _seed_nonempty_0248_state(conn)
        command.upgrade(config, "20260811_0254")
        wide_text = "".join(hashlib.sha256(f"radar-wide-{index}".encode()).hexdigest() for index in range(600))
        wide_event = make_event(
            "event-radar-wide-index-safety",
            text=wide_text,
            received_at_ms=1_778_100_000_001,
        )
        repos = repositories_for_connection(conn)
        with repos.transaction():
            repos.evidence.insert_event_row(event_to_row(wide_event, now_ms=1_778_100_000_001))
        assert (
            conn.execute(
                "SELECT pg_column_size(search_text) AS bytes FROM events WHERE event_id = %s",
                (wide_event.event_id,),
            ).fetchone()["bytes"]
            > 8_191
        )
        fingerprint = "sha256:" + ("4" * 64)
        conn.execute(
            """
            UPDATE token_radar_current
               SET ruleset_version = 'token_radar_rules_v3',
                   ruleset_fingerprint = %s,
                   input_fingerprint = %s,
                   state_fingerprint = %s,
                   evidence_as_of_ms = 30,
                   evaluation_at_ms = 31,
                   input_rows = 32,
                   input_bytes = 33,
                   latest_attempt_status = 'failed',
                   latest_error_code = 'old_v3_failure',
                   failure_count = 4,
                   served_payload = %s::jsonb,
                   state_changed_at_ms = 29,
                   created_at_ms = 10,
                   updated_at_ms = 31
             WHERE singleton_key = true
            """,
            (
                fingerprint,
                fingerprint,
                fingerprint,
                '{"schema_version":"token_radar_snapshot_v3","social_evidence_as_of_ms":30,'
                '"eligible_total":1,"items":[{}]}',
            ),
        )
        conn.commit()
        before_facts = _fact_identities(conn)
        before_tables = _public_tables(conn)
        before_columns = _table_columns(conn, "token_radar_current")

        command.upgrade(config, "20260812_0255")

        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == {"version_num": "20260812_0255"}
        assert _fact_identities(conn) == before_facts
        assert _public_tables(conn) == before_tables
        assert _table_columns(conn, "token_radar_current") == before_columns
        assert conn.execute(
            """
            SELECT schema_version, ruleset_version, ruleset_fingerprint,
                   input_fingerprint, state_fingerprint, evidence_as_of_ms,
                   evaluation_at_ms, input_rows, input_bytes,
                   latest_attempt_status, latest_error_code, failure_count,
                   served_payload, state_changed_at_ms,
                   created_at_ms, updated_at_ms
              FROM token_radar_current
             WHERE singleton_key = true
            """
        ).fetchone() == {
            "schema_version": "token_radar_snapshot_v4",
            "ruleset_version": None,
            "ruleset_fingerprint": None,
            "input_fingerprint": None,
            "state_fingerprint": None,
            "evidence_as_of_ms": 0,
            "evaluation_at_ms": 0,
            "input_rows": 0,
            "input_bytes": 0,
            "latest_attempt_status": "never",
            "latest_error_code": None,
            "failure_count": 0,
            "served_payload": {
                "schema_version": "token_radar_snapshot_v4",
                "social_evidence_as_of_ms": 0,
                "eligible_total": 0,
                "items": [],
            },
            "state_changed_at_ms": 0,
            "created_at_ms": 0,
            "updated_at_ms": 0,
        }

        event_index = conn.execute(
            """
            SELECT pg_get_indexdef(indexrelid) AS definition,
                   pg_get_expr(indpred, indrelid) AS predicate
              FROM pg_index
             WHERE indexrelid = 'idx_events_token_radar_source_time'::regclass
            """
        ).fetchone()
        assert event_index is not None
        assert "USING btree (timestamp_ms, event_id, md5" in event_index["definition"]
        assert "regexp_replace" in event_index["definition"]
        assert "translate" in event_index["definition"]
        assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" in event_index["definition"]
        assert "INCLUDE (received_at_ms, created_at_ms, action, author_handle)" in event_index["definition"]
        event_predicate = str(event_index["predicate"])
        for required in (
            "source_provider = 'gmgn'::text",
            "source_transport = 'direct_ws'::text",
            "coverage = 'public_stream'::text",
            "twitter_monitor_basic",
            "twitter_monitor_token",
            "twitter_monitor_translation",
            "twitter_monitor_express",
            "'tweet'::text",
            "'quote'::text",
            "'reply'::text",
            "'repost'::text",
        ):
            assert required in event_predicate

        resolution_index = conn.execute(
            """
            SELECT pg_get_indexdef(indexrelid) AS definition
              FROM pg_index
             WHERE indexrelid =
                   'idx_token_intent_resolutions_token_radar_material'::regclass
            """
        ).fetchone()
        assert resolution_index is not None
        assert (
            "USING btree (event_id, intent_id, decision_time_ms, created_at_ms, resolution_id)"
            in resolution_index["definition"]
        )
        assert "INCLUDE (resolution_status, target_type, target_id)" in resolution_index["definition"]

        current_constraint = conn.execute(
            """
            SELECT pg_get_constraintdef(oid) AS definition
              FROM pg_constraint
             WHERE conrelid = 'token_radar_current'::regclass
               AND conname = 'token_radar_current_schema_check'
            """
        ).fetchone()
        assert current_constraint is not None
        constraint_definition = str(current_constraint["definition"])
        assert "token_radar_snapshot_v4" in constraint_definition
        assert "token_radar_snapshot_v3" not in constraint_definition
        assert "131072" in constraint_definition

        counts_constraint = conn.execute(
            """
            SELECT pg_get_constraintdef(oid) AS definition
              FROM pg_constraint
             WHERE conrelid = 'token_radar_current'::regclass
               AND conname = 'token_radar_current_counts_check'
            """
        ).fetchone()
        assert counts_constraint is not None
        counts_definition = str(counts_constraint["definition"])
        assert "input_rows >= 0" in counts_definition
        assert "input_rows <= 20000" in counts_definition
        assert "input_bytes >= 0" in counts_definition
        assert "input_bytes <= 16777216" in counts_definition

        conn.execute(
            """
            UPDATE token_radar_current
               SET input_rows = 20000,
                   input_bytes = 16777216
             WHERE singleton_key = true
            """
        )
        conn.rollback()

        with pytest.raises(CheckViolation):
            conn.execute(
                """
                UPDATE token_radar_current
                   SET input_rows = 20001
                 WHERE singleton_key = true
                """
            )
        conn.rollback()

        with pytest.raises(CheckViolation):
            conn.execute(
                """
                UPDATE token_radar_current
                   SET input_bytes = 16777217
                 WHERE singleton_key = true
                """
            )
        conn.rollback()

        with pytest.raises(CheckViolation):
            conn.execute(
                """
                UPDATE token_radar_current
                   SET schema_version = 'token_radar_snapshot_v3',
                       served_payload =
                         '{"schema_version":"token_radar_snapshot_v3",'
                         '"social_evidence_as_of_ms":0,"eligible_total":0,"items":[]}'::jsonb
                 WHERE singleton_key = true
                """
            )
        conn.rollback()
    finally:
        reset_postgres_schema(conn)
        conn.close()


def test_0255_downgrade_is_explicitly_irreversible() -> None:
    migration = importlib.import_module(
        "tracefold.platform.postgres.alembic.versions.20260812_0255_token_radar_v4_four_hour_hard_cut"
    )

    with pytest.raises(RuntimeError, match="irreversible Token Radar v4 hard cut"):
        migration.downgrade()


def _reset_to_0248() -> tuple[Any, Any]:
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    conn = connect_postgres_test(read_only=False)
    conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.execute("GRANT ALL ON SCHEMA public TO public")
    conn.commit()
    command.upgrade(config, "20260810_0248")
    return config, conn


def _public_tables(conn: Any) -> set[str]:
    return {
        str(row["tablename"])
        for row in conn.execute(
            """
            SELECT tablename
              FROM pg_tables
             WHERE schemaname = 'public'
            """
        ).fetchall()
    }


def _table_columns(conn: Any, table_name: str) -> set[str]:
    return {
        str(row["column_name"])
        for row in conn.execute(
            """
            SELECT column_name
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name = %s
            """,
            (table_name,),
        ).fetchall()
    }


def _seed_nonempty_0248_state(conn: Any) -> tuple[str, str, str]:
    now_ms = 1_778_100_000_000
    repos = repositories_for_connection(conn)
    with repos.transaction():
        event = make_event("event-radar-hard-cut", received_at_ms=now_ms)
        repos.evidence.insert_event_row(event_to_row(event, now_ms=now_ms))
        repos.token_intents.insert(
            {
                "intent_id": "intent-radar-hard-cut",
                "event_id": event.event_id,
                "intent_key": "intent-key-radar-hard-cut",
                "construction_policy": "migration-test",
                "primary_evidence_id": None,
                "display_symbol": "RADAR",
                "display_name": None,
                "chain_hint": None,
                "address_hint": None,
                "intent_status": "ready",
                "intent_confidence": 1.0,
                "created_at_ms": now_ms,
                "updated_at_ms": now_ms,
            }
        )
        asset = repos.registry.upsert_chain_asset(
            chain_id="eip155:1",
            address="0x1111111111111111111111111111111111111111",
            observed_at_ms=now_ms,
        )
        cex_token = repos.registry.upsert_cex_token(
            base_symbol="RADAR",
            source="migration-test",
            observed_at_ms=now_ms,
        )
        price_feed = repos.registry.upsert_pricefeed(
            feed_type="cex_swap",
            provider="binance",
            subject_type="CexToken",
            subject_id=str(cex_token["cex_token_id"]),
            native_market_id="RADARUSDT",
            base_cex_token_id=str(cex_token["cex_token_id"]),
            base_symbol="RADAR",
            quote_symbol="USDT",
            observed_at_ms=now_ms,
        )
        repos.intent_resolutions.insert_resolution(
            {
                "intent_id": "intent-radar-hard-cut",
                "event_id": event.event_id,
                "resolution_status": "EXACT",
                "resolver_policy_version": "migration-test",
                "target_type": "CexToken",
                "target_id": str(cex_token["cex_token_id"]),
                "pricefeed_id": str(price_feed["pricefeed_id"]),
                "reason_codes": [],
                "candidate_ids": [],
                "lookup_keys": [],
                "decision_time_ms": now_ms,
                "created_at_ms": now_ms,
            }
        )
        target_id = "binance:RADARUSDT"
        repos.market_ticks.insert_ticks_returning_rows(
            [
                MarketTick(
                    tick_id=market_tick_id(
                        target_type="cex_symbol",
                        target_id=target_id,
                        source_provider="binance_cex_rest",
                        observed_at_ms=now_ms,
                    ),
                    target_type="cex_symbol",
                    target_id=target_id,
                    chain=None,
                    token_address=None,
                    exchange="binance",
                    instrument="RADARUSDT",
                    pricefeed_id=str(price_feed["pricefeed_id"]),
                    source_tier="tier2_poll",
                    source_provider="binance_cex_rest",
                    observed_at_ms=now_ms,
                    received_at_ms=now_ms,
                    price_usd=Decimal("1.25"),
                    liquidity_usd=None,
                    volume_24h_usd=None,
                    market_cap_usd=None,
                    holders=None,
                    created_at_ms=now_ms,
                )
            ]
        )
        assert asset["asset_id"]
        radar_terminal = terminalize_source_row(
            conn,
            owner_key="radar_projection",
            source_table="radar_projection_frontiers",
            target_key="retired-radar-target",
            source_row={"attempt_count": 3, "updated_at_ms": now_ms},
            final_status="failed",
            final_reason="retired_radar_fixture",
            now_ms=now_ms,
        )
        radar_source_terminal = terminalize_source_row(
            conn,
            owner_key="event_anchor_backfill",
            source_table="token_radar_current_rows",
            target_key="retired-radar-source-target",
            source_row={"attempt_count": 1, "updated_at_ms": now_ms},
            final_status="failed",
            final_reason="retired_radar_source_fixture",
            now_ms=now_ms,
        )
        retained_terminal = terminalize_source_row(
            conn,
            owner_key="event_anchor_backfill",
            source_table="event_anchor_backfill_jobs",
            target_key="retained-non-radar-target",
            source_row={"attempt_count": 2, "updated_at_ms": now_ms},
            final_status="failed",
            final_reason="retained_non_radar_fixture",
            now_ms=now_ms,
        )
    return (
        str(radar_terminal["terminal_id"]),
        str(radar_source_terminal["terminal_id"]),
        str(retained_terminal["terminal_id"]),
    )


def _fact_identities(conn: Any) -> dict[str, tuple[int, tuple[str, ...]]]:
    result: dict[str, tuple[int, tuple[str, ...]]] = {}
    for table, identity in _FACT_IDENTITIES.items():
        rows = conn.execute(f'SELECT "{identity}" AS identity FROM "{table}" ORDER BY "{identity}"').fetchall()
        result[table] = (len(rows), tuple(str(row["identity"]) for row in rows))
    return result
