"""Bind Telegram fanout fixtures and executor liveness to independent users.

Revision ID: 20260829_0339
Revises: 20260829_0338
"""

from __future__ import annotations

from alembic import op

revision = "20260829_0339"
down_revision = "20260829_0338"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE trading_account_bindings DROP CONSTRAINT trading_account_bindings_account_lane_venue_key")
    op.execute(
        "CREATE UNIQUE INDEX trading_account_bindings_auto_lane_venue_key "
        "ON trading_account_bindings (account_lane, venue) WHERE account_lane = 'auto'"
    )
    op.execute(
        "ALTER TABLE trading_telegram_development_test_news "
        "DROP CONSTRAINT trading_telegram_development_test_news_delivery_message_id_key"
    )
    op.execute(
        "ALTER TABLE trading_telegram_development_test_news "
        "ADD CONSTRAINT trading_telegram_development_test_news_target_message_key "
        "UNIQUE (delivery_target_sha256, delivery_message_id)"
    )
    op.execute("DROP TABLE trading_onchain_executor_runtime")
    op.execute(
        """
        CREATE TABLE trading_onchain_executor_runtime (
          wallet_fingerprint TEXT PRIMARY KEY CHECK (wallet_fingerprint ~ '^[0-9a-f]{64}$'),
          started_at_ms      BIGINT NOT NULL CHECK (started_at_ms > 0),
          heartbeat_at_ms    BIGINT NOT NULL CHECK (heartbeat_at_ms >= started_at_ms)
        )
        """
    )
    op.execute(
        "REVOKE ALL ON trading_onchain_executor_runtime "
        "FROM tracefold_workers, tracefold_serve, tracefold_nautilus, tracefold_onchain"
    )
    op.execute(
        "GRANT SELECT ON trading_onchain_executor_runtime TO tracefold_workers, tracefold_serve, tracefold_onchain"
    )
    op.execute(
        "GRANT INSERT (wallet_fingerprint, started_at_ms, heartbeat_at_ms), "
        "UPDATE (started_at_ms, heartbeat_at_ms) "
        "ON trading_onchain_executor_runtime TO tracefold_onchain"
    )


def downgrade() -> None:
    raise RuntimeError("20260829_0339 owns Telegram trading profiles and cannot be downgraded")
