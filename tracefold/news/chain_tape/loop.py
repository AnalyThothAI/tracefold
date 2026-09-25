"""The `news-chain-tape` turn: read the chain's logs, store what the followed wallets did.

One bounded `advance()`. App owns the tick, the stop event and the process lifecycle, exactly as it does
for the market notification loop and the Signal lane, because this loop exposes one business action.

The roster is **read** here and refreshed nowhere near here. It used to be rebuilt at the top of this
turn, one blocking per-trader request at a time, ahead of the first chain call: 45 handles against a
15 s read timeout is a collection turn that can spend eleven minutes not collecting, and a provider
that rate-limited half of them still published a new eligibility list (#649 §3 row 1). The refresh is
its own Workers task now (`roster_refresh.py`), and what this turn does with the list is read the last
published version out of PostgreSQL.

Two flows share the turn, and the extension gate names them separately because their contracts differ
(#572 §5.1):

* the wallet `Transfer` logs are a `durable_event` stream -- every one matters, the chain is the
  authority, and the position they were classified to is durable, so a restart resumes rather than
  re-reads from the head;
* the classification and its cash leg are `derived_work` -- rebuildable from the same receipts, planned
  in bounded batches, and idempotent on the chain's own identity.

Provider failures retain the durable position for a later turn. Unexpected errors reach Workers
supervision. The roster refresh, the net-buy detector and the event price sampler run independently
over committed facts.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Sequence
from typing import Any, ClassVar, Final, Protocol

from ..bus import DeferError, TransientError, now_ms
from ..telemetry import (
    NewsExternalDataOutcome,
    NewsExternalDataSkipReason,
    NewsExternalDataSource,
    NewsExternalDataTelemetryPort,
    NewsWorkSemantics,
)
from .classify import CashLeg, classify_receipt, usd_face_value
from .contracts import (
    BLOCK_COMPLETE_TX_INDEX,
    CHAIN_TAPE_NAME,
    ChainTapeDatabasePort,
    ClassifiedFill,
    CompletePrefix,
    RosterSnapshot,
    TapeCursor,
    retry_delay_ms,
)
from .evm import TRANSFER_TOPIC, address_topic

# Blocks are ~0.101 s apart. Thirty of them is three seconds of overlap on every turn: enough that a tip
# that answered short is re-read on the next turn, and cheap because the classified position is durable
# and the fills' identity is the chain's own.
BLOCK_OVERLAP: Final = 30
# One turn may not walk further than this. Measured: 100,000 blocks (2.8 h) with a 35-address topic array
# answers in 1.1-1.7 s, so a whole outage is caught up in a handful of turns rather than in one unbounded
# request (#572 §3.3).
CATCH_UP_BLOCKS_MAX: Final = 100_000
# Receipts are the expensive call: one round trip each, on a public endpoint that publishes a rate limit.
RECEIPTS_PER_TURN_MAX: Final = 20
POLL_INTERVAL_SECONDS: Final = 2.0
_DB_READ_TIMEOUT_SECONDS: Final = 5.0
_DB_WRITE_TIMEOUT_SECONDS: Final = 10.0

CHAIN_SOURCE: Final[NewsExternalDataSource] = "robinhood_rpc"

# "This provider call did not answer". Distinct from a provider that answered `None`, which is a fact
# about the chain (no such transaction) rather than a failure.
_FAILED: Final = object()
# A missing complete receipt stops the continuous prefix. Optional metadata never does.
_HOLD: Final = object()


class ChainLogPort(Protocol):
    """The read-only chain access one turn needs. The adapter lives in `tracefold.integrations`."""

    @property
    def chain_id(self) -> int: ...

    @property
    def last_response_bytes(self) -> int: ...

    async def block_number(self) -> int: ...

    async def logs(
        self,
        *,
        from_block: int,
        to_block: int,
        topics: Sequence[str | None | Sequence[str]],
    ) -> Sequence[Any]: ...

    async def receipt(self, transaction_hash: str) -> Any | None: ...

    async def block_timestamp_ms(self, block_number: int) -> int: ...

    async def token(self, address: str) -> Any: ...

    async def token_decimals(self, address: str) -> int | None: ...


class ChainTapeLoop:
    """One turn of the wallet tape. Owns no clock, no timer and no task of its own."""

    work_semantics: ClassVar[tuple[NewsWorkSemantics, ...]] = (
        "durable_event",
        "derived_work",
    )

    def __init__(
        self,
        *,
        db: ChainTapeDatabasePort,
        chain: ChainLogPort,
        block_overlap: int = BLOCK_OVERLAP,
        catch_up_blocks_max: int = CATCH_UP_BLOCKS_MAX,
        receipts_per_turn_max: int = RECEIPTS_PER_TURN_MAX,
        telemetry: NewsExternalDataTelemetryPort | None = None,
        clock: Callable[[], int] = now_ms,
    ) -> None:
        self.db = db
        self.chain = chain
        self.block_overlap = max(0, int(block_overlap))
        self.catch_up_blocks_max = max(1, int(catch_up_blocks_max))
        self.receipts_per_turn_max = max(1, int(receipts_per_turn_max))
        self.telemetry = telemetry
        self._clock = clock
        self._failures = 0
        self._retry_after_ms = 0
        self._blocked_tx: str | None = None
        self._enrichment_errors: list[str] = []
        self.last_result: dict[str, Any] | None = None
        self.last_error: str | None = None
        self._roster: RosterSnapshot | None = None
        self._store_refused = False
        self._coverage_from: int | None = None
        self._scanned_at: int | None = None
        self._scanned_block: int | None = None
        self._scanned_log: int | None = None
        self._collection_wallets: tuple[str, ...] = ()

    async def aclose(self) -> None:
        """Release whatever the chain port holds. A port with nothing to release says so by not
        having the method; the loop never learns what an adapter's session is."""

        close = getattr(self.chain, "aclose", None)
        if close is not None:
            await close()

    # ------------------------------------------------------------------ the turn
    async def advance(self) -> dict[str, Any]:
        """Classify one bounded slice of the chain's logs, against the last published roster.

        Every path out of here writes the tape's state row, including the ones a provider failure ends
        early. An operator reading `last_outcome` and `last_error` is asking "did the last turn work",
        and a row that still says `success` because the turn returned before the write would answer a
        different question than the one they asked.
        """

        started = time.perf_counter()
        self._network_start = int(getattr(self.chain, "request_count", 0))
        self._bytes_start = int(getattr(self.chain, "response_bytes_total", 0))
        self.last_error = None
        self._store_refused = False
        self._coverage_from = None
        self._scanned_at = None
        self._scanned_block = None
        self._scanned_log = None
        self._collection_wallets = ()
        errors: list[str] = []
        result = _empty_result()
        self._retry_after_ms = 0
        self._blocked_tx = None
        self._enrichment_errors = []
        try:
            stored_state, self._roster, wallets = await self.db.read(
                "news_chain_tape_plan", _read_plan, timeout_seconds=_DB_READ_TIMEOUT_SECONDS
            )
        except (TransientError, DeferError) as exc:
            errors.append(f"db:{type(exc).__name__}")
            self._record_turn(started, "error", result, errors)
            return result
        if self._clock() < int((stored_state or {}).get("next_attempt_at_ms") or 0):
            result["deferred"] = True
            self.last_result = result
            return result
        self._failures = int((stored_state or {}).get("consecutive_failures") or 0)
        roster = self._roster
        self._collection_wallets = wallets
        cursor = _cursor_of(stored_state)
        noise_cursor = _noise_cursor_of(stored_state)
        result["roster_version"] = 0 if roster is None else roster.roster_version
        result["wallets"] = len(wallets)
        if roster is None or not wallets:
            # No list, nothing to watch. Not an error and not a turn: the roster is what defines the work.
            self._record_turn(started, "success" if not errors else "partial", result, errors)
            if self.telemetry is not None:
                self.telemetry.record_external_data_skipped(CHAIN_TAPE_NAME, "no_work")
            return result

        # Everything after this point is the chain half. A roster refresh that failed above must not
        # decide whether the block range was classified: the chain answered or it did not.
        errors_before_chain = len(errors)
        head = await self._provider(CHAIN_SOURCE, self.chain.block_number, errors)
        if head is _FAILED:
            return await self._end(
                started, result, cursor=cursor, noise_cursor=noise_cursor, roster=roster, errors=errors
            )

        if int(head) < cursor.block_number:
            errors.append("robinhood_rpc:reorg_unresolved_head_regressed")
            return await self._end(
                started, result, cursor=cursor, noise_cursor=noise_cursor, roster=roster, errors=errors
            )
        from_block, to_block, cursor = self._range(cursor, head=int(head))
        result["from_block"] = from_block
        result["to_block"] = to_block
        logs = await self._wallet_logs(wallets, from_block=from_block, to_block=to_block, errors=errors)
        if logs is None:
            return await self._end(
                started, result, cursor=cursor, noise_cursor=noise_cursor, roster=roster, errors=errors
            )
        result["logs"] = len(logs)
        if any(bool(getattr(log, "removed", False)) for log in logs):
            errors.append("robinhood_rpc:reorg_unresolved")
            return await self._end(
                started, result, cursor=cursor, noise_cursor=noise_cursor, roster=roster, errors=errors
            )
        try:
            overlap = await self.db.read(
                "news_chain_tape_overlap",
                lambda repos: repos.news.chain_tape_overlap_fills(
                    chain_id=self.chain.chain_id,
                    from_block=from_block,
                    to_block=to_block,
                    wallets=wallets,
                ),
                timeout_seconds=_DB_READ_TIMEOUT_SECONDS,
            )
        except (TransientError, DeferError) as exc:
            errors.append(f"db:{type(exc).__name__}")
            return await self._end(
                started, result, cursor=cursor, noise_cursor=noise_cursor, roster=roster, errors=errors
            )
        offered = {(log.transaction_hash, log.log_index): log.block_hash for log in logs}
        if any(offered.get((row["tx_hash"], row["log_index"])) != row["block_hash"] for row in overlap):
            errors.append("robinhood_rpc:reorg_unresolved_overlap")
            return await self._end(
                started, result, cursor=cursor, noise_cursor=noise_cursor, roster=roster, errors=errors
            )
        start_time = await self._provider(CHAIN_SOURCE, lambda: self.chain.block_timestamp_ms(from_block), errors)
        if start_time is _FAILED:
            return await self._end(
                started, result, cursor=cursor, noise_cursor=noise_cursor, roster=roster, errors=errors
            )
        self._coverage_from = int(start_time)

        discovered = _transactions_after(logs, cursor)
        candidates = discovered
        result["candidates"] = len(candidates)
        taken = candidates[: self.receipts_per_turn_max]
        result["pending"] = len(candidates) - len(taken)

        fills: list[ClassifiedFill] = []
        classified_through = cursor
        prefix: CompletePrefix | None = None
        counted_through = noise_cursor
        # A missing receipt is a real gap. Stop there rather than repeatedly processing
        # a successful suffix that cannot extend the continuous complete prefix.
        held = False
        for position in taken:
            outcome = await self._classify(position, wallets=wallets, roster=roster, errors=errors)
            if outcome is None:
                # The chain contradicted itself -- a withdrawn log, a receipt for another block. That
                # is not one transaction's problem, so the turn stops reading here.
                break
            if outcome is _HOLD:
                held = True
                self._blocked_tx = position.transaction_hash
                break
            fills.extend(outcome.fills)
            # A fill collapses on its primary key however many times the lagging position re-offers it.
            # A count has no key to collapse on, so the marker is the key: what is at or below it has
            # already been counted, and this pass only reports what is above it.
            if noise_cursor.precedes(position.block_number, position.transaction_index):
                result["ignored_inbound"] += outcome.ignored_inbound
                result["unknown"] += outcome.unknown
            result["receipts"] += 1
            prefix = outcome.prefix
            classified_through = prefix.cursor
            counted_through = classified_through
        if not held and result["receipts"] == len(candidates) and len(errors) == errors_before_chain:
            # The overlap remains speculative; a successful tail is stored but is not
            # released to detectors until a later turn establishes its complete prefix.
            lagged = TapeCursor(_lagged(from_block, to_block, self.block_overlap), BLOCK_COMPLETE_TX_INDEX)
            if cursor.precedes(lagged.block_number, lagged.transaction_index):
                stamp = await self._provider(
                    CHAIN_SOURCE, lambda: self.chain.block_timestamp_ms(lagged.block_number), errors
                )
                prefix = None if stamp is _FAILED else CompletePrefix(lagged, BLOCK_COMPLETE_TX_INDEX, int(stamp))
                classified_through = cursor if prefix is None else prefix.cursor
            else:
                # Never rewind an already committed partial prefix on a short/head-stalled turn.
                prefix = None
                classified_through = cursor
        if prefix is not None:
            self._scanned_at = prefix.event_at_ms
            self._scanned_block = prefix.cursor.block_number
            self._scanned_log = prefix.log_index
        result["pending"] = len(candidates) - int(result["receipts"])
        result["enrichment_errors"] = len(self._enrichment_errors)
        result["written"] = await self._store(
            fills,
            cursor=classified_through,
            roster=roster,
            outcome="partial" if errors else "success",
            errors=errors,
            counts=result,
            # Un-lagged, always: the last movement this turn actually looked at. Nothing below it is
            # ever counted again, whatever the classified position does.
            noise_cursor=counted_through,
        )
        self._record_turn(
            started,
            "partial" if errors else "success",
            result,
            errors,
            stored=not self._store_refused,
        )
        return result

    async def _end(
        self,
        started: float,
        result: dict[str, Any],
        *,
        cursor: TapeCursor,
        noise_cursor: TapeCursor,
        roster: RosterSnapshot,
        errors: list[str],
    ) -> dict[str, Any]:
        """End a turn a provider cut short: the position does not move, the outcome and the error do."""

        await self._store(
            (),
            cursor=cursor,
            roster=roster,
            outcome="error",
            errors=errors,
            counts=result,
            noise_cursor=noise_cursor,
        )
        self._record_turn(started, "error", result, errors, stored=not self._store_refused)
        return result

    # ------------------------------------------------------------------ chain
    def _range(self, cursor: TapeCursor, *, head: int) -> tuple[int, int, TapeCursor]:
        """The block window this turn reads, and the position it must not re-classify.

        A first start does not backfill history: it begins one overlap behind the head, because the tape
        exists to watch what happens next and the provider's own ledger is the record of what happened
        before.
        """

        if cursor.block_number <= 0 and cursor.transaction_index != BLOCK_COMPLETE_TX_INDEX:
            start = max(0, head - self.block_overlap)
            return start, head, TapeCursor(start, -1)
        from_block = max(0, cursor.block_number - self.block_overlap)
        to_block = min(head, cursor.block_number + self.catch_up_blocks_max)
        return from_block, max(from_block, to_block), cursor

    async def _wallet_logs(
        self,
        wallets: Sequence[str],
        *,
        from_block: int,
        to_block: int,
        errors: list[str],
    ) -> tuple[Any, ...] | None:
        """Both sides of every roster wallet's `Transfer`, as two topic-array calls and no address filter."""

        topics = [address_topic(wallet) for wallet in wallets]
        collected: list[Any] = []
        for filter_topics in (
            [TRANSFER_TOPIC, None, topics],
            [TRANSFER_TOPIC, topics],
        ):
            answer = await self._provider(
                CHAIN_SOURCE,
                functools.partial(
                    self.chain.logs,
                    from_block=from_block,
                    to_block=to_block,
                    topics=filter_topics,
                ),
                errors,
            )
            if answer is _FAILED:
                return None
            collected.extend(answer)
        return tuple(collected)

    async def _classify(
        self,
        position: _Transaction,
        *,
        wallets: Sequence[str],
        roster: RosterSnapshot,
        errors: list[str],
    ) -> Any | None:
        receipt = await self._provider(
            CHAIN_SOURCE,
            lambda: self.chain.receipt(position.transaction_hash),
            errors,
        )
        if receipt is _FAILED:
            return _HOLD
        if receipt is None:
            return self._missing_receipt(position, errors)
        if (
            receipt.transaction_hash != position.transaction_hash
            or receipt.block_number != position.block_number
            or any(log.removed or log.block_hash != receipt.block_hash for log in receipt.logs)
        ):
            errors.append("robinhood_rpc:reorg_unresolved_receipt")
            return None
        event_at_ms = await self._provider(
            CHAIN_SOURCE,
            lambda: self.chain.block_timestamp_ms(position.block_number),
            errors,
        )
        if event_at_ms is _FAILED:
            return _HOLD
        stamp = self._clock()
        classification = classify_receipt(
            receipt,
            roster_wallets=wallets,
            chain_id=int(self.chain.chain_id),
            event_at_ms=int(event_at_ms),
            received_at_ms=stamp,
            classified_at_ms=stamp,
            roster_version=roster.roster_version,
        )
        priced = []
        for fill in classification.fills:
            enriched = await self._price(fill, errors=errors)
            priced.append(enriched)
        return _Classified(
            fills=tuple(priced),
            ignored_inbound=classification.ignored_inbound,
            unknown=classification.unknown,
            prefix=CompletePrefix(
                TapeCursor(position.block_number, position.transaction_index),
                max((log.log_index for log in receipt.logs), default=-1),
                int(event_at_ms),
            ),
        )

    def _missing_receipt(self, position: _Transaction, errors: list[str]) -> Any:
        """Missing is unresolved: hold the cursor until the entire receipt is available."""
        del position
        errors.append(f"{CHAIN_SOURCE}:receipt_missing")
        return _HOLD

    async def _price(self, fill: ClassifiedFill, *, errors: list[str]) -> ClassifiedFill:
        """Attach the two tokens' own metadata, and a dollar figure only when the cash leg is the stablecoin."""

        # These are display/valuation failures, not missing chain facts. In particular,
        # they must not affect the global error count used to finish a block range.
        del errors
        traded = await self._provider(CHAIN_SOURCE, lambda: self.chain.token(fill.token), self._enrichment_errors)
        if traded is _FAILED:
            traded = None
        cash_decimals: int | None = None
        if fill.cash_token:
            cash = await self._provider(
                CHAIN_SOURCE, lambda: self.chain.token_decimals(str(fill.cash_token)), self._enrichment_errors
            )
            if cash is not _FAILED:
                cash_decimals = cash
        usd, usd_source = usd_face_value(
            None if fill.cash_token is None else CashLeg(fill.cash_token, int(fill.cash_amount_raw or 0)),
            cash_decimals=cash_decimals,
        )
        return ClassifiedFill(
            chain_id=fill.chain_id,
            tx_hash=fill.tx_hash,
            log_index=fill.log_index,
            block_number=fill.block_number,
            block_hash=fill.block_hash,
            wallet=fill.wallet,
            token=fill.token,
            kind=fill.kind,
            amount_raw=fill.amount_raw,
            event_at_ms=fill.event_at_ms,
            received_at_ms=fill.received_at_ms,
            classified_at_ms=fill.classified_at_ms,
            roster_version=fill.roster_version,
            token_symbol=getattr(traded, "symbol", None),
            token_decimals=getattr(traded, "decimals", None),
            cash_token=fill.cash_token,
            cash_amount_raw=fill.cash_amount_raw,
            cash_decimals=cash_decimals,
            usd=usd,
            usd_source=usd_source,
            provider=fill.provider,
        )

    # ------------------------------------------------------------------ storage
    async def _store(
        self,
        fills: Sequence[ClassifiedFill],
        *,
        cursor: TapeCursor,
        roster: RosterSnapshot,
        outcome: str,
        errors: list[str],
        counts: dict[str, Any],
        noise_cursor: TapeCursor,
    ) -> int:
        def _write(repos: Any) -> int:
            written = repos.news.chain_tape_record_fills(fills)
            repos.news.chain_tape_save_state(
                cursor=cursor,
                roster_version=roster.roster_version,
                outcome=outcome,
                error=errors[0] if errors else None,
                now_ms=self._clock(),
                succeeded=not errors,
                ignored_inbound=int(counts.get("ignored_inbound") or 0),
                unknown=int(counts.get("unknown") or 0),
                noise_cursor=noise_cursor,
                next_attempt_at_ms=self._clock() + retry_delay_ms(self._failures + 1, self._retry_after_ms)
                if errors
                else 0,
                consecutive_failures=self._failures + 1 if errors else 0,
                blocked_tx_hash=self._blocked_tx,
                enrichment_error=self._enrichment_errors[0] if self._enrichment_errors else None,
            )
            repos.news.chain_tape_record_coverage(
                from_ms=self._coverage_from,
                through_ms=self._scanned_at,
                through_block=self._scanned_block,
                through_log=self._scanned_log,
                gap_at_ms=self._clock() if any("reorg_unresolved" in error for error in errors) else None,
                wallets=self._collection_wallets,
                roster_version=roster.roster_version,
            )
            return int(written)

        try:
            return await self.db.tx("news_chain_tape_store", _write, timeout_seconds=_DB_WRITE_TIMEOUT_SECONDS)
        except (TransientError, DeferError) as exc:
            # The lane refused or the write overran. Nothing committed, so the next turn re-reads the
            # same range from the same position and writes the same rows -- and counts the same
            # movements, which is why the skip counters wait for a committed write.
            errors.append(f"db:{type(exc).__name__}")
            self._store_refused = True
            return 0

    # ------------------------------------------------------------------ provider and telemetry
    async def _provider(
        self,
        source: NewsExternalDataSource,
        call: Callable[[], Any],
        errors: list[str],
    ) -> Any:
        """One bounded provider attempt, measured. A failure is this call's answer, never the process's.

        Returns `_FAILED` when the call did not answer, so a provider that legitimately answers `None`
        -- a node that does not have a transaction -- is not read as an outage.
        """

        started = time.perf_counter()
        requests_before = getattr(self.chain, "request_count", None)
        bytes_before = int(getattr(self.chain, "response_bytes_total", 0))
        try:
            answer = await call()
        except Exception as exc:  # provider failures are expected; the turn ends with state intact
            if errors is not self._enrichment_errors:
                self._retry_after_ms = max(self._retry_after_ms, int(getattr(exc, "retry_after_ms", 0) or 0))
            code = getattr(exc, "code", None) or type(exc).__name__
            errors.append(f"{source}:{code}")
            if self.telemetry is not None:
                self.telemetry.record_external_data_provider_call(
                    CHAIN_TAPE_NAME,
                    source,
                    "error",
                    time.perf_counter() - started,
                )
            return _FAILED
        if self.telemetry is not None and (
            requests_before is None or int(getattr(self.chain, "request_count", 0)) > requests_before
        ):
            self.telemetry.record_external_data_provider_call(
                CHAIN_TAPE_NAME,
                source,
                "success",
                time.perf_counter() - started,
                byte_count=(int(getattr(self.chain, "response_bytes_total", 0)) - bytes_before)
                if requests_before is not None
                else _response_bytes(self.chain),
            )
        return answer

    def _record_turn(
        self,
        started: float,
        outcome: NewsExternalDataOutcome,
        result: dict[str, Any],
        errors: Sequence[str],
        *,
        stored: bool = True,
    ) -> None:
        """One turn's measurement, including what the turn deliberately did not store.

        The two skip counters are the honest form of "counted in telemetry": an inbound token nobody
        asked for and a movement the receipt could not explain are both real volume this loop read and
        chose not to persist, and without a counter the only evidence they existed would be their
        absence. They are also accumulated on the tape's own state row, because the week-one calibration
        in #572 §6 is answered in SQL and Prometheus cannot be joined to a fills table.

        `stored` is why the two are not two answers. The state row's totals move only when the write
        commits; a refused write rolls the whole turn back and the next one re-reads the same movements
        and counts them again. Emitting the Prometheus counters anyway would make them disagree with
        the row by exactly the refused turns, so they are emitted only when the row moved too.
        """

        result["rpc_requests"] = int(getattr(self.chain, "request_count", 0)) - self._network_start
        result["rpc_bytes"] = int(getattr(self.chain, "response_bytes_total", 0)) - self._bytes_start
        self.last_result = dict(result)
        self.last_error = ",".join(errors) or None
        if self.telemetry is None:
            return
        self.telemetry.record_external_data_turn(
            CHAIN_TAPE_NAME,
            outcome,
            time.perf_counter() - started,
            target_count=int(result.get("wallets") or 0),
            source_count=1,
        )
        if not stored:
            return
        skipped: tuple[tuple[NewsExternalDataSkipReason, int], ...] = (
            ("airdrop_ignored", int(result.get("ignored_inbound") or 0)),
            ("unclassified", int(result.get("unknown") or 0)),
        )
        for reason, count in skipped:
            for _ in range(count):
                self.telemetry.record_external_data_skipped(CHAIN_TAPE_NAME, reason)


class _Transaction:
    """One roster transaction discovered in the log window, at its position on the chain."""

    __slots__ = ("block_number", "transaction_hash", "transaction_index")

    def __init__(self, transaction_hash: str, block_number: int, transaction_index: int) -> None:
        self.transaction_hash = transaction_hash
        self.block_number = block_number
        self.transaction_index = transaction_index


class _Classified:
    """One receipt's outcome, after the tokens' metadata was attached."""

    __slots__ = ("fills", "ignored_inbound", "prefix", "unknown")

    def __init__(
        self, fills: tuple[ClassifiedFill, ...], ignored_inbound: int, unknown: int, prefix: CompletePrefix
    ) -> None:
        self.fills = fills
        self.ignored_inbound = ignored_inbound
        self.unknown = unknown
        self.prefix = prefix


def _empty_result() -> dict[str, Any]:
    """One turn's counters, all zero. Every key exists on every path, including the ones that failed."""

    return {
        "roster_version": 0,
        "wallets": 0,
        "from_block": 0,
        "to_block": 0,
        "logs": 0,
        "candidates": 0,
        "receipts": 0,
        "written": 0,
        "ignored_inbound": 0,
        "unknown": 0,
        "pending": 0,
    }


def _lagged(from_block: int, to_block: int, overlap: int) -> int:
    """The durable position for a fully classified range: one overlap behind the block it was read to.

    Never behind the window's own start, because the range below `from_block` was not read this turn.
    """

    return max(int(from_block), int(to_block) - max(0, int(overlap)))


def _cursor_of(state: Any) -> TapeCursor:
    if state is None:
        return TapeCursor(0, -1)
    return TapeCursor(int(state["high_water_block"]), int(state["high_water_tx_index"]))


def _noise_cursor_of(state: Any) -> TapeCursor:
    """How far the noise counts have been taken. Never lags, so nothing is counted twice."""

    if state is None:
        return TapeCursor(0, -1)
    return TapeCursor(int(state["noise_through_block"]), int(state["noise_through_tx_index"]))


def _transactions_after(logs: Sequence[Any], cursor: TapeCursor) -> tuple[_Transaction, ...]:
    """Distinct transactions strictly after the classified position, in chain order.

    The same transaction appears in both topic calls when a wallet is on both sides of it, and the whole
    overlap window is re-read on every turn: both collapse here, before a receipt is ever requested.
    """

    seen: dict[str, _Transaction] = {}
    for log in logs:
        if bool(getattr(log, "removed", False)):
            # The node says this log is no longer on the chain it is serving. PR-1 does not detect a
            # reorg; it declines to classify a log that has already been withdrawn (#572 §10).
            continue
        block_number = int(getattr(log, "block_number", 0))
        transaction_index = int(getattr(log, "transaction_index", 0))
        if not cursor.precedes(block_number, transaction_index):
            continue
        transaction_hash = str(getattr(log, "transaction_hash", "")).lower()
        if not transaction_hash or transaction_hash in seen:
            continue
        seen[transaction_hash] = _Transaction(transaction_hash, block_number, transaction_index)
    return tuple(sorted(seen.values(), key=lambda item: (item.block_number, item.transaction_index)))


def _response_bytes(provider: Any) -> int | None:
    value = getattr(provider, "last_response_bytes", None)
    return None if value is None else int(value)


__all__ = [
    "BLOCK_OVERLAP",
    "CATCH_UP_BLOCKS_MAX",
    "POLL_INTERVAL_SECONDS",
    "RECEIPTS_PER_TURN_MAX",
    "ChainLogPort",
    "ChainTapeLoop",
]


def _read_plan(repos: Any) -> tuple[Any, RosterSnapshot | None, tuple[str, ...]]:
    plan: tuple[Any, RosterSnapshot | None, tuple[str, ...]] = repos.news.chain_tape_collection_plan()
    return plan
