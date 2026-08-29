"""The bounded Price Review loops (#88, #304): current Quote Snapshots and deterministic Event Reactions.

Neither is a broker consumer and neither is on the News hot path. They are bounded polling loops that read
their own work from PostgreSQL, call public venue REST with no database connection held, and write one short
batched transaction. Quotes use ordinary business admission; heavier candle Reactions keep the one-slot
heavy gate. Neither can occupy the four News lane slots owned by Deduper, Triage and Deliverer.

Failure is local by construction: a venue that times out, blocks, rate-limits or answers nonsense is skipped
for that turn, leaves its previous row untouched (a stale quote stays visibly stale, it never becomes zero or
disappears) and cannot affect ingestion, judgment, delivery, readiness or shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from decimal import Decimal
from typing import Any, ClassVar, Protocol

from ..bus import DeferError, TransientError, now_ms
from ..telemetry import (
    NewsExternalDataOutcome,
    NewsExternalDataSource,
    NewsExternalDataTelemetryPort,
    NewsWorkSemantics,
)
from .pricing import (
    CANDLE_INTERVAL_MS,
    EXTERNAL_CONCURRENCY,
    HORIZON_MS,
    QUOTE_DAY_PERIOD_SECONDS,
    QUOTE_LOOKBACK_MS,
    QUOTE_PERIOD_SECONDS,
    QUOTE_SOURCE_GROUP_MAX,
    QUOTE_TURN_DEADLINE_SECONDS,
    REACTION_CANDLE_REQUESTS_MAX,
    REACTION_DUE_BATCH,
    REACTION_HISTORY_MAX_AGE_MS,
    REACTION_PERIOD_SECONDS,
    Candle,
    PriceInstrument,
    ProviderQuote,
    Quote,
    parse_change_pct,
    reference_freshness,
    return_bps,
    select_candle,
    source_rank,
)

log = logging.getLogger("tracefold.news.price")

QuoteFetcher = Callable[[Sequence[str]], Awaitable[Sequence[ProviderQuote]]]
CandleFetcher = Callable[[str, int, int], Awaitable[Sequence[Candle]]]
QuoteFetcherFactory = Callable[[str], QuoteFetcher | None]
CandleFetcherFactory = Callable[[str], CandleFetcher | None]

# A backfill turn that filled its whole batch immediately asks for the next one; without a ceiling that is a
# hot loop, and with one the cadence takes over as soon as the backlog is drained.
_MAX_CHAINED_TURNS = 20
_CHAIN_YIELD_SECONDS = 0.2
_DB_READ_TIMEOUT_SECONDS = 10.0
_DB_WRITE_TIMEOUT_SECONDS = 10.0
# One merged candle request must stay a bounded page. 1000 five-minute bars is ~3.5 days.
_MAX_MERGED_SPAN_MS = 1000 * CANDLE_INTERVAL_MS


class QuoteDatabasePort(Protocol):
    """Bounded quote plan/store; the composition root maps it to ordinary business admission."""

    async def tx[T](self, name: str, fn: Callable[[Any], T], *, timeout_seconds: float) -> T: ...

    async def read[T](self, name: str, fn: Callable[[Any], T], *, timeout_seconds: float) -> T: ...


class ReactionDatabasePort(Protocol):
    """Bounded reaction read/write; the composition root maps it to heavy business admission."""

    async def tx[T](self, name: str, fn: Callable[[Any], T], *, timeout_seconds: float) -> T: ...

    async def read[T](self, name: str, fn: Callable[[Any], T], *, timeout_seconds: float) -> T: ...


async def _sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop_event.wait(), timeout=max(0.001, float(seconds)))


async def _gather_bounded(calls: Sequence[Callable[[], Awaitable[Any]]], *, limit: int) -> list[Any]:
    """Run provider calls with bounded concurrency; one failure is that call's result, never the batch's."""

    gate = asyncio.Semaphore(max(1, int(limit)))

    async def _one(call: Callable[[], Awaitable[Any]]) -> Any:
        async with gate:
            return await call()

    return await asyncio.gather(*(_one(call) for call in calls), return_exceptions=True)


# ---------------------------------------------------------------------------- quotes
class QuoteSnapshotLoop:
    """Current prices for the bounded working set: one batch request per source, one row per source.

    Work is `O(source groups)`, never `O(Events x assets)` — a hundred Events mentioning BTC are one target
    and one provider result. Turns never overlap and are never queued: a turn that overruns the cadence
    simply means the next one starts late, and the refresh it missed had no durable value anyway.
    """

    work_semantics: ClassVar[tuple[NewsWorkSemantics, ...]] = ("latest_state",)

    def __init__(
        self,
        *,
        db: QuoteDatabasePort,
        fetcher_for: QuoteFetcherFactory,
        day_fetcher_for: QuoteFetcherFactory | None = None,
        watchlist: Sequence[str] = (),
        period_seconds: float = QUOTE_PERIOD_SECONDS,
        day_period_seconds: float = QUOTE_DAY_PERIOD_SECONDS,
        enabled: bool = True,
        telemetry: NewsExternalDataTelemetryPort | None = None,
    ) -> None:
        self.db = db
        self.fetcher_for = fetcher_for
        self.day_fetcher_for = day_fetcher_for
        self.watchlist = tuple(watchlist)
        self.period = float(period_seconds)
        self.day_period_ms = max(0.0, float(day_period_seconds)) * 1000.0
        self.enabled = bool(enabled)
        self.telemetry = telemetry
        self.last_result: dict[str, Any] | None = None
        self.last_error: str | None = None
        # Per source, from the last *successful* day read: the reference each symbol's day change is measured
        # against, which symbols that read covered, and when it landed. Nothing here is stamped for a call
        # that failed or was cancelled — an unanswered question has not been asked (#109).
        self._references: dict[str, dict[str, tuple[Decimal, int]]] = {}
        self._covered: dict[str, set[str]] = {}
        self._day_at_ms: dict[str, int] = {}

    async def run(self, *, stop_event: asyncio.Event) -> None:
        if not self.enabled:
            if self.telemetry is not None:
                self.telemetry.record_external_data_skipped("quote_snapshot", "disabled")
            await stop_event.wait()
            return
        while not stop_event.is_set():
            started = time.perf_counter()
            result: dict[str, Any] = {}
            try:
                result = await self.turn()
            except Exception:
                if self.telemetry is not None:
                    self.telemetry.record_external_data_turn(
                        "quote_snapshot",
                        "error",
                        time.perf_counter() - started,
                    )
            else:
                if self.telemetry is not None:
                    self.telemetry.record_external_data_turn(
                        "quote_snapshot",
                        _external_data_outcome(self.last_error, progress=int(result.get("written") or 0)),
                        time.perf_counter() - started,
                        target_count=int(result.get("targets") or 0),
                        source_count=int(result.get("sources") or 0),
                    )
            await _sleep_or_stop(stop_event, max(0.0, self.period - (time.perf_counter() - started)))

    async def turn(self) -> dict[str, Any]:
        stamp = now_ms()

        def _plan(repos: Any) -> Any:
            return repos.price.plan_quote_targets(since_ms=stamp - QUOTE_LOOKBACK_MS, watchlist=self.watchlist)

        try:
            plan = await self.db.read("news_quote_plan", _plan, timeout_seconds=_DB_READ_TIMEOUT_SECONDS)
        except (TransientError, DeferError) as exc:
            self.last_error = f"db:{type(exc).__name__}"
            return {"targets": 0, "sources": 0, "written": 0}
        targets: list[PriceInstrument] = list(plan["targets"])
        groups: dict[str, list[PriceInstrument]] = {}
        for instrument in targets:
            groups.setdefault(instrument.source_key, []).append(instrument)
        if not groups:
            self.last_error = None
            self.last_result = {**_plan_stats(plan), "sources": 0, "written": 0, "quotes": 0}
            return self.last_result
        self._bound_cache(groups)
        planned = sorted(groups, key=lambda source: (source_rank(source), source))
        day_reads = {source for source in planned if self._day_due(source, groups[source], stamp)}
        gate = asyncio.Semaphore(EXTERNAL_CONCURRENCY)

        async def _bounded(source: str) -> tuple[Sequence[ProviderQuote], int]:
            async with gate:
                return await self._source_call(source, groups[source])()

        tasks = {asyncio.create_task(_bounded(source)): source for source in planned}
        try:
            done, pending = await asyncio.wait(tasks, timeout=QUOTE_TURN_DEADLINE_SECONDS)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            log.warning(
                "news quote turn reached its deadline completed=%d pending=%d",
                len(done),
                len(pending),
            )

        writes: list[tuple[str, list[Quote], int, int]] = []
        errors: list[str] = []
        for task, source in tasks.items():
            if task in pending:
                errors.append(f"{source}:turn_deadline")
                continue
            members = groups[source]
            try:
                result, received_at_ms = task.result()
            except asyncio.CancelledError:
                errors.append(f"{source}:cancelled")
                log.warning("news quote source cancelled source=%s", source)
                continue
            except Exception as exc:
                code = getattr(exc, "code", None) or type(exc).__name__
                errors.append(f"{source}:{code}")
                log.warning("news quote source failed source=%s code=%s", source, code)
                continue
            self._absorb_current_references(source, result, received_at_ms)
            quotes = _quotes_for(
                members,
                result,
                self._references.get(source),
                measured_at_ms=received_at_ms,
            )
            if not quotes:
                # A 200 carrying an error object parses to nothing. Replacing the row with an empty map would
                # flip every symbol on the source from `fresh` to `unavailable` — the exact failure this
                # plane exists to avoid. Leave the previous row to age instead.
                errors.append(f"{source}:venue_payload_empty")
                log.warning("news quote source answered with no usable quote source=%s", source)
                continue
            writes.append((source, quotes, len(members), received_at_ms))
        self.last_error = ",".join(errors) or None
        if not writes:
            self.last_result = {**_plan_stats(plan), "sources": len(groups), "written": 0, "quotes": 0}
            return self.last_result

        planned_sources = planned

        def _store(repos: Any, rows: list[tuple[str, list[Quote], int, int]] = writes) -> int:
            # A source whose targets rotated out of the working set has no reader left, and its row would
            # otherwise sit there ageing forever — reporting one permanently stale source and serving a
            # month-old price to anything that still resolved to it. Sources that were planned but *failed*
            # are not touched: keeping their previous value is the whole stale-not-blank invariant.
            repos.price.forget_sources_except(planned_sources)
            for source, quotes, target_count, received_at_ms in rows:
                repos.price.replace_source_snapshot(
                    source_key=source,
                    quotes=quotes,
                    target_count=target_count,
                    source_at_ms=max((q.source_at_ms for q in quotes if q.source_at_ms is not None), default=None),
                    received_at_ms=received_at_ms,
                    now_ms=received_at_ms,
                )
            return len(rows)

        try:
            written = await self.db.tx("news_quote_store", _store, timeout_seconds=_DB_WRITE_TIMEOUT_SECONDS)
        except (TransientError, DeferError) as exc:
            self.last_error = f"db:{type(exc).__name__}"
            written = 0
        if written:
            day_errors = await self._refresh_day_references(
                [source for source, _quotes, _target_count, _received_at_ms in writes if source in day_reads][:2],
                groups,
            )
            errors.extend(day_errors)
            self.last_error = ",".join(errors) or None
        self.last_result = {
            **_plan_stats(plan),
            "sources": len(groups),
            "written": int(written),
            "quotes": sum(len(quotes) for _, quotes, _, _ in writes),
        }
        return self.last_result

    def _source_call(
        self, source: str, members: Sequence[PriceInstrument]
    ) -> Callable[[], Awaitable[tuple[Sequence[ProviderQuote], int]]]:
        """The mandatory current request for one source; day enrichment never enters this deadline."""

        fetcher = self.fetcher_for(source)
        symbols = [instrument.venue_symbol for instrument in members]

        return self._provider_call(source, symbols, fetcher)

    def _day_call(
        self, source: str, members: Sequence[PriceInstrument]
    ) -> Callable[[], Awaitable[tuple[Sequence[ProviderQuote], int]]]:
        fetcher = self.day_fetcher_for(source) if self.day_fetcher_for else None
        symbols = [instrument.venue_symbol for instrument in members]
        return self._provider_call(source, symbols, fetcher)

    def _provider_call(
        self,
        source: str,
        symbols: Sequence[str],
        fetcher: QuoteFetcher | None,
    ) -> Callable[[], Awaitable[tuple[Sequence[ProviderQuote], int]]]:

        async def _call() -> tuple[Sequence[ProviderQuote], int]:
            started = time.perf_counter()
            try:
                if fetcher is None:
                    raise TransientError(f"quote_source_unavailable:{source}")
                result = await fetcher(symbols)
            except asyncio.CancelledError:
                if self.telemetry is not None:
                    self.telemetry.record_external_data_provider_call(
                        "quote_snapshot",
                        _external_data_source(source),
                        "error",
                        time.perf_counter() - started,
                    )
                raise
            except Exception:
                if self.telemetry is not None:
                    self.telemetry.record_external_data_provider_call(
                        "quote_snapshot",
                        _external_data_source(source),
                        "error",
                        time.perf_counter() - started,
                    )
                raise
            if self.telemetry is not None:
                self.telemetry.record_external_data_provider_call(
                    "quote_snapshot",
                    _external_data_source(source),
                    "success",
                    time.perf_counter() - started,
                )
            return result, now_ms()

        return _call

    async def _refresh_day_references(
        self,
        sources: Sequence[str],
        groups: Mapping[str, Sequence[PriceInstrument]],
    ) -> list[str]:
        """Refresh at most the two Binance references after the current transaction has committed."""

        if not sources:
            return []
        results = await asyncio.gather(
            *(self._day_call(source, groups[source])() for source in sources),
            return_exceptions=True,
        )
        errors: list[str] = []
        for source, result in zip(sources, results, strict=True):
            if isinstance(result, BaseException):
                code = getattr(result, "code", None) or type(result).__name__
                errors.append(f"{source}:day:{code}")
                log.warning("news quote day reference failed source=%s code=%s", source, code)
                continue
            quotes, received_at_ms = result
            if not self._absorb_day_read(source, groups[source], quotes, received_at_ms):
                errors.append(f"{source}:day:venue_payload_empty")
        return errors

    def _day_due(self, source: str, members: Sequence[PriceInstrument], stamp: int) -> bool:
        """Due on the cadence, and immediately for a symbol no day read has covered yet.

        The working set is ordered newest Event first, so the symbol that just joined it is the card the
        operator is looking at right now — making it wait five minutes for its percentage is the wrong
        trade. `_covered` records what the last day read *asked* for, not what it answered, so a symbol no
        venue lists is covered after one attempt and cannot pin the source to the expensive endpoint.
        """

        if source not in {"binance.perp", "binance.spot"}:
            return False
        if self.day_fetcher_for is None or self.day_fetcher_for(source) is None:
            return False
        landed = self._day_at_ms.get(source)
        if landed is None or stamp - landed >= self.day_period_ms:
            return True
        covered = self._covered.get(source) or set()
        return any(instrument.venue_symbol not in covered for instrument in members)

    def _absorb_day_read(
        self, source: str, members: Sequence[PriceInstrument], quotes: Sequence[ProviderQuote], stamp: int
    ) -> bool:
        """Cache what this optional read answered. Only a usable reference stamps its cadence."""

        references = {quote.venue_symbol: quote.reference_price for quote in quotes if quote.reference_price}
        if not references:
            return False
        cache = self._references.setdefault(source, {})
        cache.update({symbol: (reference, stamp) for symbol, reference in references.items()})
        self._covered[source] = {instrument.venue_symbol for instrument in members}
        self._day_at_ms[source] = stamp
        return True

    def _absorb_current_references(
        self,
        source: str,
        quotes: Sequence[ProviderQuote],
        received_at_ms: int,
    ) -> None:
        """Hyperliquid carries its day reference in the mandatory current response."""

        references = {quote.venue_symbol: quote.reference_price for quote in quotes if quote.reference_price}
        if not references:
            return
        cache = self._references.setdefault(source, {})
        cache.update({symbol: (reference, received_at_ms) for symbol, reference in references.items()})

    def _bound_cache(self, groups: Mapping[str, Any]) -> None:
        """Keep each active source bounded to its current members without punishing a one-turn absence.

        A source can drop out of a single plan (a burst of Events pushes it past `QUOTE_TARGET_MAX`) and be
        back the next turn; evicting on sight would make it re-pay for the wide endpoint every time that
        happens. An active source, however, must not retain every symbol that ever rotated through its plan.
        """

        for source, members in groups.items():
            wanted = {instrument.venue_symbol for instrument in members}
            if source in self._references:
                self._references[source] = {
                    symbol: reference for symbol, reference in self._references[source].items() if symbol in wanted
                }
            if source in self._covered:
                self._covered[source].intersection_update(wanted)
        if len(self._references) <= 2 * QUOTE_SOURCE_GROUP_MAX:
            return
        for source in [key for key in self._references if key not in groups]:
            self._references.pop(source, None)
            self._covered.pop(source, None)
            self._day_at_ms.pop(source, None)


def _plan_stats(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "targets": len(plan["targets"]),
        "input_symbol_count": int(plan.get("input_symbol_count") or 0),
        "unique_symbol_count": int(plan.get("unique_symbol_count") or 0),
        "unique_instrument_count": int(plan.get("unique_instrument_count") or 0),
        "source_group_count": int(plan.get("source_group_count") or 0),
        "dedupe_ratio": float(plan.get("dedupe_ratio") or 0.0),
    }


def _quotes_for(
    members: Sequence[PriceInstrument],
    provider: Sequence[ProviderQuote],
    references: Mapping[str, tuple[Decimal, int]] | None = None,
    *,
    measured_at_ms: int,
) -> list[Quote]:
    """Provider numbers keep provider identity; News identity comes from the resolved instrument.

    The percentage is always derived from this current price and a separately aged reference. Hyperliquid
    refreshes that reference in the same current response; Binance refreshes it after the current store and
    therefore exposes it on the next natural turn. Missing or expired references remove only the percentage.
    """

    by_symbol = {quote.venue_symbol: quote for quote in provider}
    out: list[Quote] = []
    for instrument in members:
        quote = by_symbol.get(instrument.venue_symbol)
        if quote is None:
            continue
        reference = references.get(instrument.venue_symbol) if references else None
        reference_price, reference_at_ms = reference if reference else (None, None)
        _reference_age_ms, reference_valid = reference_freshness(
            measured_at_ms=measured_at_ms,
            reference_at_ms=reference_at_ms,
        )
        change_pct = parse_change_pct(quote.price, reference_price) if reference_valid else None
        out.append(
            Quote(
                venue=instrument.venue,
                venue_symbol=instrument.venue_symbol,
                base_symbol=instrument.base_symbol,
                price=quote.price,
                price_kind=instrument.price_kind,
                instrument_class=instrument.instrument_class,
                quote_asset=instrument.quote_asset,
                change_pct=change_pct,
                change_basis=quote.change_basis,
                source_at_ms=quote.source_at_ms,
                reference_at_ms=reference_at_ms,
            )
        )
    return out


# ---------------------------------------------------------------------------- event reactions
class EventReactionLoop:
    """Deterministic 1H/4H returns for due Event-assets, from historical closed candles.

    The loop is a cold evaluator, not a delivery plane: it never republishes, never wakes a consumer, and
    being offline at anchor+1H costs nothing because the work is durable in PostgreSQL and the candles are
    still there an hour later.
    """

    work_semantics: ClassVar[tuple[NewsWorkSemantics, ...]] = ("derived_work",)

    def __init__(
        self,
        *,
        db: ReactionDatabasePort,
        fetcher_for: CandleFetcherFactory,
        period_seconds: float = REACTION_PERIOD_SECONDS,
        enabled: bool = True,
        telemetry: NewsExternalDataTelemetryPort | None = None,
    ) -> None:
        self.db = db
        self.fetcher_for = fetcher_for
        self.period = float(period_seconds)
        self.enabled = bool(enabled)
        self.telemetry = telemetry
        self.last_result: dict[str, Any] | None = None
        self.last_error: str | None = None
        self._last_source_count = 0

    async def run(self, *, stop_event: asyncio.Event) -> None:
        if not self.enabled:
            if self.telemetry is not None:
                self.telemetry.record_external_data_skipped("event_reaction", "disabled")
            await stop_event.wait()
            return
        while not stop_event.is_set():
            chained = 0
            full = True
            while full and chained < _MAX_CHAINED_TURNS and not stop_event.is_set():
                started = time.perf_counter()
                result: dict[str, Any] = {}
                try:
                    result = await self.turn()
                except Exception:
                    if self.telemetry is not None:
                        self.telemetry.record_external_data_turn(
                            "event_reaction",
                            "error",
                            time.perf_counter() - started,
                        )
                else:
                    if self.telemetry is not None:
                        self.telemetry.record_external_data_turn(
                            "event_reaction",
                            _external_data_outcome(self.last_error, progress=int(result.get("written") or 0)),
                            time.perf_counter() - started,
                            target_count=int(result.get("due") or 0),
                            source_count=self._last_source_count,
                        )
                full = bool(result.get("full_batch"))
                chained += 1
                if full:
                    await _sleep_or_stop(stop_event, _CHAIN_YIELD_SECONDS)
            await _sleep_or_stop(stop_event, self.period)

    async def turn(self) -> dict[str, Any]:
        stamp = now_ms()
        self._last_source_count = 0

        def _due(repos: Any) -> Any:
            rows = repos.price.due_reactions(now_ms=stamp, limit=REACTION_DUE_BATCH)
            symbols = sorted({str(row["symbol"]) for row in rows})
            return rows, repos.price.resolve_instruments(symbols)

        try:
            rows, instruments = await self.db.read("news_reaction_due", _due, timeout_seconds=_DB_READ_TIMEOUT_SECONDS)
        except (TransientError, DeferError) as exc:
            self.last_error = f"db:{type(exc).__name__}"
            return {"due": 0, "written": 0, "requests": 0, "full_batch": False}
        if not rows:
            self.last_error = None
            self.last_result = {"due": 0, "written": 0, "requests": 0, "full_batch": False}
            return self.last_result

        pending: list[dict[str, Any]] = []
        terminal: list[dict[str, Any]] = []
        for row in rows:
            anchor = int(row["anchor_at_ms"])
            instrument = _pinned_instrument(row) or instruments.get(str(row["symbol"]))
            reason = None
            if instrument is None:
                reason = "instrument_unresolved"
            elif anchor < stamp - REACTION_HISTORY_MAX_AGE_MS:
                # A public candle window is finite, so an Event this old will never be priced. Saying so once
                # is a fact; asking the provider again every minute for the rest of retention is a spin.
                reason = "history_expired"
            if reason is not None:
                terminal.append(
                    {
                        "event_id": row["event_id"],
                        "symbol": row["symbol"],
                        "anchor_at_ms": anchor,
                        "venue": instrument.venue if instrument else "",
                        "venue_symbol": instrument.venue_symbol if instrument else "",
                        "instrument_class": instrument.instrument_class if instrument else "unknown",
                        "is_primary": bool(row.get("is_primary")),
                        "state": "unavailable",
                        "unavailable_reason": reason,
                    }
                )
                continue
            pending.append({**row, "instrument": instrument})

        requests = _plan_candle_requests(pending, now_ms=stamp)
        self._last_source_count = len({str(request["venue"]) for request in requests})
        results = await _gather_bounded(
            [self._candle_call(request) for request in requests], limit=EXTERNAL_CONCURRENCY
        )
        candles: dict[tuple[str, str], list[Candle]] = {}
        # Which instruments the provider actually answered for. An answer carrying no bars is a fact about
        # the market — Hyperliquid lists spot pairs that have never traded and returns `[]` for them
        # forever — while no answer at all is loop health. Conflating the two left those rows unwritten and
        # therefore permanently due: 31 of them sat at the head of the oldest-first scan pinning the backlog
        # SLO at 52 h and re-requesting dead markets every turn.
        answered: set[tuple[str, str]] = set()
        errors: list[str] = []
        for request, result in zip(requests, results, strict=False):
            key = (request["venue"], request["venue_symbol"])
            if isinstance(result, BaseException):
                code = getattr(result, "code", None) or type(result).__name__
                errors.append(f"{request['venue']}:{code}")
                continue
            answered.add(key)
            candles.setdefault(key, []).extend(result)
        self.last_error = ",".join(sorted(set(errors))) or None

        writes = [*terminal]
        for row in pending:
            computed = _reaction_row(row, candles=candles, answered=answered, now_ms=stamp)
            if computed is not None:
                writes.append(computed)
        if not writes:
            self.last_result = {
                "due": len(rows),
                "written": 0,
                "requests": len(requests),
                "full_batch": len(rows) >= REACTION_DUE_BATCH,
            }
            return self.last_result

        def _store(repos: Any, batch: list[dict[str, Any]] = writes) -> int:
            for row in batch:
                repos.price.upsert_reaction(row, now_ms=stamp)
            return len(batch)

        try:
            written = await self.db.tx("news_reaction_store", _store, timeout_seconds=_DB_WRITE_TIMEOUT_SECONDS)
        except (TransientError, DeferError) as exc:
            self.last_error = f"db:{type(exc).__name__}"
            written = 0
        self.last_result = {
            "due": len(rows),
            "written": int(written),
            "requests": len(requests),
            "terminal": len(terminal),
            "instruments": len({(row["instrument"].venue, row["instrument"].venue_symbol) for row in pending}),
            "full_batch": len(rows) >= REACTION_DUE_BATCH,
        }
        return self.last_result

    def _candle_call(self, request: Mapping[str, Any]) -> Callable[[], Awaitable[Any]]:
        fetcher = self.fetcher_for(str(request["venue"]))

        async def _call() -> Sequence[Candle]:
            started = time.perf_counter()
            venue = str(request["venue"])
            try:
                if fetcher is None:
                    raise TransientError(f"candle_source_unavailable:{venue}")
                result = await fetcher(
                    str(request["venue_symbol"]),
                    int(request["start_ms"]),
                    int(request["end_ms"]),
                )
            except Exception:
                if self.telemetry is not None:
                    self.telemetry.record_external_data_provider_call(
                        "event_reaction",
                        _external_data_source(venue),
                        "error",
                        time.perf_counter() - started,
                    )
                raise
            if self.telemetry is not None:
                self.telemetry.record_external_data_provider_call(
                    "event_reaction",
                    _external_data_source(venue),
                    "success",
                    time.perf_counter() - started,
                )
            return result

        return _call


def _external_data_source(source: str) -> NewsExternalDataSource:
    if source == "binance.spot":
        return "binance_spot"
    if source == "binance.perp":
        return "binance_perp"
    if source.startswith("hl."):
        return "hyperliquid"
    if source.startswith("okx."):
        return "okx"
    return "other"


def _external_data_outcome(error: str | None, *, progress: int) -> NewsExternalDataOutcome:
    if error is None:
        return "success"
    return "partial" if progress > 0 else "error"


def _pinned_instrument(row: Mapping[str, Any]) -> PriceInstrument | None:
    """A partially filled row keeps its source pinned: 4H must be measured on the contract 1H came from."""

    venue, venue_symbol = str(row.get("venue") or ""), str(row.get("venue_symbol") or "")
    if not venue or not venue_symbol:
        return None
    return PriceInstrument(
        venue=venue,
        venue_symbol=venue_symbol,
        base_symbol=str(row.get("symbol") or ""),
        instrument_class=str(row.get("instrument_class") or "unknown"),
    )


def _needed_window(row: Mapping[str, Any], *, now_ms: int) -> tuple[int, int] | None:
    """Only the neighbourhood this row still needs: a persisted price point is never refetched.

    A row measured for the first time *after* its 4H horizon has matured — every Event in the initial
    backfill, and everything behind a worker outage longer than three hours — needs one window covering both
    horizons. Fetching only the 1H neighbourhood there would find no bar at anchor+4H and write the row off
    as `no_candle_within_gap`, which the due scan treats as terminal: the 4H return would be lost for good.
    """

    anchor = int(row["anchor_at_ms"])
    has_early = row.get("p0") is not None and row.get("p1") is not None
    if not has_early:
        matured = anchor + HORIZON_MS["4h"] <= int(now_ms)
        horizon = HORIZON_MS["4h"] if matured else HORIZON_MS["1h"]
        return (anchor - 2 * CANDLE_INTERVAL_MS, anchor + horizon + CANDLE_INTERVAL_MS)
    if anchor + HORIZON_MS["4h"] <= now_ms:
        target = anchor + HORIZON_MS["4h"]
        return (target - 2 * CANDLE_INTERVAL_MS, target + CANDLE_INTERVAL_MS)
    return None


def _plan_candle_requests(rows: Sequence[Mapping[str, Any]], *, now_ms: int) -> list[dict[str, Any]]:
    """One request per instrument per merged time range, capped — several Events share one response.

    Rows the cap leaves out stay durable in PostgreSQL and are simply picked up by the next turn; nothing is
    dropped and no in-memory queue holds correctness.
    """

    wanted: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for row in rows:
        window = _needed_window(row, now_ms=now_ms)
        if window is None:
            continue
        instrument: PriceInstrument = row["instrument"]
        wanted.setdefault((instrument.venue, instrument.venue_symbol), []).append(window)
    planned: list[dict[str, Any]] = []
    for (venue, venue_symbol), windows in sorted(wanted.items()):
        for start_ms, end_ms in _merge_ranges(windows):
            planned.append({"venue": venue, "venue_symbol": venue_symbol, "start_ms": start_ms, "end_ms": end_ms})
    planned.sort(key=lambda request: (request["start_ms"], request["venue"], request["venue_symbol"]))
    return planned[:REACTION_CANDLE_REQUESTS_MAX]


def _merge_ranges(windows: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start_ms, end_ms in sorted(windows):
        if merged and start_ms <= merged[-1][1] and end_ms - merged[-1][0] <= _MAX_MERGED_SPAN_MS:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end_ms))
            continue
        merged.append((start_ms, end_ms))
    return merged


def _reaction_row(
    row: Mapping[str, Any],
    *,
    candles: Mapping[tuple[str, str], Sequence[Candle]],
    answered: Collection[tuple[str, str]],
    now_ms: int,
) -> dict[str, Any] | None:
    """One Event-asset's row, or None when the provider never answered and the work stays retryable.

    A transient provider failure is loop health, never a semantic reason: leaving the row untouched keeps it
    in the due scan instead of terminalizing an Event as unpriceable because a request timed out once. An
    empty answer is the opposite — the venue is telling us this contract has no trades in that window, which
    is exactly `no_candle_within_gap` and is terminal.
    """

    instrument: PriceInstrument = row["instrument"]
    anchor = int(row["anchor_at_ms"])
    window = _needed_window(row, now_ms=now_ms)
    base = {
        "event_id": row["event_id"],
        "symbol": row["symbol"],
        "anchor_at_ms": anchor,
        # Recorded at measurement time: the review's event-level sample is the median over the model's
        # primaries, and reading that back out of verdict JSONB per request does not fit the 720 h budget.
        "is_primary": bool(row.get("is_primary")),
        "venue": instrument.venue,
        "venue_symbol": instrument.venue_symbol,
        "instrument_class": instrument.instrument_class,
        "p0": row.get("p0"),
        "p0_at_ms": row.get("p0_at_ms"),
        "p1": row.get("p1"),
        "p1_at_ms": row.get("p1_at_ms"),
    }
    if window is None:
        return None
    key = (instrument.venue, instrument.venue_symbol)
    bars = candles.get(key)
    if not bars:
        if key not in answered:
            return None  # no answer (failed, or the request cap deferred it): stay due
        # The horizon is already due — the due scan is what put this row here — so an empty window is a
        # hole the provider has no bar for, not a "not yet".
        state = "partial" if base["p0"] is not None else "unavailable"
        return {**base, "state": state, "unavailable_reason": "no_candle_within_gap"}
    if base["p0"] is None:
        p0 = select_candle(bars, target_ms=anchor)
        p1 = select_candle(bars, target_ms=anchor + HORIZON_MS["1h"])
        if p0 is None or p1 is None:
            return {**base, "state": "unavailable", "unavailable_reason": "no_candle_within_gap"}
        base |= {
            "p0": p0.close,
            "p0_at_ms": p0.close_at_ms,
            "p1": p1.close,
            "p1_at_ms": p1.close_at_ms,
            "return_1h_bps": return_bps(p0.close, p1.close),
        }
    if anchor + HORIZON_MS["4h"] > now_ms:
        return {**base, "state": "partial"}
    p4 = select_candle(bars, target_ms=anchor + HORIZON_MS["4h"])
    if p4 is None:
        # 1H stands on its own, so the row keeps it and names why 4H is missing rather than discarding a
        # measurement that already succeeded. The named reason is what makes the row terminal in the due scan.
        return {**base, "state": "partial", "unavailable_reason": "no_candle_within_gap"}
    return {
        **base,
        "p4": p4.close,
        "p4_at_ms": p4.close_at_ms,
        "return_4h_bps": return_bps(base["p0"], p4.close),
        "state": "complete",
    }


__all__ = [
    "CandleFetcher",
    "CandleFetcherFactory",
    "EventReactionLoop",
    "QuoteFetcher",
    "QuoteFetcherFactory",
    "QuoteSnapshotLoop",
]
