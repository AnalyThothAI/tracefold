"""The two cold Price Review loops (#88): current Quote Snapshots and deterministic Event Reactions.

Neither is a broker consumer and neither is on the News hot path. They are bounded polling loops that read
their own work from PostgreSQL, call public venue REST with no database connection held, and write one short
batched transaction through a *separate* one-slot cold admission — the four News lane slots belong to the
Deduper, Triage and the Deliverer, and a price backlog must never compete for them.

Failure is local by construction: a venue that times out, blocks, rate-limits or answers nonsense is skipped
for that turn, leaves its previous row untouched (a stale quote stays visibly stale, it never becomes zero or
disappears) and cannot affect ingestion, judgment, delivery, readiness or shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from tracefold.platform.resource import ResourceAdmissionTimeout, ResourceOperationOverrun

from .bus import DeferError, TransientError, now_ms
from .pricing import (
    CANDLE_INTERVAL_MS,
    EXTERNAL_CONCURRENCY,
    HORIZON_MS,
    QUOTE_LOOKBACK_MS,
    QUOTE_PERIOD_SECONDS,
    QUOTE_TURN_DEADLINE_SECONDS,
    REACTION_CANDLE_REQUESTS_MAX,
    REACTION_DUE_BATCH,
    REACTION_HISTORY_MAX_AGE_MS,
    REACTION_PERIOD_SECONDS,
    Candle,
    PriceInstrument,
    ProviderQuote,
    Quote,
    return_bps,
    select_candle,
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
_COLD_READ_TIMEOUT_SECONDS = 10.0
_COLD_WRITE_TIMEOUT_SECONDS = 10.0
# One merged candle request must stay a bounded page. 1000 five-minute bars is ~3.5 days.
_MAX_MERGED_SPAN_MS = 1000 * CANDLE_INTERVAL_MS


class _ColdDb:
    """The price plane's own database admission: one slot, on the heavy-business lane, never the News lane."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self._lane = db.heavy_business()

    async def tx(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float) -> Any:
        def _run() -> Any:
            with self._db.worker_session(name, timeout_seconds) as repos, repos.transaction():
                return fn(repos)

        return await self._run(name, _run, timeout_seconds)

    async def read(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float) -> Any:
        def _run() -> Any:
            with self._db.worker_session(name, timeout_seconds) as repos:
                return fn(repos)

        return await self._run(name, _run, timeout_seconds)

    async def _run(self, name: str, fn: Callable[[], Any], timeout_seconds: float) -> Any:
        try:
            return await self._lane.run_business(name, fn, operation_timeout_seconds=timeout_seconds)
        except ResourceAdmissionTimeout as exc:
            raise DeferError(f"cold_db_admission_timeout:{name}") from exc
        except ResourceOperationOverrun as exc:
            raise TransientError(f"cold_db_overrun:{name}") from exc


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

    def __init__(
        self,
        *,
        db: Any,
        fetcher_for: QuoteFetcherFactory,
        watchlist: Sequence[str] = (),
        period_seconds: float = QUOTE_PERIOD_SECONDS,
        enabled: bool = True,
    ) -> None:
        self.db = _ColdDb(db)
        self.fetcher_for = fetcher_for
        self.watchlist = tuple(watchlist)
        self.period = float(period_seconds)
        self.enabled = bool(enabled)
        self.last_result: dict[str, Any] | None = None
        self.last_error: str | None = None

    async def run(self, *, stop_event: asyncio.Event) -> None:
        if not self.enabled:
            await stop_event.wait()
            return
        while not stop_event.is_set():
            with contextlib.suppress(Exception):
                await self.turn()
            await _sleep_or_stop(stop_event, self.period)

    async def turn(self) -> dict[str, Any]:
        stamp = now_ms()

        def _plan(repos: Any) -> Any:
            return repos.price.plan_quote_targets(since_ms=stamp - QUOTE_LOOKBACK_MS, watchlist=self.watchlist)

        try:
            plan = await self.db.read("news_quote_plan", _plan, timeout_seconds=_COLD_READ_TIMEOUT_SECONDS)
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
        try:
            results = await asyncio.wait_for(
                _gather_bounded(
                    [self._source_call(source, members) for source, members in sorted(groups.items())],
                    limit=EXTERNAL_CONCURRENCY,
                ),
                timeout=QUOTE_TURN_DEADLINE_SECONDS,
            )
        except TimeoutError:
            self.last_error = "turn_deadline"
            log.warning("news quote turn exceeded its deadline sources=%d", len(groups))
            return {**_plan_stats(plan), "sources": len(groups), "written": 0, "quotes": 0}
        received_at_ms = now_ms()
        writes: list[tuple[str, list[Quote], int]] = []
        errors: list[str] = []
        for (source, members), result in zip(sorted(groups.items()), results, strict=False):
            if isinstance(result, BaseException):
                code = getattr(result, "code", None) or type(result).__name__
                errors.append(f"{source}:{code}")
                log.warning("news quote source failed source=%s code=%s", source, code)
                continue
            quotes = _quotes_for(members, result)
            if not quotes:
                # A 200 carrying an error object parses to nothing. Replacing the row with an empty map would
                # flip every symbol on the source from `fresh` to `unavailable` — the exact failure this
                # plane exists to avoid. Leave the previous row to age instead.
                errors.append(f"{source}:venue_payload_empty")
                log.warning("news quote source answered with no usable quote source=%s", source)
                continue
            writes.append((source, quotes, len(members)))
        self.last_error = ",".join(errors) or None
        if not writes:
            return {**_plan_stats(plan), "sources": len(groups), "written": 0, "quotes": 0}

        def _store(repos: Any, rows: list[tuple[str, list[Quote], int]] = writes) -> int:
            for source, quotes, target_count in rows:
                repos.price.replace_source_snapshot(
                    source_key=source,
                    quotes=quotes,
                    target_count=target_count,
                    source_at_ms=max((q.source_at_ms for q in quotes if q.source_at_ms), default=None),
                    received_at_ms=received_at_ms,
                    now_ms=received_at_ms,
                )
            return len(rows)

        try:
            written = await self.db.tx("news_quote_store", _store, timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS)
        except (TransientError, DeferError) as exc:
            self.last_error = f"db:{type(exc).__name__}"
            written = 0
        self.last_result = {
            **_plan_stats(plan),
            "sources": len(groups),
            "written": int(written),
            "quotes": sum(len(quotes) for _, quotes, _ in writes),
        }
        return self.last_result

    def _source_call(self, source: str, members: Sequence[PriceInstrument]) -> Callable[[], Awaitable[Any]]:
        fetcher = self.fetcher_for(source)
        symbols = [instrument.venue_symbol for instrument in members]

        async def _call() -> Sequence[ProviderQuote]:
            if fetcher is None:
                raise TransientError(f"quote_source_unavailable:{source}")
            return await fetcher(symbols)

        return _call


def _plan_stats(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "targets": len(plan["targets"]),
        "input_symbol_count": int(plan.get("input_symbol_count") or 0),
        "unique_symbol_count": int(plan.get("unique_symbol_count") or 0),
        "unique_instrument_count": int(plan.get("unique_instrument_count") or 0),
        "source_group_count": int(plan.get("source_group_count") or 0),
        "dedupe_ratio": float(plan.get("dedupe_ratio") or 0.0),
    }


def _quotes_for(members: Sequence[PriceInstrument], provider: Sequence[ProviderQuote]) -> list[Quote]:
    """Provider numbers keep provider identity; News identity comes from the resolved instrument."""

    by_symbol = {quote.venue_symbol: quote for quote in provider}
    out: list[Quote] = []
    for instrument in members:
        quote = by_symbol.get(instrument.venue_symbol)
        if quote is None:
            continue
        out.append(
            Quote(
                venue=instrument.venue,
                venue_symbol=instrument.venue_symbol,
                base_symbol=instrument.base_symbol,
                price=quote.price,
                price_kind=instrument.price_kind,
                instrument_class=instrument.instrument_class,
                quote_asset=instrument.quote_asset,
                change_pct=quote.change_pct,
                change_basis=quote.change_basis,
                source_at_ms=quote.source_at_ms,
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

    def __init__(
        self,
        *,
        db: Any,
        fetcher_for: CandleFetcherFactory,
        period_seconds: float = REACTION_PERIOD_SECONDS,
        enabled: bool = True,
    ) -> None:
        self.db = _ColdDb(db)
        self.fetcher_for = fetcher_for
        self.period = float(period_seconds)
        self.enabled = bool(enabled)
        self.last_result: dict[str, Any] | None = None
        self.last_error: str | None = None

    async def run(self, *, stop_event: asyncio.Event) -> None:
        if not self.enabled:
            await stop_event.wait()
            return
        while not stop_event.is_set():
            chained = 0
            full = True
            while full and chained < _MAX_CHAINED_TURNS and not stop_event.is_set():
                result: dict[str, Any] = {}
                with contextlib.suppress(Exception):
                    result = await self.turn()
                full = bool(result.get("full_batch"))
                chained += 1
                if full:
                    await _sleep_or_stop(stop_event, _CHAIN_YIELD_SECONDS)
            await _sleep_or_stop(stop_event, self.period)

    async def turn(self) -> dict[str, Any]:
        stamp = now_ms()

        def _due(repos: Any) -> Any:
            rows = repos.price.due_reactions(now_ms=stamp, limit=REACTION_DUE_BATCH)
            symbols = sorted({str(row["symbol"]) for row in rows})
            return rows, repos.price.resolve_instruments(symbols)

        try:
            rows, instruments = await self.db.read(
                "news_reaction_due", _due, timeout_seconds=_COLD_READ_TIMEOUT_SECONDS
            )
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
        results = await _gather_bounded(
            [self._candle_call(request) for request in requests], limit=EXTERNAL_CONCURRENCY
        )
        candles: dict[tuple[str, str], list[Candle]] = {}
        errors: list[str] = []
        for request, result in zip(requests, results, strict=False):
            key = (request["venue"], request["venue_symbol"])
            if isinstance(result, BaseException):
                code = getattr(result, "code", None) or type(result).__name__
                errors.append(f"{request['venue']}:{code}")
                continue
            candles.setdefault(key, []).extend(result)
        self.last_error = ",".join(sorted(set(errors))) or None

        writes = [*terminal]
        for row in pending:
            computed = _reaction_row(row, candles=candles, now_ms=stamp)
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
            written = await self.db.tx("news_reaction_store", _store, timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS)
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
            if fetcher is None:
                raise TransientError(f"candle_source_unavailable:{request['venue']}")
            return await fetcher(str(request["venue_symbol"]), int(request["start_ms"]), int(request["end_ms"]))

        return _call


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
    row: Mapping[str, Any], *, candles: Mapping[tuple[str, str], Sequence[Candle]], now_ms: int
) -> dict[str, Any] | None:
    """One Event-asset's row, or None when the provider call failed and the work stays retryable.

    A transient provider failure is loop health, never a semantic reason: leaving the row untouched keeps it
    in the due scan instead of terminalizing an Event as unpriceable because a request timed out once.
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
    bars = candles.get((instrument.venue, instrument.venue_symbol))
    if not bars:
        return None
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
