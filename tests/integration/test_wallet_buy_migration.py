"""Literal predecessor evidence survives both wallet hard cuts; no legacy runtime model."""

from contextlib import closing
from decimal import Decimal

import pytest
from alembic import command

from tests.postgres_test_utils import connect_postgres_test, postgres_migration_test_dsn
from tracefold.app.repository_session import repositories_for_connection
from tracefold.platform.postgres.migrations import alembic_config

pytestmark = [pytest.mark.integration, pytest.mark.migration, pytest.mark.usefixtures("postgres_migration_dsn")]
STAMP = 1789000000000
ITEM = "a" * 64


def test_legacy_receipts_keep_trigger_identity_and_archive_losslessly(postgres_migration_dsn):
    config = alembic_config()
    config.attributes["database_url"] = postgres_migration_test_dsn()
    command.upgrade(config, "20260907_0374")
    with closing(connect_postgres_test(read_only=False)) as conn:
        repos = repositories_for_connection(conn)
        with repos.transaction():
            conn.execute(
                """
                INSERT INTO news_items(item_id,source_id,source_item_key,title,published_at_ms,
                    observed_at_ms,first_ingest_mode,created_at_ms,updated_at_ms,market_kind,market_notify_state)
                VALUES (%s,'news-robinhood-chain','legacy','legacy wallet',%s,%s,'live',%s,%s,'wallet','pending')
            """,
                (ITEM, STAMP, STAMP, STAMP, STAMP),
            )
            conn.execute(
                """
                INSERT INTO news_market_wallet_events(item_id,kind,chain_id,wallet,token,roster_version,
                    window_from_ms,window_to_ms,segment_key,event_at_ms,received_at_ms,created_at_ms,
                    evidence,peer_wallets,peer_usd)
                VALUES (%s,'crowding',4663,%s,%s,1,%s,%s,'legacy',%s,%s,%s,
                    '{"members":[{"wallet":"original","usd":"600.1234567890"}]}',3,1200)
            """,
                (ITEM, "0x" + "1" * 40, "0x" + "2" * 40, STAMP, STAMP, STAMP, STAMP, STAMP),
            )
            conn.execute(
                """
                INSERT INTO news_market_wallet_checks(chain_id,tx_hash,log_index,basis,q_sell_raw,checked_at_ms,error)
                VALUES(4663,%s,1,'chain_balance',123456789012345678901234567890,%s,'unavailable')
            """,
                ("0x" + "3" * 64, STAMP),
            )
            for key, state in [
                ("sent", "sent"),
                ("unknown", "unknown"),
                ("attempted", None),
                ("retry", "pending"),
                ("held", "unavailable"),
                ("pending", None),
            ]:
                assert repos.news.market_open_delivery(
                    delivery_key=key,
                    group_key="wallet|legacy|" + key,
                    market_kind="wallet",
                    trigger_reason="first",
                    trigger_item_id=ITEM,
                    due_at_ms=STAMP,
                    now_ms=STAMP,
                )
                if key != "pending":
                    assert repos.news.market_begin_send(
                        delivery_key=key,
                        card={"title": "frozen", "number": "0.000000000000000001"},
                        covered_count=1,
                        covered_from_ms=STAMP,
                        covered_to_ms=STAMP,
                        attempts=0,
                        due_at_ms=STAMP,
                        now_ms=STAMP,
                    )
                    if state is not None:
                        assert repos.news.market_settle_delivery(
                            delivery_key=key,
                            state=state,
                            receipt={"message_id": key} if state == "sent" else None,
                            error=None,
                            next_attempt_at_ms=None,
                            now_ms=STAMP,
                        )
            conn.execute(
                """
                INSERT INTO news_market_wallet_outcomes(delivery_key,horizon,price,at_ms,source)
                VALUES('sent','1h',1.2,%s,'recorded'),('sent','4h',NULL,%s,'unavailable')
            """,
                (STAMP + 3600000, STAMP + 14400000),
            )
    command.upgrade(config, "20260908_0375")
    with closing(connect_postgres_test(read_only=False)) as conn:
        rows = conn.execute("SELECT * FROM news_market_wallet_outcomes ORDER BY horizon").fetchall()
        assert [r["price"] for r in rows] == [Decimal("1.2"), None]
        assert all(
            r["item_id"] == ITEM and r["reference_price"] is None and r["reference_kind"] == "legacy_delivery"
            for r in rows
        )
        archive_before = {}
        for table in ("events", "outcomes", "checks", "tape_state"):
            archive_before[table] = conn.execute(
                "SELECT to_jsonb(t) AS data FROM news_market_wallet_" + table + " t"
            ).fetchall()
        deliveries_before = conn.execute(
            "SELECT to_jsonb(d) AS data FROM news_market_deliveries d "
            "WHERE delivery_key <> 'pending' ORDER BY delivery_key"
        ).fetchall()
    command.upgrade(config, "20260912_0376")
    with closing(connect_postgres_test(read_only=False)) as conn:
        for table, original in archive_before.items():
            archived = conn.execute(
                "SELECT payload AS data FROM news_market_wallet_archive WHERE record_type=%s", (table,)
            ).fetchall()
            assert sorted(original, key=str) == sorted(archived, key=str)
        assert (
            conn.execute(
                "SELECT to_jsonb(d) AS data FROM news_market_deliveries d "
                "WHERE delivery_key <> 'pending' ORDER BY delivery_key"
            ).fetchall()
            == deliveries_before
        )
        pending = conn.execute(
            "SELECT state,error,attempts FROM news_market_deliveries WHERE delivery_key='pending'"
        ).fetchone()
        assert pending == {"state": "failed", "error": "wallet_net_buy_cutover", "attempts": 0}
        assert conn.execute("SELECT count(*) AS n FROM news_market_wallet_events").fetchone()["n"] == 0
        assert (
            repositories_for_connection(conn).news.market_due_delivery(
                now_ms=STAMP + 999999, wallet_notifications_enabled=True
            )
            is None
        )

        news = repositories_for_connection(conn).news
        news.market_stop_wallet_deliveries(reason="wallet_notifications_disabled", now_ms=STAMP + 1, limit=20)
        news.market_hold_unavailable(reason="disabled", now_ms=STAMP + 1)
        news.market_release_unavailable(now_ms=STAMP + 2)
        news.market_sweep_interrupted_sends(reason="interrupted", now_ms=STAMP + 3)
        assert (
            conn.execute(
                "SELECT to_jsonb(d) AS data FROM news_market_deliveries d "
                "WHERE delivery_key <> 'pending' ORDER BY delivery_key"
            ).fetchall()
            == deliveries_before
        )
