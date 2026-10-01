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

from tracefold.app.workers.wiring.database import WorkerQuoteDatabase
from tracefold.news.market_review import loops as loops_module
from tracefold.news.market_review.loops import QuoteSnapshotLoop
from tracefold.news.market_review.pricing import (
    QUOTE_DAY_PERIOD_SECONDS,
    QUOTE_SOURCE_GROUP_MAX,
    QUOTE_TARGET_MAX,
    PriceInstrument,
    ProviderQuote,
)
from tracefold.platform.observability import TelemetryRegistry


class _FakePrice:
    """The repository surface the loops touch, recording what each turn asked for and wrote."""

    def __init__(self, *, targets: list[PriceInstrument] | None = None):
        self._targets = targets or []
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.forgotten: list[str] = []
        self.instruments: dict[str, PriceInstrument] = {}
        self.requested_markets: list[tuple[str, str]] = []

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

    def resolve_instruments(self, requests: Any) -> dict[Any, PriceInstrument]:
        # Keyed by the whole request (#651 §6.2): one batch may ask about the same symbol in two markets.
        self.requested_markets = [(r.symbol, r.market_type) for r in requests]
        return {
            request: self.instruments[request.symbol]
            for request in requests
            if request.symbol in self.instruments and request.accepts(self.instruments[request.symbol].instrument_class)
        }


class _FakeColdDatabase:
    """The fake exposes ordinary and heavy business admission, never the News consumer lane.

    It stands in for `WorkerDatabase`, and satisfies the Quote port through the production
    adapter rather than a second hand-written one, so the lane, session and transaction wiring the loops
    actually run under is the wiring under test.
    """

    def __init__(self, price: _FakePrice) -> None:
        self.price = price
        self.in_transaction = False
        self.operations: list[str] = []
        self.lanes: list[str] = []
        self._port = WorkerQuoteDatabase(self)

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


def _instrument(venue: str, venue_symbol: str, base: str) -> PriceInstrument:
    return PriceInstrument(venue=venue, venue_symbol=venue_symbol, base_symbol=base, instrument_class="crypto")


def test_quote_database_port_uses_ordinary_business_admission() -> None:
    """Current quote reads use ordinary business admission."""

    quote_db = _FakeColdDatabase(_FakePrice())

    asyncio.run(quote_db.read("quote", lambda repos: repos.price, timeout_seconds=1.0))

    assert quote_db.lanes == ["ordinary"]


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


def test_reference_is_valid_through_600_seconds_then_only_the_change_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#304 F2P: reference staleness removes the ratio, never the current price or its basis.

    #562 §5 row 10 widened the window from 360 s to 600 s. The day read that refreshes the reference
    runs every 300 s and is optional -- it never enters the turn deadline -- so the old ceiling gave it
    no room to miss one: 361 s after the last successful read the card lost its 24 h change entirely.
    """

    clock = [100]
    monkeypatch.setattr(loops_module, "now_ms", lambda: clock[0])
    price = _FakePrice(targets=[_instrument("binance.perp", "BTCUSDT", "BTC")])
    venue = _BinanceLike()
    loop = venue.loop(price)

    asyncio.run(loop.turn())
    loop.day_fetcher_for = None  # hold the one successful reference fixed while current keeps moving

    clock[0] += 360_001  # one missed day read: what used to expire the change and no longer does
    asyncio.run(loop.turn())
    missed_read = price.snapshots["binance.perp"]["quotes"][0]
    assert missed_read.change_pct is not None

    clock[0] = 100 + 600_000
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


def test_a_disabled_quote_loop_runs_no_turn_at_all() -> None:
    db = _FakeColdDatabase(_FakePrice())
    stop = asyncio.Event()
    stop.set()
    loop = QuoteSnapshotLoop(db=db, fetcher_for=lambda _s: None, enabled=False)
    asyncio.run(loop.run(stop_event=stop))
    assert db.operations == []
