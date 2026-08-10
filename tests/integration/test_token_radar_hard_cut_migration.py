from __future__ import annotations

from decimal import Decimal
from typing import Any

from alembic import command

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


def test_upgrade_from_0248_discards_only_legacy_radar_state() -> None:
    config, conn = _reset_to_0248()
    try:
        radar_terminal_id, radar_source_terminal_id, retained_terminal_id = _seed_nonempty_0248_state(conn)
        before = _fact_identities(conn)

        command.upgrade(config, "head")

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
