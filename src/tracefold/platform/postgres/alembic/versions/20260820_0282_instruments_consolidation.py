"""Instrument universe consolidation (#89): drop the listing-diff column, make seed aliases code-owned.

`first_seen_ms` existed to date an exchange listing derived by diffing two snapshots. That lane is gone: OpenNews
pushes listing/delisting frames and the pipeline admits them (`listing_deterministic`, #72), and the snapshot diff
could only ever see the two venues we poll — 41% of the frames observed in a week. With no reader, the column and
its index are dead weight.

`news_symbol_aliases.source` gains `seed` and loses `operator`. Every existing `operator` row was in fact written by
`ALIAS_SEEDS` in the source tree, and the reconcile loop now deletes seed rows the code has dropped — deleting rows
labelled `operator` would be a trap the day a human write path exists.

**Order matters and is the whole reason these statements are a list.** The CHECK constraint has to go *before* the
rows are rewritten: the deployed constraint allows only `venue | opennews_prefix | operator`, so an `UPDATE ... SET
source = 'seed'` ahead of it fails on any database that already holds alias rows. The first deploy of this
migration did exactly that, and a from-scratch test database never caught it because its table was empty.

Revision ID: 20260820_0282
Revises: 20260820_0281
"""

from __future__ import annotations

from typing import Final

from alembic import op

revision = "20260820_0282"
down_revision = "20260820_0281"
branch_labels = None
depends_on = None

# Exposed so a test can replay them against a database that carries the pre-0282 state; see
# `test_0282_rewrites_alias_rows_that_already_exist`.
UPGRADE_SQL: Final[tuple[str, ...]] = (
    "DROP INDEX IF EXISTS ix_news_instruments_listed",
    "ALTER TABLE news_market_instruments DROP COLUMN IF EXISTS first_seen_ms",
    "ALTER TABLE news_symbol_aliases DROP CONSTRAINT IF EXISTS news_symbol_aliases_source_check",
    "UPDATE news_symbol_aliases SET source = 'seed' WHERE source = 'operator'",
    "ALTER TABLE news_symbol_aliases ADD CONSTRAINT news_symbol_aliases_source_check"
    " CHECK (source IN ('venue', 'opennews_prefix', 'seed'))",
)

DOWNGRADE_SQL: Final[tuple[str, ...]] = (
    "ALTER TABLE news_symbol_aliases DROP CONSTRAINT IF EXISTS news_symbol_aliases_source_check",
    "UPDATE news_symbol_aliases SET source = 'operator' WHERE source = 'seed'",
    "ALTER TABLE news_symbol_aliases ADD CONSTRAINT news_symbol_aliases_source_check"
    " CHECK (source IN ('venue', 'opennews_prefix', 'operator'))",
    "ALTER TABLE news_market_instruments ADD COLUMN IF NOT EXISTS first_seen_ms bigint",
    "UPDATE news_market_instruments SET first_seen_ms = last_seen_ms WHERE first_seen_ms IS NULL",
    "ALTER TABLE news_market_instruments ALTER COLUMN first_seen_ms SET NOT NULL",
    "CREATE INDEX ix_news_instruments_listed ON news_market_instruments (first_seen_ms DESC) WHERE status = 'trading'",
)


def upgrade() -> None:
    for statement in UPGRADE_SQL:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE_SQL:
        op.execute(statement)
