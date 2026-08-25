"""Typed liquidation facts and versioned Trading strategy identity (#213).

Revision ID: 20260826_0310
Revises: 20260825_0309
"""

from __future__ import annotations

from alembic import op

revision = "20260826_0310"
down_revision = "20260825_0309"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE news_market_liquidations (
          source_key                TEXT    PRIMARY KEY,
          item_id                   TEXT    NOT NULL,
          fact_id                   TEXT    NOT NULL,
          symbol                    TEXT    NOT NULL,
          venue                     TEXT    NOT NULL,
          liquidated_position_side  TEXT    NOT NULL,
          forced_order_side         TEXT    NOT NULL,
          notional_usd              NUMERIC NOT NULL,
          quantity                  NUMERIC,
          price                     NUMERIC NOT NULL,
          event_at_ms               BIGINT  NOT NULL,
          received_at_ms            BIGINT  NOT NULL,
          parser_version            TEXT    NOT NULL,
          created_at_ms             BIGINT  NOT NULL,
          CONSTRAINT news_market_liquidations_item_fk
            FOREIGN KEY (item_id) REFERENCES news_items (item_id) ON DELETE CASCADE,
          CONSTRAINT news_market_liquidations_fact_unique UNIQUE (item_id, fact_id, parser_version),
          CONSTRAINT news_market_liquidations_venue_check
            CHECK (venue IN ('binance', 'hyperliquid')),
          CONSTRAINT news_market_liquidations_position_side_check
            CHECK (liquidated_position_side IN ('long', 'short')),
          CONSTRAINT news_market_liquidations_forced_side_check
            CHECK (forced_order_side IN ('buy', 'sell')),
          CONSTRAINT news_market_liquidations_side_semantics_check CHECK (
            (liquidated_position_side = 'short' AND forced_order_side = 'buy') OR
            (liquidated_position_side = 'long' AND forced_order_side = 'sell')
          ),
          CONSTRAINT news_market_liquidations_notional_positive CHECK (notional_usd > 0),
          CONSTRAINT news_market_liquidations_quantity_positive CHECK (quantity IS NULL OR quantity > 0),
          CONSTRAINT news_market_liquidations_price_positive CHECK (price > 0),
          CONSTRAINT news_market_liquidations_time_order CHECK (received_at_ms >= event_at_ms)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_news_market_liquidations_symbol_event "
        "ON news_market_liquidations (symbol, venue, event_at_ms DESC)"
    )
    op.execute("GRANT SELECT ON news_market_liquidations TO tracefold_serve")
    op.execute("GRANT SELECT, INSERT ON news_market_liquidations TO tracefold_workers")

    op.execute("ALTER TABLE trading_cases RENAME COLUMN case_kind TO trigger_kind")
    op.execute("ALTER TABLE trading_cases DROP CONSTRAINT trading_cases_kind_check")
    op.execute("ALTER TABLE trading_cases ADD COLUMN strategy_id TEXT")
    op.execute("ALTER TABLE trading_cases ADD COLUMN strategy_version TEXT")
    op.execute("ALTER TABLE trading_cases ADD COLUMN strategy_config_digest TEXT")
    op.execute(
        """
        UPDATE trading_cases
           SET strategy_id = CASE
                 WHEN trigger_kind = 'oi_only' THEN 'oi_momentum_v1'
                 ELSE 'news_oi_alignment_v1'
               END,
               strategy_version = CASE
                 WHEN trigger_kind = 'oi_only' THEN 'oi_momentum_v1'
                 ELSE 'news_oi_alignment_v1'
               END,
               strategy_config_digest = repeat('0', 64),
               trigger_kind = CASE
                 WHEN trigger_kind = 'oi_only' THEN 'oi'
                 WHEN trigger_kind = 'news_only' THEN 'news'
                 WHEN primary_source_key LIKE 'oi:%' THEN 'oi'
                 ELSE 'news'
               END
        """
    )
    op.execute("ALTER TABLE trading_cases ALTER COLUMN strategy_id SET NOT NULL")
    op.execute("ALTER TABLE trading_cases ALTER COLUMN strategy_version SET NOT NULL")
    op.execute("ALTER TABLE trading_cases ALTER COLUMN strategy_config_digest SET NOT NULL")
    op.execute(
        "ALTER TABLE trading_cases ADD CONSTRAINT trading_cases_trigger_kind_check "
        "CHECK (trigger_kind IN ('oi', 'liquidation', 'news'))"
    )
    op.execute(
        "ALTER TABLE trading_cases ADD CONSTRAINT trading_cases_strategy_digest_check "
        "CHECK (strategy_config_digest ~ '^[0-9a-f]{64}$')"
    )
    op.execute("CREATE INDEX ix_trading_cases_strategy ON trading_cases (strategy_id, created_at_ms DESC)")

    op.execute(
        """
        CREATE TABLE trading_strategy_evaluations (
          evaluation_id          TEXT    PRIMARY KEY,
          trigger_source_key     TEXT    NOT NULL,
          underlying_key         TEXT    NOT NULL,
          trigger_kind           TEXT    NOT NULL,
          strategy_id            TEXT    NOT NULL,
          strategy_version       TEXT    NOT NULL,
          strategy_config_digest TEXT    NOT NULL,
          manifest               JSONB   NOT NULL,
          manifest_sha256        TEXT    NOT NULL,
          decision               TEXT    NOT NULL,
          rule                   TEXT    NOT NULL,
          setup                  TEXT    NOT NULL,
          invalidation           TEXT    NOT NULL,
          expected_horizon       TEXT    NOT NULL,
          permission             TEXT    NOT NULL,
          cutoff_ms              BIGINT  NOT NULL,
          created_at_ms          BIGINT  NOT NULL,
          market_outcome         JSONB,
          market_outcome_version TEXT,
          completed_at_ms        BIGINT,
          CONSTRAINT trading_strategy_evaluations_identity_unique UNIQUE (
            trigger_source_key, strategy_id, strategy_version, strategy_config_digest
          ),
          CONSTRAINT trading_strategy_evaluations_source_key_check
            CHECK (trigger_source_key ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_strategy_evaluations_trigger_check CHECK (trigger_kind = 'liquidation'),
          CONSTRAINT trading_strategy_evaluations_strategy_check CHECK (
            strategy_id IN (
              'liquidation_continuation_shadow_v1',
              'liquidation_exhaustion_shadow_v1'
            )
          ),
          CONSTRAINT trading_strategy_evaluations_digest_check
            CHECK (strategy_config_digest ~ '^[0-9a-f]{64}$' AND manifest_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_strategy_evaluations_decision_check
            CHECK (decision IN ('no_trade', 'long', 'short')),
          CONSTRAINT trading_strategy_evaluations_horizon_check
            CHECK (expected_horizon IN ('minutes', 'hours', 'none')),
          CONSTRAINT trading_strategy_evaluations_permission_check CHECK (permission = 'shadow'),
          CONSTRAINT trading_strategy_evaluations_time_check CHECK (
            created_at_ms >= cutoff_ms AND (completed_at_ms IS NULL OR completed_at_ms >= created_at_ms)
          ),
          CONSTRAINT trading_strategy_evaluations_outcome_check CHECK (
            (market_outcome IS NULL AND market_outcome_version IS NULL AND completed_at_ms IS NULL) OR
            (market_outcome IS NOT NULL AND jsonb_typeof(market_outcome) = 'object'
              AND market_outcome_version IS NOT NULL AND completed_at_ms IS NOT NULL)
          )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_trading_strategy_evaluations_cohort "
        "ON trading_strategy_evaluations (strategy_id, created_at_ms DESC)"
    )
    op.execute("GRANT SELECT ON trading_strategy_evaluations TO tracefold_serve")
    op.execute("GRANT SELECT, INSERT, UPDATE ON trading_strategy_evaluations TO tracefold_workers")


def downgrade() -> None:
    raise RuntimeError("20260826_0310 is an irreversible material-fact migration; restore a backup to downgrade")
