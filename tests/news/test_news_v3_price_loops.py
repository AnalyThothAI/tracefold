"""Worker-turn tests for the bounded Price Review loops (#88, #304), against injected fake venue adapters.

The seam under test is the highest useful slice that stays deterministic: fake adapter -> one loop turn ->
the repository calls that turn produced. Provider payloads, HTTP and PostgreSQL each have their own tests;
what matters here is the arithmetic of the planners — how much work a turn creates, what it writes when a
venue fails, and what it refuses to terminalize.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import pytest

from tracefold.app.workers.wiring.database import WorkerQuoteDatabase, WorkerReactionDatabase
from tracefold.news.bus import now_ms
from tracefold.news.market_review import loops as loops_module
from tracefold.news.market_review.loops import EventReactionLoop, QuoteSnapshotLoop
from tracefold.news.market_review.pricing import (
    CANDLE_INTERVAL_MS,
    HORIZON_MS,
    QUOTE_DAY_PERIOD_SECONDS,
    QUOTE_SOURCE_GROUP_MAX,
    QUOTE_TARGET_MAX,
    REACTION_CANDLE_REQUESTS_MAX,
    Candle,
    PriceInstrument,
    ProviderQuote,
)
from tracefold.platform.observability import TelemetryRegistry

ANCHOR = 1_787_000_100_000


class _FakePrice:
    """The repository surface the loops touch, recording what each turn asked for and wrote."""

    def __init__(self, *, targets: list[PriceInstrument] | None = None, due: list[dict[str, Any]] | None = None):
        self._targets = targets or []
        self._due = due or []
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.forgotten: list[str] = []
        self.reactions: list[dict[str, Any]] = []
        self.instruments: dict[str, PriceInstrument] = {}

    def plan_quote_targets(self, *, since_ms: int, watchlist: Any = ()) -> dict[str, Any]:
        del since_ms, watchlist
        return {
            "targets": self._targets,
            "input_symbol_count": len(self._targets),
            "unique_symbol_count": len(self._targets),
            "unique_instrument_count": len(self._targets),
            "source_group_count": len({t.source_key for t in self._targets}),
            "dedupe_ratio": 1.0,
        }

    def replace_source_snapshot(self, *, source_key: str, quotes: Any, **kwargs: Any) -> None:
        self.snapshots[source_key] = {"quotes": list(quotes), **kwargs}

    def forget_sources_except(self, source_keys: Any) -> int:
        kept = set(source_keys)
        dropped = [key for key in self.snapshots if key not in kept]
        for key in dropped:
            del self.snapshots[key]
        self.forgotten.extend(dropped)
        return len(dropped)

    def due_reactions(self, *, now_ms: int, limit: int) -> list[dict[str, Any]]:
        del now_ms
        return self._due[:limit]

    def resolve_instruments(self, symbols: Any) -> dict[str, PriceInstrument]:
        return {symbol: self.instruments[symbol] for symbol in symbols if symbol in self.instruments}

    def upsert_reaction(self, row: Any, *, now_ms: int) -> None:
        del now_ms
        self.reactions.append(dict(row))


class _FakeColdDatabase:
    """The fake exposes ordinary and heavy business admission, never the News consumer lane.

    It stands in for `WorkerDatabase`, and satisfies the Quote/Reaction ports through the production
    adapter rather than a second hand-written one, so the lane, session and transaction wiring the loops
    actually run under is the wiring under test.
    """

    def __init__(self, price: _FakePrice, *, reaction: bool = False) -> None:
        self.price = price
        self.in_transaction = False
        self.operations: list[str] = []
        self.lanes: list[str] = []
        self._port = WorkerReactionDatabase(self) if reaction else WorkerQuoteDatabase(self)

    def heavy_business(self) -> _FakeHeavyDatabase:
        return _FakeHeavyDatabase(self)

    async def read(self, name: str, fn: Any, *, timeout_seconds: float) -> Any:
        return await self._port.read(name, fn, timeout_seconds=timeout_seconds)

    async def tx(self, name: str, fn: Any, *, timeout_seconds: float) -> Any:
        return await self._port.tx(name, fn, timeout_seconds=timeout_seconds)

    async def run_business(self, name: str, fn: Any, *, operation_timeout_seconds: float) -> Any:
        del operation_timeout_seconds
        self.operations.append(name)
        self.lanes.append("ordinary")
        return fn()

    @contextmanager
    def worker_session(self, name: str, *_args: Any, **_kwargs: Any):
        del name
        outer = self

        class _Session:
            price = outer.price

            @contextmanager
            def transaction(self):
                outer.in_transaction = True
                try:
                    yield
                finally:
                    outer.in_transaction = False

        yield _Session()


class _FakeHeavyDatabase:
    def __init__(self, owner: _FakeColdDatabase) -> None:
        self.owner = owner

    async def run_business(self, name: str, fn: Any, *, operation_timeout_seconds: float) -> Any:
        del operation_timeout_seconds
        self.owner.operations.append(name)
        self.owner.lanes.append("heavy")
        return fn()


def _instrument(venue: str, venue_symbol: str, base: str) -> PriceInstrument:
    return PriceInstrument(venue=venue, venue_symbol=venue_symbol, base_symbol=base, instrument_class="crypto")


def _bars(first_open_ms: int, count: int, start: float = 100.0, step: float = 1.0) -> list[Candle]:
    return [
        Candle(
            open_at_ms=first_open_ms + index * CANDLE_INTERVAL_MS,
            close_at_ms=first_open_ms + (index + 1) * CANDLE_INTERVAL_MS,
            close=Decimal(str(start + index * step)),
        )
        for index in range(count)
    ]


def test_quote_and_reaction_database_ports_use_distinct_existing_business_admission() -> None:
    """#304 F2P: Quote uses ordinary business while Reaction remains on the one-slot heavy gate."""

    quote_db = _FakeColdDatabase(_FakePrice())
    reaction_db = _FakeColdDatabase(_FakePrice(), reaction=True)

    asyncio.run(quote_db.read("quote", lambda repos: repos.price, timeout_seconds=1.0))
    asyncio.run(reaction_db.read("reaction", lambda repos: repos.price, timeout_seconds=1.0))

    assert quote_db.lanes == ["ordinary"]
    assert reaction_db.lanes == ["heavy"]


# ---------------------------------------------------------------------------- quote turns
def test_quote_turn_issues_one_batch_per_source_and_writes_one_row_each() -> None:
    price = _FakePrice(
        targets=[
            _instrument("binance.perp", "BTCUSDT", "BTC"),
            _instrument("binance.perp", "ETHUSDT", "ETH"),
            _instrument("hl.perp", "HYPE", "HYPE"),
        ]
    )
    db = _FakeColdDatabase(price)
    calls: list[tuple[str, tuple[str, ...]]] = []

    def fetcher_for(source: str):
        async def fetch(symbols):
            calls.append((source, tuple(symbols)))
            return [ProviderQuote(venue_symbol=symbol, price=Decimal("1")) for symbol in symbols]

        return fetch

    result = asyncio.run(QuoteSnapshotLoop(db=db, fetcher_for=fetcher_for).turn())

    assert [source for source, _ in calls] == ["binance.perp", "hl.perp"]
    assert dict(calls)["binance.perp"] == ("BTCUSDT", "ETHUSDT")  # one request, both symbols
    assert set(price.snapshots) == {"binance.perp", "hl.perp"}
    assert result["sources"] == 2 and result["written"] == 2


def test_each_source_is_stamped_when_its_own_normalized_response_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    price = _FakePrice(
        targets=[
            _instrument("binance.perp", "BTCUSDT", "BTC"),
            _instrument("hl.perp", "HYPE", "HYPE"),
        ]
    )
    stamps = iter((100, 111, 222))  # plan, Binance completion, Hyperliquid completion
    monkeypatch.setattr(loops_module, "now_ms", lambda: next(stamps))

    def fetcher_for(_source: str):
        async def fetch(symbols):
            return [ProviderQuote(venue_symbol=symbol, price=Decimal("1")) for symbol in symbols]

        return fetch

    asyncio.run(QuoteSnapshotLoop(db=_FakeColdDatabase(price), fetcher_for=fetcher_for).turn())

    assert price.snapshots["binance.perp"]["received_at_ms"] == 111
    assert price.snapshots["hl.perp"]["received_at_ms"] == 222


def test_quote_runtime_emits_bounded_turn_and_provider_telemetry() -> None:
    async def scenario() -> str:
        price = _FakePrice(targets=[_instrument("binance.perp", "BTCUSDT", "BTC")])
        telemetry = TelemetryRegistry()
        stop = asyncio.Event()

        def fetcher_for(_source: str):
            async def fetch(symbols):
                stop.set()
                return [ProviderQuote(venue_symbol=symbol, price=Decimal("1")) for symbol in symbols]

            return fetch

        loop = QuoteSnapshotLoop(
            db=_FakeColdDatabase(price),
            fetcher_for=fetcher_for,
            telemetry=telemetry,
        )
        await loop.run(stop_event=stop)
        return telemetry.render_prometheus_text()

    rendered = asyncio.run(scenario())
    assert 'tracefold_external_data_turn_total{name="quote_snapshot",outcome="success"} 1.0' in rendered
    assert 'tracefold_external_data_target_count{name="quote_snapshot"} 1.0' in rendered
    assert (
        'tracefold_external_data_provider_call_total{name="quote_snapshot",outcome="success",source="binance_perp"}'
        " 1.0" in rendered
    )


def test_a_hundred_events_naming_one_asset_are_one_target_and_one_provider_result() -> None:
    """#88 §13: quote work is `O(source groups)`, never `O(Events x assets)`."""

    price = _FakePrice(targets=[_instrument("binance.perp", "BTCUSDT", "BTC")])
    db = _FakeColdDatabase(price)
    fetches = 0

    def fetcher_for(_source: str):
        async def fetch(symbols):
            nonlocal fetches
            fetches += 1
            return [ProviderQuote(venue_symbol=symbol, price=Decimal("68000")) for symbol in symbols]

        return fetch

    asyncio.run(QuoteSnapshotLoop(db=db, fetcher_for=fetcher_for).turn())

    assert fetches == 1
    assert len(price.snapshots["binance.perp"]["quotes"]) == 1


def test_one_failing_venue_never_clears_another_and_writes_nothing_of_its_own() -> None:
    price = _FakePrice(targets=[_instrument("binance.perp", "BTCUSDT", "BTC"), _instrument("hl.perp", "HYPE", "HYPE")])
    db = _FakeColdDatabase(price)

    def fetcher_for(source: str):
        async def fetch(symbols):
            if source == "hl.perp":
                raise RuntimeError("venue_timeout")
            return [ProviderQuote(venue_symbol=symbol, price=Decimal("1")) for symbol in symbols]

        return fetch

    loop = QuoteSnapshotLoop(db=db, fetcher_for=fetcher_for)
    result = asyncio.run(loop.turn())

    assert set(price.snapshots) == {"binance.perp"}  # the failed source keeps whatever it had
    assert result["written"] == 1
    assert loop.last_error is not None and "hl.perp" in loop.last_error


def test_current_deadline_keeps_a_completed_source_and_cancels_the_pending_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#304 F2P: a hanging venue cannot erase a current response that already completed."""

    async def scenario() -> tuple[dict[str, Any], _FakePrice, int]:
        price = _FakePrice(
            targets=[
                _instrument("binance.perp", "BTCUSDT", "BTC"),
                _instrument("hl.perp", "HYPE", "HYPE"),
            ]
        )
        never = asyncio.Event()
        cancelled = 0

        def fetcher_for(source: str):
            async def fetch(symbols):
                nonlocal cancelled
                if source == "hl.perp":
                    try:
                        await never.wait()
                    except asyncio.CancelledError:
                        cancelled += 1
                        raise
                return [ProviderQuote(venue_symbol=symbol, price=Decimal("68000")) for symbol in symbols]

            return fetch

        result = await QuoteSnapshotLoop(db=_FakeColdDatabase(price), fetcher_for=fetcher_for).turn()
        await asyncio.sleep(0)
        return result, price, cancelled

    monkeypatch.setattr(loops_module, "QUOTE_TURN_DEADLINE_SECONDS", 0.01)
    result, price, cancelled = asyncio.run(scenario())

    assert result["written"] == 1
    assert set(price.snapshots) == {"binance.perp"}
    assert price.snapshots["binance.perp"]["received_at_ms"] > 0
    assert cancelled == 1


def test_cancelling_a_quote_turn_cancels_and_awaits_every_current_source() -> None:
    """#304 F2P: worker shutdown must not leave provider tasks behind the cancelled turn."""

    async def scenario() -> int:
        price = _FakePrice(
            targets=[
                _instrument("binance.perp", "BTCUSDT", "BTC"),
                _instrument("hl.perp", "HYPE", "HYPE"),
            ]
        )
        never = asyncio.Event()
        both_started = asyncio.Event()
        starts = 0
        cancelled = 0

        def fetcher_for(_source: str):
            async def fetch(_symbols):
                nonlocal starts, cancelled
                starts += 1
                if starts == 2:
                    both_started.set()
                try:
                    await never.wait()
                except asyncio.CancelledError:
                    cancelled += 1
                    raise

            return fetch

        turn = asyncio.create_task(QuoteSnapshotLoop(db=_FakeColdDatabase(price), fetcher_for=fetcher_for).turn())
        await asyncio.wait_for(both_started.wait(), timeout=0.1)
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
        await asyncio.sleep(0)
        return cancelled

    assert asyncio.run(scenario()) == 2


def test_one_source_cancellation_is_attributed_without_discarding_other_done_results() -> None:
    """#304 F2P: a provider-local cancellation is one failed source, not a cancelled turn."""

    price = _FakePrice(
        targets=[
            _instrument("binance.perp", "BTCUSDT", "BTC"),
            _instrument("hl.perp", "HYPE", "HYPE"),
        ]
    )

    def fetcher_for(source: str):
        async def fetch(symbols):
            if source == "hl.perp":
                raise asyncio.CancelledError
            return [ProviderQuote(venue_symbol=symbol, price=Decimal("68000")) for symbol in symbols]

        return fetch

    loop = QuoteSnapshotLoop(db=_FakeColdDatabase(price), fetcher_for=fetcher_for)
    result = asyncio.run(loop.turn())

    assert result["written"] == 1
    assert set(price.snapshots) == {"binance.perp"}
    assert loop.last_error == "hl.perp:cancelled"


def test_twelve_source_deadline_preserves_every_done_result_and_starts_binance_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#304 F2P: the bounded shared queue promises completed preservation, not absolute isolation."""

    async def scenario() -> tuple[dict[str, Any], _FakePrice, list[str], list[str], set[str], str | None]:
        sources = ["binance.perp", "binance.spot", *(f"hl.s{index}" for index in range(10))]
        price = _FakePrice(
            targets=[_instrument(source, f"S{index}", f"B{index}") for index, source in enumerate(sources)]
        )
        hanging = {"hl.s0", "hl.s1"}
        for source in hanging:
            price.snapshots[source] = {"quotes": ["previous"]}
        never = asyncio.Event()
        starts: list[str] = []
        day_starts: list[str] = []
        both_day_calls_started = asyncio.Event()
        cancelled: set[str] = set()

        def fetcher_for(source: str):
            async def fetch(symbols):
                starts.append(source)
                if source in hanging:
                    try:
                        await never.wait()
                    except asyncio.CancelledError:
                        cancelled.add(source)
                        raise
                return [ProviderQuote(venue_symbol=symbol, price=Decimal("1")) for symbol in symbols]

            return fetch

        def day_fetcher_for(source: str):
            if source not in {"binance.perp", "binance.spot"}:
                return None

            async def fetch(symbols):
                day_starts.append(source)
                if len(day_starts) == 2:
                    both_day_calls_started.set()
                await asyncio.wait_for(both_day_calls_started.wait(), timeout=0.1)
                return [
                    ProviderQuote(
                        venue_symbol=symbol,
                        price=Decimal("1"),
                        reference_price=Decimal("1"),
                    )
                    for symbol in symbols
                ]

            return fetch

        loop = QuoteSnapshotLoop(
            db=_FakeColdDatabase(price),
            fetcher_for=fetcher_for,
            day_fetcher_for=day_fetcher_for,
        )
        result = await loop.turn()
        return result, price, starts, day_starts, cancelled, loop.last_error

    monkeypatch.setattr(loops_module, "QUOTE_TURN_DEADLINE_SECONDS", 0.02)
    result, price, starts, day_starts, cancelled, last_error = asyncio.run(scenario())

    assert starts[:2] == ["binance.perp", "binance.spot"]
    assert day_starts == ["binance.perp", "binance.spot"]
    assert len(starts) + len(day_starts) == 14
    assert result["sources"] == 12 and result["written"] == 10
    assert cancelled == {"hl.s0", "hl.s1"}
    assert all(price.snapshots[source] == {"quotes": ["previous"]} for source in cancelled)
    assert last_error is not None and ":day:" not in last_error


@pytest.mark.parametrize(("turn_seconds", "expected_sleep"), [(8.0, 12.0), (25.0, 0.0)])
def test_quote_runtime_uses_start_based_cadence(
    monkeypatch: pytest.MonkeyPatch,
    turn_seconds: float,
    expected_sleep: float,
) -> None:
    """#304 F2P: work consumes the 20 s period; a slow turn is not followed by another fixed 20 s delay."""

    async def scenario() -> list[float]:
        clock = [100.0]
        sleeps: list[float] = []
        stop = asyncio.Event()
        loop = QuoteSnapshotLoop(db=_FakeColdDatabase(_FakePrice()), fetcher_for=lambda _source: None)

        async def turn() -> dict[str, Any]:
            clock[0] += turn_seconds
            return {"targets": 0, "sources": 0, "written": 0}

        async def sleep_or_stop(_stop: asyncio.Event, seconds: float) -> None:
            sleeps.append(seconds)
            stop.set()

        loop.turn = turn  # type: ignore[method-assign]
        monkeypatch.setattr(loops_module.time, "perf_counter", lambda: clock[0])
        monkeypatch.setattr(loops_module, "_sleep_or_stop", sleep_or_stop)
        await loop.run(stop_event=stop)
        return sleeps

    assert asyncio.run(scenario()) == [expected_sleep]


def test_no_provider_call_happens_inside_a_database_transaction() -> None:
    price = _FakePrice(targets=[_instrument("binance.perp", "BTCUSDT", "BTC")])
    db = _FakeColdDatabase(price)
    observed: list[bool] = []

    def fetcher_for(_source: str):
        async def fetch(symbols):
            observed.append(db.in_transaction)
            return [ProviderQuote(venue_symbol=symbol, price=Decimal("1")) for symbol in symbols]

        return fetch

    asyncio.run(QuoteSnapshotLoop(db=db, fetcher_for=fetcher_for).turn())

    assert observed == [False]  # network latency never occupies a database slot


def test_a_source_that_left_the_working_set_does_not_linger_as_a_stale_row() -> None:
    """A source with no targets has no reader; its row would otherwise age forever and report as stale."""

    price = _FakePrice(targets=[_instrument("binance.perp", "BTCUSDT", "BTC")])
    price.snapshots["hl.mkts"] = {"quotes": [], "stale": True}
    db = _FakeColdDatabase(price)

    def fetcher_for(_source: str):
        async def fetch(symbols):
            return [ProviderQuote(venue_symbol=symbol, price=Decimal("1")) for symbol in symbols]

        return fetch

    asyncio.run(QuoteSnapshotLoop(db=db, fetcher_for=fetcher_for).turn())

    assert price.forgotten == ["hl.mkts"]
    assert set(price.snapshots) == {"binance.perp"}


def test_a_planned_source_that_failed_keeps_its_row_through_the_prune() -> None:
    """Stale-not-blank still wins: only sources absent from the plan are dropped."""

    price = _FakePrice(targets=[_instrument("binance.perp", "BTCUSDT", "BTC"), _instrument("hl.perp", "HYPE", "HYPE")])
    price.snapshots["hl.perp"] = {"quotes": ["previous"]}
    db = _FakeColdDatabase(price)

    def fetcher_for(source: str):
        async def fetch(symbols):
            if source == "hl.perp":
                raise RuntimeError("venue_timeout")
            return [ProviderQuote(venue_symbol=symbol, price=Decimal("1")) for symbol in symbols]

        return fetch

    asyncio.run(QuoteSnapshotLoop(db=db, fetcher_for=fetcher_for).turn())

    assert price.forgotten == []
    assert price.snapshots["hl.perp"] == {"quotes": ["previous"]}


def test_a_source_that_answers_with_nothing_usable_keeps_its_previous_row() -> None:
    """A 200 carrying an error object parses to zero quotes; replacing the row would blank every symbol."""

    price = _FakePrice(targets=[_instrument("binance.perp", "BTCUSDT", "BTC")])
    db = _FakeColdDatabase(price)

    def fetcher_for(_source: str):
        async def fetch(_symbols):
            return []

        return fetch

    loop = QuoteSnapshotLoop(db=db, fetcher_for=fetcher_for)
    result = asyncio.run(loop.turn())

    assert price.snapshots == {}  # the stale row ages instead of becoming unavailable
    assert result["written"] == 0
    assert loop.last_error is not None and "venue_payload_empty" in loop.last_error


def test_a_source_with_no_adapter_is_skipped_rather_than_crashing_the_turn() -> None:
    price = _FakePrice(targets=[_instrument("hl.unknowndex", "x:AAPL", "AAPL")])
    db = _FakeColdDatabase(price)
    result = asyncio.run(QuoteSnapshotLoop(db=db, fetcher_for=lambda _source: None).turn())
    assert result["written"] == 0 and price.snapshots == {}


def test_quote_budgets_are_code_owned_constants() -> None:
    assert QUOTE_TARGET_MAX == 256
    assert QUOTE_SOURCE_GROUP_MAX == 12
    assert QUOTE_DAY_PERIOD_SECONDS == 300.0


# -------------------------------------------------------------- current-first day enrichment (#304 hard-cuts #109)
class _BinanceLike:
    """One Binance-shaped source: a narrow price endpoint and a wide one that also carries the day open."""

    def __init__(self, *, price: Decimal = Decimal("68000"), reference: Decimal | None = Decimal("67000")):
        self.price = price
        self.reference = reference
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []
        self.fail: BaseException | None = None

    def fetcher_for(self, source: str):
        async def fetch(symbols):
            self.calls.append(("price", source, tuple(symbols)))
            return [
                ProviderQuote(venue_symbol=symbol, price=self.price, change_basis="rolling_24h") for symbol in symbols
            ]

        return fetch

    def day_fetcher_for(self, source: str):
        if not source.startswith("binance."):
            return None

        async def fetch(symbols):
            self.calls.append(("day", source, tuple(symbols)))
            if self.fail is not None:
                raise self.fail
            return [
                ProviderQuote(
                    venue_symbol=symbol,
                    price=self.price,
                    change_basis="rolling_24h",
                    reference_price=self.reference,
                )
                for symbol in symbols
            ]

        return fetch

    def loop(self, price: _FakePrice, **kwargs: Any) -> QuoteSnapshotLoop:
        return QuoteSnapshotLoop(
            db=_FakeColdDatabase(price),
            fetcher_for=self.fetcher_for,
            day_fetcher_for=self.day_fetcher_for,
            **kwargs,
        )


def test_due_day_read_runs_after_current_store_and_only_enriches_the_next_turn() -> None:
    """#304 product-contract change: current is mandatory; a day read is post-store optional enrichment."""

    price = _FakePrice(targets=[_instrument("binance.perp", "BTCUSDT", "BTC")])
    venue = _BinanceLike(price=Decimal("68000"), reference=Decimal("67000"))

    def day_fetcher_for(source: str):
        fetch = venue.day_fetcher_for(source)
        assert fetch is not None

        async def after_store(symbols):
            assert price.snapshots["binance.perp"]["quotes"][0].change_pct is None
            return await fetch(symbols)

        return after_store

    loop = QuoteSnapshotLoop(
        db=_FakeColdDatabase(price),
        fetcher_for=venue.fetcher_for,
        day_fetcher_for=day_fetcher_for,
    )

    first = asyncio.run(loop.turn())
    assert first["written"] == 1
    assert [kind for kind, _, _ in venue.calls] == ["price", "day"]
    assert price.snapshots["binance.perp"]["quotes"][0].change_pct is None

    asyncio.run(loop.turn())
    assert [kind for kind, _, _ in venue.calls] == ["price", "day", "price"]
    assert price.snapshots["binance.perp"]["quotes"][0].change_pct == pytest.approx(1.4925, abs=1e-3)


def test_the_percentage_is_recomputed_from_each_turn_own_price_not_frozen_with_it() -> None:
    """The reference ages, the ratio does not: the number can never disagree with the price beside it."""

    price = _FakePrice(targets=[_instrument("binance.perp", "BTCUSDT", "BTC")])
    venue = _BinanceLike(price=Decimal("67000"), reference=Decimal("67000"))
    loop = venue.loop(price)

    asyncio.run(loop.turn())  # current stored without a reference; day reference lands after the write
    assert price.snapshots["binance.perp"]["quotes"][0].change_pct is None

    venue.price = Decimal("70350")  # the market moves 5% on the next 20 s turn
    asyncio.run(loop.turn())

    quote = price.snapshots["binance.perp"]["quotes"][0]
    assert quote.price == Decimal("70350")
    assert quote.change_pct == pytest.approx(5.0)  # not the frozen 0.0 a cached percentage would show


def test_a_symbol_joining_the_working_set_gets_its_percentage_without_waiting_for_the_cadence() -> None:
    """The newest Event is the card being looked at; it must not be the one with no percentage."""

    price = _FakePrice(targets=[_instrument("binance.perp", "BTCUSDT", "BTC")])
    venue = _BinanceLike()
    loop = venue.loop(price)
    asyncio.run(loop.turn())  # current first, then the initial day reference

    price._targets = [_instrument("binance.perp", "BTCUSDT", "BTC"), _instrument("binance.perp", "SOLUSDT", "SOL")]
    asyncio.run(loop.turn())  # SOL current is stored without a percentage, then the day cache is refreshed

    quotes = {quote.venue_symbol: quote for quote in price.snapshots["binance.perp"]["quotes"]}
    assert quotes["BTCUSDT"].change_pct is not None
    assert quotes["SOLUSDT"].change_pct is None

    asyncio.run(loop.turn())

    assert [kind for kind, _, _ in venue.calls] == ["price", "day", "price", "day", "price"]
    assert all(quote.change_pct is not None for quote in price.snapshots["binance.perp"]["quotes"])


def test_a_symbol_no_venue_answers_for_cannot_pin_the_source_to_the_wide_endpoint() -> None:
    """`_covered` records what we asked for, not what came back, or an unlisted symbol never stops asking."""

    price = _FakePrice(targets=[_instrument("binance.perp", "NOSUCHUSDT", "NOSUCH")])
    venue = _BinanceLike()

    def day_fetcher_for(source: str):
        async def fetch(symbols):
            venue.calls.append(("day", source, tuple(symbols)))
            return [ProviderQuote(venue_symbol="BTCUSDT", price=Decimal("1"), reference_price=Decimal("1"))]

        return fetch

    loop = QuoteSnapshotLoop(
        db=_FakeColdDatabase(price), fetcher_for=venue.fetcher_for, day_fetcher_for=day_fetcher_for
    )
    for _ in range(3):
        asyncio.run(loop.turn())

    assert [kind for kind, _, _ in venue.calls] == ["price", "day", "price", "price"]


def test_a_failed_day_read_leaves_the_current_write_in_place_and_remains_due() -> None:
    """#304 F2P: optional reference failure cannot roll back or suppress the mandatory current write."""

    price = _FakePrice(targets=[_instrument("binance.perp", "BTCUSDT", "BTC")])
    venue = _BinanceLike()
    venue.fail = RuntimeError("venue_rate_limited")
    loop = venue.loop(price)

    first = asyncio.run(loop.turn())
    assert first["written"] == 1
    assert price.snapshots["binance.perp"]["quotes"][0].price == Decimal("68000")
    assert price.snapshots["binance.perp"]["quotes"][0].change_pct is None
    assert loop.last_error is not None and "binance.perp" in loop.last_error

    venue.fail = None
    asyncio.run(loop.turn())

    assert [kind for kind, _, _ in venue.calls] == ["price", "day", "price", "day"]
    assert price.snapshots["binance.perp"]["quotes"][0].change_pct is None  # reference-only writes are forbidden

    asyncio.run(loop.turn())
    assert price.snapshots["binance.perp"]["quotes"][0].change_pct == pytest.approx(1.4925, abs=1e-3)


def test_a_day_read_that_answers_without_any_reference_does_not_stamp_the_cadence() -> None:
    price = _FakePrice(targets=[_instrument("binance.perp", "BTCUSDT", "BTC")])
    venue = _BinanceLike(reference=None)
    loop = venue.loop(price)

    asyncio.run(loop.turn())
    asyncio.run(loop.turn())

    assert [kind for kind, _, _ in venue.calls] == ["price", "day", "price", "day"]
    assert price.snapshots["binance.perp"]["quotes"][0].change_pct is None  # a price, never a borrowed number


def test_a_source_absent_for_one_turn_does_not_re_pay_for_the_wide_endpoint() -> None:
    """A burst of Events can push a source out of one plan; that is not a reason to refetch 270 kB."""

    binance = _instrument("binance.perp", "BTCUSDT", "BTC")
    price = _FakePrice(targets=[binance])
    venue = _BinanceLike()
    loop = venue.loop(price)

    asyncio.run(loop.turn())
    price._targets = [_instrument("hl.perp", "HYPE", "HYPE")]
    asyncio.run(loop.turn())
    price._targets = [binance]
    asyncio.run(loop.turn())

    assert [kind for kind, _, _ in venue.calls if kind] == ["price", "day", "price", "price"]


def test_a_symbol_rotated_out_of_an_active_source_must_reacquire_its_reference() -> None:
    """An old reference cannot reappear when a symbol returns after other members replaced it."""

    price = _FakePrice(targets=[_instrument("binance.perp", "BTCUSDT", "BTC")])
    venue = _BinanceLike()
    loop = venue.loop(price)

    for symbol in ("BTC", "ETH", "SOL"):
        price._targets = [_instrument("binance.perp", f"{symbol}USDT", symbol)]
        asyncio.run(loop.turn())

    price._targets = [_instrument("binance.perp", "BTCUSDT", "BTC")]
    asyncio.run(loop.turn())
    assert price.snapshots["binance.perp"]["quotes"][0].change_pct is None

    asyncio.run(loop.turn())
    assert price.snapshots["binance.perp"]["quotes"][0].change_pct is not None


def test_reference_is_valid_through_360_seconds_then_only_the_change_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#304 F2P: reference staleness removes the ratio, never the current price or its basis."""

    clock = [100]
    monkeypatch.setattr(loops_module, "now_ms", lambda: clock[0])
    price = _FakePrice(targets=[_instrument("binance.perp", "BTCUSDT", "BTC")])
    venue = _BinanceLike()
    loop = venue.loop(price)

    asyncio.run(loop.turn())
    loop.day_fetcher_for = None  # hold the one successful reference fixed while current keeps moving

    clock[0] += 360_000
    asyncio.run(loop.turn())
    boundary = price.snapshots["binance.perp"]["quotes"][0]
    assert boundary.change_pct is not None
    assert boundary.reference_at_ms == 100

    clock[0] += 1
    asyncio.run(loop.turn())
    expired = price.snapshots["binance.perp"]["quotes"][0]
    assert expired.price == Decimal("68000")
    assert expired.change_basis == "rolling_24h"
    assert expired.change_pct is None
    assert expired.reference_at_ms == 100


# ---------------------------------------------------------------------------- reaction turns
def _due_row(event_id: str, symbol: str = "BTC", **overrides: Any) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "symbol": symbol,
        "anchor_at_ms": ANCHOR,
        "state": None,
        "venue": None,
        "venue_symbol": None,
        "instrument_class": None,
        "p0": None,
        "p0_at_ms": None,
        "p1": None,
        "p1_at_ms": None,
        **overrides,
    }


def _reaction_loop(price: _FakePrice, bars: list[Candle], *, record: list[Any] | None = None) -> EventReactionLoop:
    """A fake that honours the requested range, exactly like the real adapters.

    A fetcher that ignores `start_ms`/`end_ms` hides planner bugs: the first version of `_needed_window`
    asked only for the 1H neighbourhood on a matured backfill row and would have written every backfilled
    Event off as `no_candle_within_gap`, while a range-blind fake reported it complete.
    """

    def fetcher_for(_venue: str):
        async def fetch(venue_symbol: str, start_ms: int, end_ms: int):
            if record is not None:
                record.append((venue_symbol, start_ms, end_ms))
            return [bar for bar in bars if start_ms <= bar.open_at_ms <= end_ms]

        return fetch

    return EventReactionLoop(db=_FakeColdDatabase(price, reaction=True), fetcher_for=fetcher_for)


def test_one_candle_response_fills_many_events_on_the_same_instrument() -> None:
    price = _FakePrice(due=[_due_row(f"e{index}") for index in range(5)])
    price.instruments["BTC"] = _instrument("binance.perp", "BTCUSDT", "BTC")
    requests: list[Any] = []
    bars = _bars(ANCHOR - 2 * CANDLE_INTERVAL_MS, 60)

    result = asyncio.run(_reaction_loop(price, bars, record=requests).turn())

    assert len(requests) == 1  # five Events, one merged range, one provider call
    assert result["due"] == 5 and result["written"] == 5
    # These anchors matured long ago, so one backfill turn fills both legs from the same response.
    assert {row["state"] for row in price.reactions} == {"complete"}
    assert all(row["return_1h_bps"] is not None for row in price.reactions)
    assert all(row["return_4h_bps"] is not None for row in price.reactions)


def test_every_event_keeps_its_own_row_because_every_anchor_is_different() -> None:
    """Reaction identity cannot be deduplicated away — only its provider reads are coalesced."""

    price = _FakePrice(
        due=[
            _due_row("e1", anchor_at_ms=ANCHOR),
            _due_row("e2", anchor_at_ms=ANCHOR + 30 * 60_000),
        ]
    )
    price.instruments["BTC"] = _instrument("binance.perp", "BTCUSDT", "BTC")
    requests: list[Any] = []
    asyncio.run(_reaction_loop(price, _bars(ANCHOR - 2 * CANDLE_INTERVAL_MS, 80), record=requests).turn())

    assert len(requests) == 1
    assert {row["event_id"] for row in price.reactions} == {"e1", "e2"}
    assert price.reactions[0]["return_1h_bps"] != price.reactions[1]["return_1h_bps"]


def test_an_unresolvable_symbol_is_recorded_once_with_a_stable_reason() -> None:
    price = _FakePrice(due=[_due_row("e1", symbol="NOTATHING")])
    result = asyncio.run(_reaction_loop(price, []).turn())

    assert result["written"] == 1
    assert price.reactions[0]["state"] == "unavailable"
    assert price.reactions[0]["unavailable_reason"] == "instrument_unresolved"


def test_an_event_older_than_the_history_window_says_so_instead_of_asking_forever() -> None:
    price = _FakePrice(due=[_due_row("e1", anchor_at_ms=1_000)])
    price.instruments["BTC"] = _instrument("binance.perp", "BTCUSDT", "BTC")
    asyncio.run(_reaction_loop(price, []).turn())

    assert price.reactions[0]["unavailable_reason"] == "history_expired"


def test_a_transient_provider_failure_writes_nothing_and_leaves_the_work_due() -> None:
    price = _FakePrice(due=[_due_row("e1")])
    price.instruments["BTC"] = _instrument("binance.perp", "BTCUSDT", "BTC")

    def fetcher_for(_venue: str):
        async def fetch(*_args: Any):
            raise RuntimeError("venue_timeout")

        return fetch

    loop = EventReactionLoop(db=_FakeColdDatabase(price, reaction=True), fetcher_for=fetcher_for)
    result = asyncio.run(loop.turn())

    assert price.reactions == []  # a timeout is loop health, never a semantic reason
    assert result["written"] == 0
    assert loop.last_error is not None


def test_a_backfilled_event_gets_both_horizons_from_one_window() -> None:
    """An Event first measured after anchor+4H must not lose its 4H leg to a too-narrow request.

    This is the whole initial backfill, and everything behind an outage longer than three hours.
    """

    price = _FakePrice(due=[_due_row("e1")])
    price.instruments["BTC"] = _instrument("binance.perp", "BTCUSDT", "BTC")
    requests: list[Any] = []
    asyncio.run(_reaction_loop(price, _bars(ANCHOR - 2 * CANDLE_INTERVAL_MS, 60), record=requests).turn())

    _, _start_ms, end_ms = requests[0]
    assert end_ms >= ANCHOR + HORIZON_MS["4h"]  # one window covering both horizons
    written = price.reactions[0]
    assert written["state"] == "complete"
    assert written["return_1h_bps"] is not None and written["return_4h_bps"] is not None
    assert written.get("unavailable_reason") is None


def test_an_immature_event_asks_only_for_the_first_horizon() -> None:
    fresh_anchor = now_ms() - 2 * HORIZON_MS["1h"]
    price = _FakePrice(due=[_due_row("e1", anchor_at_ms=fresh_anchor)])
    price.instruments["BTC"] = _instrument("binance.perp", "BTCUSDT", "BTC")
    requests: list[Any] = []
    asyncio.run(_reaction_loop(price, _bars(fresh_anchor - 2 * CANDLE_INTERVAL_MS, 30), record=requests).turn())

    _, _, end_ms = requests[0]
    assert end_ms < fresh_anchor + HORIZON_MS["4h"]  # 4H has not matured; nothing asks for it yet
    assert price.reactions[0]["state"] == "partial"
    assert price.reactions[0].get("unavailable_reason") is None  # still due for its 4H leg


def test_a_market_that_has_never_traded_is_named_once_instead_of_asked_forever() -> None:
    """Hyperliquid lists spot pairs with no trades at all and answers `[]` for them, permanently.

    Treating that as a transient failure left the row unwritten and therefore permanently due: in production
    31 such rows sat at the head of the oldest-first scan, pinning the backlog SLO at 52 h and re-requesting
    dead markets every turn.
    """

    price = _FakePrice(due=[_due_row("e1")])
    price.instruments["BTC"] = _instrument("hl.spot", "@293", "BTC")
    result = asyncio.run(_reaction_loop(price, []).turn())

    assert result["written"] == 1
    assert price.reactions[0]["state"] == "unavailable"
    assert price.reactions[0]["unavailable_reason"] == "no_candle_within_gap"


def test_no_answer_at_all_still_leaves_the_work_due() -> None:
    """The other half of the same rule: a failed request is loop health and must stay retryable."""

    price = _FakePrice(due=[_due_row("e1")])
    price.instruments["BTC"] = _instrument("binance.perp", "BTCUSDT", "BTC")

    def fetcher_for(_venue: str):
        async def fetch(*_args: Any):
            raise RuntimeError("venue_timeout")

        return fetch

    loop = EventReactionLoop(db=_FakeColdDatabase(price, reaction=True), fetcher_for=fetcher_for)
    asyncio.run(loop.turn())

    assert price.reactions == []


def test_a_partial_row_keeps_its_first_horizon_when_the_second_window_is_empty() -> None:
    matured = now_ms() - HORIZON_MS["4h"] - 60_000
    price = _FakePrice(
        due=[
            _due_row(
                "e1",
                state="partial",
                venue="hl.spot",
                venue_symbol="@293",
                p0=Decimal("100"),
                p0_at_ms=matured,
                p1=Decimal("101"),
                p1_at_ms=matured + HORIZON_MS["1h"],
                anchor_at_ms=matured,
            )
        ]
    )
    asyncio.run(_reaction_loop(price, []).turn())

    written = price.reactions[0]
    assert written["state"] == "partial"  # the 1H measurement survives
    assert written["p0"] == Decimal("100")
    assert written["unavailable_reason"] == "no_candle_within_gap"


def test_a_hole_at_the_horizon_is_named_rather_than_forward_filled() -> None:
    price = _FakePrice(due=[_due_row("e1")])
    price.instruments["BTC"] = _instrument("binance.perp", "BTCUSDT", "BTC")
    # Bars stop long before anchor+1H, so p1 falls in a gap.
    asyncio.run(_reaction_loop(price, _bars(ANCHOR - 2 * CANDLE_INTERVAL_MS, 2)).turn())

    assert price.reactions[0]["state"] == "unavailable"
    assert price.reactions[0]["unavailable_reason"] == "no_candle_within_gap"


def test_the_four_hour_leg_reuses_the_pinned_source_and_never_refetches_p0() -> None:
    matured = ANCHOR + HORIZON_MS["4h"] + 60_000
    price = _FakePrice(
        due=[
            _due_row(
                "e1",
                state="partial",
                venue="binance.perp",
                venue_symbol="BTCUSDT",
                instrument_class="crypto",
                p0=Decimal("100"),
                p0_at_ms=ANCHOR,
                p1=Decimal("101"),
                p1_at_ms=ANCHOR + HORIZON_MS["1h"],
                anchor_at_ms=matured - HORIZON_MS["4h"] - 60_000,
            )
        ]
    )
    # No resolvable symbol: the row must price from its pin, not from a fresh lookup.
    requests: list[Any] = []
    anchor = price._due[0]["anchor_at_ms"]
    bars = _bars(anchor + HORIZON_MS["4h"] - 2 * CANDLE_INTERVAL_MS, 4, start=110.0, step=0.0)
    result = asyncio.run(_reaction_loop(price, bars, record=requests).turn())

    assert result["written"] == 1
    written = price.reactions[0]
    assert written["state"] == "complete"
    assert written["venue_symbol"] == "BTCUSDT"
    assert written["p0"] == Decimal("100")  # persisted price points are never refetched
    assert written["return_4h_bps"] == 1000  # 100 -> 110
    # Only the 4H neighbourhood is requested, never the whole anchor..+4H span.
    _, start_ms, end_ms = requests[0]
    assert end_ms - start_ms <= 4 * CANDLE_INTERVAL_MS


def test_a_turn_never_exceeds_the_merged_candle_request_cap() -> None:
    price = _FakePrice(due=[_due_row(f"e{index}", symbol=f"S{index}") for index in range(50)])
    for index in range(50):
        price.instruments[f"S{index}"] = _instrument("binance.perp", f"S{index}USDT", f"S{index}")
    requests: list[Any] = []
    asyncio.run(_reaction_loop(price, _bars(ANCHOR - 2 * CANDLE_INTERVAL_MS, 60), record=requests).turn())

    assert len(requests) == REACTION_CANDLE_REQUESTS_MAX
    # Rows the cap left out are simply not written this turn; they stay due in PostgreSQL.
    assert len(price.reactions) == REACTION_CANDLE_REQUESTS_MAX


@pytest.mark.parametrize("loop_name", ["quotes", "reactions"])
def test_a_disabled_loop_runs_no_turn_at_all(loop_name: str) -> None:
    price = _FakePrice()
    db = _FakeColdDatabase(price)
    stop = asyncio.Event()
    stop.set()
    loop: Any = (
        QuoteSnapshotLoop(db=db, fetcher_for=lambda _s: None, enabled=False)
        if loop_name == "quotes"
        else EventReactionLoop(db=db, fetcher_for=lambda _v: None, enabled=False)
    )
    asyncio.run(loop.run(stop_event=stop))
    assert db.operations == []
