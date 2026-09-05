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
       last_outcome, last_error, last_success_at_ms, updated_at_ms
  FROM news_market_wallet_tape_state
 WHERE state_id = %s
"""

_SAVE_TAPE_STATE_SQL: Final = """
INSERT INTO news_market_wallet_tape_state (
    state_id, high_water_block, high_water_tx_index, roster_version,
    last_outcome, last_error, last_success_at_ms, updated_at_ms
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (state_id) DO UPDATE SET
    high_water_block = EXCLUDED.high_water_block,
    high_water_tx_index = EXCLUDED.high_water_tx_index,
    roster_version = EXCLUDED.roster_version,
    last_outcome = EXCLUDED.last_outcome,
    last_error = EXCLUDED.last_error,
    last_success_at_ms = COALESCE(EXCLUDED.last_success_at_ms,
                                  news_market_wallet_tape_state.last_success_at_ms),
    updated_at_ms = EXCLUDED.updated_at_ms
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


class ChainTapeStateRow(TypedDict):
    """Where the tape got to, and what the last turn did there."""

    high_water_block: int
    high_water_tx_index: int
    roster_version: int
    last_outcome: str
    last_error: str | None
    last_success_at_ms: int | None
    updated_at_ms: int


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
    ) -> None:
        """Record the classified position and the turn's outcome as one row.

        `last_success_at_ms` only moves forward on a successful turn: a failed turn must be able to say
        "the tape has not advanced since" without the operator reconstructing it from logs.
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
            ),
        )

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


__all__ = ["TAPE_STATE_ID", "ChainTapeStateRow", "ChainTapeStorage"]
