"""Preserve real predecessor wallet receipts through the #614 identity hard cut."""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from decimal import Decimal

import pytest
from alembic import command

from tests.integration.test_news_chain_tape_digest import START, _event, _fill
from tests.postgres_test_utils import connect_postgres_test, postgres_migration_test_dsn
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.pipeline.admission import admit_market_item, prepare_wallet_observation, wallet_item_id
from tracefold.platform.postgres.migrations import alembic_config

pytestmark = [pytest.mark.integration, pytest.mark.migration, pytest.mark.usefixtures("postgres_migration_dsn")]


def test_legacy_receipts_keep_trigger_identity_and_unknown_reference(postgres_migration_dsn: str) -> None:
    config = alembic_config()
    config.attributes["database_url"] = postgres_migration_test_dsn()
    command.upgrade(config, "20260907_0374")
    stamp = START + 1000
    event = replace(
        _event("0x" + "11" * 20),
        kind="exit",
        ratio_bps=10_000,
        basis="chain_balance",
        quantity_raw=100,
        evidence={},
        title="legacy exit",
    )
    event = replace(event, item_id=wallet_item_id(event))
    fill = _fill(1, wallet=event.wallet)
    with closing(connect_postgres_test(read_only=False)) as conn:
        repos = repositories_for_connection(conn)
        with repos.transaction():
            repos.news.chain_tape_record_fills([fill])
            admit_market_item(
                repos, prepare_wallet_observation(event), ingest_mode="live", trace_id="legacy", now_ms=stamp
            )
            assert repos.news.market_open_delivery(
                delivery_key="legacy-wallet-delivery",
                group_key="wallet|exit|legacy",
                market_kind="wallet",
                trigger_reason="first",
                trigger_item_id=event.item_id,
                due_at_ms=stamp,
                now_ms=stamp,
            )
            assert repos.news.market_begin_send(
                delivery_key="legacy-wallet-delivery",
                card={"title": "old wallet card"},
                covered_count=1,
                covered_from_ms=stamp,
                covered_to_ms=stamp,
                attempts=0,
                due_at_ms=stamp,
                now_ms=stamp,
            )
            assert repos.news.market_settle_delivery(
                delivery_key="legacy-wallet-delivery",
                state="sent",
                receipt={"message_id": "old-message"},
                error=None,
                next_attempt_at_ms=None,
                now_ms=stamp,
            )
            conn.execute(
                """INSERT INTO news_market_wallet_outcomes (delivery_key,horizon,price,at_ms,source)
                   VALUES ('legacy-wallet-delivery','1h',1.2,%s,'dexscreener'),
                          ('legacy-wallet-delivery','4h',NULL,%s,'unavailable')""",
                (stamp + 3_600_000, stamp + 14_400_000),
            )

    command.upgrade(config, "20260908_0375")

    with closing(connect_postgres_test(read_only=False)) as conn:
        rows = conn.execute("SELECT * FROM news_market_wallet_outcomes ORDER BY horizon").fetchall()
        assert len(rows) == 2
        assert [row["price"] for row in rows] == [Decimal("1.2"), None]
        for row, horizon_ms in zip(rows, (3_600_000, 14_400_000), strict=True):
            assert row["item_id"] == event.item_id
            assert row["delivery_key"] == "legacy-wallet-delivery"
            assert row["reference_price"] is None
            assert row["reference_kind"] == "legacy_delivery"
            assert row["reference_at_ms"] == stamp
            assert row["target_at_ms"] == row["at_ms"] == stamp + horizon_ms
        old_fill = conn.execute("SELECT derived_at_ms, classified_at_ms FROM news_market_wallet_fills").fetchone()
        assert old_fill["derived_at_ms"] == old_fill["classified_at_ms"] == stamp
        cards = repositories_for_connection(conn).news.chain_tape_cards(from_ms=START, to_ms=stamp + 1, limit=10)
        assert len(cards) == 1
        assert cards[0]["outcomes"][1]["return_bps"] is None
