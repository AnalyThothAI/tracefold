"""Publish what the provider proves about *how* an OI frame was measured (#265).

`news_oi_signals` held four numbers and no statement about the interval they describe. The frame's own
title carries none — `TRUMP OI Rise 4.55%, OI Value 32.17M, …` says nothing about five minutes — and
neither does any field of the provider payload, so a strategy that wants to act on "5 minute OI rise
>= 10%" had no way to prove the first half of that sentence from the ledger.

The three columns are nullable, and that is the contract rather than a convenience: `NULL` means the
measurement window could not be proven for this frame, and a consumer must refuse it rather than
assume. A default of 300000 here would have made every unprovable frame silently claim to be a 5-minute
measurement, which is precisely the failure the columns exist to prevent.

The backfill reproduces the code-owned identity table (`tracefold.news.oi_signals._SOURCE_WINDOWS`) as
of this revision, against the exact provider strategy identity stored on the leader Item. That is the
same re-parse the table's own docstring already promises — every row here is reproducible from the Item
that produced it — so it establishes no fact the running code would not have written itself. A frame
whose identity does not match the tuple keeps `NULL` and stays unproven.

Revision ID: 20260827_0313
Revises: 20260826_0312
"""

from __future__ import annotations

from alembic import op

revision = "20260827_0313"
down_revision = "20260826_0312"
branch_labels = None
depends_on = None

# The exact identity that reaches `news_items.provider_metadata.strategies[0]` for OpenNews strategy
# 1019. All four members, not the id alone: a Strategy id is an account-scoped handle, and if it is ever
# repointed at a different monitor the tuple stops matching and the window goes back to unproven.
_IDENTITY_1019 = '[{"id":"1019","name":"OI Event Monitor","source_type":"market","engine_type":"market"}]'


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE news_oi_signals
          ADD COLUMN source_strategy_id      TEXT,
          ADD COLUMN source_contract_version TEXT,
          ADD COLUMN measurement_window_ms   BIGINT,
          -- Either all three are present or none is. A window with no identity behind it is a number
          -- nobody can audit, and an identity with no window proves nothing about the measurement.
          ADD CONSTRAINT news_oi_signals_source_contract_check CHECK (
            (source_strategy_id IS NULL AND source_contract_version IS NULL AND measurement_window_ms IS NULL)
            OR (source_strategy_id IS NOT NULL AND source_contract_version IS NOT NULL
                AND measurement_window_ms IS NOT NULL AND measurement_window_ms > 0)
          )
        """
    )
    op.execute(
        f"""
        UPDATE news_oi_signals s
           SET source_strategy_id = '1019',
               source_contract_version = 'opennews_oi_source_v1',
               measurement_window_ms = 300000
          FROM news_events e
          JOIN news_items i ON i.item_id = e.leader_item_id
         WHERE e.event_id = s.event_id
           AND i.provider_metadata -> 'strategies' @> '{_IDENTITY_1019}'::jsonb
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260827_0313 is an irreversible source-contract publication")
