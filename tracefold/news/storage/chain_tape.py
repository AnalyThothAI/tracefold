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
from .collectors import ChainTapeState, CollectorsStorage, WalletRosterState

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

_TAPE_FIELDS = ", ".join(
    f"{name} {'text' if name in {'last_outcome', 'last_error', 'blocked_tx_hash', 'enrichment_error'} else 'bigint'}"
    for name in ChainTapeState.model_fields
)
_TAPE_COLUMNS = ", ".join("t." + name for name in ChainTapeState.model_fields)
WALLET_TAPE_STATE_SQL: Final = f"""
    SELECT {_TAPE_COLUMNS}, GREATEST(c.updated_at_ms,r.updated_at_ms) AS updated_at_ms,
           (r.state->>'last_attempt_at_ms')::bigint AS roster_last_attempt_at_ms,
           (r.state->>'last_success_at_ms')::bigint AS roster_last_success_at_ms,
           r.state->>'last_error' AS roster_last_error,
           (r.state->>'next_attempt_at_ms')::bigint AS roster_next_attempt_at_ms,
           (r.state->>'consecutive_failures')::bigint AS roster_consecutive_failures
    FROM news_collectors c CROSS JOIN LATERAL jsonb_to_record(c.state) AS t({_TAPE_FIELDS})
    JOIN news_collectors r ON r.collector_id='wallet_roster' WHERE c.collector_id=%s
"""  # noqa: S608 -- code-owned model fields, with collector identity bound.
_CURRENT_VERSION_SQL = """
    SELECT COALESCE(max(v),0) FROM (
      SELECT joined_version AS v FROM news_market_wallets UNION ALL
      SELECT left_version FROM news_market_wallets WHERE left_version IS NOT NULL) versions
"""
WALLET_ROSTER_ROWS_SQL: Final = f"""
    SELECT ({_CURRENT_VERSION_SQL}) AS roster_version,
           COALESCE((SELECT (state->>'last_success_at_ms')::bigint FROM news_collectors
                     WHERE collector_id='wallet_roster'),joined_at_ms) AS taken_at_ms,
           wallet,handle,provider,monitoring_from_ms
    FROM news_market_wallets WHERE left_version IS NULL ORDER BY wallet
"""  # noqa: S608 -- code-owned version expression.
_CURRENT_ROSTER_SQL = WALLET_ROSTER_ROWS_SQL

_PURGE_FILLS_SQL: Final = """
DELETE FROM news_market_wallet_fills
 WHERE ctid = ANY (ARRAY(
       SELECT ctid FROM news_market_wallet_fills
        WHERE event_at_ms < %s
        ORDER BY event_at_ms
        LIMIT %s))
"""

# One statement pins membership, sweep addresses and progress to one MVCC snapshot.
_COLLECTION_PLAN_SQL: Final = f"""
    WITH state AS ({WALLET_TAPE_STATE_SQL}), current_roster AS ({WALLET_ROSTER_ROWS_SQL})
    SELECT (SELECT to_jsonb(s) FROM state s) AS state,
           COALESCE((SELECT jsonb_agg(to_jsonb(r) ORDER BY r.wallet) FROM current_roster r),'[]') AS roster,
           ARRAY(SELECT DISTINCT wallet FROM news_market_wallets
                 WHERE left_version IS NULL OR (monitoring_from_ms IS NOT NULL AND
                   left_at_ms > COALESCE((SELECT scanned_at_ms FROM state),0)-1800000)
                 ORDER BY wallet) AS wallets
"""  # noqa: S608 -- code-owned production projections.


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
        sql = WALLET_TAPE_STATE_SQL + (" FOR SHARE OF c" if for_share else "")
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

        with CollectorsStorage(self.conn).mutate_collector("chain_tape", ChainTapeState, now_ms=now_ms) as (state, _):
            state.high_water_block = int(cursor.block_number)
            state.high_water_tx_index = int(cursor.transaction_index)
            state.roster_version = roster_version
            state.last_outcome = outcome
            state.last_error = error
            if succeeded:
                state.last_success_at_ms = now_ms
            state.ignored_inbound_total += max(0, ignored_inbound)
            state.unknown_total += max(0, unknown)
            if noise_cursor is not None and (noise_cursor.block_number, noise_cursor.transaction_index) > (
                state.noise_through_block,
                state.noise_through_tx_index,
            ):
                state.noise_through_block = noise_cursor.block_number
                state.noise_through_tx_index = noise_cursor.transaction_index
            state.next_attempt_at_ms = next_attempt_at_ms
            state.consecutive_failures = consecutive_failures
            state.blocked_tx_hash = blocked_tx_hash
            state.enrichment_error = enrichment_error

    def chain_tape_begin_roster_refresh(self, *, now_ms: int, next_attempt_at_ms: int) -> None:
        """An in-flight request is an attempt, never a success or a fabricated failure."""
        with CollectorsStorage(self.conn).mutate_collector("wallet_roster", WalletRosterState, now_ms=now_ms) as (
            state,
            _,
        ):
            state.last_attempt_at_ms = now_ms
            state.next_attempt_at_ms = next_attempt_at_ms

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
        with CollectorsStorage(self.conn).mutate_collector(
            "wallet_roster", WalletRosterState, now_ms=completed_at_ms or now_ms
        ) as (state, _):
            state.last_attempt_at_ms = now_ms
            if succeeded:
                state.last_success_at_ms = completed_at_ms or now_ms
            state.last_error = None if succeeded else error or "roster_refresh_failed"
            state.next_attempt_at_ms = next_attempt_at_ms
            state.consecutive_failures = consecutive_failures

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
        """Version source membership; retain continuous coverage and intervals still being swept."""
        members = tuple(sorted(members, key=lambda member: member.wallet))
        if not members or len({m.wallet for m in members}) != len(members):
            raise ValueError("roster_members_empty_or_duplicate")
        with CollectorsStorage(self.conn).mutate_collector("wallet_roster", WalletRosterState, now_ms=now_ms) as (
            refresh,
            _,
        ):
            current = self.chain_tape_current_roster()
            current_wallets = () if current is None else current.wallets
            incoming = tuple(member.wallet for member in members)
            version = 1 if current is None else current.roster_version
            if incoming != current_wallets:
                if current is not None:
                    version += 1
                monitoring = {
                    row["wallet"]: row["monitoring_from_ms"]
                    for row in self.conn.execute("""
                        SELECT DISTINCT ON (wallet) wallet,monitoring_from_ms FROM news_market_wallets
                        WHERE left_version IS NULL OR (monitoring_from_ms IS NOT NULL AND left_at_ms >
                          COALESCE((SELECT (state->>'scanned_at_ms')::bigint FROM news_collectors
                                    WHERE collector_id='chain_tape'),0)-1800000)
                        ORDER BY wallet,joined_version DESC
                    """).fetchall()
                }
                self.conn.execute(
                    """
                    UPDATE news_market_wallets SET left_version=%s,left_at_ms=%s
                    WHERE left_version IS NULL AND NOT (wallet=ANY(%s))
                """,
                    (version, now_ms, list(incoming)),
                )
                for member in members:
                    if member.wallet not in current_wallets:
                        self.conn.execute(
                            """
                            INSERT INTO news_market_wallets
                              (wallet,joined_version,joined_at_ms,handle,provider,monitoring_from_ms)
                            VALUES (%s,%s,%s,%s,%s,%s)
                        """,
                            (
                                member.wallet,
                                version,
                                now_ms,
                                member.handle,
                                ROSTER_PROVIDER,
                                monitoring.get(member.wallet),
                            ),
                        )
            for member in members:
                self.conn.execute(
                    """
                    UPDATE news_market_wallets SET handle=%s
                    WHERE wallet=%s AND left_version IS NULL AND handle IS DISTINCT FROM %s
                """,
                    (member.handle, member.wallet, member.handle),
                )
            refresh.last_success_at_ms = now_ms
            return RosterSnapshot(version, now_ms, members)

    def chain_tape_collection_wallets(self, *, through_at_ms: int) -> tuple[str, ...]:
        """Keep removed members until their last supported thirty-minute window is scanned."""
        rows = self.conn.execute(
            """
            SELECT DISTINCT wallet FROM news_market_wallets WHERE left_version IS NULL
              OR (left_at_ms > %s-1800000 AND monitoring_from_ms IS NOT NULL) ORDER BY wallet
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
        with CollectorsStorage(self.conn).mutate_collector("chain_tape", ChainTapeState, now_ms=through_ms or 0) as (
            state,
            _,
        ):
            if state.coverage_from_ms is None:
                state.coverage_from_ms = from_ms
            if (
                through_block is not None
                and through_log is not None
                and (
                    state.scanned_block is None
                    or (through_block, through_log) >= (state.scanned_block, state.scanned_log or 0)
                )
            ):
                if through_ms is not None:
                    state.scanned_at_ms = through_ms
                state.scanned_block = through_block
                state.scanned_log = through_log
            if gap_at_ms is not None:
                state.gap_at_ms = gap_at_ms
            if from_ms is not None and through_ms is not None and from_ms <= through_ms:
                self.conn.execute(
                    f"""
                    UPDATE news_market_wallets SET monitoring_from_ms=GREATEST(joined_at_ms,%s)
                    WHERE wallet=ANY(%s) AND monitoring_from_ms IS NULL
                      AND joined_version <= COALESCE(%s,({_CURRENT_VERSION_SQL}))
                """,  # noqa: S608 -- module-owned version SQL.
                    (int(from_ms), list(wallets), roster_version),
                )

    def chain_tape_members(self, version: int) -> list[dict[str, Any]]:
        return list(
            self.conn.execute(
                """
            WITH boundary AS (
              SELECT joined_at_ms AS at_ms FROM news_market_wallets WHERE joined_version=%s
              UNION ALL SELECT left_at_ms FROM news_market_wallets WHERE left_version=%s
            )
            SELECT %s::bigint AS roster_version,wallet,handle,
                   (SELECT max(at_ms) FROM boundary) AS known_at_ms,monitoring_from_ms,
                   (SELECT max(at_ms) FROM boundary) AS taken_at_ms,provider
            FROM news_market_wallets WHERE joined_version<=%s AND (left_version IS NULL OR left_version>%s)
            ORDER BY wallet
        """,
                (version, version, version, version, version),
            ).fetchall()
        )
