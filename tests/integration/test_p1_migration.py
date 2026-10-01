"""P0 -> P1 preserves public market projections and rejects unsafe legacy facts."""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command

from tests.news.net_buy_fixtures import movement as fill
from tests.news.net_buy_fixtures import roster, snapshot
from tests.postgres_test_utils import (
    connect_postgres_test,
    postgres_migration_test_dsn,
    prepare_test_migration_database,
)
from tests.support.news_update_pg import EVENT, seed_event
from tests.support.p1_legacy_market import LegacyMarketSeed
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.liquidations import parse_liquidation
from tracefold.news.oi_signals import measurement_definition, oi_source_contract
from tracefold.news.smart_money import parse_smart_money
from tracefold.news.storage.market import _OBSERVATIONS_SQL, _observation
from tracefold.platform.postgres.migrations import alembic_config

pytestmark = [pytest.mark.integration, pytest.mark.migration, pytest.mark.usefixtures("postgres_migration_dsn")]
SOURCE, TARGET = "20261001_0420", "20261001_0421"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
NOW = 1_789_200_000_000
WALLETS = ("0x" + "1" * 40, "0x" + "2" * 40)


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


def legacy_item(conn, item_id, kind, *, strategy="1019", parsed=True, at=NOW):
    conn.execute(
        """INSERT INTO news_items(item_id,source_id,source_item_key,title,raw_first_line,description,
               published_at_ms,observed_at_ms,provider_metadata,provider_params,first_ingest_mode,
               created_at_ms,updated_at_ms,market_kind,market_source_strategy_id,market_parse_status,
               market_parse_error,market_notify_state)
           VALUES (%s,'opennews',%s,%s,%s,'fixture',%s,%s,'{}','{"record": 1}','live',%s,%s,%s,%s,%s,%s,'pending')""",
        (
            item_id,
            item_id,
            item_id,
            item_id,
            at,
            at,
            at,
            at,
            kind,
            strategy,
            "parsed" if parsed else "raw",
            None if parsed else "unstructured",
        ),
    )


def seed_market(conn):
    seed = LegacyMarketSeed(conn)
    contract = oi_source_contract({"strategies": [{"id": "1019"}]})
    assert contract is not None
    for index in (1, 2):
        item_id = f"oi-{index}"
        legacy_item(conn, item_id, "oi", at=NOW + index)
        seed.insert_oi_signal(
            event_id=f"event-{item_id}",
            metric_version="oi_signal_v1",
            symbol="BTC",
            raw_instrument="BTC",
            direction="rise",
            oi_change_bps=455,
            oi_value_usd=32170000,
            whale_long_profit_bps=8021,
            whale_oi_ratio_bps=2279,
            observed_at_ms=NOW + index,
            received_at_ms=NOW + index,
            now_ms=NOW + index + 1,
            provider="opennews",
            source_strategy_id="1019",
            source_contract_version=contract.contract_version,
            measurement_window_ms=contract.measurement_window_ms,
            measurement_definition=measurement_definition(contract),
            source_item_id=item_id,
            source_venue="binance",
        )
    legacy_item(conn, "liquidation", "liquidation", strategy="2083", at=NOW + 3)
    liquidation = parse_liquidation(
        "SOL Large Short Liquidation 202.71K at $137.01",
        item_id="liquidation",
        fact_id="fact-liquidation",
        source_strategy_id="2083",
        provider_source="okx",
        event_at_ms=NOW + 3,
        received_at_ms=NOW + 3,
    )
    assert liquidation is not None
    seed.insert_market_liquidation(fact=liquidation, ingest_mode="live", now_ms=NOW + 4)
    legacy_item(conn, "smart-money", "smart_money", strategy="2026", at=NOW + 4)
    smart_money = parse_smart_money(
        "js-2 Close Short SOL $482,113.55 , Price $137.01 , PNL -$8,204.10",
        item_id="smart-money",
        fact_id="fact-smart-money",
        source_strategy_id="2026",
        provider_source="hyperliquid",
        related_address=WALLETS[0],
        event_at_ms=NOW + 4,
        received_at_ms=NOW + 4,
    )
    assert smart_money is not None
    seed.insert_market_smart_money(fact=smart_money, ingest_mode="live", now_ms=NOW + 5)
    legacy_item(conn, "raw", "unknown_market", parsed=False, at=NOW + 5)
    legacy_item(conn, "wallet", "wallet", strategy="net_buy", at=NOW + 6)
    evidence = snapshot(
        [fill(i) for i in range(1, 6)], members=roster(), cutoff_at_ms=NOW, coverage_from_ms=NOW - 3600000
    )
    conn.execute(
        """INSERT INTO news_market_wallet_events(item_id,chain_id,token,token_symbol,trigger_tx_hash,event_at_ms,
             received_at_ms,detected_at_ms,last_effective_buy_at_ms,initial_snapshot,latest_snapshot,latest_matched,
             change_reason,updated_at_ms,trigger_max_age_s,notification_eligible,notification_reason)
           VALUES ('wallet',4663,%s,'XYZ',%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,true,'triggered',%s,60,true,NULL)""",
        (
            fill(1).token,
            fill(5).tx_hash,
            NOW + 6,
            NOW + 6,
            NOW + 6,
            NOW + 6,
            evidence.model_dump_json(),
            evidence.model_dump_json(),
            NOW + 6,
        ),
    )
    conn.execute(
        """INSERT INTO news_market_deliveries(delivery_key,group_key,market_kind,state,trigger_reason,
             trigger_item_id,next_attempt_at_ms,created_at_ms,updated_at_ms)
           VALUES ('sending-fixture','raw|unknown_market|raw','unknown_market','sending','raw',
                   'raw',%s,%s,%s)""",
        (NOW, NOW, NOW),
    )
    conn.execute(
        "UPDATE news_items SET market_notify_state='processed',market_notify_group_key="
        "'raw|unknown_market|raw',market_notify_delivery_key='sending-fixture' WHERE item_id='raw'"
    )


def seed_collectors(conn):
    for version, members in ((1, WALLETS), (4, WALLETS[:1]), (7, WALLETS)):
        for wallet in members:
            conn.execute(
                """INSERT INTO news_market_wallet_roster(roster_version,taken_at_ms,wallet,handle,known_at_ms,
                     monitoring_from_ms) VALUES (%s,%s,%s,%s,%s,1000)""",
                (version, version * 1000, wallet, f"handle-{version}", version * 1000),
            )
    conn.execute(
        """INSERT INTO news_market_wallet_tape_state(state_id,roster_version,high_water_block,high_water_tx_index,
             scanned_block,scanned_log,scanned_at_ms,coverage_from_ms,updated_at_ms)
           VALUES ('chain_tape',7,99,2,100,4,8000,1000,9000)"""
    )
    conn.execute(
        "INSERT INTO news_ingest_state(singleton_key,connected,broker_snapshot,updated_at_ms) "
        "VALUES ('opennews',true,'{" + '"connected":true' + "}',9000) "
        "ON CONFLICT (singleton_key) DO UPDATE SET connected=EXCLUDED.connected, "
        "broker_snapshot=EXCLUDED.broker_snapshot,updated_at_ms=EXCLUDED.updated_at_ms"
    )
    conn.execute(
        """INSERT INTO news_opennews_incidents(cause_class,opened_at_ms,closed_at_ms,recovery_status,
             recovered_count,recovery_from_at_ms,recovery_to_at_ms,last_error_code,created_at_ms,updated_at_ms)
           SELECT 'network_connect',1000+g,CASE WHEN g=28 THEN NULL ELSE 2000+g END,
             CASE WHEN g>=28 THEN 'pending' ELSE 'recovered' END,g,1000,2000,'e-'||g,1000,3000
           FROM generate_series(1,29) g"""
    )
    conn.execute("SELECT setval('news_opennews_incidents_incident_id_seq',100,true)")
    conn.execute(
        "INSERT INTO news_market_instrument_snapshot_state(venue,last_snapshot_ms) "
        "VALUES ('binance.perp',9000),('okx.perp',8000)"
    )


def public_market(news):
    groups, truncated = news.market_groups(
        kinds=(), from_ms=NOW - 1, to_ms=NOW + 100, cursor_received_at_ms=1 << 62, cursor_item_id="", limit=50
    )
    assert not truncated
    return {
        "groups": groups,
        "sources": news.market_sources(from_ms=NOW - 1, to_ms=NOW + 100),
        "items": {
            item_id: news.market_item(item_id=item_id)
            for item_id in ("oi-1", "oi-2", "liquidation", "smart-money", "raw", "wallet")
        },
        "timelines": {group["group_key"]: news.market_group_timeline(group_key=group["group_key"]) for group in groups},
    }


def test_populated_upgrade_preserves_market_json_wallets_and_collectors(source):
    with closing(connect_postgres_test()) as conn:
        seed_market(conn)
        seed_collectors(conn)
        old = [_observation(row) for row in conn.execute((FIXTURES / "p1_market_0420.sql").read_text()).fetchall()]
        active = conn.execute(
            "SELECT to_jsonb(k)-'created_at_ms' AS data FROM news_opennews_incidents k "
            "WHERE closed_at_ms IS NULL OR recovery_status='pending' ORDER BY incident_id"
        ).fetchall()
        conn.commit()
    command.upgrade(source, TARGET)
    with closing(connect_postgres_test()) as conn:
        new = [_observation(row) for row in conn.execute((FIXTURES / "p1_market_0421.sql").read_text()).fetchall()]
        assert sorted(old, key=lambda row: row["item_id"]) == sorted(new, key=lambda row: row["item_id"])
        conn.commit()
    # Current runtime consumes the fully upgraded schema; verify the exact P1 projection first.
    command.upgrade(source, "head")
    with closing(connect_postgres_test()) as conn:
        assert sorted(old, key=lambda row: row["item_id"]) == sorted(
            [_observation(row) for row in conn.execute(_OBSERVATIONS_SQL).fetchall()],
            key=lambda row: row["item_id"],
        )
        news = repositories_for_connection(conn).news
        expected = json.loads((FIXTURES / "p1_market_public_0420.json").read_text())
        assert public_market(news) == expected
        assert conn.execute("SELECT count(*) AS n FROM news_items").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM news_market_observations").fetchone()["n"] == 6
        assert conn.execute("SELECT count(*) AS n FROM news_market_wallet_events").fetchone()["n"] == 1
        assert news.market_delivery(delivery_key="sending-fixture")["state"] == "sending"
        assert news.market_delivery_item_ids(delivery_key="sending-fixture") == ["raw"]
        assert conn.execute("SELECT count(*) AS n FROM news_market_wallets").fetchone()["n"] == 3
        for version, members in ((1, WALLETS), (4, WALLETS[:1]), (7, WALLETS)):
            restored = news.chain_tape_members(version)
            assert tuple(row["wallet"] for row in restored) == members
            assert all(row["known_at_ms"] == version * 1000 for row in restored)
        rows = conn.execute("SELECT collector_id,state,incidents FROM news_collectors").fetchall()
        assert len(rows) == 4
        collectors = {row["collector_id"]: row for row in rows}
        state = collectors["chain_tape"]["state"]
        assert (
            state["high_water_block"],
            state["high_water_tx_index"],
            state["scanned_block"],
            state["scanned_log"],
        ) == (
            99,
            2,
            100,
            4,
        )
        assert collectors["instrument_catalog"]["state"]["venues"] == {"binance.perp": 9000, "okx.perp": 8000}
        incidents = collectors["opennews"]["incidents"]
        assert len(incidents) == 22
        assert [item for item in incidents if item["incident_id"] >= 28] == [row["data"] for row in active]
        assert collectors["opennews"]["state"]["next_incident_id"] == 101
        assert news.open_incident(cause_class="unknown", now_ms=10000) == 101


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            "UPDATE news_items SET evidence_text='unexpected' WHERE item_id='raw'",
            "market_item_carries_editorial_evidence",
        ),
        ("UPDATE news_items SET market_kind='unknown_market' WHERE item_id='oi-1'", "oi_fact_without_oi_item"),
        ("UPDATE news_market_liquidations SET quantity=1", "liquidation_derived_columns_differ"),
        ("UPDATE news_market_liquidations SET symbol_contract_identity='wrong'", "liquidation_derived_columns_differ"),
        ("UPDATE news_market_smart_money SET provider_record_identity='wrong'", "smart_money_record_identity_differs"),
    ],
)
def test_prechecks_roll_back_unsafe_market_data(source, mutation, reason):
    with closing(connect_postgres_test()) as conn:
        seed_market(conn)
        conn.execute(mutation)
        conn.commit()
    with pytest.raises(Exception, match="p1_" + reason):
        command.upgrade(source, TARGET)
    with closing(connect_postgres_test()) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == SOURCE
        assert (
            conn.execute("SELECT to_regclass('news_market_observations') AS table_name").fetchone()["table_name"]
            is None
        )
        assert conn.execute("SELECT count(*) AS n FROM news_items").fetchone()["n"] == 6


@pytest.mark.parametrize("relation", ["member", "leader", "revision"])
def test_prechecks_reject_editorial_market_references(source, relation):
    if relation != "revision":
        seed_event()
    with closing(connect_postgres_test()) as conn:
        if relation == "revision":
            legacy_item(conn, "raw", "unknown_market", parsed=False)
            conn.execute(
                """INSERT INTO news_item_revisions (
                    item_id,revision_sha256,evidence_text,reporting_origin,source_artifact_id,
                    published_at_ms,received_at_ms,content_sha256,revision_sequence)
                    VALUES ('raw',%s,'editorial','opennews','raw',1000,1000,%s,1)""",
                ("a" * 64, "b" * 64),
            )
        else:
            conn.execute(
                """UPDATE news_items SET market_kind='unknown_market',market_source_strategy_id='1019',
                    market_parse_status='raw',market_parse_error='fixture',market_notify_state='pending'
                    WHERE item_id=%s""",
                (f"it-{EVENT}",),
            )
            if relation == "leader":
                conn.execute("DELETE FROM news_event_members WHERE event_id=%s", (EVENT,))
        conn.commit()
    reason = {"member": "is_event_member", "leader": "leads_event", "revision": "has_revisions"}[relation]
    with pytest.raises(Exception, match="p1_market_item_" + reason):
        command.upgrade(source, TARGET)
    with closing(connect_postgres_test()) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == SOURCE


@pytest.mark.parametrize("kind", ["liquidation", "smart_money"])
def test_prechecks_reject_nonconstant_parser_contracts(source, kind):
    with closing(connect_postgres_test()) as conn:
        seed_market(conn)
        legacy_item(conn, "second", kind, strategy="2083" if kind == "liquidation" else "2026")
        if kind == "liquidation":
            conn.execute(
                """INSERT INTO news_market_liquidations
                   SELECT (jsonb_populate_record(NULL::news_market_liquidations,
                       to_jsonb(l) || '{"item_id": "second", "fact_id": "second",
                                      "source_key": "second", "price_semantics": "different"}')).*
                   FROM news_market_liquidations l"""
            )
        else:
            conn.execute(
                """INSERT INTO news_market_smart_money
                   SELECT (jsonb_populate_record(NULL::news_market_smart_money,
                       to_jsonb(l) || '{"item_id": "second", "fact_id": "second",
                                      "source_key": "second", "price_semantics": "different"}')).*
                   FROM news_market_smart_money l"""
            )
        conn.commit()
    with pytest.raises(Exception, match="p1_" + kind + "_contract_not_constant_per_parser"):
        command.upgrade(source, TARGET)


def test_prechecks_reject_active_news_writer_before_ddl(source):
    with closing(connect_postgres_test()) as writer:
        writer.execute("SET application_name='tracefold_analysis'")
        writer.commit()
        with pytest.raises(Exception, match="p1_news_writers_connected"):
            command.upgrade(source, TARGET)
    with closing(connect_postgres_test()) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == SOURCE
        assert conn.execute("SELECT to_regclass('news_market_observations') AS name").fetchone()["name"] is None
