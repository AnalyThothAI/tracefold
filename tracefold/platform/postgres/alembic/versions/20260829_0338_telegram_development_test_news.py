"""Durable, expiring Telegram development-test news sources.

Revision ID: 20260829_0338
Revises: 20260829_0337
"""

from __future__ import annotations

from alembic import op

revision = "20260829_0338"
down_revision = "20260829_0337"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE trading_telegram_development_test_news (
          source_id            UUID PRIMARY KEY,
          delivery_message_id  BIGINT NOT NULL UNIQUE CHECK (delivery_message_id > 0),
          delivery_target_sha256 TEXT NOT NULL
            CHECK (delivery_target_sha256 ~ '^[0-9a-f]{64}$'),
          test_kind            TEXT NOT NULL CHECK (test_kind IN ('futures', 'onchain')),
          headline_zh          TEXT NOT NULL CHECK (length(headline_zh) BETWEEN 1 AND 240),
          direction            TEXT NOT NULL CHECK (direction IN ('bullish', 'bearish')),
          displayed_targets    JSONB NOT NULL CHECK (
            jsonb_typeof(displayed_targets) = 'array'
            AND jsonb_array_length(displayed_targets) BETWEEN 1 AND 4
          ),
          source_observed_at_ms BIGINT NOT NULL CHECK (source_observed_at_ms > 0),
          expires_at_ms         BIGINT NOT NULL CHECK (expires_at_ms > source_observed_at_ms),
          created_at_ms         BIGINT NOT NULL CHECK (created_at_ms >= source_observed_at_ms)
        )
        """
    )
    op.execute(
        "CREATE INDEX trading_telegram_development_test_news_expiry_idx "
        "ON trading_telegram_development_test_news (expires_at_ms)"
    )
    op.execute(
        "REVOKE ALL ON trading_telegram_development_test_news "
        "FROM tracefold_workers, tracefold_serve, tracefold_nautilus, tracefold_onchain"
    )
    op.execute("GRANT SELECT ON trading_telegram_development_test_news TO tracefold_workers")
    op.execute(
        "GRANT INSERT (source_id, delivery_message_id, delivery_target_sha256, test_kind, "
        "headline_zh, direction, displayed_targets, source_observed_at_ms, expires_at_ms, created_at_ms) "
        "ON trading_telegram_development_test_news TO tracefold_workers"
    )


def downgrade() -> None:
    raise RuntimeError("20260829_0338 owns Telegram development-test sources and cannot be downgraded")
