"""Market observations become first-class stored facts, and the OI ledger stops depending on an Event (#553 PR-1).

Migration evidence:

- category: additive columns and one new table, two constraint deletions, one column rename, three new
  unique/foreign-key constraints, and two bounded backfills over the retained News corpus
- why_database_must_change: four provider Strategies publish market observations and the schema could
  only hold two of them. `news_oi_signals` was reachable only through `news_events`, so a recovery
  frame -- which never reaches Triage -- produced no row at all, and a frame the title deduper merged
  into another Event produced no row either. `news_market_liquidations` refused any venue outside
  `binance`/`hyperliquid`, which discarded 13 of the 143 real liquidation reports in the retained
  window, and it had no foreign key at all, so a purged Item left its liquidation behind as an
  unreachable orphan. Strategy 2026 (smart money) had nowhere to be stored. And the frames' own
  business payload -- `relatedAddress`, `strategy.metrics` -- was dropped by the metadata whitelist
  before persistence, so no consumer could read it back at any precision.

  The read model these columns serve is a *market* one: which provider, which venue, which native
  instrument, which measurement definition, which account, which direction. Every one of those is a
  column here because a reader collapses consecutive observations of one group, and a group key that
  is not a column is a group key that is recomputed differently by every caller.
- current_source_revision: 20260905_0364
- minimum_supported_source_revision: 20260905_0364
- lock_level_and_order: `ACCESS EXCLUSIVE` on `news_items`, then `news_oi_signals`, then
  `news_market_liquidations`, then the new `news_market_smart_money`, in that order. Each is held for
  a catalog update plus, on `news_items` and `news_oi_signals`, one table scan for the backfill and
  the unique index build. No other table is written; `news_events` and `news_event_members` are read
  by the rebuild
- statement_timeout: 120s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: production holds 2 741 `news_items`, 364 `news_oi_signals` and 0
  `news_market_liquidations` at the audit SHA. The `news_items` backfill touches the market subset
  (about 640 rows in the retained window); the OI rebuild reads `news_event_members` joined to
  `news_events` filtered to `event_kind = 'oi'` and inserts only the members that have no ledger row,
  which the audit measured at 18 recovery frames plus the merged members
- estimated_bytes: five new `news_items` columns (four nullable `text`, one `jsonb` defaulting to
  `'{}'`), six new `news_oi_signals` columns, five new `news_market_liquidations` columns, one new
  table, and three new indexes. Single-digit megabytes at the measured row counts
- rewrite_or_index_build: `ADD COLUMN ... DEFAULT` with a non-volatile default does not rewrite the
  heap on PostgreSQL 11+. Three index builds (`news_items` market lookup, the `news_oi_signals`
  observation key, `news_market_liquidations` item key) are ordinary in-transaction builds at these
  row counts. `ALTER COLUMN ... DROP NOT NULL` and `RENAME COLUMN` are catalog-only
- preflight_and_maintenance_boundary: writers must be stopped. `news_oi_signals` gains a unique key
  the old writer does not know and `news_market_liquidations` renames a column the old writer names,
  so an old process writing against the new schema fails. `make up` stops Workers, which is the
  boundary this revision needs
- archive_current_compatibility: every existing row keeps every value it had. The OI ledger's
  `event_id` keeps its exact string and is now an opaque source identifier rather than a foreign key,
  so a frozen Trading Case still resolves its source. Rows this revision reconstructs are flagged
  `historical = true`: their `observed_at_ms` and `received_at_ms` are the original provider and host
  stamps, and their `available_at_ms` is the rebuild moment, because that is genuinely the first
  instant any consumer could read them. `news_verdicts_current_judgment_check` is deliberately left
  intact: the market judgment branches it validates describe verdicts already written, and no new
  verdict of those origins is produced after this revision
- role_and_grant_impact: none; the single `tracefold` login is unchanged
- failure_state: the transaction rolls back completely and every table keeps its current shape
- roll_forward_or_verified_backup_restore: `downgrade` is refused. Dropping `news_market_smart_money`
  and the five `news_items` columns would delete provider payloads and typed facts that exist nowhere
  else, and `learning_epoch` cannot be reconstructed for a row written after this revision. Roll
  forward with a new revision
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260905_0365
Revises: 20260905_0364
Create Date: 2026-09-05 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260905_0365"
down_revision = "20260905_0364"
branch_labels = None
depends_on = None

# The exact 1019 wire template and arithmetic, frozen into this revision. Spelled here rather than
# imported from `tracefold.news.oi_signals` on purpose: a rebuild is a statement about what the
# provider sent in 2026, and a future parser revision must not silently change what this migration
# reconstructed. That freedom is also the risk, so the two are held together by a test rather than by
# a shared import -- `test_the_rebuild_reproduces_the_parsers_own_arithmetic` drives the same frames
# through `parse_oi_signal` and through this statement and compares every field.
#
# The three places the arithmetic has to agree exactly, each of which was wrong once:
#   * basis points are half-up on the decimal digits, which `round(x * 100)` is for `numeric`;
#   * the OI value truncates the fraction at six digits *before* applying the unit, so
#     `3.8600005M` is 3_860_000 and not 3_860_001 -- `round(x, 6)` would have rounded it up;
#   * the venue is lowercased, trimmed and capped at 32 characters, as `parse_liquidation` caps it.
_OI_TEMPLATE = (
    r"^\s*(\S{1,16})\s+OI\s+(Rise|Fall|Drop)\s+(-?\d+(?:\.\d+)?)\s*%,\s*"
    r"OI\s+Value\s+(\d+(?:\.\d+)?)([KMB]?),\s*"
    r"Whale\s+Long\s+Profit\s+(-?\d+(?:\.\d+)?)\s*%,\s*"
    r"Whale/OI\s+Ratio\s+(-?\d+(?:\.\d+)?)\s*%\s*$"
)
_MARKET_STRATEGY_KINDS = "('1019', 'oi'), ('2000', 'liquidation'), ('2083', 'liquidation'), ('2026', 'smart_money')"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")

    _news_items_market_columns()
    _oi_ledger_without_an_event()
    _liquidations_without_a_venue_allowlist()
    _smart_money_table()
    _backfill_market_items()
    _rebuild_missing_oi_facts()
    _stamp_market_parse_status()


def _news_items_market_columns() -> None:
    op.execute(
        """
        ALTER TABLE public.news_items
          ADD COLUMN market_kind text,
          ADD COLUMN market_source_strategy_id text,
          ADD COLUMN market_parse_status text,
          ADD COLUMN market_parse_error text,
          ADD COLUMN provider_params jsonb NOT NULL DEFAULT '{}'::jsonb
        """
    )
    # `market_kind IS NULL` is exactly "this Item is ordinary news", and the other three columns are
    # meaningless without it. The pair is one fact, so one CHECK states it.
    op.execute(
        """
        ALTER TABLE public.news_items
          ADD CONSTRAINT news_items_market_kind_check
            CHECK (market_kind IS NULL OR market_kind = ANY (
              ARRAY['oi'::text, 'liquidation'::text, 'smart_money'::text, 'unknown_market'::text])),
          ADD CONSTRAINT news_items_market_parse_status_check
            CHECK (
              (market_kind IS NULL
                 AND market_parse_status IS NULL
                 AND market_source_strategy_id IS NULL
                 AND market_parse_error IS NULL)
              OR (market_kind IS NOT NULL
                 AND market_parse_status = ANY (ARRAY['parsed'::text, 'raw'::text])
                 AND (market_parse_status = 'parsed') = (market_parse_error IS NULL)))
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_items_market_observed
            ON public.news_items (market_kind, observed_at_ms DESC, item_id DESC)
         WHERE market_kind IS NOT NULL
        """
    )


def _oi_ledger_without_an_event() -> None:
    op.execute("ALTER TABLE public.news_oi_signals DROP CONSTRAINT news_oi_signals_event_id_fkey")
    op.execute("ALTER TABLE public.news_oi_signals DROP CONSTRAINT news_oi_signals_learning_epoch_nonempty")
    op.execute("ALTER TABLE public.news_oi_signals DROP COLUMN learning_epoch")
    op.execute(
        """
        ALTER TABLE public.news_oi_signals
          ADD COLUMN raw_instrument text NOT NULL DEFAULT '',
          ADD COLUMN provider text NOT NULL DEFAULT 'opennews',
          ADD COLUMN received_at_ms bigint,
          ADD COLUMN measurement_definition text NOT NULL DEFAULT '',
          ADD COLUMN historical boolean NOT NULL DEFAULT false
        """
    )
    # Existing rows: the provider token is the normalized symbol (the only spelling the old parser
    # kept), the host received the frame when it observed the Item, and the definition is whatever the
    # row's own source contract already proves.
    op.execute(
        """
        UPDATE public.news_oi_signals s
           SET raw_instrument = s.symbol,
               received_at_ms = COALESCE(i.observed_at_ms, s.observed_at_ms),
               measurement_definition = s.metric_version || '|'
                 || COALESCE(s.source_contract_version, 'unproven') || '|'
                 || COALESCE(s.measurement_window_ms::text, 'unproven')
          FROM public.news_items i
         WHERE i.item_id = s.source_item_id
        """
    )
    op.execute("UPDATE public.news_oi_signals SET received_at_ms = observed_at_ms WHERE received_at_ms IS NULL")
    op.execute("ALTER TABLE public.news_oi_signals ALTER COLUMN received_at_ms SET NOT NULL")
    # One provider record parsed under one metric version is one observation. Defensive, because a
    # multi-unit Item could historically have produced two ledger rows for one record: keep the row
    # that became durable first, which is the one any consumer could have acted on.
    op.execute(
        """
        DELETE FROM public.news_oi_signals s
         USING public.news_oi_signals keep
         WHERE s.source_item_id = keep.source_item_id
           AND s.metric_version = keep.metric_version
           AND (keep.created_at_ms, keep.event_id) < (s.created_at_ms, s.event_id)
        """
    )
    op.execute(
        """
        ALTER TABLE public.news_oi_signals
          ADD CONSTRAINT news_oi_signals_observation_key UNIQUE (source_item_id, metric_version)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_oi_signals_group_observed
            ON public.news_oi_signals
               (provider, source_venue, raw_instrument, measurement_definition, observed_at_ms DESC)
        """
    )


def _liquidations_without_a_venue_allowlist() -> None:
    op.execute("ALTER TABLE public.news_market_liquidations DROP CONSTRAINT news_market_liquidations_venue_check")
    op.execute("ALTER TABLE public.news_market_liquidations RENAME COLUMN venue TO source_venue")
    op.execute("ALTER TABLE public.news_market_liquidations ALTER COLUMN source_venue DROP NOT NULL")
    op.execute(
        """
        ALTER TABLE public.news_market_liquidations
          ADD COLUMN provider text NOT NULL DEFAULT 'opennews',
          ADD COLUMN raw_instrument text NOT NULL DEFAULT '',
          ADD COLUMN source_strategy_id text NOT NULL DEFAULT '2000',
          ADD COLUMN available_at_ms bigint
        """
    )
    op.execute(
        """
        UPDATE public.news_market_liquidations
           SET raw_instrument = symbol, available_at_ms = created_at_ms
         WHERE raw_instrument = '' OR available_at_ms IS NULL
        """
    )
    op.execute("ALTER TABLE public.news_market_liquidations ALTER COLUMN available_at_ms SET NOT NULL")
    # A liquidation whose Item has already been purged is unreachable evidence; the foreign key below
    # is what stops the next one being created, and these are the ones it cannot adopt.
    op.execute(
        """
        DELETE FROM public.news_market_liquidations l
         WHERE NOT EXISTS (SELECT 1 FROM public.news_items i WHERE i.item_id = l.item_id)
        """
    )
    op.execute(
        """
        DELETE FROM public.news_market_liquidations l
         USING public.news_market_liquidations keep
         WHERE l.item_id = keep.item_id
           AND (keep.created_at_ms, keep.source_key) < (l.created_at_ms, l.source_key)
        """
    )
    op.execute(
        """
        ALTER TABLE public.news_market_liquidations
          ADD CONSTRAINT news_market_liquidations_item_key UNIQUE (item_id),
          ADD CONSTRAINT news_market_liquidations_item_fk
            FOREIGN KEY (item_id) REFERENCES public.news_items(item_id) ON DELETE CASCADE
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_market_liquidations_group_event
            ON public.news_market_liquidations
               (provider, source_venue, raw_instrument, liquidated_position_side, event_at_ms DESC)
        """
    )


def _smart_money_table() -> None:
    op.execute(
        """
        CREATE TABLE public.news_market_smart_money (
            source_key text NOT NULL,
            item_id text NOT NULL,
            fact_id text NOT NULL,
            ingest_mode text NOT NULL,
            provider text NOT NULL,
            source_strategy_id text NOT NULL,
            trader_label text NOT NULL,
            account_address text,
            source_venue text,
            raw_instrument text NOT NULL,
            symbol text NOT NULL,
            action text NOT NULL,
            position_side text NOT NULL,
            reported_notional_usd numeric NOT NULL,
            price numeric NOT NULL,
            pnl_usd numeric,
            event_at_ms bigint NOT NULL,
            received_at_ms bigint NOT NULL,
            available_at_ms bigint NOT NULL,
            created_at_ms bigint NOT NULL,
            parser_version text NOT NULL,
            provider_record_identity text NOT NULL,
            source_contract_version text NOT NULL,
            notional_semantics text NOT NULL,
            price_semantics text NOT NULL,
            completeness_assumption text NOT NULL,
            CONSTRAINT news_market_smart_money_pkey PRIMARY KEY (source_key),
            CONSTRAINT news_market_smart_money_item_key UNIQUE (item_id),
            CONSTRAINT news_market_smart_money_item_fk
                FOREIGN KEY (item_id) REFERENCES public.news_items(item_id) ON DELETE CASCADE,
            CONSTRAINT news_market_smart_money_ingest_mode_check
                CHECK (ingest_mode = ANY (ARRAY['live'::text, 'recovery'::text])),
            CONSTRAINT news_market_smart_money_action_check
                CHECK (action = ANY (ARRAY['open'::text, 'close'::text])),
            CONSTRAINT news_market_smart_money_position_side_check
                CHECK (position_side = ANY (ARRAY['long'::text, 'short'::text])),
            CONSTRAINT news_market_smart_money_notional_positive CHECK (reported_notional_usd > (0)::numeric),
            CONSTRAINT news_market_smart_money_price_positive CHECK (price > (0)::numeric),
            CONSTRAINT news_market_smart_money_label_nonempty CHECK (trader_label <> ''::text),
            CONSTRAINT news_market_smart_money_instrument_nonempty
                CHECK (raw_instrument <> ''::text AND symbol <> ''::text),
            CONSTRAINT news_market_smart_money_source_contract_check
                CHECK (source_contract_version <> ''::text
                   AND provider_record_identity <> ''::text
                   AND parser_version <> ''::text
                   AND notional_semantics <> ''::text
                   AND price_semantics <> ''::text
                   AND completeness_assumption <> ''::text)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_market_smart_money_group_event
            ON public.news_market_smart_money
               (provider, source_strategy_id, trader_label, account_address, source_venue,
                raw_instrument, action, position_side, event_at_ms DESC)
        """
    )


def _backfill_market_items() -> None:
    """Classify every retained Item that a market Strategy reported.

    By Strategy id, exactly as the live classifier now does. An Item whose Event was recorded as
    `unsupported_market` under a Strategy this repository still has no template for keeps that answer
    under its current name, `unknown_market`: the frame was always readable, and calling it
    unsupported was a statement about this code, not about the observation.
    """

    op.execute(
        f"""
        WITH kinds(strategy_id, market_kind) AS (VALUES {_MARKET_STRATEGY_KINDS})
        UPDATE public.news_items i
           SET market_kind = kinds.market_kind,
               market_source_strategy_id = kinds.strategy_id,
               market_parse_status = 'raw',
               market_parse_error = 'market_backfill_not_reparsed'
          FROM kinds
         WHERE i.market_kind IS NULL
           AND i.provider_metadata #>> '{{strategies,0,id}}' = kinds.strategy_id
        """  # noqa: S608 -- `kinds` is a module-owned literal tuple list, not caller input
    )
    op.execute(
        """
        UPDATE public.news_items i
           SET market_kind = 'unknown_market',
               market_source_strategy_id = COALESCE(i.provider_metadata #>> '{strategies,0,id}', ''),
               market_parse_status = 'raw',
               market_parse_error = 'unknown_market_source'
         WHERE i.market_kind IS NULL
           AND EXISTS (
             SELECT 1
               FROM public.news_event_members m
               JOIN public.news_events e ON e.event_id = m.event_id
              WHERE m.item_id = i.item_id AND e.event_kind = 'unsupported_market')
        """
    )


def _rebuild_missing_oi_facts() -> None:
    """Reconstruct the OI observations the Event-shaped write path never produced.

    Two populations, one cause: a recovery frame skipped Triage entirely, and a frame the title
    deduper joined to an existing Event was recorded as a member with no ledger row of its own. Both
    are real measurements that arrived, were stored as Items, and then could not be read as facts.

    The source of truth is `news_event_members`, which carries the exact `fact_id` and `fact_text`
    each observation was admitted under -- so the identity below is the same sha256 the live path
    computes, and a leader whose row already exists collides on the observation key and is left
    exactly as it was.
    """

    op.execute(
        f"""
        INSERT INTO public.news_oi_signals (
          event_id, metric_version, symbol, raw_instrument, direction,
          oi_change_bps, oi_value_usd, whale_long_profit_bps, whale_oi_ratio_bps,
          observed_at_ms, received_at_ms, created_at_ms, available_at_ms,
          provider, source_strategy_id, source_contract_version, measurement_window_ms,
          measurement_definition, source_item_id, source_venue, historical
        )
        SELECT
          encode(sha256(convert_to(
            'news_event_identity_v6' || chr(31) || m.item_id || chr(31) || m.fact_id || chr(31) || 'oi',
            'UTF8'::name)), 'hex'),
          'oi_signal_v1',
          regexp_replace(upper(btrim(parts[1])), '^XYZ-', ''),
          left(btrim(parts[1]), 32),
          CASE WHEN lower(parts[2]) IN ('fall', 'drop') THEN 'fall' ELSE 'rise' END,
          round(parts[3]::numeric * 100)::bigint,
          trunc(trunc(parts[4]::numeric * 1000000) * CASE upper(parts[5])
            WHEN 'K' THEN 1000 WHEN 'M' THEN 1000000 WHEN 'B' THEN 1000000000 ELSE 1 END / 1000000)::bigint,
          round(parts[6]::numeric * 100)::bigint,
          round(parts[7]::numeric * 100)::bigint,
          i.published_at_ms,
          i.observed_at_ms,
          (extract(epoch FROM now()) * 1000)::bigint,
          (extract(epoch FROM now()) * 1000)::bigint,
          'opennews',
          CASE WHEN proven.ok THEN '1019' END,
          CASE WHEN proven.ok THEN 'opennews_oi_source_v1' END,
          CASE WHEN proven.ok THEN 300000 END,
          CASE WHEN proven.ok
               THEN 'oi_signal_v1|opennews_oi_source_v1|300000'
               ELSE 'oi_signal_v1|unproven|unproven' END,
          m.item_id,
          NULLIF(left(lower(btrim(COALESCE(i.provider_metadata ->> 'source', ''))), 32), ''),
          true
          FROM public.news_event_members m
          JOIN public.news_events e ON e.event_id = m.event_id AND e.event_kind = 'oi'
          JOIN public.news_items i ON i.item_id = m.item_id
          CROSS JOIN LATERAL (SELECT regexp_match(m.fact_text, '{_OI_TEMPLATE}', 'i') AS parts) AS matched
          CROSS JOIN LATERAL (SELECT EXISTS (
            SELECT 1 FROM jsonb_array_elements(
              COALESCE(i.provider_metadata -> 'strategies', '[]'::jsonb)) AS s
             WHERE s ->> 'id' = '1019') AS ok) AS proven
         WHERE matched.parts IS NOT NULL
           AND abs(round(parts[3]::numeric * 100)) < 9223372036854775807
           AND round(parts[6]::numeric * 100) < 9223372036854775807
           AND round(parts[7]::numeric * 100) < 9223372036854775807
        ON CONFLICT (source_item_id, metric_version) DO NOTHING
        """  # noqa: S608 -- the only interpolation is this revision's own frozen template literal
    )


def _stamp_market_parse_status() -> None:
    """Record, per market Item, whether a typed row now exists beside it.

    The backfill above marked every market Item `raw / market_backfill_not_reparsed`, which is the
    honest state of a frame no parser has been run against -- not the same claim as "the parser ran
    and the template did not match". This upgrades the ones that now have a fact.
    """

    op.execute(
        """
        UPDATE public.news_items i
           SET market_parse_status = 'parsed', market_parse_error = NULL
         WHERE i.market_kind IS NOT NULL
           AND i.market_parse_status = 'raw'
           AND (EXISTS (SELECT 1 FROM public.news_oi_signals s WHERE s.source_item_id = i.item_id)
             OR EXISTS (SELECT 1 FROM public.news_market_liquidations l WHERE l.item_id = i.item_id))
        """
    )


def downgrade() -> None:
    """Refused. Every column this revision adds holds evidence that exists nowhere else."""

    raise RuntimeError("news_market_facts_downgrade_unsupported")
