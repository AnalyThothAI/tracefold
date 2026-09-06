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
from ..chain_tape.rules import CrowdingBuyer, PreviousCrowding, PreviousExit
from ..wallet_contracts import (
    OUTCOME_GIVE_UP_MS,
    WALLET_OUTCOME_HORIZONS,
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

_TAPE_STATE_SQL: Final = """
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


class DueOutcomeRow(TypedDict):
    """One price receipt this turn may take: which card, which horizon, and which token to price."""

    delivery_key: str
    horizon: str
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
        row = self.conn.execute(_TAPE_STATE_SQL, (TAPE_STATE_ID,)).fetchone()
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


__all__ = ["TAPE_STATE_ID", "ChainTapeStateRow", "ChainTapeStorage", "DueOutcomeRow"]
