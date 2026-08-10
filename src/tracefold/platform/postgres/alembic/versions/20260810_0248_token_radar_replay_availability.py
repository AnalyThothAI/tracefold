"""Record immutable database availability time for Radar replay facts.

Revision ID: 20260810_0248
Revises: 20260809_0247
"""

from __future__ import annotations

from alembic import op

revision = "20260810_0248"
down_revision = "20260809_0247"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("SET LOCAL transaction_timeout = '300s'")
    op.execute(
        r"""
        CREATE FUNCTION enforce_fact_persisted_at_ms()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          IF TG_OP = 'INSERT' THEN
            NEW.persisted_at_ms :=
              (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint;
          ELSIF NEW.persisted_at_ms IS DISTINCT FROM OLD.persisted_at_ms THEN
            RAISE EXCEPTION '% persisted_at_ms is immutable', TG_TABLE_NAME;
          END IF;
          RETURN NEW;
        END;
        $$;

        ALTER TABLE token_intents
          ADD COLUMN persisted_at_ms bigint NOT NULL
            DEFAULT ((EXTRACT(EPOCH FROM statement_timestamp()) * 1000)::bigint),
          ADD CONSTRAINT token_intents_persisted_at_ms_check
            CHECK (persisted_at_ms >= 0);

        ALTER TABLE token_intent_resolutions
          ADD COLUMN persisted_at_ms bigint NOT NULL
            DEFAULT ((EXTRACT(EPOCH FROM statement_timestamp()) * 1000)::bigint),
          ADD CONSTRAINT token_intent_resolutions_persisted_at_ms_check
            CHECK (persisted_at_ms >= 0);

        ALTER TABLE market_ticks
          ADD COLUMN persisted_at_ms bigint NOT NULL
            DEFAULT ((EXTRACT(EPOCH FROM statement_timestamp()) * 1000)::bigint),
          ADD CONSTRAINT market_ticks_persisted_at_ms_check
            CHECK (persisted_at_ms >= 0);

        ALTER TABLE registry_assets
          ADD COLUMN persisted_at_ms bigint NOT NULL
            DEFAULT ((EXTRACT(EPOCH FROM statement_timestamp()) * 1000)::bigint),
          ADD CONSTRAINT registry_assets_persisted_at_ms_check
            CHECK (persisted_at_ms >= 0);

        ALTER TABLE cex_tokens
          ADD COLUMN persisted_at_ms bigint NOT NULL
            DEFAULT ((EXTRACT(EPOCH FROM statement_timestamp()) * 1000)::bigint),
          ADD CONSTRAINT cex_tokens_persisted_at_ms_check
            CHECK (persisted_at_ms >= 0);

        ALTER TABLE price_feeds
          ADD COLUMN persisted_at_ms bigint NOT NULL
            DEFAULT ((EXTRACT(EPOCH FROM statement_timestamp()) * 1000)::bigint),
          ADD CONSTRAINT price_feeds_persisted_at_ms_check
            CHECK (persisted_at_ms >= 0);

        CREATE TRIGGER token_intents_persisted_at_immutable
          BEFORE INSERT OR UPDATE ON token_intents
          FOR EACH ROW EXECUTE FUNCTION enforce_fact_persisted_at_ms();
        CREATE TRIGGER token_intent_resolutions_persisted_at_immutable
          BEFORE INSERT OR UPDATE ON token_intent_resolutions
          FOR EACH ROW EXECUTE FUNCTION enforce_fact_persisted_at_ms();
        CREATE TRIGGER market_ticks_persisted_at_immutable
          BEFORE INSERT OR UPDATE ON market_ticks
          FOR EACH ROW EXECUTE FUNCTION enforce_fact_persisted_at_ms();
        CREATE TRIGGER registry_assets_persisted_at_immutable
          BEFORE INSERT OR UPDATE ON registry_assets
          FOR EACH ROW EXECUTE FUNCTION enforce_fact_persisted_at_ms();
        CREATE TRIGGER cex_tokens_persisted_at_immutable
          BEFORE INSERT OR UPDATE ON cex_tokens
          FOR EACH ROW EXECUTE FUNCTION enforce_fact_persisted_at_ms();
        CREATE TRIGGER price_feeds_persisted_at_immutable
          BEFORE INSERT OR UPDATE ON price_feeds
          FOR EACH ROW EXECUTE FUNCTION enforce_fact_persisted_at_ms();
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260810_0248 is irreversible Radar replay availability evidence")
