"""At-most-once reader-card delivery stage."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, ClassVar, Protocol

from ..bus import Q_DELIVER, BusMessage, DeferError, PermanentError, TransientError, now_ms
from ..delivery import reader_assets, reader_market_movements, reader_trade_targets, render_first_card
from ..market_review.pricing import Candle, PriceInstrument, PricePoint, select_candle
from ..models import ReaderDeliveryPresentation
from ..oi_signals import DEFAULT_OI_POLICY, OiPolicy, program_sha256
from ..oi_signals import METRIC_VERSION as OI_METRIC_VERSION
from ..oi_signals import PROGRAM_VERSION as OI_PROGRAM_VERSION
from ..telemetry import NewsWorkSemantics
from .runtime import NewsDatabasePort

# The quote read gets its own short session. A price is display-only and must
# never delay, retry, or suppress a delivery; every failure degrades to no
# market line while the card proceeds normally (#113).
_QUOTE_READ_TIMEOUT_SECONDS = 1.5
_DELIVERY_CANDLE_TIMEOUT_SECONDS = 2.0
_DELIVERY_PRICE_SOURCE_TIMEOUT_SECONDS = 2.0
_DELIVERY_CANDLE_GAP_MS = 90_000
_ONE_HOUR_MS = 3_600_000

DeliveryCandleFetcher = Callable[[str, int, int], Awaitable[Sequence[Candle]]]
DeliveryCandleFetcherFor = Callable[[str], DeliveryCandleFetcher | None]
DeliveryPriceFetcher = Callable[[str, Sequence[int]], Awaitable[Mapping[int, PricePoint]]]
DeliveryPriceFetcherFor = Callable[[str], DeliveryPriceFetcher | None]


class NewsPushSender(Protocol):
    """Synchronous provider boundary executed by the finite-operation runner."""

    def prepare(self) -> None: ...

    def send_card(
        self,
        card: Mapping[str, Any],
        *,
        presentation: ReaderDeliveryPresentation | None = None,
    ) -> Mapping[str, Any]: ...

    def close(self) -> None: ...


class DelivererConsumer:
    """SAC consumer: one provider attempt per (event, kind); crash between send and ack never resends."""

    work_semantics: ClassVar[tuple[NewsWorkSemantics, ...]] = ("durable_event",)

    def __init__(
        self,
        *,
        bus: Any,
        db: NewsDatabasePort,
        sender: NewsPushSender | None,
        finite_operations: Any,
        min_interval_seconds: float,
        oi_policy: OiPolicy = DEFAULT_OI_POLICY,
        candle_fetcher_for: DeliveryCandleFetcherFor | None = None,
        price_fetcher_for: DeliveryPriceFetcherFor | None = None,
    ) -> None:
        self.bus = bus
        self.db = db
        self.sender = sender
        self.finite = finite_operations
        self.min_interval = float(min_interval_seconds)
        self._oi_program_sha256 = program_sha256(oi_policy)
        self._candle_fetcher_for = candle_fetcher_for
        self._price_fetcher_for = price_fetcher_for
        self._last_send_at = 0.0

    async def run(self, *, stop_event: asyncio.Event) -> None:
        with contextlib.suppress(TransientError, DeferError):
            await self.db.tx(
                "news_delivery_reconcile", lambda repos: repos.news.terminalize_interrupted_deliveries(now_ms=now_ms())
            )
        await self.bus.consume(Q_DELIVER, self.handle, prefetch=1, stop_event=stop_event)

    async def handle(self, message: BusMessage) -> None:
        event_id = str(message.payload.get("event_id") or "")
        kind = "first"  # one Event, one card; there is no follow-up lane
        if not event_id:
            raise PermanentError("news_event_id_missing")
        stamp = now_ms()
        bundle = await self.db.read("news_delivery_load", lambda repos: self._load(repos, event_id, stamp))
        if bundle is None:
            raise PermanentError("news_delivery_inputs_missing")
        card, triage_row, oi_signal, _admission, event_kind, timing = bundle
        # A delivery message can outlive the source-contract migration that held its Event. Immutable
        # evidence and historical verdicts remain audit facts; current PostgreSQL routing still wins before
        # a delivery ledger row, quote read, or external send is attempted.
        if event_kind == "unsupported_market":
            return
        tv = dict(triage_row.get("verdict") or {})
        if triage_row["final_decision"] not in {"push", "escalate"}:
            return
        if self.sender is None:
            await self._settle_direct(event_id, kind, "delivery_unavailable", stamp)
            return
        try:
            await self.finite.run(
                "news_delivery_prepare",
                self.sender.prepare,
                timeout_seconds=8.0,
            )
        except Exception as exc:
            prepare_error_code = getattr(exc, "code", None) or f"news_delivery_failed:{type(exc).__name__}"
            await self._settle_direct(event_id, kind, prepare_error_code, stamp)
            return
        # Only query a quote after every policy return above. A quote failure
        # never changes the delivery decision.
        shown = reader_assets(
            event_kind=event_kind,
            verdict=tv,
            grounded_assets=list(card.get("grounded_assets") or []),
            program_version=str(triage_row.get("program_version") or ""),
            verdict_program_sha256=str(triage_row.get("program_sha256") or ""),
            expected_program_sha256=self._oi_program_sha256,
            oi_signal=oi_signal,
        )
        news_at_ms = int(timing["news_at_ms"]) if timing and timing.get("news_at_ms") is not None else None
        observed_at_ms = int(timing["observed_at_ms"]) if timing and timing.get("observed_at_ms") is not None else None
        wait = self.min_interval - (time.monotonic() - self._last_send_at)
        if wait > 0:
            await asyncio.sleep(wait)
        price_at_ms = now_ms()
        quotes = await self._market_data(shown, price_at_ms, news_at_ms=news_at_ms)
        card_payload = render_first_card(
            event=card,
            verdict=tv,
            decision=str(triage_row["final_decision"]),
            grounded_assets=list(card.get("grounded_assets") or []),
            assets=shown,
            degraded=bool(triage_row.get("degraded")),
            quotes=quotes,
        )
        presentation = ReaderDeliveryPresentation(
            trade_targets=reader_trade_targets(quotes),
            market_movements=reader_market_movements(shown, quotes),
            news_at_ms=news_at_ms,
            observed_at_ms=observed_at_ms,
        )
        state = await self.db.tx(
            "news_delivery_begin",
            lambda repos: repos.news.begin_delivery(event_id=event_id, kind=kind, card=card_payload, now_ms=stamp),
        )
        if state != "new":
            if state == "sending":
                await self.db.tx(
                    "news_delivery_ambiguous",
                    lambda repos: repos.news.settle_delivery(
                        event_id=event_id,
                        kind=kind,
                        state="terminal",
                        receipt=None,
                        error_code="ambiguous_after_crash",
                        now_ms=now_ms(),
                    ),
                )
            return
        error_code: str | None = None
        receipt: dict[str, Any] | None = None
        try:
            result = await self.finite.run(
                "news_delivery_send",
                self.sender.send_card,
                card_payload,
                presentation=presentation,
                timeout_seconds=8.0,
            )
            receipt = dict(result)
        except Exception as exc:
            error_code = getattr(exc, "code", None) or f"news_delivery_failed:{type(exc).__name__}"
        finally:
            self._last_send_at = time.monotonic()
        settled_state = "sent" if error_code is None else "terminal"
        try:
            await self.db.tx(
                "news_delivery_settle",
                lambda repos: repos.news.settle_delivery(
                    event_id=event_id,
                    kind=kind,
                    state=settled_state,
                    receipt=receipt,
                    error_code=error_code,
                    now_ms=now_ms(),
                ),
            )
        except (TransientError, DeferError) as exc:
            raise RuntimeError("news_delivery_settlement_unavailable") from exc

    async def _settle_direct(self, event_id: str, kind: str, error_code: str, stamp: int) -> None:
        def _fn(repos: Any) -> None:
            state = repos.news.begin_delivery(event_id=event_id, kind=kind, card={}, now_ms=stamp)
            if state == "new":
                repos.news.settle_delivery(
                    event_id=event_id, kind=kind, state="terminal", receipt=None, error_code=error_code, now_ms=stamp
                )

        await self.db.tx("news_delivery_settle_direct", _fn)

    async def _market_data(
        self,
        shown: Sequence[str],
        stamp: int,
        *,
        news_at_ms: int | None,
    ) -> list[dict[str, Any]]:
        """Fresh push prices plus the two historical anchors rendered on the card.

        The caller passes the same code-verified asset list to the renderer, so
        the facts and quote lines cannot describe different symbols. Resolution
        remains owned by PriceRepository. Every price-plane failure returns an
        empty display value and leaves the already-made send decision untouched.
        """

        if not shown:
            return []
        if self._price_fetcher_for is not None:
            return await self._point_market_data(shown, stamp, news_at_ms=news_at_ms)
        try:
            rows = await self.db.read(
                "news_delivery_quotes",
                lambda repos: repos.price.quotes_for_symbols(shown, now_ms=stamp),
                timeout_seconds=_QUOTE_READ_TIMEOUT_SECONDS,
            )
        except Exception:  # price is display-only; all failures degrade to no line
            return []
        quotes = [dict(row) for row in rows or [] if isinstance(row, Mapping)]
        if self._candle_fetcher_for is None:
            return quotes
        news_target_ms = (
            news_at_ms
            if isinstance(news_at_ms, int) and not isinstance(news_at_ms, bool) and 0 < news_at_ms <= stamp
            else None
        )
        tasks: list[Awaitable[tuple[int, Sequence[Candle]] | None]] = []
        for index, quote in enumerate(quotes):
            if quote.get("state") != "fresh":
                continue
            venue = str(quote.get("venue") or "").strip()
            venue_symbol = str(quote.get("venue_symbol") or "").strip()
            fetcher = self._candle_fetcher_for(venue) if venue and venue_symbol else None
            if fetcher is None:
                continue
            targets = [stamp - _ONE_HOUR_MS]
            if news_target_ms is not None:
                targets.append(news_target_ms)
            start_ms = min(targets) - _DELIVERY_CANDLE_GAP_MS
            tasks.append(self._delivery_candles(index, fetcher, venue_symbol, start_ms, stamp))
        if not tasks:
            return quotes
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) or result is None:
                continue
            index, candles = result
            hour = select_candle(candles, target_ms=stamp - _ONE_HOUR_MS, max_gap_ms=_DELIVERY_CANDLE_GAP_MS)
            if hour is not None:
                quotes[index]["price_one_hour_before_push"] = str(hour.close)
            if news_target_ms is not None:
                news = select_candle(candles, target_ms=news_target_ms, max_gap_ms=_DELIVERY_CANDLE_GAP_MS)
                if news is not None:
                    quotes[index]["price_at_news"] = str(news.close)
        return quotes

    async def _point_market_data(
        self,
        shown: Sequence[str],
        stamp: int,
        *,
        news_at_ms: int | None,
    ) -> list[dict[str, Any]]:
        """Trade-first anchors with whole-calculation venue failover.

        A candidate is accepted only as one unit: current, news and one-hour prices all retain the same
        ``(venue, venue_symbol)``. Partial values are kept only if no later venue can provide the complete set.
        """

        try:
            rows, candidates = await self.db.read(
                "news_delivery_price_sources",
                lambda repos: (
                    repos.price.quotes_for_symbols(shown, now_ms=stamp),
                    repos.price.instruments_for_symbols(shown),
                ),
                timeout_seconds=_QUOTE_READ_TIMEOUT_SECONDS,
            )
        except Exception:
            return []
        originals = {
            str(row.get("requested_symbol") or ""): dict(row) for row in rows or [] if isinstance(row, Mapping)
        }
        news_target = (
            news_at_ms
            if isinstance(news_at_ms, int) and not isinstance(news_at_ms, bool) and 0 < news_at_ms <= stamp
            else None
        )
        tasks = [
            self._point_quote(
                symbol,
                originals.get(symbol, {}),
                tuple(candidates.get(symbol, ())),
                stamp=stamp,
                news_target_ms=news_target,
            )
            for symbol in shown
        ]
        resolved = await asyncio.gather(*tasks, return_exceptions=True)
        out: list[dict[str, Any]] = []
        for symbol, result in zip(shown, resolved, strict=True):
            if isinstance(result, BaseException):
                fallback = originals.get(symbol)
                if fallback:
                    out.append(dict(fallback))
            elif result:
                out.append(result)
        return out

    async def _point_quote(
        self,
        symbol: str,
        original: Mapping[str, Any],
        instruments: Sequence[PriceInstrument],
        *,
        stamp: int,
        news_target_ms: int | None,
    ) -> dict[str, Any]:
        targets = [stamp, stamp - _ONE_HOUR_MS]
        if news_target_ms is not None:
            targets.append(news_target_ms)
        expected_class = str(instruments[0].instrument_class) if instruments else ""
        candidates = [
            instrument
            for instrument in instruments
            if not expected_class
            or expected_class == "unknown"
            or instrument.instrument_class in {expected_class, "unknown"}
        ] or list(instruments)
        candidates = self._bounded_price_candidates(candidates)
        first_partial: dict[str, Any] | None = None
        seen_contracts: set[tuple[str, str]] = set()
        for instrument in candidates:
            contract = (instrument.venue, instrument.venue_symbol)
            if contract in seen_contracts:
                continue
            seen_contracts.add(contract)
            fetcher = self._price_fetcher_for(instrument.venue) if self._price_fetcher_for else None
            if fetcher is None:
                continue
            try:
                points = await asyncio.wait_for(
                    fetcher(instrument.venue_symbol, targets),
                    timeout=_DELIVERY_PRICE_SOURCE_TIMEOUT_SECONDS,
                )
            except Exception:  # noqa: S112 - one provider failure is the signal to try the next venue
                continue
            current = points.get(stamp)
            if current is None:
                continue
            quote = self._quote_from_points(
                symbol,
                original,
                instrument,
                points,
                stamp=stamp,
                news_target_ms=news_target_ms,
            )
            if first_partial is None:
                first_partial = quote
            if all(target in points for target in targets):
                return quote
        return first_partial or dict(original)

    @staticmethod
    def _bounded_price_candidates(instruments: Sequence[PriceInstrument]) -> list[PriceInstrument]:
        """At most two Binance contracts, then one Hyperliquid and one OKX contract."""

        limits = {"binance": 2, "hl": 1, "okx": 1}
        counts = {family: 0 for family in limits}
        out: list[PriceInstrument] = []
        for instrument in instruments:
            family = instrument.venue.split(".", 1)[0]
            if family not in limits or counts[family] >= limits[family]:
                continue
            counts[family] += 1
            out.append(instrument)
        return out

    @staticmethod
    def _quote_from_points(
        symbol: str,
        original: Mapping[str, Any],
        instrument: PriceInstrument,
        points: Mapping[int, PricePoint],
        *,
        stamp: int,
        news_target_ms: int | None,
    ) -> dict[str, Any]:
        current = points[stamp]
        same_snapshot = (
            str(original.get("venue") or "") == instrument.venue
            and str(original.get("venue_symbol") or "") == instrument.venue_symbol
            and original.get("state") == "fresh"
        )
        quote: dict[str, Any] = {
            "requested_symbol": symbol,
            "symbol": instrument.base_symbol,
            "base_symbol": instrument.base_symbol,
            "venue": instrument.venue,
            "venue_symbol": instrument.venue_symbol,
            "instrument_class": instrument.instrument_class,
            "quote_asset": instrument.quote_asset,
            "price": str(current.price),
            "price_kind": "last",
            "price_kind_zh": "成交价",
            "source_at_ms": current.at_ms,
            "received_at_ms": stamp,
            "age_ms": max(0, stamp - current.at_ms),
            "state": "fresh",
            "state_zh": "实时",
            "delivery_price_basis": current.basis,
            "change_pct": original.get("change_pct") if same_snapshot else None,
            "change_basis": original.get("change_basis") if same_snapshot else None,
            "change_basis_zh": original.get("change_basis_zh") if same_snapshot else None,
        }
        hour = points.get(stamp - _ONE_HOUR_MS)
        if hour is not None:
            quote["price_one_hour_before_push"] = str(hour.price)
            quote["price_one_hour_before_push_basis"] = hour.basis
        if news_target_ms is not None:
            news = points.get(news_target_ms)
            if news is not None:
                quote["price_at_news"] = str(news.price)
                quote["price_at_news_basis"] = news.basis
        return quote

    async def _delivery_candles(
        self,
        index: int,
        fetcher: DeliveryCandleFetcher,
        venue_symbol: str,
        start_ms: int,
        end_ms: int,
    ) -> tuple[int, Sequence[Candle]] | None:
        try:
            candles = await asyncio.wait_for(
                fetcher(venue_symbol, start_ms, end_ms),
                timeout=_DELIVERY_CANDLE_TIMEOUT_SECONDS,
            )
        except Exception:
            return None
        return index, candles

    def _load(self, repos: Any, event_id: str, stamp: int) -> tuple[Any, ...] | None:
        del stamp
        card = repos.news.event_card(event_id)
        routing = repos.news.event_admission(event_id)
        timing = repos.news.event_delivery_timing(event_id)
        if card is None or routing is None:
            return None
        admission = str(routing.get("admission") or "")
        event_kind = str(routing.get("event_kind") or "")
        if event_kind == "unsupported_market":
            return card, None, None, admission, event_kind, timing
        triage = repos.news.latest_verdict(event_id=event_id, stage="triage")
        if triage is None:
            return None
        oi_signal = None
        if (
            event_kind == "oi"
            and str(triage.get("program_version") or "") == OI_PROGRAM_VERSION
            and str(triage.get("program_sha256") or "") == self._oi_program_sha256
        ):
            oi_signal = repos.news.oi_signal(event_id=event_id, metric_version=OI_METRIC_VERSION)
        return card, triage, oi_signal, admission, event_kind, timing

    async def close(self) -> None:
        if self.sender is not None:
            with contextlib.suppress(Exception):
                await self.finite.run(
                    "news_delivery_sender_close", self.sender.close, timeout_seconds=5.0, allow_shutdown=True
                )
