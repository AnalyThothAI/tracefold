"""The `news-chain-tape` turn: refresh the roster, read the chain's logs, store what the wallets did.

One bounded `advance()`. App owns the tick, the stop event and the process lifecycle, exactly as it does
for the market notification loop and the Signal lane, because this loop exposes one business action.

Three flows share the turn, and the extension gate names them separately because their contracts differ
(#572 §5.1):

* the roster snapshot is `latest_state` -- a failed refresh keeps the previous version, and a version
  that has not changed is re-stamped rather than re-inserted;
* the wallet `Transfer` logs are a `durable_event` stream -- every one matters, the chain is the
  authority, and the position they were classified to is durable, so a restart resumes rather than
  re-reads from the head;
* the classification and its cash leg are `derived_work` -- rebuildable from the same receipts, planned
  in bounded batches, and idempotent on the chain's own identity.

Nothing here can be root-fatal by construction. A provider failure ends the turn with the previous state
intact and is recorded; the only exceptions that leave `advance()` are the database port's, which the
Workers root confines to the `chain_tape` capability.

Store-only in PR-1: no `news_items` row, no card, no notification, no balance check. The first week's
counts are the calibration input for the thresholds in #572 §6.4.
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
from .classify import TRANSFER_TOPIC, CashLeg, classify_receipt, usd_face_value
from .contracts import (
    BLOCK_COMPLETE_TX_INDEX,
    CHAIN_TAPE_NAME,
    STABLE_CASH_TOKEN,
    ClassifiedFill,
    RosterSnapshot,
    TapeCursor,
)
from .evm import address_topic
from .roster import RosterRules, quality_candidates, select_roster

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
ROSTER_REFRESH_MS: Final = 3_600_000
POLL_INTERVAL_SECONDS: Final = 2.0
# How many turns a transaction the node cannot find is carried before it is given up on as
# unknown. The public endpoint is load balanced, so a transaction that has just appeared in one
# node's logs can legitimately 404 from another for a moment.
MISSING_RECEIPT_ATTEMPTS: Final = 3

_DB_READ_TIMEOUT_SECONDS: Final = 5.0
_DB_WRITE_TIMEOUT_SECONDS: Final = 10.0

CHAIN_SOURCE: Final[NewsExternalDataSource] = "robinhood_rpc"
ROSTER_SOURCE: Final[NewsExternalDataSource] = "robinhoodtrenches"

# "This provider call did not answer". Distinct from a provider that answered `None`, which is a fact
# about the chain (no such transaction) rather than a failure.
_FAILED: Final = object()


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


class RosterProviderPort(Protocol):
    """The roster's authority: the tracked list, and one document per handle for its profit factor."""

    @property
    def last_response_bytes(self) -> int: ...

    async def traders(self, *, window: str = "7d") -> Sequence[Any]: ...

    async def trader(self, handle: str) -> Any | None: ...


class ChainTapeRepositories(Protocol):
    """The callback capability one turn needs; no instruments, no price, no Trading."""

    @property
    def news(self) -> Any: ...


class ChainTapeDatabasePort(Protocol):
    """Bounded read/transaction, in the News error vocabulary. The composition root picks the lane."""

    async def read[T](
        self, name: str, fn: Callable[[ChainTapeRepositories], T], *, timeout_seconds: float = 3.0
    ) -> T: ...

    async def tx[T](
        self, name: str, fn: Callable[[ChainTapeRepositories], T], *, timeout_seconds: float = 3.0
    ) -> T: ...


class ChainTapeLoop:
    """One turn of the wallet tape. Owns no clock, no timer and no task of its own."""

    work_semantics: ClassVar[tuple[NewsWorkSemantics, ...]] = (
        "durable_event",
        "derived_work",
        "latest_state",
    )

    def __init__(
        self,
        *,
        db: ChainTapeDatabasePort,
        chain: ChainLogPort,
        roster_provider: RosterProviderPort,
        rules: RosterRules | None = None,
        roster_refresh_ms: int = ROSTER_REFRESH_MS,
        block_overlap: int = BLOCK_OVERLAP,
        catch_up_blocks_max: int = CATCH_UP_BLOCKS_MAX,
        receipts_per_turn_max: int = RECEIPTS_PER_TURN_MAX,
        telemetry: NewsExternalDataTelemetryPort | None = None,
    ) -> None:
        self.db = db
        self.chain = chain
        self.roster_provider = roster_provider
        self.rules = rules or RosterRules()
        self.roster_refresh_ms = max(0, int(roster_refresh_ms))
        self.block_overlap = max(0, int(block_overlap))
        self.catch_up_blocks_max = max(1, int(catch_up_blocks_max))
        self.receipts_per_turn_max = max(1, int(receipts_per_turn_max))
        self.telemetry = telemetry
        self.last_result: dict[str, Any] | None = None
        self.last_error: str | None = None
        self._roster: RosterSnapshot | None = None
        # Transactions the node could not produce a receipt for, and how many turns each has been
        # asked for. Bounded by the log window: an entry that stops being a candidate is dropped.
        self._missing_receipts: dict[str, int] = {}

    async def aclose(self) -> None:
        """Release whatever the two provider ports hold. A port with nothing to release says so by
        not having the method; the loop never learns what an adapter's session is."""

        for provider in (self.chain, self.roster_provider):
            close = getattr(provider, "aclose", None)
            if close is not None:
                await close()

    # ------------------------------------------------------------------ the turn
    async def advance(self) -> dict[str, Any]:
        """Refresh the roster if it is due, then classify one bounded slice of the chain's logs.

        Every path out of here writes the tape's state row, including the ones a provider failure ends
        early. An operator reading `last_outcome` and `last_error` is asking "did the last turn work",
        and a row that still says `success` because the turn returned before the write would answer a
        different question than the one they asked.
        """

        started = time.perf_counter()
        self.last_error = None
        errors: list[str] = []
        result = _empty_result()
        try:
            stored_state, self._roster = await self.db.read(
                "news_chain_tape_state",
                lambda repos: (repos.news.chain_tape_state(), repos.news.chain_tape_current_roster()),
                timeout_seconds=_DB_READ_TIMEOUT_SECONDS,
            )
        except (TransientError, DeferError) as exc:
            # The lane refused the read or it overran. There is no position to record and nothing to
            # record it with; the next turn re-reads the same row.
            errors.append(f"db:{type(exc).__name__}")
            self._record_turn(started, "error", result, errors)
            return result
        roster_error = await self._refresh_roster_if_due()
        if roster_error:
            errors.append(roster_error)
        roster = self._roster
        wallets = tuple(roster.wallets) if roster is not None else ()
        cursor = _cursor_of(stored_state)
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
            return await self._end(started, result, cursor=cursor, roster=roster, errors=errors)

        from_block, to_block, cursor = self._range(cursor, head=int(head))
        result["from_block"] = from_block
        result["to_block"] = to_block
        logs = await self._wallet_logs(wallets, from_block=from_block, to_block=to_block, errors=errors)
        if logs is None:
            return await self._end(started, result, cursor=cursor, roster=roster, errors=errors)
        result["logs"] = len(logs)

        candidates = _transactions_after(logs, cursor)
        result["candidates"] = len(candidates)
        taken = candidates[: self.receipts_per_turn_max]
        result["pending"] = len(candidates) - len(taken)
        self._forget_missing_receipts_outside(candidates)

        fills: list[ClassifiedFill] = []
        classified_through = cursor
        for position in taken:
            outcome = await self._classify(position, wallets=wallets, roster=roster, errors=errors)
            if outcome is None:
                # The receipt or one of its tokens did not answer. Everything from here stays pending,
                # and the position this turn already classified is what is recorded.
                break
            fills.extend(outcome.fills)
            result["ignored_inbound"] += outcome.ignored_inbound
            result["unknown"] += outcome.unknown
            result["receipts"] += 1
            classified_through = TapeCursor(position.block_number, position.transaction_index)
        if result["receipts"] == len(candidates) and len(errors) == errors_before_chain:
            # The whole planned range is classified -- and the durable position deliberately stops one
            # overlap short of the head it was read to.
            #
            # A mark set to `to_block` would declare the head block complete on the very turn it was
            # first read, and every re-fetched log from the overlap would then be filtered out before a
            # receipt was ever requested: sixty blocks fetched and discarded every two seconds, and a
            # tip that answered short never picked up. Lagging the mark is what makes the overlap an
            # overlap. The re-read costs at most a handful of receipts, and the fills' identity is the
            # chain's own, so writing them again is one `ON CONFLICT DO NOTHING`.
            classified_through = TapeCursor(_lagged(from_block, to_block, self.block_overlap), BLOCK_COMPLETE_TX_INDEX)
        # A partial turn keeps the position it actually reached instead of the lagged one. Clamping it
        # back would re-plan the same bounded batch of receipts every turn and never drain a backlog.

        result["written"] = await self._store(
            fills,
            cursor=classified_through,
            roster=roster,
            outcome="partial" if errors else "success",
            errors=errors,
            counts=result,
        )
        self._record_turn(started, "partial" if errors else "success", result, errors)
        return result

    async def _end(
        self,
        started: float,
        result: dict[str, Any],
        *,
        cursor: TapeCursor,
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
        )
        self._record_turn(started, "error", result, errors)
        return result

    # ------------------------------------------------------------------ roster
    async def _refresh_roster_if_due(self) -> str | None:
        """Rebuild the list when it is older than the refresh period. A failure keeps the last one."""

        if (
            self._roster is not None
            and self.roster_refresh_ms
            and now_ms() - int(self._roster.taken_at_ms) < self.roster_refresh_ms
        ):
            return None
        errors: list[str] = []
        candidates = await self._provider(ROSTER_SOURCE, self.roster_provider.traders, errors)
        if candidates is _FAILED:
            # The site did not answer. The previous version stays exactly as it is; that is the whole
            # of the `latest_state` contract for this flow (#572 §5.1).
            return errors[0] if errors else "roster_unavailable"
        profit_factors: dict[str, float | None] = {}
        for row in quality_candidates(candidates, rules=self.rules):
            handle = str(getattr(row, "handle", "") or "")
            if not handle or handle in profit_factors:
                continue
            stats = await self._provider(
                ROSTER_SOURCE,
                functools.partial(self.roster_provider.trader, handle),
                errors,
            )
            if stats is _FAILED or stats is None:
                # One unreadable trader is not a broken roster: it simply cannot pass the quality rule.
                profit_factors[handle] = None
                continue
            profit_factors[handle] = getattr(stats, "profit_factor", None)
        members = select_roster(candidates, profit_factors=profit_factors, rules=self.rules)
        if not members:
            return "roster_selected_nobody"
        stamp = now_ms()
        try:
            self._roster = await self.db.tx(
                "news_chain_tape_roster",
                lambda repos: repos.news.chain_tape_store_roster(members, now_ms=stamp),
                timeout_seconds=_DB_WRITE_TIMEOUT_SECONDS,
            )
        except (TransientError, DeferError) as exc:
            # Same shape as the fill write: nothing committed, the previous version is still current,
            # and the chain half of this turn carries on against it.
            return f"db:{type(exc).__name__}"
        return errors[0] if errors else None

    # ------------------------------------------------------------------ chain
    def _range(self, cursor: TapeCursor, *, head: int) -> tuple[int, int, TapeCursor]:
        """The block window this turn reads, and the position it must not re-classify.

        A first start does not backfill history: it begins one overlap behind the head, because the tape
        exists to watch what happens next and the provider's own ledger is the record of what happened
        before.
        """

        if cursor.block_number <= 0:
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
            return None
        if receipt is None:
            return self._missing_receipt(position, errors)
        self._missing_receipts.pop(position.transaction_hash, None)
        event_at_ms = await self._provider(
            CHAIN_SOURCE,
            lambda: self.chain.block_timestamp_ms(position.block_number),
            errors,
        )
        if event_at_ms is _FAILED:
            return None
        stamp = now_ms()
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
            if enriched is None:
                return None
            priced.append(enriched)
        return _Classified(
            fills=tuple(priced),
            ignored_inbound=classification.ignored_inbound,
            unknown=classification.unknown,
        )

    def _missing_receipt(self, position: _Transaction, errors: list[str]) -> Any | None:
        """A transaction the node will not produce a receipt for: carried, then given up on.

        The public endpoint is load balanced, so a transaction that has just appeared in one node's
        `eth_getLogs` answer can legitimately 404 from another for a moment. Treating that as classified
        would drop it for ever, because the position would advance past it. It is carried instead --
        the turn stops here and everything from this position stays pending -- and after a bounded
        number of turns it is recorded as one `unknown` so the tape cannot stall on it.
        """

        attempts = self._missing_receipts.get(position.transaction_hash, 0) + 1
        self._missing_receipts[position.transaction_hash] = attempts
        if attempts < MISSING_RECEIPT_ATTEMPTS:
            errors.append(f"{CHAIN_SOURCE}:receipt_missing")
            return None
        self._missing_receipts.pop(position.transaction_hash, None)
        return _Classified(fills=(), ignored_inbound=0, unknown=1)

    def _forget_missing_receipts_outside(self, candidates: Sequence[_Transaction]) -> None:
        """Drop carried transactions the log window no longer offers, so the map stays bounded."""

        if not self._missing_receipts:
            return
        offered = {position.transaction_hash for position in candidates}
        for transaction_hash in [key for key in self._missing_receipts if key not in offered]:
            del self._missing_receipts[transaction_hash]

    async def _price(self, fill: ClassifiedFill, *, errors: list[str]) -> ClassifiedFill | None:
        """Attach the two tokens' own metadata, and a dollar figure only when the cash leg is the stablecoin."""

        traded = await self._provider(CHAIN_SOURCE, lambda: self.chain.token(fill.token), errors)
        if traded is _FAILED:
            return None
        cash_decimals: int | None = None
        if fill.cash_token:
            cash = await self._provider(CHAIN_SOURCE, lambda: self.chain.token(str(fill.cash_token)), errors)
            if cash is _FAILED:
                return None
            cash_decimals = getattr(cash, "decimals", None)
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
    ) -> int:
        def _write(repos: Any) -> int:
            written = repos.news.chain_tape_record_fills(fills)
            repos.news.chain_tape_save_state(
                cursor=cursor,
                roster_version=roster.roster_version,
                outcome=outcome,
                error=errors[0] if errors else None,
                now_ms=now_ms(),
                succeeded=not errors,
                ignored_inbound=int(counts.get("ignored_inbound") or 0),
                unknown=int(counts.get("unknown") or 0),
            )
            return int(written)

        try:
            return await self.db.tx("news_chain_tape_store", _write, timeout_seconds=_DB_WRITE_TIMEOUT_SECONDS)
        except (TransientError, DeferError) as exc:
            # The lane refused or the write overran. Nothing committed, so the next turn re-reads the
            # same range from the same position and writes the same rows.
            errors.append(f"db:{type(exc).__name__}")
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
        try:
            answer = await call()
        except Exception as exc:  # provider failures are expected; the turn ends with state intact
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
        if self.telemetry is not None:
            self.telemetry.record_external_data_provider_call(
                CHAIN_TAPE_NAME,
                source,
                "success",
                time.perf_counter() - started,
                byte_count=_response_bytes(self.chain if source == CHAIN_SOURCE else self.roster_provider),
            )
        return answer

    def _record_turn(
        self,
        started: float,
        outcome: NewsExternalDataOutcome,
        result: dict[str, Any],
        errors: Sequence[str],
    ) -> None:
        """One turn's measurement, including what the turn deliberately did not store.

        The two skip counters are the honest form of "counted in telemetry": an inbound token nobody
        asked for and a movement the receipt could not explain are both real volume this loop read and
        chose not to persist, and without a counter the only evidence they existed would be their
        absence. They are also accumulated on the tape's own state row, because the week-one calibration
        in #572 §6 is answered in SQL and Prometheus cannot be joined to a fills table.
        """

        self.last_result = dict(result)
        self.last_error = ",".join(errors) or None
        if self.telemetry is None:
            return
        self.telemetry.record_external_data_turn(
            CHAIN_TAPE_NAME,
            outcome,
            time.perf_counter() - started,
            target_count=int(result.get("wallets") or 0),
            source_count=2,
        )
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

    __slots__ = ("fills", "ignored_inbound", "unknown")

    def __init__(self, fills: tuple[ClassifiedFill, ...], ignored_inbound: int, unknown: int) -> None:
        self.fills = fills
        self.ignored_inbound = ignored_inbound
        self.unknown = unknown


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
    "ROSTER_REFRESH_MS",
    "STABLE_CASH_TOKEN",
    "ChainLogPort",
    "ChainTapeDatabasePort",
    "ChainTapeLoop",
    "ChainTapeRepositories",
    "RosterProviderPort",
]
