"""The wallet tape becomes a market family: `wallet` observations, their checks and their receipts (#572 PR-2).

Migration evidence:

- category: two CHECK constraints replaced -- `news_items.market_kind` and `news_market_tracks.family`,
  each gaining the same fifth value -- plus three new tables with five indexes between them, and one new
  index on `news_market_wallet_fills`. No existing column, row or index is rewritten.
- why_database_must_change: PR-1 stores what a followed wallet did and stops. This revision is what turns
  those fills into something a reader receives, and every part of that has to be durable.
  `news_market_wallet_events` is the derived observation itself -- a cascade child of `news_items`, so a
  wallet card is an Item like every other market observation and reaches the existing notification loop,
  delivery ledger, detail page and retention with no second mechanism.
  `news_market_wallet_checks` records every verification attempt against a sell, including the ones that
  produced no card: the public RPC keeps state for about ten minutes, and "how often was the chain still
  able to answer" is a question only the failed attempts can settle -- it is also the audit trail behind
  the `site_reported` label a card prints when the chain could not.
  `news_market_wallet_outcomes` is the effect receipt #572 §11 asks for: the token's price one and four
  hours after a card was sent. It is keyed by `delivery_key` because the subject is the card, not the
  observation -- one card can speak for several observations, and the receipt belongs to what the reader
  was actually told.
  `news_items_market_kind_check` and `news_market_tracks_family_check` have to be replaced rather than
  extended because a CHECK is a single expression. Each swap validates its table's existing rows once:
  12.7k market Items when #572's database evaluation measured them, and one row per notification group
  in the tracks table -- both one short scan under the local statement timeout.
- current_source_revision: 20260906_0371
- minimum_supported_source_revision: 20260906_0371
- lock_level_and_order: two `ALTER TABLE DROP CONSTRAINT` + `ADD CONSTRAINT` pairs -- `news_items` then
  `news_market_tracks` -- each taking `ACCESS EXCLUSIVE` on its own table for one validating scan, then
  three `CREATE TABLE`s and four `CREATE INDEX`es on new relations, then one `CREATE INDEX` on
  `news_market_wallet_fills`, which takes `SHARE` on that table for the build. The foreign key on
  `news_market_wallet_events` takes a `SHARE ROW EXCLUSIVE` reference on `news_items` at creation time
  and nothing afterwards.
- statement_timeout: 60s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: all three tables start empty. #572 §6.4's medium tier projects 25-40 wallet cards a
  day, so `news_market_wallet_events` grows by roughly a thousand rows a month and
  `news_market_wallet_outcomes` by twice that. `news_market_wallet_checks` grows with *sells looked at*
  rather than with cards -- the measured roster produces on the order of a hundred a day.
- estimated_bytes: single-digit megabytes a year across all three, plus their indexes.
- rewrite_or_index_build: no table is rewritten. Each CHECK replacement is a validating scan of its own
  table, not a rewrite. Every index on the three new tables is built on an empty table; the one on
  `news_market_wallet_fills` is built over whatever the tape has stored since 2026-09-06 -- tens of
  thousands of rows at the measured rate, seconds under the local statement timeout, and the tape's own
  writer is blocked only for that build.
- preflight_and_maintenance_boundary: none required. A writer running the previous code inserts no
  `wallet` row and is unaffected by a CHECK that admits one more value, and the new fills index blocks
  that writer only for the length of its build.
- archive_current_compatibility: every existing row keeps every value it had, including its
  `market_kind` and its `family`; the four values that were valid before are still valid on both.
- role_and_grant_impact: none; the single `tracefold` login is unchanged.
- failure_state: the transaction rolls back completely and the database keeps its current shape,
  including both original CHECKs.
- roll_forward_or_verified_backup_restore: `downgrade` is refused. Dropping the events would delete the
  observations cards were sent for, and the fills they were derived from are subject to a 90-day
  retention that the derived rows are not. Roll forward with a new revision.
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260906_0372
Revises: 20260906_0371
Create Date: 2026-09-06 06:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260906_0372"
down_revision = "20260906_0371"
branch_labels = None
depends_on = None

_ADDRESS_PATTERN = "^0x[0-9a-f]{40}$"
_TX_HASH_PATTERN = "^0x[0-9a-f]{64}$"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")

    _market_kind_admits_wallet()
    _track_family_admits_wallet()
    _wallet_events()
    _wallet_checks()
    _wallet_outcomes()
    _token_window_index()


def _market_kind_admits_wallet() -> None:
    """A fifth market kind. Nothing else about the Item changes -- a wallet card *is* a market card."""

    op.execute("ALTER TABLE public.news_items DROP CONSTRAINT IF EXISTS news_items_market_kind_check")
    op.execute(
        """
        ALTER TABLE public.news_items
          ADD CONSTRAINT news_items_market_kind_check
          CHECK (market_kind IS NULL OR market_kind = ANY (ARRAY[
            'oi'::text, 'liquidation'::text, 'smart_money'::text, 'unknown_market'::text, 'wallet'::text]))
        """
    )


def _track_family_admits_wallet() -> None:
    """The notification group's family vocabulary gains the same fifth member.

    `news_market_tracks.family` is the branch of the loop that owns a group, and it was written down as
    a CHECK for exactly the reason this revision is touching it: a family nobody declared cannot be
    saved by accident. The table holds one row per notification group -- tens, not thousands -- so the
    validating scan is trivial.

    The list this adds to is `20260906_0371`'s three, not the four that preceded it: #582 §3.2 deleted
    the `raw` card family, and `wallet` is the fourth this revision leaves behind rather than the fifth.
    """

    op.execute("ALTER TABLE public.news_market_tracks DROP CONSTRAINT IF EXISTS news_market_tracks_family_check")
    op.execute(
        """
        ALTER TABLE public.news_market_tracks
          ADD CONSTRAINT news_market_tracks_family_check
          CHECK (family = ANY (ARRAY[
            'oi'::text, 'liquidation'::text, 'smart_money'::text, 'wallet'::text]))
        """
    )


def _wallet_events() -> None:
    """One derived observation per card-worthy movement, beside its Item.

    Two kinds share one table because they share one shape: a subject (wallet and token), a window, a
    set of numbers and the fill identities that produced them. A table per kind would need the same
    eleven columns twice and a union in the read model to put them back together.

    `provider` is a column rather than an implied constant for the reason #553 already wrote down on the
    other market facts: the notification group key merges on the provider, and a second provider's
    groups must not silently merge into OpenNews's. This is the row where that stops being hypothetical.
    """

    op.execute(
        f"""
        CREATE TABLE public.news_market_wallet_events (
            item_id text NOT NULL,
            kind text NOT NULL,
            provider text NOT NULL DEFAULT 'robinhood_chain',
            chain_id bigint NOT NULL,
            wallet text NOT NULL,
            handle text NOT NULL DEFAULT '',
            followers bigint NOT NULL DEFAULT 0,
            token text NOT NULL,
            token_symbol text,
            token_decimals integer,
            roster_version bigint NOT NULL,
            -- The subject's own bounds on the chain's clock: an exit's sell instant, a crowding
            -- window's first and last opening buy.
            window_from_ms bigint NOT NULL,
            window_to_ms bigint NOT NULL,
            -- Which position segment (exit) or which window (crowding) this card belongs to. It is the
            -- third part of the notification group key, which is why it is stored rather than derived:
            -- a follow-up has to land in the same group as the card it follows.
            segment_key text NOT NULL,
            tone text NOT NULL DEFAULT '',
            ratio_bps integer,
            basis text,
            quantity_raw numeric(78,0),
            balance_before_raw numeric(78,0),
            usd numeric(38,10),
            position_usd numeric(38,10),
            entry_price numeric(38,18),
            mark_price numeric(38,18),
            peer_wallets integer NOT NULL DEFAULT 0,
            peer_usd numeric(38,10),
            premium_bps integer,
            liquidity_usd numeric(38,10),
            tx_hash text,
            block_number bigint,
            closed boolean NOT NULL DEFAULT false,
            evidence jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            event_at_ms bigint NOT NULL,
            received_at_ms bigint NOT NULL,
            created_at_ms bigint NOT NULL,
            CONSTRAINT news_market_wallet_events_pkey PRIMARY KEY (item_id),
            -- The observation dies with its Item, exactly as the other typed market facts do, so
            -- retention stays one decision about the Item rather than four.
            CONSTRAINT news_market_wallet_events_item_fkey
                FOREIGN KEY (item_id) REFERENCES public.news_items (item_id) ON DELETE CASCADE,
            CONSTRAINT news_market_wallet_events_kind_check
                CHECK (kind = ANY (ARRAY['exit'::text, 'crowding'::text])),
            CONSTRAINT news_market_wallet_events_provider_check
                CHECK (provider = 'robinhood_chain'::text),
            CONSTRAINT news_market_wallet_events_identity_check
                CHECK (wallet ~ '{_ADDRESS_PATTERN}'
                       AND token ~ '{_ADDRESS_PATTERN}'
                       AND (tx_hash IS NULL OR tx_hash ~ '{_TX_HASH_PATTERN}')),
            CONSTRAINT news_market_wallet_events_position_check
                CHECK (chain_id > 0 AND roster_version > 0 AND segment_key <> ''::text
                       AND (block_number IS NULL OR block_number > 0)),
            -- An exit is a ratio from a stated basis against a movement on the chain. All four are
            -- present together or the row is not an exit.
            CONSTRAINT news_market_wallet_events_exit_check
                CHECK ((kind <> 'exit'::text)
                       OR (ratio_bps IS NOT NULL AND basis IS NOT NULL AND quantity_raw IS NOT NULL
                           AND tx_hash IS NOT NULL AND block_number IS NOT NULL)),
            CONSTRAINT news_market_wallet_events_basis_check
                CHECK (basis IS NULL OR basis = ANY (ARRAY['chain_balance'::text, 'site_reported'::text])),
            CONSTRAINT news_market_wallet_events_tone_check
                CHECK (tone = ANY (ARRAY[''::text, 'late'::text])),
            CONSTRAINT news_market_wallet_events_amount_check
                CHECK ((ratio_bps IS NULL OR (ratio_bps >= 0 AND ratio_bps <= 10000))
                       AND (quantity_raw IS NULL OR quantity_raw >= 0)
                       AND (balance_before_raw IS NULL OR balance_before_raw >= 0)
                       AND (usd IS NULL OR usd >= 0)
                       AND (position_usd IS NULL OR position_usd >= 0)
                       AND (peer_usd IS NULL OR peer_usd >= 0)
                       AND (liquidity_usd IS NULL OR liquidity_usd >= 0)
                       AND peer_wallets >= 0
                       AND (token_decimals IS NULL OR (token_decimals >= 0 AND token_decimals <= 77))),
            CONSTRAINT news_market_wallet_events_window_check
                CHECK (window_from_ms > 0 AND window_to_ms >= window_from_ms),
            CONSTRAINT news_market_wallet_events_clock_check
                CHECK (event_at_ms > 0 AND received_at_ms > 0 AND created_at_ms > 0)
        )
        """
    )
    # "What has this wallet already been carded for in this token" -- the exit rule's segment lookup,
    # and the crowding rule's per-token lookup reads the same index from its second column.
    op.execute(
        """
        CREATE INDEX ix_news_market_wallet_events_subject
            ON public.news_market_wallet_events (wallet, token, event_at_ms DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_market_wallet_events_token
            ON public.news_market_wallet_events (token, kind, event_at_ms DESC)
        """
    )


def _wallet_checks() -> None:
    """Every verification attempt against a sell fill, whatever it proved.

    Keyed by the fill's own chain identity rather than by a surrogate, so a re-read of the same movement
    updates the same row instead of accumulating attempts nobody can tell apart. `q_before_raw` is
    nullable because a failed attempt is a real row: it is the evidence for how often the public node's
    ten-minute state window was still open, which is the whole reason the relaxed `site_reported` basis
    exists.
    """

    op.execute(
        f"""
        CREATE TABLE public.news_market_wallet_checks (
            chain_id bigint NOT NULL,
            tx_hash text NOT NULL,
            log_index integer NOT NULL,
            basis text NOT NULL,
            q_before_raw numeric(78,0),
            q_sell_raw numeric(78,0) NOT NULL,
            ratio_bps integer,
            block_hash text NOT NULL DEFAULT '',
            checked_at_ms bigint NOT NULL,
            error text,
            CONSTRAINT news_market_wallet_checks_pkey PRIMARY KEY (chain_id, tx_hash, log_index),
            CONSTRAINT news_market_wallet_checks_basis_check
                CHECK (basis = ANY (ARRAY['chain_balance'::text, 'site_reported'::text])),
            CONSTRAINT news_market_wallet_checks_identity_check
                CHECK (tx_hash ~ '{_TX_HASH_PATTERN}' AND chain_id > 0 AND log_index >= 0),
            CONSTRAINT news_market_wallet_checks_amount_check
                CHECK ((q_before_raw IS NULL OR q_before_raw >= 0) AND q_sell_raw >= 0
                       AND (ratio_bps IS NULL OR (ratio_bps >= 0 AND ratio_bps <= 10000))),
            CONSTRAINT news_market_wallet_checks_clock_check
                CHECK (checked_at_ms > 0)
        )
        """
    )
    # The calibration read: how many sells were checked in a window, and on which basis.
    op.execute(
        """
        CREATE INDEX ix_news_market_wallet_checks_checked_at
            ON public.news_market_wallet_checks (checked_at_ms)
        """
    )


def _wallet_outcomes() -> None:
    """One price receipt per card per horizon. Not a gate and not a threshold -- evidence (#572 §11).

    Keyed on the card rather than on the observation, because a card is what a reader received. `price`
    is nullable and `source` says why: a horizon nothing could price after a whole day is recorded as
    `unavailable`, which is a different fact from a horizon that has not arrived yet -- and the absence
    of a row is what "not yet" means.
    """

    op.execute(
        """
        CREATE TABLE public.news_market_wallet_outcomes (
            delivery_key text NOT NULL,
            horizon text NOT NULL,
            price numeric(38,18),
            at_ms bigint NOT NULL,
            source text NOT NULL,
            CONSTRAINT news_market_wallet_outcomes_pkey PRIMARY KEY (delivery_key, horizon),
            CONSTRAINT news_market_wallet_outcomes_delivery_fkey
                FOREIGN KEY (delivery_key) REFERENCES public.news_market_deliveries (delivery_key)
                ON DELETE CASCADE,
            CONSTRAINT news_market_wallet_outcomes_horizon_check
                CHECK (horizon = ANY (ARRAY['1h'::text, '4h'::text])),
            CONSTRAINT news_market_wallet_outcomes_price_check
                CHECK ((price IS NULL) = (source = 'unavailable'::text) AND (price IS NULL OR price > 0)),
            CONSTRAINT news_market_wallet_outcomes_clock_check
                CHECK (at_ms > 0 AND source <> ''::text)
        )
        """
    )
    # "Which receipts landed in this window" -- the operator query in OPERATIONS.md reads this one.
    op.execute(
        """
        CREATE INDEX ix_news_market_wallet_outcomes_at
            ON public.news_market_wallet_outcomes (at_ms)
        """
    )


def _token_window_index() -> None:
    """`(token, event_at_ms)` on the fills, for the crowding rule's own window scan.

    PR-1 gave the fills two indexes, and neither leads with the token: one leads with the wallet
    ("what has this wallet done in this token") and one is the bare event time (retention and the
    calibration counts). The crowding rule asks a third question -- "who bought *this token* in the
    last fifteen minutes" -- and on the event-time index alone that is a scan of every wallet's every
    movement in the window, growing with the roster rather than with the token.

    Write cost is one more index on a table taking on the order of a thousand rows a day, all of them
    appended at the current block time, so the new leading column is the only thing that is not already
    sequential.
    """

    op.execute(
        """
        CREATE INDEX ix_news_market_wallet_fills_token_event_at
            ON public.news_market_wallet_fills (token, event_at_ms)
        """
    )


def downgrade() -> None:
    """Refused. The events are the observations cards were sent for; the fills expire before they do."""

    raise RuntimeError("news_market_wallet_cards_downgrade_unsupported")
