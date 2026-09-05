"""The Robinhood Chain wallet tape gets its three tables: fills, roster versions, ingest position (#572 PR-1).

Migration evidence:

- category: three new tables with five indexes between them, no change to any existing table, column,
  constraint or row
- why_database_must_change: the tape's whole product is durable facts. `news_market_wallet_fills` is the
  ledger the exit and crowding rules in PR-2 read and the week-one calibration counts in #572 §6 are
  computed from; its identity is the chain's own `(chain_id, tx_hash, log_index)`, which is what makes a
  re-read of an overlapping block range write nothing new instead of duplicating a movement.
  `news_market_wallet_roster` versions the list a card must be able to name: a fill records which version
  was being followed when it was seen, so a later roster cannot be used to reinterpret an old signal.
  `news_market_wallet_tape_state` is one row holding how far the tape has been classified. It has to be
  in PostgreSQL rather than in the loop's memory for the reason every restart makes obvious: a process
  that resumed from the chain head would silently lose everything that happened while it was down, and
  one that resumed from a fixed lookback would re-fetch receipts for ever. The position is a
  `(block, transaction index)` pair rather than a block number because one block can hold more roster
  transactions than a turn may fetch receipts for, and a block-only mark cannot say "half of this block
  is classified" -- with one, a busy block would be re-planned every turn and the tape would not advance.

  Amounts are `numeric(78,0)`, not `bigint` and not `double precision`. A single 18-decimal balance
  overflows `bigint` at four tokens of supply, and the sell rule this feeds compares a pre-trade quantity
  against a sold quantity exactly. `usd` is a separate `numeric(38,10)` because it is a derived dollar
  figure and not a chain quantity, and it is nullable on purpose: a leg settled in something other than
  the pinned stablecoin is `unpriced`, which is not zero.
- current_source_revision: 20260906_0368
- minimum_supported_source_revision: 20260906_0368
- lock_level_and_order: three `CREATE TABLE`s and five `CREATE INDEX`es on those new tables only. No
  `ALTER TABLE`, no foreign key, and therefore no lock on `news_items` or on any existing relation
- statement_timeout: 30s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: all three tables start empty. The measured stream is 599 outbound roster transactions
  per 2.8 hours across 35 wallets (#572 §3.3), so the fills table is expected to reach the low tens of
  thousands of rows per week at the 90-day retention this PR configures; the roster holds 35-40 rows per
  version and the state table holds exactly one row for ever
- estimated_bytes: single-digit megabytes at the projected week-one row counts, plus the two fill indexes
- rewrite_or_index_build: nothing is rewritten -- no existing table is touched. Every index is built on
  an empty table
- preflight_and_maintenance_boundary: none required. Nothing here changes a shape an already-running
  writer uses, so an old process keeps working unchanged; the `news-chain-tape` task is disabled by
  default and writes nothing until an operator sets `news.chain_tape.enabled`
- archive_current_compatibility: every existing row keeps every value it had. No column, constraint or
  index outside these three new tables is read or written
- role_and_grant_impact: none; the single `tracefold` login is unchanged
- failure_state: the transaction rolls back completely and the database keeps its current shape
- roll_forward_or_verified_backup_restore: `downgrade` is refused. Dropping the fills would delete the
  only record of what a tracked wallet did -- the public RPC keeps state for about ten minutes and the
  provider's own tape is missing about two thirds of its closes, so nothing else can rebuild it. Roll
  forward with a new revision
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260906_0369
Revises: 20260906_0368
Create Date: 2026-09-06 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260906_0369"
down_revision = "20260906_0368"
branch_labels = None
depends_on = None

_ADDRESS_PATTERN = "^0x[0-9a-f]{40}$"
_TX_HASH_PATTERN = "^0x[0-9a-f]{64}$"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")

    _wallet_fills()
    _wallet_roster()
    _tape_state()


def _wallet_fills() -> None:
    """One roster wallet's movement in one transaction, read as what it was.

    The kind vocabulary is three values, not four. An inbound transfer with no swap in the same receipt
    is an airdrop or dust: it is counted in telemetry and never stored, because a row for every token
    somebody pushed at a followed wallet would be most of the table and none of the product (#572 §5.2).
    """

    op.execute(
        f"""
        CREATE TABLE public.news_market_wallet_fills (
            chain_id bigint NOT NULL,
            tx_hash text NOT NULL,
            log_index integer NOT NULL,
            block_number bigint NOT NULL,
            block_hash text NOT NULL,
            wallet text NOT NULL,
            token text NOT NULL,
            token_symbol text,
            token_decimals integer,
            kind text NOT NULL,
            amount_raw numeric(78,0) NOT NULL,
            cash_token text,
            cash_amount_raw numeric(78,0),
            cash_decimals integer,
            usd numeric(38,10),
            usd_source text,
            event_at_ms bigint NOT NULL,
            received_at_ms bigint NOT NULL,
            classified_at_ms bigint NOT NULL,
            roster_version bigint NOT NULL,
            provider text NOT NULL DEFAULT 'robinhood_chain',
            -- The chain assigned this identity. Nothing local invents one, which is exactly why a
            -- second delivery of the same movement is the same row.
            CONSTRAINT news_market_wallet_fills_pkey PRIMARY KEY (chain_id, tx_hash, log_index),
            CONSTRAINT news_market_wallet_fills_kind_check
                CHECK (kind = ANY (ARRAY['buy'::text, 'sell'::text, 'transfer_out'::text])),
            CONSTRAINT news_market_wallet_fills_provider_check
                CHECK (provider = 'robinhood_chain'::text),
            CONSTRAINT news_market_wallet_fills_identity_check
                CHECK (tx_hash ~ '{_TX_HASH_PATTERN}'
                       AND wallet ~ '{_ADDRESS_PATTERN}'
                       AND token ~ '{_ADDRESS_PATTERN}'
                       AND (cash_token IS NULL OR cash_token ~ '{_ADDRESS_PATTERN}')),
            CONSTRAINT news_market_wallet_fills_position_check
                CHECK (chain_id > 0 AND block_number > 0 AND log_index >= 0
                       AND block_hash <> ''::text AND roster_version > 0),
            CONSTRAINT news_market_wallet_fills_amount_check
                CHECK (amount_raw >= 0
                       AND (cash_amount_raw IS NULL OR cash_amount_raw >= 0)
                       AND (token_decimals IS NULL OR (token_decimals >= 0 AND token_decimals <= 77))
                       AND (cash_decimals IS NULL OR (cash_decimals >= 0 AND cash_decimals <= 77))),
            -- A cash leg is a token and an amount together, or it is absent. Half of one would be a
            -- quantity of nothing.
            CONSTRAINT news_market_wallet_fills_cash_pair_check
                CHECK ((cash_token IS NULL) = (cash_amount_raw IS NULL)
                       AND (cash_decimals IS NULL OR cash_token IS NOT NULL)),
            -- A trade is a trade because the money moved. `transfer_out` is the kind that carries no
            -- cash leg, and it is the only one.
            CONSTRAINT news_market_wallet_fills_trade_cash_check
                CHECK ((kind = 'transfer_out'::text) = (cash_token IS NULL)),
            -- A dollar figure exists only with the statement of how it was derived, and only where
            -- there was a cash leg to derive it from.
            CONSTRAINT news_market_wallet_fills_usd_check
                CHECK ((usd IS NULL) = (usd_source IS NULL)
                       AND (usd IS NULL OR cash_token IS NOT NULL)
                       AND (usd IS NULL OR usd >= 0)),
            CONSTRAINT news_market_wallet_fills_clock_check
                CHECK (event_at_ms > 0 AND received_at_ms > 0 AND classified_at_ms > 0)
        )
        """
    )
    # "What has this wallet done in this token, newest first" -- the exit rule's own question, and the
    # console's. Leading with the wallet keeps the roster's 35 addresses the first cut.
    op.execute(
        """
        CREATE INDEX ix_news_market_wallet_fills_wallet_token
            ON public.news_market_wallet_fills (wallet, token, event_at_ms DESC)
        """
    )
    # The window scan: the calibration counts, the crowding rule's "who else bought this token in the
    # last fifteen minutes", and the retention pass's cutoff all read this one.
    op.execute(
        """
        CREATE INDEX ix_news_market_wallet_fills_event_at
            ON public.news_market_wallet_fills (event_at_ms)
        """
    )


def _wallet_roster() -> None:
    """One row per wallet per roster version: who was being followed, and which list put them there.

    Both ranks are nullable and a member may hold both: the quality and whale lists overlapped by five
    addresses on the day the rules were chosen. A NULL rank is "this list did not select this wallet",
    which is not rank 0.
    """

    op.execute(
        f"""
        CREATE TABLE public.news_market_wallet_roster (
            roster_version bigint NOT NULL,
            taken_at_ms bigint NOT NULL,
            wallet text NOT NULL,
            handle text NOT NULL DEFAULT '',
            followers bigint NOT NULL DEFAULT 0,
            realized_pnl double precision NOT NULL DEFAULT 0,
            closed_trades integer NOT NULL DEFAULT 0,
            win_rate double precision NOT NULL DEFAULT 0,
            profit_factor double precision,
            open_cost double precision NOT NULL DEFAULT 0,
            rank_quality integer,
            rank_whale integer,
            provider text NOT NULL DEFAULT 'robinhoodtrenches',
            CONSTRAINT news_market_wallet_roster_pkey PRIMARY KEY (roster_version, wallet),
            CONSTRAINT news_market_wallet_roster_provider_check
                CHECK (provider = 'robinhoodtrenches'::text),
            CONSTRAINT news_market_wallet_roster_wallet_check
                CHECK (wallet ~ '{_ADDRESS_PATTERN}'),
            CONSTRAINT news_market_wallet_roster_version_check
                CHECK (roster_version > 0 AND taken_at_ms > 0),
            -- A member is on this version because at least one list selected it.
            CONSTRAINT news_market_wallet_roster_rank_check
                CHECK ((rank_quality IS NOT NULL OR rank_whale IS NOT NULL)
                       AND (rank_quality IS NULL OR rank_quality > 0)
                       AND (rank_whale IS NULL OR rank_whale > 0))
        )
        """
    )
    # "Which version was current" -- read at the start of every turn and by every card that pins a
    # version. Descending, because the current version is the only one anything asks for by default.
    op.execute(
        """
        CREATE INDEX ix_news_market_wallet_roster_version
            ON public.news_market_wallet_roster (roster_version DESC, wallet)
        """
    )


def _tape_state() -> None:
    """Exactly one row: how far the tape is classified, and what the last turn did.

    A one-row table rather than a column on something else, because nothing else here has the same
    lifetime: the fills are a ledger, the roster is versioned, and this is the loop's own cursor.
    """

    op.execute(
        """
        CREATE TABLE public.news_market_wallet_tape_state (
            state_id text NOT NULL,
            high_water_block bigint NOT NULL DEFAULT 0,
            high_water_tx_index integer NOT NULL DEFAULT -1,
            roster_version bigint NOT NULL DEFAULT 0,
            last_outcome text NOT NULL DEFAULT '',
            last_error text,
            last_success_at_ms bigint,
            updated_at_ms bigint NOT NULL,
            CONSTRAINT news_market_wallet_tape_state_pkey PRIMARY KEY (state_id),
            -- One tape, one row. The identity is a constant so a second row cannot be inserted by a
            -- writer that thought it was starting fresh.
            CONSTRAINT news_market_wallet_tape_state_singleton_check
                CHECK (state_id = 'chain_tape'::text),
            CONSTRAINT news_market_wallet_tape_state_position_check
                CHECK (high_water_block >= 0 AND high_water_tx_index >= -1 AND roster_version >= 0),
            CONSTRAINT news_market_wallet_tape_state_outcome_check
                CHECK (last_outcome = ANY (
                  ARRAY[''::text, 'success'::text, 'partial'::text, 'error'::text])),
            CONSTRAINT news_market_wallet_tape_state_clock_check
                CHECK (updated_at_ms > 0 AND (last_success_at_ms IS NULL OR last_success_at_ms > 0))
        )
        """
    )


def downgrade() -> None:
    """Refused. The fills here are the only record of what a followed wallet did; nothing can rebuild them."""

    raise RuntimeError("news_market_wallet_tape_downgrade_unsupported")
