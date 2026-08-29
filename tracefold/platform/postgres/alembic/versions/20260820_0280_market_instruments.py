"""Tradeable instrument universe (#75): news_market_instruments + news_symbol_aliases.

`news_market_instruments` is a provider fact table: one row per contract per venue, rebuildable from the venues' public
catalogues. `first_seen_ms` is the listing time, derived by diffing consecutive snapshots — which is how News learns
about an exchange listing without depending on a news frame arriving (#72 showed that lane can fail silently).
`news_symbol_aliases` collapses the several names one issuer trades under (SKHY/SKHX/SKHYNIX) so the storyline throttle
buckets by issuer rather than by contract.

The `news_market_instruments` name was freed by #68, which dropped Macro's unrelated table of the same name.

Revision ID: 20260820_0280
Revises: 20260820_0279
"""

from __future__ import annotations

from alembic import op

revision = "20260820_0280"
down_revision = "20260820_0279"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE news_market_instruments (
          venue             text   NOT NULL,
          venue_symbol      text   NOT NULL,
          base_symbol       text   NOT NULL,
          instrument_class  text   NOT NULL DEFAULT 'unknown',
          quote_asset       text,
          status            text   NOT NULL DEFAULT 'trading',
          first_seen_ms     bigint NOT NULL,
          last_seen_ms      bigint NOT NULL,
          PRIMARY KEY (venue, venue_symbol),
          CONSTRAINT news_market_instruments_status_check CHECK (status IN ('trading', 'delisted')),
          CONSTRAINT news_market_instruments_class_check CHECK (
            instrument_class IN ('crypto', 'equity', 'commodity', 'index', 'fx', 'pre_ipo', 'unknown')
          )
        )
        """
    )
    op.execute("CREATE INDEX ix_news_instruments_base ON news_market_instruments (base_symbol, status)")
    op.execute(
        "CREATE INDEX ix_news_instruments_listed ON news_market_instruments (first_seen_ms DESC)"
        " WHERE status = 'trading'"
    )
    op.execute(
        """
        CREATE TABLE news_symbol_aliases (
          alias        text NOT NULL PRIMARY KEY,
          base_symbol  text NOT NULL,
          source       text NOT NULL,
          updated_at_ms bigint NOT NULL,
          CONSTRAINT news_symbol_aliases_source_check CHECK (source IN ('venue', 'opennews_prefix', 'operator'))
        )
        """
    )
    op.execute("CREATE INDEX ix_news_aliases_base ON news_symbol_aliases (base_symbol)")
    # Explicit grants are required: `runtime_roles.sql` sets ALTER DEFAULT PRIVILEGES FOR ROLE tracefold_owner,
    # but a migration creates tables as `tracefold_migrate`, so those defaults do not apply. Verified by
    # `test_both_runtime_roles_have_the_expected_privileges`, which failed without these two lines.
    op.execute("GRANT SELECT ON news_market_instruments, news_symbol_aliases TO tracefold_serve")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON news_market_instruments, news_symbol_aliases TO tracefold_workers"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS news_symbol_aliases")
    op.execute("DROP TABLE IF EXISTS news_market_instruments")
