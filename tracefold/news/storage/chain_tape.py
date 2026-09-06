"""Persistence for the Robinhood Chain wallet tape: fills, roster versions, ingest position (#572 PR-1).

Three durable shapes with three different lifetimes, which is why they are three tables:

* `news_market_wallet_fills` is a `durable_event` ledger. Its identity is the chain's own
  `(chain_id, tx_hash, log_index)`, so re-reading an overlapping block range writes nothing new and a
  crash mid-turn costs a re-read rather than a duplicate.
* `news_market_wallet_roster` is `latest_state` with history. A new version appears only when the
  membership or the ranks change, because a card must be able to say which list it was following when
  it fired, and an hourly version bump would make that statement meaningless.
* `news_market_wallet_tape_state` is one row: how far the tape has been classified, and what the last
  turn did. A block high-water mark alone cannot say "this block is half classified", so the position
  is a `(block, transaction index)` pair.

Every amount is `numeric(78,0)` -- a raw integer in the token's own units. `bigint` cannot hold an
18-decimal balance and a float cannot hold it exactly, and the sell rule this feeds compares two of
them.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any, Final, TypedDict

from ..chain_tape.contracts import (
    CHAIN_TAPE_PROVIDER,
    ROSTER_PROVIDER,
    ClassifiedFill,
    RosterMember,
    RosterSnapshot,
    TapeCursor,
)
from ..chain_tape.digest import (
    DIGEST_CARDS_MAX,
    DIGEST_COSTS_MAX,
    DIGEST_WALLETS_MAX,
    DigestCardRow,
    DigestOutcomeRow,
    DigestWindowRows,
    LastDigest,
    TokenWindowFlow,
    WalletWindowActivity,
)
from ..chain_tape.rules import CrowdingBuyer, PreviousCrowding, PreviousExit
from ..wallet_contracts import (
    DIGEST_KIND,
    OUTCOME_GIVE_UP_MS,
    WALLET_OUTCOME_HORIZONS,
    OutcomeHorizon,
    WalletCheck,
    WalletEvent,
    WalletOutcome,
)
from .sql_values import _dumps

TAPE_STATE_ID: Final = "chain_tape"

_INSERT_FILL_SQL: Final = """
INSERT INTO news_market_wallet_fills (
    chain_id, tx_hash, log_index, block_number, block_hash, wallet, token,
    token_symbol, token_decimals, kind, amount_raw,
    cash_token, cash_amount_raw, cash_decimals, usd, usd_source,
    event_at_ms, received_at_ms, classified_at_ms, roster_version, provider
) VALUES (
    %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s
)
ON CONFLICT (chain_id, tx_hash, log_index) DO NOTHING
"""

WALLET_TAPE_STATE_SQL: Final = """
SELECT high_water_block, high_water_tx_index, roster_version,
       last_outcome, last_error, last_success_at_ms, updated_at_ms,
       ignored_inbound_total, unknown_total,
       noise_through_block, noise_through_tx_index
  FROM news_market_wallet_tape_state
 WHERE state_id = %s
"""

_SAVE_TAPE_STATE_SQL: Final = """
INSERT INTO news_market_wallet_tape_state (
    state_id, high_water_block, high_water_tx_index, roster_version,
    last_outcome, last_error, last_success_at_ms, updated_at_ms,
    ignored_inbound_total, unknown_total,
    noise_through_block, noise_through_tx_index
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (state_id) DO UPDATE SET
    high_water_block = EXCLUDED.high_water_block,
    high_water_tx_index = EXCLUDED.high_water_tx_index,
    roster_version = EXCLUDED.roster_version,
    last_outcome = EXCLUDED.last_outcome,
    last_error = EXCLUDED.last_error,
    last_success_at_ms = COALESCE(EXCLUDED.last_success_at_ms,
                                  news_market_wallet_tape_state.last_success_at_ms),
    updated_at_ms = EXCLUDED.updated_at_ms,
    -- Monotonic: what the tape read and chose not to store. The fills table cannot answer "how much of
    -- this stream is noise", because the answer is the rows that are not in it (#572 §6).
    ignored_inbound_total = news_market_wallet_tape_state.ignored_inbound_total
                            + EXCLUDED.ignored_inbound_total,
    unknown_total = news_market_wallet_tape_state.unknown_total + EXCLUDED.unknown_total,
    -- The counted-through marker only ever advances. `high_water_*` lags the head by the log overlap so
    -- the tip is re-read; this one must not, or the same movement would be counted again on every pass.
    noise_through_block = GREATEST(news_market_wallet_tape_state.noise_through_block,
                                   EXCLUDED.noise_through_block),
    noise_through_tx_index = CASE
        WHEN EXCLUDED.noise_through_block > news_market_wallet_tape_state.noise_through_block
            THEN EXCLUDED.noise_through_tx_index
        WHEN EXCLUDED.noise_through_block = news_market_wallet_tape_state.noise_through_block
            THEN GREATEST(news_market_wallet_tape_state.noise_through_tx_index,
                          EXCLUDED.noise_through_tx_index)
        ELSE news_market_wallet_tape_state.noise_through_tx_index
    END
"""

_CURRENT_ROSTER_SQL: Final = """
SELECT roster_version, taken_at_ms, wallet, handle, followers, realized_pnl,
       closed_trades, win_rate, profit_factor, open_cost, rank_quality, rank_whale, provider
  FROM news_market_wallet_roster
 WHERE roster_version = (SELECT max(roster_version) FROM news_market_wallet_roster)
 ORDER BY wallet
"""

_INSERT_ROSTER_MEMBER_SQL: Final = """
INSERT INTO news_market_wallet_roster (
    roster_version, taken_at_ms, wallet, handle, followers, realized_pnl,
    closed_trades, win_rate, profit_factor, open_cost, rank_quality, rank_whale, provider
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_TOUCH_ROSTER_SQL: Final = """
UPDATE news_market_wallet_roster
   SET taken_at_ms = %s
 WHERE roster_version = %s
"""

# One bounded delete per maintenance pass, on the same index the read model uses. `ctid` keeps the
# batch a plain index scan plus a delete rather than a self-join on the composite identity.
_PURGE_FILLS_SQL: Final = """
DELETE FROM news_market_wallet_fills
 WHERE ctid = ANY (ARRAY(
       SELECT ctid FROM news_market_wallet_fills
        WHERE event_at_ms < %s
        ORDER BY event_at_ms
        LIMIT %s))
"""


# --- the derived observations, their checks and their receipts (#572 PR-2) -------------------------
#
# Every read below starts at a typed fact table on an indexed field and a time window, and joins to the
# Item only where the Item is what is being asked about. That is #570 A3's shape, and it is why none of
# these restates the read model's `group_key` string.

_INSERT_WALLET_EVENT_SQL: Final = """
INSERT INTO news_market_wallet_events (
    item_id, kind, provider, chain_id, wallet, handle, followers, token, token_symbol, token_decimals,
    roster_version, window_from_ms, window_to_ms, segment_key, tone, ratio_bps, basis,
    quantity_raw, balance_before_raw, usd, position_usd, entry_price, mark_price,
    peer_wallets, peer_usd, premium_bps, liquidity_usd, tx_hash, block_number, closed, evidence,
    event_at_ms, received_at_ms, created_at_ms
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
    %s, %s, %s
)
ON CONFLICT (item_id) DO NOTHING
"""

# One row per sell fill, on the fill's own chain identity, and the *first* answer is the one kept. The
# log overlap re-offers the same movement for a few seconds, and by the second offer the block has
# usually left the node's state window -- so an update would quietly rewrite a card's audit row from
# `chain_balance` to `site_reported` and disagree with the card that was already sent.
_INSERT_WALLET_CHECK_SQL: Final = """
INSERT INTO news_market_wallet_checks (
    chain_id, tx_hash, log_index, basis, q_before_raw, q_sell_raw, ratio_bps, block_hash,
    checked_at_ms, error
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (chain_id, tx_hash, log_index) DO NOTHING
"""

_LAST_EXIT_SQL: Final = """
SELECT segment_key, ratio_bps, closed
  FROM news_market_wallet_events
 WHERE kind = 'exit' AND chain_id = %s AND wallet = %s AND token = %s
 ORDER BY event_at_ms DESC, item_id DESC
 LIMIT 1
"""

_LAST_CROWDING_SQL: Final = """
SELECT window_from_ms, window_to_ms, peer_wallets
  FROM news_market_wallet_events
 WHERE kind = 'crowding' AND chain_id = %s AND token = %s
 ORDER BY event_at_ms DESC, item_id DESC
 LIMIT 1
"""

# The crowding card an exit in the same token can point back at: the reader who was told three wallets
# were piling in is the reader who should be told the lead just got out.
_RECENT_CROWDING_ITEM_SQL: Final = """
SELECT item_id
  FROM news_market_wallet_events
 WHERE kind = 'crowding' AND chain_id = %s AND token = %s AND event_at_ms >= %s
 ORDER BY event_at_ms DESC, item_id DESC
 LIMIT 1
"""

# The exit rule's cascade arm: other roster wallets that bought this token in the window before the sell.
_CASCADE_BUYS_SQL: Final = """
SELECT count(DISTINCT wallet) AS wallets, COALESCE(sum(usd), 0) AS usd
  FROM news_market_wallet_fills
 WHERE chain_id = %s AND token = %s AND kind = 'buy'
   AND wallet <> %s
   AND event_at_ms >= %s AND event_at_ms <= %s
"""

# The crowding rule's window: who *opened* a position in this token inside it, and for how much.
#
# The scan is bounded to the window itself, and both halves have an index that can serve them:
# `ix_news_market_wallet_fills_token_event_at` for the window, and PR-1's
# `(wallet, token, event_at_ms DESC)` for the "was this wallet already holding" anti-join. Which one
# the planner actually picks is its business -- on a small table either is cheap, and on a large one
# the point is that neither half degrades into a scan of the token's whole retained history, which is
# what finding each wallet's first buy without a lower bound would have cost.
_CROWDING_BUYERS_SQL: Final = """
WITH window_buys AS (
  SELECT wallet, event_at_ms, usd, amount_raw, token_decimals
    FROM news_market_wallet_fills
   WHERE chain_id = %(chain_id)s AND token = %(token)s AND kind = 'buy'
     AND event_at_ms >= %(from_ms)s AND event_at_ms <= %(to_ms)s
), firsts AS (
  SELECT DISTINCT ON (wallet)
         wallet, event_at_ms AS first_at_ms, usd AS first_usd,
         amount_raw AS first_amount_raw, token_decimals AS first_decimals
    FROM window_buys
   ORDER BY wallet, event_at_ms, usd
)
SELECT f.wallet, f.first_at_ms, f.first_usd, f.first_amount_raw, f.first_decimals,
       COALESCE(sum(b.usd), 0) AS window_usd
  FROM firsts f
  JOIN window_buys b ON b.wallet = f.wallet
 WHERE NOT EXISTS (
   SELECT 1 FROM news_market_wallet_fills held
    WHERE held.chain_id = %(chain_id)s AND held.token = %(token)s AND held.kind = 'buy'
      AND held.wallet = f.wallet AND held.event_at_ms < %(from_ms)s)
 GROUP BY f.wallet, f.first_at_ms, f.first_usd, f.first_amount_raw, f.first_decimals
 ORDER BY f.first_at_ms, f.wallet
"""

_INSERT_OUTCOME_SQL: Final = """
INSERT INTO news_market_wallet_outcomes (delivery_key, horizon, price, at_ms, source)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (delivery_key, horizon) DO NOTHING
"""

# Due price receipts: a sent wallet card whose horizon has passed and which has no row yet. The card's
# own settle time is the anchor, because the subject of a receipt is what the reader was told and when.
_DUE_OUTCOMES_SQL: Final = """
SELECT d.delivery_key, d.settled_at_ms, e.token
  FROM news_market_deliveries d
  JOIN news_market_wallet_events e ON e.item_id = d.trigger_item_id
  LEFT JOIN news_market_wallet_outcomes o
         ON o.delivery_key = d.delivery_key AND o.horizon = %(horizon)s
 WHERE d.market_kind = 'wallet'
   AND d.state = 'sent'
   AND d.settled_at_ms IS NOT NULL
   AND d.settled_at_ms + %(horizon_ms)s <= %(now_ms)s
   AND o.delivery_key IS NULL
 ORDER BY d.settled_at_ms
 LIMIT %(limit)s
"""


# --- the four-hourly digest (#572 PR-3) -----------------------------------------------------------
#
# Five statements, six times a day. They are deliberately not one query: the digest states five
# different kinds of fact -- the window's totals, what each wallet did, what each position costs, what
# the rules sent and what the receipts came back with -- and a single joined statement would have to
# fan one of them out across the others and then de-duplicate it in Python.

# Where the last digest ended, and how many model calls the last day has already spent. Bounded to the
# last day on purpose: a digest older than that starts a fresh window anyway, and the model-call budget
# is a rolling day, so a scan of the whole retention would answer a question nobody asked.
_LAST_DIGEST_SQL: Final = f"""
SELECT max(window_to_ms) AS window_to_ms,
       count(*) FILTER (WHERE (evidence ->> 'model_called') = 'true') AS model_calls
  FROM news_market_wallet_events
 WHERE kind = '{DIGEST_KIND}' AND window_to_ms >= %(since_ms)s
"""  # noqa: S608 -- the only interpolation is this repository's own code-owned kind literal

# When the tape last got as far as trying, whether or not the write that followed committed. It is on
# the tape's own state row rather than on a digest row for exactly that reason: a refused write leaves
# no digest row, and without this a broken write would call the model on every two-second turn.
_MARK_DIGEST_ATTEMPT_SQL: Final = """
INSERT INTO news_market_wallet_tape_state (state_id, updated_at_ms, digest_attempted_at_ms)
VALUES (%s, %s, %s)
ON CONFLICT (state_id) DO UPDATE SET
    digest_attempted_at_ms = GREATEST(news_market_wallet_tape_state.digest_attempted_at_ms,
                                      EXCLUDED.digest_attempted_at_ms)
"""

_DIGEST_ATTEMPTED_SQL: Final = """
SELECT digest_attempted_at_ms FROM news_market_wallet_tape_state WHERE state_id = %s
"""

# The window's own totals, and the chain the facts came from. A digest with no chain id has nothing to
# be a digest about, which is the same condition `DigestWindowRows.is_empty` reports.
_DIGEST_TOTALS_SQL: Final = """
SELECT count(DISTINCT token) AS tokens,
       max(chain_id) AS chain_id,
       count(*) FILTER (WHERE kind <> 'transfer_out' AND usd IS NULL) AS unpriced
  FROM news_market_wallet_fills
 WHERE event_at_ms >= %(from_ms)s AND event_at_ms < %(to_ms)s
"""

_DIGEST_ACTIVITY_SQL: Final = """
SELECT wallet,
       count(*) FILTER (WHERE kind = 'buy') AS buys,
       COALESCE(sum(usd) FILTER (WHERE kind = 'buy'), 0) AS buy_usd,
       count(*) FILTER (WHERE kind = 'sell') AS sells,
       COALESCE(sum(usd) FILTER (WHERE kind = 'sell'), 0) AS sell_usd,
       count(*) FILTER (WHERE kind = 'transfer_out') AS transfers_out,
       count(*) FILTER (WHERE kind <> 'transfer_out' AND usd IS NULL) AS unpriced,
       COALESCE(sum(usd), 0) AS window_usd
  FROM news_market_wallet_fills
 WHERE event_at_ms >= %(from_ms)s AND event_at_ms < %(to_ms)s
 GROUP BY wallet
 ORDER BY window_usd DESC, wallet
 LIMIT %(limit)s
"""

# The three cost bases are computed from these sums and nowhere else. The window halves answer "what
# did this position do in the last four hours"; the unfiltered halves are every fill still retained for
# the pair, which is what a net cash recovery line has to be measured against.
_DIGEST_FLOWS_SQL: Final = """
WITH active AS (
  SELECT DISTINCT wallet, token
    FROM news_market_wallet_fills
   WHERE event_at_ms >= %(from_ms)s AND event_at_ms < %(to_ms)s
     AND kind IN ('buy', 'sell')
)
SELECT f.wallet, f.token,
       COALESCE(max(f.token_symbol), '') AS token_symbol,
       max(f.token_decimals) AS token_decimals,
       COALESCE(sum(f.usd) FILTER (
         WHERE f.kind = 'buy' AND f.event_at_ms >= %(from_ms)s AND f.event_at_ms < %(to_ms)s), 0)
         AS window_buy_usd,
       COALESCE(sum(f.amount_raw) FILTER (
         WHERE f.kind = 'buy' AND f.event_at_ms >= %(from_ms)s AND f.event_at_ms < %(to_ms)s), 0)
         AS window_buy_raw,
       COALESCE(sum(f.usd) FILTER (
         WHERE f.kind = 'sell' AND f.event_at_ms >= %(from_ms)s AND f.event_at_ms < %(to_ms)s), 0)
         AS window_sell_usd,
       COALESCE(sum(f.usd) FILTER (WHERE f.kind = 'buy'), 0) AS lifetime_buy_usd,
       COALESCE(sum(f.usd) FILTER (WHERE f.kind = 'sell'), 0) AS lifetime_sell_usd,
       COALESCE(sum(f.amount_raw) FILTER (WHERE f.kind = 'buy'), 0) AS lifetime_buy_raw,
       COALESCE(sum(f.amount_raw) FILTER (WHERE f.kind = 'sell'), 0) AS lifetime_sell_raw,
       COALESCE(sum(f.amount_raw) FILTER (WHERE f.kind = 'transfer_out'), 0) AS lifetime_out_raw,
       COALESCE(sum(f.usd) FILTER (
         WHERE f.event_at_ms >= %(from_ms)s AND f.event_at_ms < %(to_ms)s), 0) AS window_usd
  FROM news_market_wallet_fills f
  JOIN active a ON a.wallet = f.wallet AND a.token = f.token
 GROUP BY f.wallet, f.token
 ORDER BY window_usd DESC, f.wallet, f.token
 LIMIT %(limit)s
"""

_DIGEST_CARDS_SQL: Final = f"""
SELECT e.kind, e.handle, COALESCE(e.token_symbol, '') AS token_symbol, e.ratio_bps, e.basis,
       e.peer_wallets, e.usd, e.position_usd, e.tone, e.chain_id,
       COALESCE(d.state = 'sent', false) AS sent
  FROM news_market_wallet_events e
  LEFT JOIN news_items i ON i.item_id = e.item_id
  LEFT JOIN news_market_deliveries d ON d.delivery_key = i.market_notify_delivery_key
 WHERE e.event_at_ms >= %(from_ms)s AND e.event_at_ms < %(to_ms)s AND e.kind <> '{DIGEST_KIND}'
 ORDER BY e.event_at_ms
 LIMIT %(limit)s
"""  # noqa: S608 -- the only interpolation is this repository's own code-owned kind literal

# What the price receipts that landed in this window said, against the price the card itself printed.
# `percentile_cont` skips the rows nothing could price, so `priced` is the honest denominator of the
# median beside it.
_DIGEST_OUTCOMES_SQL: Final = """
WITH receipts AS (
  SELECT o.horizon, o.price, COALESCE(e.mark_price, e.entry_price) AS reference
    FROM news_market_wallet_outcomes o
    JOIN news_market_deliveries d ON d.delivery_key = o.delivery_key
    JOIN news_market_wallet_events e ON e.item_id = d.trigger_item_id
   WHERE o.at_ms >= %(from_ms)s AND o.at_ms < %(to_ms)s
)
SELECT horizon,
       count(*) AS receipts,
       count(*) FILTER (WHERE price IS NOT NULL) AS priced,
       percentile_cont(0.5) WITHIN GROUP (
         ORDER BY CASE WHEN price IS NOT NULL AND reference IS NOT NULL AND reference > 0
                       THEN (price / reference - 1) * 10000 END) AS median_bps
  FROM receipts
 GROUP BY horizon
 ORDER BY horizon
"""


# --- the console read models (#572 PR-3) ----------------------------------------------------------
#
# `/api/news/wallets` is four bounded statements and `/api/news/wallets/cards` is one. Each of them
# starts at a typed fact table on an indexed field and joins to the Item only where the Item is what is
# being asked about, which is #570 A3's shape.

WALLET_ROSTER_ROWS_SQL: Final = """
SELECT roster_version, taken_at_ms, wallet, handle, followers, realized_pnl,
       closed_trades, win_rate, profit_factor, open_cost, rank_quality, rank_whale, provider
  FROM news_market_wallet_roster
 WHERE roster_version = (SELECT max(roster_version) FROM news_market_wallet_roster)
 ORDER BY COALESCE(rank_quality, 1000000), COALESCE(rank_whale, 1000000), wallet
"""

WALLET_FILLS_BY_KIND_SQL: Final = """
SELECT kind,
       count(*) AS fills,
       COALESCE(sum(usd), 0)::text AS usd,
       -- The same predicate the calibration query in OPERATIONS.md uses: a movement with no swap has
       -- no cash leg by construction, so counting it as unpriced would report the tape's own
       -- classification as a pricing failure.
       count(*) FILTER (WHERE kind <> 'transfer_out' AND usd IS NULL) AS unpriced,
       count(DISTINCT wallet) AS wallets,
       count(DISTINCT token) AS tokens
  FROM news_market_wallet_fills
 WHERE event_at_ms >= %(from_ms)s
 GROUP BY kind
 ORDER BY kind
"""

WALLET_CARDS_BY_KIND_SQL: Final = """
SELECT e.kind,
       count(*) AS cards,
       count(*) FILTER (WHERE d.state = 'sent') AS sent,
       max(e.event_at_ms) AS last_event_at_ms
  FROM news_market_wallet_events e
  LEFT JOIN news_items i ON i.item_id = e.item_id
  LEFT JOIN news_market_deliveries d ON d.delivery_key = i.market_notify_delivery_key
 WHERE e.event_at_ms >= %(from_ms)s
 GROUP BY e.kind
 ORDER BY e.kind
"""

# One page of cards with their receipts. The return is computed against the price the card itself
# printed -- the chain's mark at the moment it fired, or the lead's entry for a crowding window -- and
# clamped, because these pools print prices spanning thirty orders of magnitude and an unclamped ratio
# of two of them does not fit the integer it crosses the wire as.
WALLET_CARDS_SQL: Final = f"""
WITH cards AS (
  SELECT e.item_id, e.kind, e.handle, e.wallet, e.token, e.token_symbol, e.tone,
         e.ratio_bps, e.basis, e.closed, e.peer_wallets, e.premium_bps,
         e.usd, e.position_usd, e.entry_price, e.mark_price, e.evidence,
         e.event_at_ms, e.window_from_ms, e.window_to_ms,
         COALESCE(e.mark_price, e.entry_price) AS reference_price,
         i.market_notify_delivery_key AS delivery_key
    FROM news_market_wallet_events e
    LEFT JOIN news_items i ON i.item_id = e.item_id
   WHERE e.event_at_ms >= %(from_ms)s AND e.event_at_ms < %(to_ms)s
   ORDER BY e.event_at_ms DESC
   LIMIT %(limit)s
)
SELECT c.item_id, c.kind, c.handle, c.wallet, c.token, c.token_symbol, c.tone,
       c.ratio_bps, c.basis, c.closed, c.peer_wallets, c.premium_bps,
       c.usd::text AS usd, c.position_usd::text AS position_usd,
       c.entry_price::text AS entry_price, c.mark_price::text AS mark_price,
       c.event_at_ms, c.window_from_ms, c.window_to_ms,
       c.delivery_key, d.state AS delivery_state, d.settled_at_ms,
       o1.source AS outcome_1h_source,
       CASE WHEN o1.price IS NOT NULL AND c.reference_price > 0
            THEN LEAST(10000000, GREATEST(-10000000,
                 round((o1.price / c.reference_price - 1) * 10000)))::integer END AS return_1h_bps,
       o4.source AS outcome_4h_source,
       CASE WHEN o4.price IS NOT NULL AND c.reference_price > 0
            THEN LEAST(10000000, GREATEST(-10000000,
                 round((o4.price / c.reference_price - 1) * 10000)))::integer END AS return_4h_bps,
       CASE WHEN c.kind = '{DIGEST_KIND}' THEN (
              SELECT jsonb_agg(line ->> 'text' ORDER BY ord)
                FROM jsonb_array_elements(c.evidence -> 'lines') WITH ORDINALITY AS t(line, ord)
            ) END AS digest_lines,
       CASE WHEN c.kind = '{DIGEST_KIND}'
            THEN COALESCE((c.evidence ->> 'model_used') = 'true', false) END AS digest_model_used
  FROM cards c
  LEFT JOIN news_market_deliveries d ON d.delivery_key = c.delivery_key
  LEFT JOIN news_market_wallet_outcomes o1 ON o1.delivery_key = c.delivery_key AND o1.horizon = '1h'
  LEFT JOIN news_market_wallet_outcomes o4 ON o4.delivery_key = c.delivery_key AND o4.horizon = '4h'
 ORDER BY c.event_at_ms DESC
"""  # noqa: S608 -- the only interpolation is this repository's own code-owned kind literal


class DueOutcomeRow(TypedDict):
    """One price receipt this turn may take: which card, which horizon, and which token to price."""

    delivery_key: str
    horizon: OutcomeHorizon
    token: str
    expired: bool


class ChainTapeStateRow(TypedDict):
    """Where the tape got to, and what the last turn did there."""

    high_water_block: int
    high_water_tx_index: int
    roster_version: int
    last_outcome: str
    last_error: str | None
    last_success_at_ms: int | None
    updated_at_ms: int
    ignored_inbound_total: int
    unknown_total: int
    noise_through_block: int
    noise_through_tx_index: int


class ChainTapeStorage:
    conn: Any

    # ------------------------------------------------------------------ fills
    def chain_tape_record_fills(self, fills: Sequence[ClassifiedFill]) -> int:
        """Write classified fills idempotently; return how many rows were new.

        The chain assigned the identity, so a second delivery of the same movement is not an error and
        not an update: it is the same row, and `DO NOTHING` says exactly that.
        """

        written = 0
        for fill in fills:
            cursor = self.conn.execute(
                _INSERT_FILL_SQL,
                (
                    int(fill.chain_id),
                    str(fill.tx_hash),
                    int(fill.log_index),
                    int(fill.block_number),
                    str(fill.block_hash),
                    str(fill.wallet),
                    str(fill.token),
                    fill.token_symbol,
                    None if fill.token_decimals is None else int(fill.token_decimals),
                    str(fill.kind),
                    Decimal(int(fill.amount_raw)),
                    fill.cash_token,
                    None if fill.cash_amount_raw is None else Decimal(int(fill.cash_amount_raw)),
                    None if fill.cash_decimals is None else int(fill.cash_decimals),
                    fill.usd,
                    fill.usd_source,
                    int(fill.event_at_ms),
                    int(fill.received_at_ms),
                    int(fill.classified_at_ms),
                    int(fill.roster_version),
                    str(fill.provider or CHAIN_TAPE_PROVIDER),
                ),
            )
            written += int(cursor.rowcount or 0)
        return written

    def chain_tape_purge_fills(self, *, cutoff_ms: int, limit: int) -> int:
        """One bounded retention batch: fills whose block time is older than the cutoff."""

        cursor = self.conn.execute(_PURGE_FILLS_SQL, (int(cutoff_ms), max(1, int(limit))))
        return int(cursor.rowcount or 0)

    # ------------------------------------------------------------------ ingest position
    def chain_tape_state(self) -> ChainTapeStateRow | None:
        row = self.conn.execute(WALLET_TAPE_STATE_SQL, (TAPE_STATE_ID,)).fetchone()
        if row is None:
            return None
        return ChainTapeStateRow(
            high_water_block=int(row["high_water_block"]),
            high_water_tx_index=int(row["high_water_tx_index"]),
            roster_version=int(row["roster_version"]),
            last_outcome=str(row["last_outcome"] or ""),
            last_error=None if row["last_error"] is None else str(row["last_error"]),
            last_success_at_ms=None if row["last_success_at_ms"] is None else int(row["last_success_at_ms"]),
            updated_at_ms=int(row["updated_at_ms"]),
            ignored_inbound_total=int(row["ignored_inbound_total"] or 0),
            unknown_total=int(row["unknown_total"] or 0),
            noise_through_block=int(row["noise_through_block"] or 0),
            noise_through_tx_index=int(row["noise_through_tx_index"]),
        )

    def chain_tape_save_state(
        self,
        *,
        cursor: TapeCursor,
        roster_version: int,
        outcome: str,
        error: str | None,
        now_ms: int,
        succeeded: bool,
        ignored_inbound: int = 0,
        unknown: int = 0,
        noise_cursor: TapeCursor | None = None,
    ) -> None:
        """Record the classified position, the turn's outcome, and what it read but did not store.

        `last_success_at_ms` only moves forward on a successful turn: a failed turn must be able to say
        "the tape has not advanced since" without the operator reconstructing it from logs. The two
        noise counters add, because the question they answer is cumulative, and `noise_cursor` is how
        they stay counts of movements rather than of passes over them.
        """

        self.conn.execute(
            _SAVE_TAPE_STATE_SQL,
            (
                TAPE_STATE_ID,
                int(cursor.block_number),
                int(cursor.transaction_index),
                int(roster_version),
                str(outcome),
                error,
                int(now_ms) if succeeded else None,
                int(now_ms),
                max(0, int(ignored_inbound)),
                max(0, int(unknown)),
                0 if noise_cursor is None else max(0, int(noise_cursor.block_number)),
                -1 if noise_cursor is None else max(-1, int(noise_cursor.transaction_index)),
            ),
        )

    # ------------------------------------------------------------------ derived observations (PR-2)
    def chain_tape_record_check(self, check: WalletCheck) -> None:
        """Record one verification attempt against one sell, whatever it proved. First answer wins."""

        self.conn.execute(
            _INSERT_WALLET_CHECK_SQL,
            (
                int(check.chain_id),
                str(check.tx_hash),
                int(check.log_index),
                str(check.basis),
                None if check.q_before_raw is None else Decimal(int(check.q_before_raw)),
                Decimal(int(check.q_sell_raw)),
                None if check.ratio_bps is None else int(check.ratio_bps),
                str(check.block_hash or ""),
                int(check.checked_at_ms),
                check.error,
            ),
        )

    def chain_tape_insert_wallet_event(self, event: WalletEvent) -> bool:
        """Write one derived observation beside its Item; return whether the row was new.

        Idempotent on the Item's identity, so a turn that is replayed writes one row. The Item itself is
        written by `admit_market_item`, in the same transaction as this and by the same rule the other
        market kinds follow -- there is no second admission path.
        """

        cursor = self.conn.execute(
            _INSERT_WALLET_EVENT_SQL,
            (
                str(event.item_id),
                str(event.kind),
                str(event.provider),
                int(event.chain_id),
                str(event.wallet),
                str(event.handle or ""),
                int(event.followers or 0),
                str(event.token),
                event.token_symbol,
                None if event.token_decimals is None else int(event.token_decimals),
                int(event.roster_version),
                int(event.window_from_ms),
                int(event.window_to_ms),
                str(event.segment_key),
                str(event.tone or ""),
                None if event.ratio_bps is None else int(event.ratio_bps),
                event.basis,
                None if event.quantity_raw is None else Decimal(int(event.quantity_raw)),
                None if event.balance_before_raw is None else Decimal(int(event.balance_before_raw)),
                event.usd,
                event.position_usd,
                event.entry_price,
                event.mark_price,
                int(event.peer_wallets or 0),
                event.peer_usd,
                None if event.premium_bps is None else int(event.premium_bps),
                event.liquidity_usd,
                event.tx_hash,
                None if event.block_number is None else int(event.block_number),
                bool(event.closed),
                _dumps(dict(event.evidence or {})),
                int(event.event_at_ms),
                int(event.received_at_ms),
                int(event.received_at_ms),
            ),
        )
        return bool(cursor.rowcount)

    def chain_tape_last_exit(self, *, chain_id: int, wallet: str, token: str) -> PreviousExit | None:
        """The last exit card for this wallet and token, which is what decides segment and follow-up."""

        row = self.conn.execute(_LAST_EXIT_SQL, (int(chain_id), str(wallet), str(token))).fetchone()
        if row is None:
            return None
        return PreviousExit(
            segment_key=str(row["segment_key"]),
            ratio_bps=int(row["ratio_bps"] or 0),
            closed=bool(row["closed"]),
        )

    def chain_tape_last_crowding(self, *, chain_id: int, token: str) -> PreviousCrowding | None:
        """The last crowding card for this token: its window, and how many wallets it counted."""

        row = self.conn.execute(_LAST_CROWDING_SQL, (int(chain_id), str(token))).fetchone()
        if row is None:
            return None
        return PreviousCrowding(
            window_from_ms=int(row["window_from_ms"]),
            window_to_ms=int(row["window_to_ms"]),
            buyers=int(row["peer_wallets"] or 0),
        )

    def chain_tape_recent_crowding_item(self, *, chain_id: int, token: str, since_ms: int) -> str | None:
        """The crowding card an exit in the same token should point back at, if there is a recent one."""

        row = self.conn.execute(_RECENT_CROWDING_ITEM_SQL, (int(chain_id), str(token), int(since_ms))).fetchone()
        return None if row is None else str(row["item_id"])

    def chain_tape_cascade_buys(
        self, *, chain_id: int, token: str, exclude_wallet: str, from_ms: int, to_ms: int
    ) -> tuple[int, Decimal]:
        """How many *other* roster wallets bought this token in the window, and for how much."""

        row = self.conn.execute(
            _CASCADE_BUYS_SQL, (int(chain_id), str(token), str(exclude_wallet), int(from_ms), int(to_ms))
        ).fetchone()
        if row is None:
            return 0, Decimal(0)
        return int(row["wallets"] or 0), Decimal(row["usd"] or 0)

    def chain_tape_crowding_buyers(
        self, *, chain_id: int, token: str, from_ms: int, to_ms: int
    ) -> tuple[CrowdingBuyer, ...]:
        """The wallets that opened a position in this token inside the window, with their entry price.

        The price is the wallet's first buy in dollars over its quantity in the token's own units -- the
        two numbers already on the fill. A fill the cash leg could not price, or a token that answered no
        `decimals`, carries no price and simply does not contribute to the premium median.
        """

        rows = self.conn.execute(
            _CROWDING_BUYERS_SQL,
            {"chain_id": int(chain_id), "token": str(token), "from_ms": int(from_ms), "to_ms": int(to_ms)},
        ).fetchall()
        return tuple(
            CrowdingBuyer(
                wallet=str(row["wallet"]),
                first_at_ms=int(row["first_at_ms"]),
                usd=Decimal(row["window_usd"] or 0),
                price=_unit_price(row["first_usd"], row["first_amount_raw"], row["first_decimals"]),
            )
            for row in rows
        )

    # ------------------------------------------------------------------ price receipts (PR-2)
    def chain_tape_due_outcomes(self, *, now_ms: int, limit: int) -> list[DueOutcomeRow]:
        """Sent wallet cards whose horizon has passed and which have no receipt yet, oldest first.

        The budget is split evenly across the horizons rather than taken first-come. A single query
        ordered by send time would hand every slot to the older horizon whenever there is a backlog on
        it, and the newer one would never be reached -- so a token nothing can price would not merely
        be late, it would hold the four-hour receipts of every card behind it.
        """

        share = max(1, int(limit) // max(1, len(WALLET_OUTCOME_HORIZONS)))
        due: list[DueOutcomeRow] = []
        for horizon, horizon_ms in WALLET_OUTCOME_HORIZONS:
            rows = self.conn.execute(
                _DUE_OUTCOMES_SQL,
                {"horizon": horizon, "horizon_ms": int(horizon_ms), "now_ms": int(now_ms), "limit": share},
            ).fetchall()
            due.extend(
                DueOutcomeRow(
                    delivery_key=str(row["delivery_key"]),
                    horizon=horizon,
                    token=str(row["token"]),
                    # A horizon that is more than the grace period late is banked as `unavailable`: a
                    # price read long after the mark is not that mark's price, and a row that is never
                    # banked occupies the budget for as long as it stays unpriceable.
                    expired=int(now_ms) - (int(row["settled_at_ms"]) + int(horizon_ms)) >= OUTCOME_GIVE_UP_MS,
                )
                for row in rows
            )
        return due

    def chain_tape_record_outcome(self, outcome: WalletOutcome) -> bool:
        """Write one price receipt. First writer wins: a receipt is about one instant, not the latest."""

        cursor = self.conn.execute(
            _INSERT_OUTCOME_SQL,
            (
                str(outcome.delivery_key),
                str(outcome.horizon),
                outcome.price,
                int(outcome.at_ms),
                str(outcome.source),
            ),
        )
        return bool(cursor.rowcount)

    # ------------------------------------------------------------------ the digest (PR-3)
    def chain_tape_last_digest(self, *, since_ms: int) -> LastDigest | None:
        """Where the last digest ended, when one was last attempted, and the day's spent model calls.

        `None` means "nothing in the last day and no attempt on record", which is also the answer the
        caller wants when the last digest is older than that: the window it would have to cover is
        capped at a day anyway, and a day with no digest in it has spent nothing.

        The attempt clock is read from the tape's own state row rather than from a digest row, because
        the case it exists for is the one where no digest row was written.
        """

        row = self.conn.execute(_LAST_DIGEST_SQL, {"since_ms": int(since_ms)}).fetchone()
        marker = self.conn.execute(_DIGEST_ATTEMPTED_SQL, (TAPE_STATE_ID,)).fetchone()
        attempted = 0 if marker is None else int(marker["digest_attempted_at_ms"] or 0)
        written = None if row is None or row["window_to_ms"] is None else int(row["window_to_ms"])
        if written is None and attempted <= 0:
            return None
        return LastDigest(
            window_to_ms=written or 0,
            model_calls_last_day=0 if row is None else int(row["model_calls"] or 0),
            attempted_at_ms=attempted,
        )

    def chain_tape_mark_digest_attempt(self, *, now_ms: int) -> None:
        """Bank the attempt before the model is called, so a refused write cannot loop on it.

        Monotonic, and an upsert rather than an update: the tape writes its state row on every turn,
        but a database seeded with fills alone has no row yet and the marker must still land.
        """

        self.conn.execute(_MARK_DIGEST_ATTEMPT_SQL, (TAPE_STATE_ID, int(now_ms), int(now_ms)))

    def chain_tape_digest_window(self, *, from_ms: int, to_ms: int) -> DigestWindowRows:
        """Everything one digest states, read in one checkout and computed by nothing but SQL."""

        window = {"from_ms": int(from_ms), "to_ms": int(to_ms)}
        totals = self.conn.execute(_DIGEST_TOTALS_SQL, window).fetchone()
        chain_id = 0 if totals is None or totals["chain_id"] is None else int(totals["chain_id"])
        activity = tuple(
            WalletWindowActivity(
                wallet=str(row["wallet"]),
                buys=int(row["buys"] or 0),
                buy_usd=Decimal(row["buy_usd"] or 0),
                sells=int(row["sells"] or 0),
                sell_usd=Decimal(row["sell_usd"] or 0),
                transfers_out=int(row["transfers_out"] or 0),
                unpriced=int(row["unpriced"] or 0),
            )
            for row in self.conn.execute(_DIGEST_ACTIVITY_SQL, {**window, "limit": DIGEST_WALLETS_MAX}).fetchall()
        )
        flows = tuple(
            TokenWindowFlow(
                wallet=str(row["wallet"]),
                token=str(row["token"]),
                token_symbol=str(row["token_symbol"] or ""),
                token_decimals=None if row["token_decimals"] is None else int(row["token_decimals"]),
                window_buy_usd=Decimal(row["window_buy_usd"] or 0),
                window_buy_raw=int(row["window_buy_raw"] or 0),
                window_sell_usd=Decimal(row["window_sell_usd"] or 0),
                lifetime_buy_usd=Decimal(row["lifetime_buy_usd"] or 0),
                lifetime_sell_usd=Decimal(row["lifetime_sell_usd"] or 0),
                lifetime_buy_raw=int(row["lifetime_buy_raw"] or 0),
                lifetime_sell_raw=int(row["lifetime_sell_raw"] or 0),
                lifetime_out_raw=int(row["lifetime_out_raw"] or 0),
            )
            for row in self.conn.execute(_DIGEST_FLOWS_SQL, {**window, "limit": DIGEST_COSTS_MAX}).fetchall()
        )
        card_rows = self.conn.execute(_DIGEST_CARDS_SQL, {**window, "limit": DIGEST_CARDS_MAX}).fetchall()
        if chain_id <= 0 and card_rows:
            # The fills a card was derived from can age out from under it -- they are on a 90-day
            # retention the derived rows do not share -- so the card is the second place the chain the
            # window's facts came from can be read.
            chain_id = int(card_rows[0]["chain_id"] or 0)
        cards = tuple(
            DigestCardRow(
                kind=str(row["kind"]),
                handle=str(row["handle"] or ""),
                symbol=str(row["token_symbol"] or ""),
                ratio_bps=None if row["ratio_bps"] is None else int(row["ratio_bps"]),
                basis=None if row["basis"] is None else str(row["basis"]),
                peer_wallets=int(row["peer_wallets"] or 0),
                usd=None if row["usd"] is None else Decimal(row["usd"]),
                position_usd=None if row["position_usd"] is None else Decimal(row["position_usd"]),
                tone=str(row["tone"] or ""),
                sent=bool(row["sent"]),
            )
            for row in card_rows
        )
        outcomes = tuple(
            DigestOutcomeRow(
                horizon=str(row["horizon"]),
                receipts=int(row["receipts"] or 0),
                priced=int(row["priced"] or 0),
                median_bps=None if row["median_bps"] is None else round(float(row["median_bps"])),
            )
            for row in self.conn.execute(_DIGEST_OUTCOMES_SQL, window).fetchall()
        )
        return DigestWindowRows(
            chain_id=chain_id,
            activity=activity,
            flows=flows,
            cards=cards,
            outcomes=outcomes,
            tokens=0 if totals is None else int(totals["tokens"] or 0),
            unpriced=0 if totals is None else int(totals["unpriced"] or 0),
        )

    # ------------------------------------------------------------------ the console page (PR-3)
    def chain_tape_roster_rows(self) -> list[dict[str, Any]]:
        """The current roster version as the page publishes it: who is followed, and why."""

        return [
            {
                "roster_version": int(row["roster_version"]),
                "taken_at_ms": int(row["taken_at_ms"]),
                "wallet": str(row["wallet"]),
                "handle": str(row["handle"] or ""),
                "followers": int(row["followers"] or 0),
                "realized_pnl": float(row["realized_pnl"] or 0.0),
                "closed_trades": int(row["closed_trades"] or 0),
                "win_rate": float(row["win_rate"] or 0.0),
                "profit_factor": None if row["profit_factor"] is None else float(row["profit_factor"]),
                "open_cost": float(row["open_cost"] or 0.0),
                "rank_quality": None if row["rank_quality"] is None else int(row["rank_quality"]),
                "rank_whale": None if row["rank_whale"] is None else int(row["rank_whale"]),
                "provider": str(row["provider"] or ROSTER_PROVIDER),
            }
            for row in self.conn.execute(WALLET_ROSTER_ROWS_SQL).fetchall()
        ]

    def chain_tape_fill_totals(self, *, from_ms: int) -> list[dict[str, Any]]:
        """What the tape stored in the window, per kind. `unpriced` is the share nothing could price."""

        return [
            {
                "kind": str(row["kind"]),
                "fills": int(row["fills"] or 0),
                "usd": str(row["usd"] or "0"),
                "unpriced": int(row["unpriced"] or 0),
                "wallets": int(row["wallets"] or 0),
                "tokens": int(row["tokens"] or 0),
            }
            for row in self.conn.execute(WALLET_FILLS_BY_KIND_SQL, {"from_ms": int(from_ms)}).fetchall()
        ]

    def chain_tape_card_totals(self, *, from_ms: int) -> list[dict[str, Any]]:
        """What the rules opened in the window, per kind, and how much of it a reader received."""

        return [
            {
                "kind": str(row["kind"]),
                "cards": int(row["cards"] or 0),
                "sent": int(row["sent"] or 0),
                "last_event_at_ms": None if row["last_event_at_ms"] is None else int(row["last_event_at_ms"]),
            }
            for row in self.conn.execute(WALLET_CARDS_BY_KIND_SQL, {"from_ms": int(from_ms)}).fetchall()
        ]

    def chain_tape_cards(self, *, from_ms: int, to_ms: int, limit: int) -> list[dict[str, Any]]:
        """One bounded page of wallet cards, each beside the two price receipts taken for it."""

        rows = self.conn.execute(
            WALLET_CARDS_SQL,
            {"from_ms": int(from_ms), "to_ms": int(to_ms), "limit": max(1, int(limit))},
        ).fetchall()
        return [
            {
                "item_id": str(row["item_id"]),
                "kind": str(row["kind"]),
                "handle": str(row["handle"] or ""),
                "wallet": str(row["wallet"] or ""),
                "token": str(row["token"] or ""),
                "token_symbol": None if row["token_symbol"] is None else str(row["token_symbol"]),
                "tone": str(row["tone"] or ""),
                "ratio_bps": None if row["ratio_bps"] is None else int(row["ratio_bps"]),
                "basis": None if row["basis"] is None else str(row["basis"]),
                "closed": bool(row["closed"]),
                "peer_wallets": int(row["peer_wallets"] or 0),
                "premium_bps": None if row["premium_bps"] is None else int(row["premium_bps"]),
                "usd": None if row["usd"] is None else str(row["usd"]),
                "position_usd": None if row["position_usd"] is None else str(row["position_usd"]),
                "entry_price": None if row["entry_price"] is None else str(row["entry_price"]),
                "mark_price": None if row["mark_price"] is None else str(row["mark_price"]),
                "event_at_ms": int(row["event_at_ms"]),
                "window_from_ms": int(row["window_from_ms"]),
                "window_to_ms": int(row["window_to_ms"]),
                "delivery_key": None if row["delivery_key"] is None else str(row["delivery_key"]),
                "delivery_state": None if row["delivery_state"] is None else str(row["delivery_state"]),
                "settled_at_ms": None if row["settled_at_ms"] is None else int(row["settled_at_ms"]),
                "outcome_1h_source": None if row["outcome_1h_source"] is None else str(row["outcome_1h_source"]),
                "return_1h_bps": None if row["return_1h_bps"] is None else int(row["return_1h_bps"]),
                "outcome_4h_source": None if row["outcome_4h_source"] is None else str(row["outcome_4h_source"]),
                "return_4h_bps": None if row["return_4h_bps"] is None else int(row["return_4h_bps"]),
                "digest_lines": None if row["digest_lines"] is None else [str(line) for line in row["digest_lines"]],
                "digest_model_used": None if row["digest_model_used"] is None else bool(row["digest_model_used"]),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------ roster
    def chain_tape_current_roster(self) -> RosterSnapshot | None:
        rows = self.conn.execute(_CURRENT_ROSTER_SQL).fetchall()
        if not rows:
            return None
        members = tuple(
            RosterMember(
                wallet=str(row["wallet"]),
                handle=str(row["handle"] or ""),
                followers=int(row["followers"] or 0),
                realized_pnl=float(row["realized_pnl"] or 0.0),
                closed_trades=int(row["closed_trades"] or 0),
                win_rate=float(row["win_rate"] or 0.0),
                profit_factor=None if row["profit_factor"] is None else float(row["profit_factor"]),
                open_cost=float(row["open_cost"] or 0.0),
                rank_quality=None if row["rank_quality"] is None else int(row["rank_quality"]),
                rank_whale=None if row["rank_whale"] is None else int(row["rank_whale"]),
            )
            for row in rows
        )
        return RosterSnapshot(
            roster_version=int(rows[0]["roster_version"]),
            taken_at_ms=int(rows[0]["taken_at_ms"]),
            members=members,
            provider=str(rows[0]["provider"] or ROSTER_PROVIDER),
        )

    def chain_tape_store_roster(
        self,
        members: Sequence[RosterMember],
        *,
        now_ms: int,
    ) -> RosterSnapshot:
        """Version the roster only when the list itself moved; otherwise re-stamp the current one.

        The comparison is membership plus ranks. Follower counts and P&L are recorded on every version
        and never open one: they change hourly, and a version that changed hourly could not answer
        "which list was this wallet on when the card fired".
        """

        proposed = RosterSnapshot(roster_version=0, taken_at_ms=int(now_ms), members=tuple(members))
        current = self.chain_tape_current_roster()
        if current is not None and current.membership_key() == proposed.membership_key():
            self.conn.execute(_TOUCH_ROSTER_SQL, (int(now_ms), current.roster_version))
            return RosterSnapshot(
                roster_version=current.roster_version,
                taken_at_ms=int(now_ms),
                members=current.members,
                provider=current.provider,
            )
        version = 1 if current is None else current.roster_version + 1
        for member in members:
            self.conn.execute(
                _INSERT_ROSTER_MEMBER_SQL,
                (
                    version,
                    int(now_ms),
                    str(member.wallet),
                    str(member.handle or ""),
                    int(member.followers),
                    float(member.realized_pnl),
                    int(member.closed_trades),
                    float(member.win_rate),
                    None if member.profit_factor is None else float(member.profit_factor),
                    float(member.open_cost),
                    None if member.rank_quality is None else int(member.rank_quality),
                    None if member.rank_whale is None else int(member.rank_whale),
                    ROSTER_PROVIDER,
                ),
            )
        return RosterSnapshot(roster_version=version, taken_at_ms=int(now_ms), members=tuple(members))


def _unit_price(usd: Any, amount_raw: Any, decimals: Any) -> Decimal | None:
    """One fill's dollars per token, or `None` when the fill carried no price to divide.

    Decimal throughout: the numerator is a stored `numeric` and the denominator is a raw integer scaled
    by the token's own decimals, and a float division of an 18-decimal quantity is not the same number.
    """

    if usd is None or amount_raw is None or decimals is None:
        return None
    quantity = Decimal(int(amount_raw)) / (Decimal(10) ** int(decimals))
    if quantity <= 0:
        return None
    return Decimal(usd) / quantity


__all__ = [
    "TAPE_STATE_ID",
    "WALLET_CARDS_BY_KIND_SQL",
    "WALLET_CARDS_SQL",
    "WALLET_FILLS_BY_KIND_SQL",
    "WALLET_ROSTER_ROWS_SQL",
    "WALLET_TAPE_STATE_SQL",
    "ChainTapeStateRow",
    "ChainTapeStorage",
    "DueOutcomeRow",
]
