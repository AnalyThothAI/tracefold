"""Price Review plane (#88): latest-only Quote Snapshots + versioned Event Reactions.

Two derived read models, deliberately different shapes.

`news_quote_snapshots` is current display state: one row per provider source holding a bounded normalized
quote map. It is not history — no tick id, no partition, no raw payload, no candle rows. Sizing is why: at
the 256-instrument cap a row per instrument every five seconds is 4.4 M row updates/day, while one row per
source is at most 207 k and in practice ~52-86 k (#88 §14). The row is rewritten every five seconds, so it
carries a low fillfactor and its own autovacuum thresholds — a five-row table that accumulates 17 k dead
tuples per source per day is not "small enough to ignore".

The same revision drops the old `hl.spot` catalogue rows so the corrected adapter can rebuild that venue from
actual markets instead of the token registry.

`news_event_reactions` is the deterministic return between an Event's anchor and a fixed horizon, keyed by
`(event_id, symbol, metric_version)`. The version freezes the whole metric contract, so a later revision
publishes a new version beside v1 rather than changing what a stored row means. Rows cascade with the Event
under the existing retention; this feature adds no retention policy of its own.

Revision ID: 20260820_0283
Revises: 20260820_0282
"""

from __future__ import annotations

from alembic import op

revision = "20260820_0283"
down_revision = "20260820_0282"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE news_quote_snapshots (
          source_key      text    NOT NULL PRIMARY KEY,
          quotes          jsonb   NOT NULL DEFAULT '{}'::jsonb,
          target_count    integer NOT NULL DEFAULT 0,
          payload_sha256  text    NOT NULL DEFAULT '',
          source_at_ms    bigint,
          received_at_ms  bigint  NOT NULL,
          updated_at_ms   bigint  NOT NULL
        )
        WITH (
          fillfactor = 70,
          autovacuum_vacuum_scale_factor = 0.0,
          autovacuum_vacuum_threshold = 200,
          autovacuum_analyze_scale_factor = 0.0,
          autovacuum_analyze_threshold = 500
        )
        """
    )
    op.execute(
        """
        CREATE TABLE news_event_reactions (
          event_id           text   NOT NULL,
          symbol             text   NOT NULL,
          metric_version     text   NOT NULL,
          venue              text   NOT NULL DEFAULT '',
          venue_symbol       text   NOT NULL DEFAULT '',
          instrument_class   text   NOT NULL DEFAULT 'unknown',
          anchor_at_ms       bigint NOT NULL,
          p0                 numeric,
          p0_at_ms           bigint,
          p1                 numeric,
          p1_at_ms           bigint,
          p4                 numeric,
          p4_at_ms           bigint,
          return_1h_bps      integer,
          return_4h_bps      integer,
          is_primary         boolean NOT NULL DEFAULT false,
          state              text   NOT NULL DEFAULT 'pending',
          unavailable_reason text,
          created_at_ms      bigint NOT NULL,
          updated_at_ms      bigint NOT NULL,
          PRIMARY KEY (event_id, symbol, metric_version),
          CONSTRAINT news_event_reactions_event_fkey
            FOREIGN KEY (event_id) REFERENCES news_events(event_id) ON DELETE CASCADE,
          CONSTRAINT news_event_reactions_state_check
            CHECK (state IN ('pending', 'partial', 'complete', 'unavailable')),
          CONSTRAINT news_event_reactions_reason_check
            CHECK (
              unavailable_reason IS NULL
              OR unavailable_reason IN (
                'instrument_unresolved', 'reference_only', 'history_expired', 'no_candle_within_gap'
              )
            )
        )
        """
    )
    # The due scan reads unfinished rows oldest-first; oldest-due age is the backlog SLO, so it must not be a
    # sequential scan over a year of completed rows.
    op.execute(
        "CREATE INDEX ix_news_reactions_due ON news_event_reactions (anchor_at_ms)"
        " WHERE state IN ('pending', 'partial')"
    )
    # Review aggregates are bounded by an explicit window and always name their metric version. The
    # event-level sample is the median over the Triage *primaries*, and `is_primary` records that at
    # measurement time so the aggregate never has to expand 50k verdict JSONB arrays per request: at the
    # 720 h bound that expansion cost 1.2 s, past Serve's one-second statement timeout (#88 §14).
    op.execute(
        "CREATE INDEX ix_news_reactions_review ON news_event_reactions (metric_version, anchor_at_ms DESC)"
        " WHERE is_primary"
    )
    op.execute(
        "CREATE INDEX ix_news_reactions_state ON news_event_reactions (metric_version, anchor_at_ms DESC, state)"
    )
    # `hl.spot` rows were token names from `spotMeta.tokens` — 491 of them, of which only 326 are markets and
    # one is `USDC` itself, none of them a key any quote or candle request accepts (#88 §3). They are dropped
    # rather than left to age into `delisted`: they were never market identities, and a snapshot that reported
    # ~500 delistings would read as the mass-delisting failure the venue adapters exist to prevent. The
    # snapshot loop runs a turn at startup and repopulates the venue with `@N` / `PURR/USDC` pairs.
    op.execute("DELETE FROM news_market_instruments WHERE venue = 'hl.spot'")
    # The due planner walks Event-assets by anchor age; `ix_news_event_assets_symbol` leads with the symbol and
    # cannot serve that. Feed attachment and the per-Event detail read the primary key's leading column.
    op.execute("CREATE INDEX ix_news_event_assets_opened ON news_event_assets (opened_at_ms)")
    # Explicit grants: a migration creates tables as `tracefold_migrate`, so the owner's default privileges do
    # not apply (see 0280). Serve stays SELECT-only; only Workers writes price rows.
    op.execute("GRANT SELECT ON news_quote_snapshots, news_event_reactions TO tracefold_serve")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON news_quote_snapshots, news_event_reactions TO tracefold_workers"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_news_event_assets_opened")
    op.execute("DROP TABLE IF EXISTS news_event_reactions")
    op.execute("DROP TABLE IF EXISTS news_quote_snapshots")
