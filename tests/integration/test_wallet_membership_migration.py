"""#697 upgrades real predecessor rows without rewriting historical evidence."""

from __future__ import annotations

import asyncio
import json
from contextlib import closing

import pytest
from alembic import command
from pydantic import ValidationError

from tests.news.net_buy_fixtures import snapshot
from tests.postgres_test_utils import connect_postgres_test, postgres_migration_test_dsn
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.storage.wallet_snapshots import wallet_snapshot
from tracefold.platform.postgres.migrations import alembic_config

pytestmark = [pytest.mark.integration, pytest.mark.migration, pytest.mark.usefixtures("postgres_migration_dsn")]


def test_membership_cutover_archives_statistics_repairs_unsafe_cursor_and_preserves_sent_evidence(
    postgres_migration_dsn,
):
    config = alembic_config()
    config.attributes["database_url"] = postgres_migration_test_dsn()
    command.upgrade(config, "20260925_0398")
    evidence = snapshot().model_dump(mode="json")
    for m in evidence["window"]["members"]:
        m.update(rank_quality=1, source_closed_trades=20, source_profit_factor="1.2")
    with closing(connect_postgres_test(read_only=False)) as conn:
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO news_market_wallet_roster(roster_version,taken_at_ms,wallet,handle,
                    rank_quality,profit_factor,known_at_ms,monitoring_from_ms)
                VALUES (1,1000,%s,'alice',1,1.2,1000,1000)
            """,
                ("0x" + "1" * 40,),
            )
            conn.execute("""
                INSERT INTO news_market_wallet_tape_state(state_id,high_water_block,high_water_tx_index,
                    scanned_block,scanned_log,scanned_at_ms,roster_version,last_outcome,updated_at_ms,
                    coverage_from_ms,detection_cutover_at_ms)
                VALUES ('chain_tape',100,1,100,30,2000,1,'success',2000,1000,777)
            """)
            conn.execute("""
                INSERT INTO news_items(item_id,source_id,source_item_key,title,published_at_ms,
                    observed_at_ms,first_ingest_mode,created_at_ms,updated_at_ms,market_kind,market_notify_state)
                VALUES ('old-wallet','news-robinhood-chain','old-wallet','old',1000,1000,
                        'live',1000,1000,'wallet','processed')
            """)
            conn.execute(
                """
                INSERT INTO news_market_wallet_events(item_id,chain_id,token,trigger_tx_hash,
                    event_at_ms,received_at_ms,detected_at_ms,last_effective_buy_at_ms,
                    initial_snapshot,latest_snapshot,send_snapshot,latest_matched,change_reason,updated_at_ms,
                    trigger_max_age_s,notification_eligible)
                VALUES ('old-wallet',4663,%s,%s,1000,1000,1000,1000,
                        %s::jsonb,%s::jsonb,%s::jsonb,true,'triggered',1000,60,true)
            """,
                (evidence["token"], "0x" + "a" * 64, *([json.dumps(evidence)] * 3)),
            )
        original = conn.execute("SELECT initial_snapshot,send_snapshot FROM news_market_wallet_events").fetchone()
        command.upgrade(config, "head")
        assert (
            conn.execute("SELECT initial_snapshot,send_snapshot FROM news_market_wallet_events").fetchone() == original
        )
        row = conn.execute("SELECT * FROM news_market_wallet_tape_state").fetchone()
        assert (row["high_water_block"], row["high_water_tx_index"]) == (99, 2147483647)
        assert row["scanned_block"] is row["scanned_log"] is row["scanned_at_ms"] is None
        assert row["pre_0399_cursor"]["scanned_log"] == 30 and row["detection_cutover_at_ms"] == 777
        roster = conn.execute("SELECT * FROM news_market_wallet_roster").fetchone()
        assert roster["archived_source_statistics"]["profit_factor"] == 1.2
        assert roster["monitoring_from_ms"] == 1000 and "rank_quality" not in roster
        projected = repositories_for_connection(conn).news.wallet_event("old-wallet")
        assert "rank_quality" not in projected["initial_snapshot"]["window"]["members"][0]
        assert original["initial_snapshot"]["window"]["members"][0]["rank_quality"] == 1
        # Recovery re-reads from the conservative old block rather than jumping to head.
        from tests.integration.test_news_chain_tape import _Chain, _loop

        chain = _Chain([], head=150)
        asyncio.run(_loop(conn, chain).advance())
        state = repositories_for_connection(conn).news.chain_tape_state()
        assert state["scanned_block"] == state["high_water_block"] == 120
        assert state["scanned_log"] == 2147483647


def test_historical_projection_removes_only_known_retired_fields():
    evidence = snapshot().model_dump(mode="json")
    evidence["window"]["members"][0]["rank_quality"] = 4
    projected = wallet_snapshot(evidence)
    assert "rank_quality" not in projected["window"]["members"][0]
    assert evidence["window"]["members"][0]["rank_quality"] == 4
    evidence["window"]["members"][0]["unrecognized_future_field"] = 1
    with pytest.raises(ValidationError):
        wallet_snapshot(evidence)
