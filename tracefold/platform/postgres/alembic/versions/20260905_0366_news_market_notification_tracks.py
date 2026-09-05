"""Market observations get a notification to-do list: one track per group, one row per card (#553 PR-2).

Migration evidence:

- category: three additive nullable columns and one CHECK on `news_items`, four partial indexes on it,
  two new tables with four indexes between them, one foreign key each way, and one single-statement
  backfill over the retained market subset
- why_database_must_change: the notification rules in #553 §4 are stateful across restarts -- "has this
  group been told about", "what did the last card actually cover", "is a send still owed" -- and none
  of that state exists anywhere today. It has to be in PostgreSQL rather than in the loop's memory for
  three reasons the Issue states directly: a card whose result this process could not read must stay
  `unknown` across a restart instead of being re-sent (§4.5); the wait before a retry is held as a due
  time rather than a sleeping task (§5.1); and the loop's to-do list must survive the process that
  crashed mid-send, which is what makes "no new RabbitMQ queue" possible (§2).

  `news_items.market_notify_state` is that to-do list's take predicate. It is a marker rather than a
  high-water mark on `created_at_ms` or an autoincrement, deliberately: a transaction that commits
  late has an earlier stamp than one that committed before it, and a cursor over stamps skips it for
  ever. A marker cannot skip a row, whenever it becomes visible (§4.1.3).

  `market_notify_group_key` records which notification group the loop assigned an observation to, and
  `market_notify_delivery_key` which card spoke for it. They are the loop's own processing evidence,
  not a cache of a derivable value: the notification group is deliberately *not* the read model's
  display group -- a smart-money display run breaks when the account changes action, and the
  notification group must not, because that change is the thing worth a card. Recomputing one from the
  other in two places is how they would drift.
- current_source_revision: 20260905_0365
- minimum_supported_source_revision: 20260905_0365
- lock_level_and_order: `ACCESS EXCLUSIVE` on `news_items` for the catalog change, then one scan of the
  market subset for the backfill and three partial index builds over it; then the two `CREATE TABLE`s,
  which take no lock on anything existing except the `news_items` primary key referenced by the two
  foreign keys
- statement_timeout: 120s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: production holds 2 741 `news_items`, of which about 640 are market records in the
  retained window; the backfill marks exactly those `historical` in one statement -- at that row count
  batching would add a loop and protect nothing. Both new tables start empty
- estimated_bytes: three nullable `text` columns on `news_items`, four partial indexes covering only
  the market subset, and two empty tables with four indexes between them. Single-digit megabytes at
  the measured row counts
- rewrite_or_index_build: `ADD COLUMN` with no default does not rewrite the heap. The four
  `news_items` index builds are all partial -- `market_notify_state = 'pending'` and three
  market-only predicates -- so each covers a few hundred rows rather than the whole table, and every
  build is an ordinary in-transaction one at these row counts. The four indexes on the two new tables
  are built empty
- preflight_and_maintenance_boundary: writers must be stopped. The admission path starts writing
  `market_notify_state` for every new market Item, and a process on the old code would leave the
  column NULL against the new CHECK, so every market admission would fail. `make up` stops Workers,
  which is the boundary this revision needs
- archive_current_compatibility: every existing row keeps every value it had. Every market Item that
  exists when this revision runs is marked `historical`: those observations were reported before any
  notification rule was enabled, and alerting on a two-day-old OI frame at enable time would be
  interrupting a reader with news they cannot act on. That is the Issue's own "mark the pre-enable
  backlog once, then alert normally on live records" (§4.1.5), and it is written here rather than by
  the loop because the loop cannot tell "arrived before the feature existed" from "arrived while the
  process was down" -- only this revision's moment can
- role_and_grant_impact: none; the single `tracefold` login is unchanged
- failure_state: the transaction rolls back completely and every table keeps its current shape
- roll_forward_or_verified_backup_restore: `downgrade` is refused. Dropping `news_market_deliveries`
  would delete the receipts that say which cards a reader actually received, which exist nowhere else,
  and dropping `market_notify_state` would make every already-notified market Item look unprocessed,
  so the next start would re-send the whole retained backlog. Roll forward with a new revision
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260905_0366
Revises: 20260905_0365
Create Date: 2026-09-05 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260905_0366"
down_revision = "20260905_0365"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")

    _market_notification_markers()
    # The backlog is marked before the CHECK is added, because the CHECK now genuinely refuses a NULL
    # marker: adding it first would refuse every market Item already here.
    _mark_pre_enable_backlog_historical()
    _market_notify_state_check()
    _market_tracks()
    _market_deliveries()
    _market_notify_delivery_reference()


def _market_notification_markers() -> None:
    op.execute(
        """
        ALTER TABLE public.news_items
          ADD COLUMN market_notify_state text,
          ADD COLUMN market_notify_group_key text,
          ADD COLUMN market_notify_delivery_key text
        """
    )
    # The take query, and the only index it needs: the backlog is ordered by the host's own receive
    # stamp so the oldest un-notified observation is grouped first.
    op.execute(
        """
        CREATE INDEX ix_news_items_market_notify_pending
            ON public.news_items (observed_at_ms, item_id)
         WHERE market_notify_state = 'pending'
        """
    )
    # "Which observations is this group still owing a card for" -- the set an intent adopts when it is
    # created, and the set a page calls `merging`.
    op.execute(
        """
        CREATE INDEX ix_news_items_market_notify_unclaimed
            ON public.news_items (market_notify_group_key, observed_at_ms)
         WHERE market_notify_group_key IS NOT NULL AND market_notify_delivery_key IS NULL
        """
    )
    # "Which observations did this card speak for" -- read at freeze time and by the detail page.
    op.execute(
        """
        CREATE INDEX ix_news_items_market_notify_delivery
            ON public.news_items (market_notify_delivery_key)
         WHERE market_notify_delivery_key IS NOT NULL
        """
    )
    # "Does this group still have any observation at all" -- the retention pass's anti-join. The two
    # indexes above are both partial on a delivery key and neither can answer it.
    op.execute(
        """
        CREATE INDEX ix_news_items_market_notify_group_key
            ON public.news_items (market_notify_group_key)
         WHERE market_notify_group_key IS NOT NULL
        """
    )


def _market_notify_state_check() -> None:
    """The marker's own state machine, added once every existing row already satisfies it."""

    # `market_kind IS NULL` is "this Item is ordinary news", and ordinary news has no notification
    # state at all -- its deliveries are `news_deliveries` and its own Event decides them. The rest of
    # the CHECK is the state machine written down: only a processed observation can name a group or a
    # card, and a card is only ever named by an observation that was grouped first.
    op.execute(
        """
        ALTER TABLE public.news_items
          ADD CONSTRAINT news_items_market_notify_state_check
            CHECK (
              (market_kind IS NULL
                 AND market_notify_state IS NULL
                 AND market_notify_group_key IS NULL
                 AND market_notify_delivery_key IS NULL)
              OR (market_kind IS NOT NULL
                 -- `COALESCE`, not a bare comparison: a NULL marker makes `= ANY (...)` evaluate to
                 -- NULL, and a CHECK passes on NULL. Without this an old writer -- one that does not
                 -- know the column -- would insert a market Item with no marker and never be
                 -- refused, and the loop would never see the observation.
                 AND COALESCE(market_notify_state, '') = ANY (
                       ARRAY['pending'::text, 'historical'::text, 'processed'::text])
                 AND (market_notify_state = 'processed' OR market_notify_group_key IS NULL)
                 AND (market_notify_state = 'processed' OR market_notify_delivery_key IS NULL)
                 AND (market_notify_delivery_key IS NULL OR market_notify_group_key IS NOT NULL)))
        """
    )


def _market_tracks() -> None:
    """One row per notification group: when is this subject worth interrupting a reader again.

    The type-specific columns are deliberately per type and mostly NULL: an OI group has a measurement
    definition and no wallet, a smart-money group has an account and no measurement definition. A
    shared "metrics" dictionary would have had to invent a vocabulary that means something different
    per kind, which is the same as not validating it.
    """

    op.execute(
        """
        CREATE TABLE public.news_market_tracks (
            group_key text NOT NULL,
            market_kind text NOT NULL,
            family text NOT NULL,
            provider text,
            source_venue text,
            venue_known boolean NOT NULL DEFAULT false,
            raw_instrument text,
            symbol text,
            measurement_definition text,
            liquidated_position_side text,
            account_key text,
            account_verified boolean NOT NULL DEFAULT false,
            trader_label text,
            current_action text,
            current_position_side text,
            last_observed_at_ms bigint NOT NULL,
            last_observed_item_id text NOT NULL,
            anchor_state text NOT NULL DEFAULT '',
            anchor_delivery_key text,
            anchor_attempt_at_ms bigint,
            anchor_oi_change_bps bigint,
            anchor_direction text,
            anchor_action text,
            anchor_position_side text,
            open_delivery_key text,
            next_due_at_ms bigint,
            pending_reason text NOT NULL DEFAULT '',
            created_at_ms bigint NOT NULL,
            updated_at_ms bigint NOT NULL,
            CONSTRAINT news_market_tracks_pkey PRIMARY KEY (group_key),
            CONSTRAINT news_market_tracks_family_check
                CHECK (family = ANY (
                  ARRAY['oi'::text, 'liquidation'::text, 'smart_money'::text, 'raw'::text])),
            -- Three states and no fourth. An empty anchor is "nobody has been told about this group",
            -- which is also where an explicit failure leaves it: a card that failed told no one, so
            -- the next observation opens a first card rather than a follow-up to nothing. `unknown`
            -- keeps the snapshot as an anti-duplicate reference without claiming it was delivered.
            CONSTRAINT news_market_tracks_anchor_state_check
                CHECK (anchor_state = ANY (ARRAY[''::text, 'sent'::text, 'unknown'::text])),
            CONSTRAINT news_market_tracks_anchor_evidence_check
                CHECK ((anchor_state = ''::text) = (anchor_delivery_key IS NULL)),
            CONSTRAINT news_market_tracks_observed_check
                CHECK (last_observed_at_ms > 0 AND last_observed_item_id <> ''::text)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_market_tracks_observed
            ON public.news_market_tracks (last_observed_at_ms)
        """
    )


def _market_deliveries() -> None:
    """One row per card: the to-do, the frozen snapshot, and the receipt, in one place.

    Not an outbox beside a publish flag beside a retry table. Those three would be three answers to
    "is this card still owed", and the three would disagree the first time a process died between two
    of them. `next_attempt_at_ms` is the whole scheduler: a card is claimable when it is due, and a
    retry is a later due time rather than a sleeping task.
    """

    op.execute(
        """
        CREATE TABLE public.news_market_deliveries (
            delivery_key text NOT NULL,
            group_key text NOT NULL,
            market_kind text NOT NULL,
            trigger_reason text NOT NULL,
            trigger_item_id text NOT NULL,
            state text NOT NULL,
            attempts integer NOT NULL DEFAULT 0,
            covered_count integer NOT NULL DEFAULT 0,
            covered_from_ms bigint,
            covered_to_ms bigint,
            card jsonb NOT NULL DEFAULT '{}'::jsonb,
            receipt jsonb,
            error text,
            next_attempt_at_ms bigint NOT NULL,
            first_attempt_at_ms bigint,
            last_attempt_at_ms bigint,
            settled_at_ms bigint,
            created_at_ms bigint NOT NULL,
            updated_at_ms bigint NOT NULL,
            CONSTRAINT news_market_deliveries_pkey PRIMARY KEY (delivery_key),
            CONSTRAINT news_market_deliveries_trigger_fk
                FOREIGN KEY (trigger_item_id) REFERENCES public.news_items(item_id) ON DELETE CASCADE,
            CONSTRAINT news_market_deliveries_reason_check
                CHECK (trigger_reason = ANY (
                  ARRAY['first'::text, 'followup'::text, 'action_change'::text, 'raw'::text])),
            CONSTRAINT news_market_deliveries_state_check
                CHECK (state = ANY (ARRAY['pending'::text, 'sending'::text, 'sent'::text,
                                          'failed'::text, 'unknown'::text, 'unavailable'::text])),
            -- Three real attempts is the rule, and the column cannot hold a fourth.
            CONSTRAINT news_market_deliveries_attempts_check
                CHECK (attempts >= 0 AND attempts <= 3),
            -- A receipt is the evidence of a delivery and nothing else may carry one. `unknown` in
            -- particular has no receipt: that is the whole difference between it and `sent`.
            CONSTRAINT news_market_deliveries_receipt_check
                CHECK ((state = 'sent'::text) = (receipt IS NOT NULL)),
            -- An in-flight card is not settled, and a settled one is not in flight. This is the
            -- sibling of the bounded-model invariant `news_deliveries` already carries.
            CONSTRAINT news_market_deliveries_settled_check
                CHECK ((settled_at_ms IS NOT NULL)
                       = (state = ANY (ARRAY['sent'::text, 'failed'::text, 'unknown'::text]))),
            -- A card that was attempted has a frozen snapshot; one that has not been attempted has
            -- nothing frozen yet, because new observations are still merging into it.
            CONSTRAINT news_market_deliveries_snapshot_check
                CHECK ((attempts = 0) = (card = '{}'::jsonb)),
            CONSTRAINT news_market_deliveries_attempted_check
                CHECK ((attempts = 0) = (first_attempt_at_ms IS NULL))
        )
        """
    )
    # At most one un-started intent per group (§4.5): new observations merge into it rather than
    # producing a second "first" card. A card being retried is not un-started -- its snapshot is
    # frozen and nothing may join it -- which is why `attempts = 0` is part of the predicate.
    op.execute(
        """
        CREATE UNIQUE INDEX ux_news_market_deliveries_open
            ON public.news_market_deliveries (group_key)
         WHERE state = ANY (ARRAY['pending'::text, 'unavailable'::text]) AND attempts = 0
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_market_deliveries_due
            ON public.news_market_deliveries (next_attempt_at_ms)
         WHERE state = ANY (ARRAY['pending'::text, 'unavailable'::text])
        """
    )
    # The foreign key's own index. Retention deletes 500 Items per transaction and every one of them
    # makes PostgreSQL look for referencing cards; without this that is a sequential scan per row.
    op.execute(
        """
        CREATE INDEX ix_news_market_deliveries_trigger
            ON public.news_market_deliveries (trigger_item_id)
        """
    )
    # The status block's window is on `created_at_ms`, so that is what its index leads with. An index
    # on `(market_kind, settled_at_ms)` could not serve it -- and a second index on `group_key` alone
    # would duplicate what the unique partial index above already answers.
    op.execute(
        """
        CREATE INDEX ix_news_market_deliveries_created
            ON public.news_market_deliveries (created_at_ms, market_kind)
        """
    )


def _market_notify_delivery_reference() -> None:
    """An Item may only name a card that exists, and stops naming one that was cleaned up.

    Deliveries are cleaned with the record that triggered them, which is the same market retention the
    observations get. `SET NULL` is what keeps the survivors honest: an observation whose card has been
    purged reports what it can still prove -- that it was grouped -- rather than pointing at a receipt
    that is no longer there. Without the constraint the key would simply dangle and the page would
    silently read it as "no card".
    """

    op.execute(
        """
        ALTER TABLE public.news_items
          ADD CONSTRAINT news_items_market_notify_delivery_fk
            FOREIGN KEY (market_notify_delivery_key)
            REFERENCES public.news_market_deliveries(delivery_key) ON DELETE SET NULL
        """
    )


def _mark_pre_enable_backlog_historical() -> None:
    op.execute(
        """
        UPDATE public.news_items
           SET market_notify_state = 'historical'
         WHERE market_kind IS NOT NULL
        """
    )


def downgrade() -> None:
    """Refused. The receipts here say which cards a reader actually received; nothing else does."""

    raise RuntimeError("news_market_notifications_downgrade_unsupported")
