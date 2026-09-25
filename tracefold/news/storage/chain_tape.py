"""Receipt ledger, collection coverage and versioned roster storage."""

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

WALLET_TAPE_STATE_SQL: Final = """
SELECT high_water_block, high_water_tx_index, roster_version,
       last_outcome, last_error, last_success_at_ms, updated_at_ms,
       ignored_inbound_total, unknown_total,
       noise_through_block, noise_through_tx_index, detection_cutover_at_ms,
       coverage_from_ms, scanned_at_ms, scanned_block, scanned_log, gap_at_ms,
       roster_last_attempt_at_ms, roster_last_success_at_ms, roster_last_error,
       roster_next_attempt_at_ms, roster_consecutive_failures,
       next_attempt_at_ms, consecutive_failures, blocked_tx_hash, enrichment_error
  FROM news_market_wallet_tape_state
 WHERE state_id = %s
"""

_SAVE_TAPE_STATE_SQL: Final = """
INSERT INTO news_market_wallet_tape_state (
    state_id, high_water_block, high_water_tx_index, roster_version,
    last_outcome, last_error, last_success_at_ms, updated_at_ms,
    ignored_inbound_total, unknown_total,
    noise_through_block, noise_through_tx_index,
    next_attempt_at_ms, consecutive_failures, blocked_tx_hash, enrichment_error
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (state_id) DO UPDATE SET
    high_water_block = EXCLUDED.high_water_block,
    high_water_tx_index = EXCLUDED.high_water_tx_index,
    roster_version = EXCLUDED.roster_version,
    next_attempt_at_ms = EXCLUDED.next_attempt_at_ms,
    consecutive_failures = EXCLUDED.consecutive_failures,
    blocked_tx_hash = EXCLUDED.blocked_tx_hash,
    enrichment_error = EXCLUDED.enrichment_error,
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

# The roster refresh task's own result, recorded whether or not a version was published. It is an
# upsert because the refresh can legitimately run before the collector has ever written the row, and a
# refresh failure that could not be recorded is the failure this whole flow exists to make visible.
_SAVE_ROSTER_REFRESH_SQL: Final = """
INSERT INTO news_market_wallet_tape_state (
    state_id, high_water_block, high_water_tx_index, roster_version,
    last_outcome, updated_at_ms,
    roster_last_attempt_at_ms, roster_last_success_at_ms, roster_last_error,
    roster_next_attempt_at_ms, roster_consecutive_failures
) VALUES (%s, 0, -1, 0, '', %s, %s, %s, %s, %s, %s)
ON CONFLICT (state_id) DO UPDATE SET
    updated_at_ms = EXCLUDED.updated_at_ms,
    roster_last_attempt_at_ms = EXCLUDED.roster_last_attempt_at_ms,
    -- A failed attempt may never move the success stamp: "the last complete list was taken at" is
    -- the one figure an operator uses to decide whether the published roster is still the truth.
    roster_last_success_at_ms = COALESCE(EXCLUDED.roster_last_success_at_ms,
                                         news_market_wallet_tape_state.roster_last_success_at_ms),
    roster_last_error = EXCLUDED.roster_last_error,
    roster_next_attempt_at_ms = EXCLUDED.roster_next_attempt_at_ms,
    roster_consecutive_failures = EXCLUDED.roster_consecutive_failures
"""

_CURRENT_ROSTER_SQL: Final = """
SELECT roster_version, taken_at_ms, wallet, handle, provider
  FROM news_market_wallet_roster
 WHERE roster_version = (SELECT max(roster_version) FROM news_market_wallet_roster)
 ORDER BY wallet
"""

_INSERT_ROSTER_MEMBER_SQL: Final = """
INSERT INTO news_market_wallet_roster (
    roster_version, taken_at_ms, wallet, handle, provider, known_at_ms, monitoring_from_ms
) VALUES (%s, %s, %s, %s, %s, %s, %s)
"""

_PURGE_FILLS_SQL: Final = """
DELETE FROM news_market_wallet_fills
 WHERE ctid = ANY (ARRAY(
       SELECT ctid FROM news_market_wallet_fills
        WHERE event_at_ms < %s
        ORDER BY event_at_ms
        LIMIT %s))
"""

WALLET_ROSTER_ROWS_SQL: Final = """
SELECT roster_version, taken_at_ms, wallet, handle, provider, monitoring_from_ms
  FROM news_market_wallet_roster
 WHERE roster_version = (SELECT max(roster_version) FROM news_market_wallet_roster)
 ORDER BY wallet
"""

# One statement pins current membership, sweep addresses and progress to one MVCC
# snapshot. Workers already own the transaction; nesting SET TRANSACTION after the
# worker's timeout setup SELECT would fail in production.
_COLLECTION_PLAN_SQL: Final = f"""
WITH state AS ({WALLET_TAPE_STATE_SQL}), versions AS (
    SELECT roster_version, lead(min(known_at_ms)) OVER (ORDER BY roster_version) AS next_at_ms
      FROM news_market_wallet_roster GROUP BY roster_version
), current_roster AS (
    SELECT roster_version, taken_at_ms, wallet, handle, provider
      FROM news_market_wallet_roster
     WHERE roster_version = (SELECT max(roster_version) FROM versions)
), wallets AS (
    SELECT DISTINCT r.wallet FROM news_market_wallet_roster r JOIN versions v USING (roster_version)
     WHERE v.next_at_ms IS NULL OR (r.monitoring_from_ms IS NOT NULL AND
         v.next_at_ms > COALESCE((SELECT scanned_at_ms FROM state), 0) - 1800000)
)
SELECT (SELECT to_jsonb(s) FROM state s) AS state,
       COALESCE((SELECT jsonb_agg(to_jsonb(r) ORDER BY r.wallet) FROM current_roster r), '[]') AS roster,
       ARRAY(SELECT wallet FROM wallets ORDER BY wallet) AS wallets
"""  # noqa: S608 -- interpolates only the code-owned production state SELECT.


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
    detection_cutover_at_ms: int
    coverage_from_ms: int | None
    scanned_at_ms: int | None
    scanned_block: int | None
    scanned_log: int | None
    gap_at_ms: int | None
    roster_last_attempt_at_ms: int | None
    roster_last_success_at_ms: int | None
    roster_last_error: str | None
    roster_next_attempt_at_ms: int
    roster_consecutive_failures: int
    next_attempt_at_ms: int
    consecutive_failures: int
    blocked_tx_hash: str | None
    enrichment_error: str | None


class ChainTapeStorage:
    conn: Any

    def chain_tape_collection_plan(self) -> tuple[ChainTapeStateRow | None, RosterSnapshot | None, tuple[str, ...]]:
        row = self.conn.execute(_COLLECTION_PLAN_SQL, (TAPE_STATE_ID,)).fetchone()
        members = row["roster"]
        roster = (
            None
            if not members
            else RosterSnapshot(
                roster_version=members[0]["roster_version"],
                taken_at_ms=members[0]["taken_at_ms"],
                members=tuple(RosterMember(wallet=m["wallet"], handle=m["handle"]) for m in members),
                provider=members[0]["provider"],
            )
        )
        return row["state"], roster, tuple(row["wallets"])

    def chain_tape_roster_version(self, version: int) -> RosterSnapshot | None:
        rows = self.conn.execute(
            "SELECT roster_version, taken_at_ms, wallet, handle FROM news_market_wallet_roster "
            "WHERE roster_version = %s ORDER BY wallet",
            (int(version),),
        ).fetchall()
        if not rows:
            return None
        return RosterSnapshot(
            roster_version=int(version),
            taken_at_ms=int(rows[0]["taken_at_ms"]),
            members=tuple(RosterMember(**{name: r[name] for name in RosterMember.__dataclass_fields__}) for r in rows),
        )

    def chain_tape_overlap_fills(
        self, *, chain_id: int, from_block: int, to_block: int, wallets: Sequence[str]
    ) -> list[dict[str, Any]]:
        return list(
            self.conn.execute(
                """
            SELECT tx_hash, log_index, block_hash FROM news_market_wallet_fills
             WHERE chain_id = %s AND block_number BETWEEN %s AND %s AND wallet = ANY(%s)
        """,
                (chain_id, from_block, to_block, list(wallets)),
            ).fetchall()
        )

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

    def chain_tape_state(self, *, for_share: bool = False) -> ChainTapeStateRow | None:
        sql = WALLET_TAPE_STATE_SQL + (" FOR SHARE" if for_share else "")
        row = self.conn.execute(sql, (TAPE_STATE_ID,)).fetchone()
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
            detection_cutover_at_ms=int(row["detection_cutover_at_ms"]),
            coverage_from_ms=row["coverage_from_ms"],
            scanned_at_ms=row["scanned_at_ms"],
            scanned_block=row["scanned_block"],
            scanned_log=row["scanned_log"],
            gap_at_ms=row["gap_at_ms"],
            roster_last_attempt_at_ms=row["roster_last_attempt_at_ms"],
            roster_last_success_at_ms=row["roster_last_success_at_ms"],
            roster_last_error=row["roster_last_error"],
            roster_next_attempt_at_ms=int(row["roster_next_attempt_at_ms"]),
            roster_consecutive_failures=int(row["roster_consecutive_failures"]),
            next_attempt_at_ms=int(row["next_attempt_at_ms"]),
            consecutive_failures=int(row["consecutive_failures"]),
            blocked_tx_hash=row["blocked_tx_hash"],
            enrichment_error=row["enrichment_error"],
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
        next_attempt_at_ms: int = 0,
        consecutive_failures: int = 0,
        blocked_tx_hash: str | None = None,
        enrichment_error: str | None = None,
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
                next_attempt_at_ms,
                consecutive_failures,
                blocked_tx_hash,
                enrichment_error,
            ),
        )

    def chain_tape_begin_roster_refresh(self, *, now_ms: int, next_attempt_at_ms: int) -> None:
        """An in-flight request is an attempt, never a success or a fabricated failure."""
        self.conn.execute(
            """
            INSERT INTO news_market_wallet_tape_state (
                state_id, high_water_block, high_water_tx_index, roster_version, last_outcome,
                updated_at_ms, roster_last_attempt_at_ms, roster_next_attempt_at_ms
            ) VALUES ('chain_tape', 0, -1, 0, '', %s, %s, %s)
            ON CONFLICT (state_id) DO UPDATE SET
                roster_last_attempt_at_ms = EXCLUDED.roster_last_attempt_at_ms,
                roster_next_attempt_at_ms = EXCLUDED.roster_next_attempt_at_ms
        """,
            (now_ms, now_ms, next_attempt_at_ms),
        )

    def chain_tape_save_roster_refresh(
        self,
        *,
        now_ms: int,
        succeeded: bool,
        error: str | None,
        completed_at_ms: int | None = None,
        next_attempt_at_ms: int = 0,
        consecutive_failures: int = 0,
    ) -> None:
        self.conn.execute(
            _SAVE_ROSTER_REFRESH_SQL,
            (
                TAPE_STATE_ID,
                completed_at_ms or now_ms,
                now_ms,
                (completed_at_ms or now_ms) if succeeded else None,
                None if succeeded else (error or "roster_refresh_failed"),
                next_attempt_at_ms,
                consecutive_failures,
            ),
        )

    def chain_tape_roster_rows(self) -> list[dict[str, Any]]:
        return list(self.conn.execute(WALLET_ROSTER_ROWS_SQL).fetchall())

    def chain_tape_current_roster(self) -> RosterSnapshot | None:
        rows = self.conn.execute(_CURRENT_ROSTER_SQL).fetchall()
        if not rows:
            return None
        return RosterSnapshot(
            roster_version=int(rows[0]["roster_version"]),
            taken_at_ms=int(rows[0]["taken_at_ms"]),
            members=tuple(RosterMember(wallet=row["wallet"], handle=row["handle"]) for row in rows),
            provider=rows[0]["provider"],
        )

    def chain_tape_store_roster(self, members: Sequence[RosterMember], *, now_ms: int) -> RosterSnapshot:
        """Version only source membership. Preserve measured coverage across continuous collection."""
        members = tuple(sorted(members, key=lambda member: member.wallet))
        if not members or len({m.wallet for m in members}) != len(members):
            raise ValueError("roster_members_empty_or_duplicate")
        current = self.chain_tape_current_roster()
        if current is not None and current.wallets == tuple(m.wallet for m in members):
            for member in members:
                self.conn.execute(
                    """
                    UPDATE news_market_wallet_roster SET taken_at_ms = %s, handle = %s
                     WHERE roster_version = %s AND wallet = %s
                """,
                    (now_ms, member.handle, current.roster_version, member.wallet),
                )
            return RosterSnapshot(current.roster_version, now_ms, members)
        version = 1 if current is None else current.roster_version + 1
        # Last membership intervals still being swept are continuously collected.
        # Once the committed cutoff passed an interval's sweep, rejoining starts anew.
        monitoring = {
            row["wallet"]: row["monitoring_from_ms"]
            for row in self.conn.execute("""
            WITH versions AS (
                SELECT roster_version, lead(min(known_at_ms)) OVER (ORDER BY roster_version) AS next_at_ms
                  FROM news_market_wallet_roster GROUP BY roster_version
            )
            SELECT DISTINCT ON (r.wallet) r.wallet, r.monitoring_from_ms
              FROM news_market_wallet_roster r JOIN versions v USING (roster_version)
             WHERE v.next_at_ms IS NULL OR (
                 r.monitoring_from_ms IS NOT NULL AND v.next_at_ms >
                 (SELECT scanned_at_ms FROM news_market_wallet_tape_state WHERE state_id = 'chain_tape') - 1800000
             ) ORDER BY r.wallet, r.roster_version DESC
        """).fetchall()
        }
        for member in members:
            self.conn.execute(
                _INSERT_ROSTER_MEMBER_SQL,
                (
                    version,
                    now_ms,
                    member.wallet,
                    member.handle,
                    ROSTER_PROVIDER,
                    now_ms,
                    monitoring.get(member.wallet),
                ),
            )
        return RosterSnapshot(version, now_ms, members)

    def chain_tape_collection_wallets(self, *, through_at_ms: int) -> tuple[str, ...]:
        """Keep removed members until their last supported thirty-minute window is scanned."""
        rows = self.conn.execute(
            """
            WITH versions AS (
                SELECT roster_version, min(known_at_ms) AS known_at_ms,
                       lead(min(known_at_ms)) OVER (ORDER BY roster_version) AS next_at_ms
                  FROM news_market_wallet_roster GROUP BY roster_version
            )
            SELECT DISTINCT r.wallet FROM news_market_wallet_roster r
              JOIN versions v USING (roster_version)
             WHERE v.next_at_ms IS NULL
                OR (v.next_at_ms > %s - 1800000 AND r.monitoring_from_ms IS NOT NULL)
             ORDER BY r.wallet
        """,
            (int(through_at_ms),),
        ).fetchall()
        return tuple(row["wallet"] for row in rows)

    def chain_tape_record_coverage(
        self,
        *,
        from_ms: int | None,
        through_ms: int | None,
        through_block: int | None,
        through_log: int | None,
        gap_at_ms: int | None,
        wallets: Sequence[str],
        roster_version: int | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE news_market_wallet_tape_state
               SET coverage_from_ms = COALESCE(coverage_from_ms, %s),
                   scanned_at_ms = CASE WHEN scanned_block IS NULL OR (%s,%s) >= (scanned_block,scanned_log)
                                         THEN COALESCE(%s,scanned_at_ms) ELSE scanned_at_ms END,
                   scanned_log = CASE WHEN scanned_block IS NULL OR (%s,%s) >= (scanned_block,scanned_log)
                                      THEN COALESCE(%s,scanned_log) ELSE scanned_log END,
                   scanned_block = GREATEST(%s, scanned_block),
                   gap_at_ms = COALESCE(%s, gap_at_ms)
             WHERE state_id = 'chain_tape'
        """,
            (
                from_ms,
                through_block,
                through_log,
                through_ms,
                through_block,
                through_log,
                through_log,
                through_block,
                gap_at_ms,
            ),
        )
        if from_ms is not None and through_ms is not None and from_ms <= through_ms:
            self.conn.execute(
                """
                UPDATE news_market_wallet_roster
                   SET monitoring_from_ms = GREATEST(known_at_ms, %s)
                 WHERE wallet = ANY(%s) AND monitoring_from_ms IS NULL
                   AND roster_version <= COALESCE(%s, (SELECT max(roster_version) FROM news_market_wallet_roster))
            """,
                (int(from_ms), list(wallets), roster_version),
            )

    def chain_tape_members(self, version: int) -> list[dict[str, Any]]:
        return list(
            self.conn.execute(
                """
            SELECT roster_version, wallet, handle,
                   known_at_ms, monitoring_from_ms,
                   taken_at_ms, provider
              FROM news_market_wallet_roster WHERE roster_version = %s ORDER BY wallet
        """,
                (int(version),),
            ).fetchall()
        )
