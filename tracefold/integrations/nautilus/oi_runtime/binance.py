"""The Binance USD-M adapter this Runtime runs on, and the one venue read it makes itself (#680 PR-3).

Native trade reads canonicalize symbols before querying, page within a bounded
request budget, and refuse incomplete or contradictory evidence. Startup, user-data
resubscribe and the engine's missing-fill query all use this same report entry.

`BinanceVenuePositions` is the other half: the account's signed positions, read through the same
Nautilus HTTP stack and credentials, for the Strategy to compare with the Cache. A read that fails
raises; it never answers "flat".

The adapter keeps the installed-version private symbol helper local to this file; the factory builds
the client from the same public helpers `BinanceLiveExecClientFactory.create` uses.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from nautilus_trader.adapters.binance import BinanceAccountType, BinanceExecClientConfig
from nautilus_trader.adapters.binance.common.enums import (
    BinanceEnvironment,
    BinanceErrorCode,
    BinanceKeyType,
    BinanceOrderStatus,
)
from nautilus_trader.adapters.binance.common.schemas.account import BinanceUserTrade
from nautilus_trader.adapters.binance.common.urls import get_ws_base_url
from nautilus_trader.adapters.binance.factories import (
    get_cached_binance_futures_instrument_provider,
    get_cached_binance_http_client,
)
from nautilus_trader.adapters.binance.futures.execution import BinanceFuturesExecutionClient
from nautilus_trader.adapters.binance.futures.http.account import BinanceFuturesAccountHttpAPI
from nautilus_trader.adapters.binance.http.error import BinanceClientError, get_binance_error_code
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import (
    GenerateFillReports,
    GenerateOrderStatusReport,
    GenerateOrderStatusReports,
    GeneratePositionStatusReports,
)
from nautilus_trader.execution.reports import ExecutionMassStatus, FillReport, OrderStatusReport, PositionStatusReport
from nautilus_trader.live.factories import LiveExecClientFactory
from nautilus_trader.model.enums import OrderType, PositionSide
from nautilus_trader.model.identifiers import ClientOrderId, VenueOrderId
from nautilus_trader.model.objects import Quantity

from .config import BinanceRuntimeCredentials
from .order_evidence import BinanceOrderEvidence, OrderEvidenceRequest, read_order_evidence, validate_order_evidence
from .trade_history import TRADE_WINDOW_MS, IncompleteTradeHistory, TradeHistoryCursor, read_trade_history

# How long a signed positionRisk read stays valid at Binance. The venue's default is 5 s, and this
# host has measured 9-27 s round trips (`-1021`, #680); a read has no side effect a late arrival
# could repeat, so it gets the venue's maximum and the caller's timeout bounds it instead.
_POSITION_READ_RECV_WINDOW_MS = "60000"


@dataclass(slots=True)
class _OrderRecovery:
    next_attempt: float = 0
    delay: float = 5
    task: asyncio.Task[None] | None = None


class OiBinanceFuturesExecutionClient(BinanceFuturesExecutionClient):
    """Nautilus' Binance USD-M execution client, whose fill reports name each venue trade once."""

    def __init__(self, *, recovery_symbols: frozenset[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._recovery_symbols = set(recovery_symbols)
        self._order_recovery: dict[OrderEvidenceRequest, _OrderRecovery] = {}
        self._evidence_reads: dict[tuple[str, str], asyncio.Task[BinanceOrderEvidence]] = {}
        self._evidence_slots = asyncio.Semaphore(2)

    def _get_cache_active_symbols(self) -> set[str]:
        # A generation starts with an empty Cache. PG-open plans are query scope for historical
        # venue reports even when both the venue position and its child order are already closed.
        return super()._get_cache_active_symbols() | self._recovery_symbols

    def _native_fill_report(self, trade: BinanceUserTrade) -> FillReport:
        report = trade.parse_to_fill_report(
            account_id=self.account_id,
            instrument_id=self._get_cached_instrument_id(trade.symbol),
            report_id=UUID4(),
            ts_init=self._clock.timestamp_ns(),
            use_position_ids=self._use_position_ids,
        )
        # SDK millis_to_nanos uses a float; current venue epochs lose nanoseconds.
        # Preserve the actual integer millisecond timestamp in both consumers.
        if trade.time is None:
            raise ValueError("binance_native_trade_clock_missing")
        report.ts_event = trade.time * 1_000_000
        return report

    async def _read_order_evidence(self, request: OrderEvidenceRequest) -> BinanceOrderEvidence:
        key = (request.symbol, request.client_order_id)
        task = self._evidence_reads.get(key)
        if task is None:

            async def read() -> BinanceOrderEvidence:
                async with self._evidence_slots:
                    return await asyncio.wait_for(
                        read_order_evidence(
                            self._futures_http_account,
                            request=request,
                            observed_at_ns=self._clock.timestamp_ns(),
                        ),
                        timeout=10,
                    )

            task = self.create_task(read())
            self._evidence_reads[key] = task
            task.add_done_callback(lambda completed: self._evidence_reads.pop(key, None))
        evidence = await asyncio.shield(task)
        return validate_order_evidence(replace(evidence, request=request))

    def _schedule_order_recovery(self, request: OrderEvidenceRequest) -> None:
        # Duplicate WS/open-list observations do not refresh the retry budget.
        # The native client owns task cancellation when its generation stops.
        if request.conditional_type is not None:
            request = replace(request, parent_algo_id=None, venue_order_id=None)
        recovery = self._order_recovery.get(request)
        if recovery is None:
            if len(self._order_recovery) >= 64:
                self._log.error("Native order recovery scope budget exhausted")
                return
            recovery = self._order_recovery[request] = _OrderRecovery()
        if recovery.task is not None and not recovery.task.done():
            return
        if self._loop.time() < recovery.next_attempt:
            return
        recovery.next_attempt = self._loop.time() + recovery.delay
        recovery.delay = min(60, recovery.delay * 2)
        recovery.task = self.create_task(self._recover_native_order(request))

    async def _recover_native_order(self, request: OrderEvidenceRequest) -> None:
        evidence = await self._read_order_evidence(request)
        if await self._apply_order_evidence(evidence):
            self._order_recovery.pop(request, None)

    async def _apply_order_evidence(self, evidence: BinanceOrderEvidence) -> bool:
        request = evidence.request
        if not evidence.complete:
            return False
        order = self._cache.order(ClientOrderId(request.client_order_id))
        if order is not None and evidence.order is None and evidence.parent is not None:
            self._send_order_status_report(
                evidence.parent.parse_to_order_status_report(
                    account_id=self.account_id,
                    instrument_id=order.instrument_id,
                    report_id=UUID4(),
                    enum_parser=self._futures_enum_parser,
                    ts_init=self._clock.timestamp_ns(),
                )
            )
            return True
        if evidence.order is None or evidence.history is None:
            return False
        # Historical-only evidence must not create an isolated old SELL in the
        # current Cache. Startup's scoped mass reconciliation owns cold replay.
        if order is None:
            return False
        if not await self._apply_child_identity(evidence):
            return False
        order = self._cache.order(ClientOrderId(request.client_order_id))
        if (
            order.venue_order_id != VenueOrderId(str(evidence.order.orderId))
            or order.account_id != self.account_id
            or order.instrument_id != self._get_cached_instrument_id(evidence.order.symbol)
            or order.side.name != evidence.order.side.value
        ):
            raise ValueError("binance_order_application_identity_conflict")
        applied = sum(
            (
                Decimal(trade.qty)
                for trade in evidence.history.trades
                if str(trade.id) in {identity.value for identity in order.trade_ids}
            ),
            Decimal(),
        )
        if applied != order.filled_qty.as_decimal():
            # An old inferred fill cannot be safely replaced by applying its
            # economic quantity again. Retain evidence for the ledger consumer.
            raise ValueError("binance_cache_native_trade_history_conflict")
        status = ExecutionMassStatus(
            client_id=self.id,
            account_id=self.account_id,
            venue=self.venue,
            report_id=UUID4(),
            ts_init=self._clock.timestamp_ns(),
        )
        status.add_order_reports([self._application_order_report(evidence)])
        status.add_fill_reports([self._native_fill_report(trade) for trade in evidence.history.trades])
        self._send_mass_status_report(status)
        deadline = self._loop.time() + 2
        while order.filled_qty.as_decimal() != Decimal(evidence.order.executedQty):
            if self._loop.time() >= deadline:
                return False
            await asyncio.sleep(0.01)
        return True

    def _application_order_report(self, evidence: BinanceOrderEvidence) -> OrderStatusReport:
        order = evidence.order
        if order is None or order.time is None or order.updateTime is None:
            raise ValueError("binance_order_evidence_missing")
        report = order.parse_to_order_status_report(
            account_id=self.account_id,
            instrument_id=self._get_cached_instrument_id(order.symbol),
            report_id=UUID4(),
            enum_parser=self._enum_parser,
            treat_expired_as_canceled=self._treat_expired_as_canceled,
            ts_init=self._clock.timestamp_ns(),
        )
        # The raw immutable child keeps its actual client ID. This application
        # report targets the logical cached order proved by its signed parent.
        report.client_order_id = ClientOrderId(evidence.request.client_order_id)
        report.ts_accepted = order.time * 1_000_000
        report.ts_last = order.updateTime * 1_000_000
        return report

    def _handle_algo_update(self, raw: bytes) -> None:
        update = self._decoder_futures_algo_update.decode(raw)
        data = update.o
        if data.X in (BinanceOrderStatus.TRIGGERED, BinanceOrderStatus.FINISHED) or data.ai:
            if data.o.value not in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
                raise ValueError("binance_algo_type_invalid")
            self._schedule_order_recovery(
                OrderEvidenceRequest(
                    symbol=data.s,
                    client_order_id=data.caid,
                    parent_algo_id=data.aid,
                    conditional_type=data.o.value,
                )
            )
            return
        super()._handle_algo_update(raw)

    def _request_for_order(self, order: Any) -> OrderEvidenceRequest:
        instrument = self._cache.instrument(order.instrument_id)
        if instrument is None:
            raise ValueError("binance_order_instrument_missing")
        conditional_type = (
            "STOP_MARKET"
            if order.order_type == OrderType.STOP_MARKET
            else "TAKE_PROFIT_MARKET"
            if order.order_type == OrderType.MARKET_IF_TOUCHED
            else None
        )
        # After a trigger, the Cache ID names the child. Query a conditional
        # parent's exact client ID, so no stale numeric ID enters the wrong API.
        return OrderEvidenceRequest(
            symbol=instrument.raw_symbol.value,
            client_order_id=order.client_order_id.value,
            venue_order_id=(
                int(order.venue_order_id.value)
                if conditional_type is None and order.venue_order_id is not None
                else None
            ),
            conditional_type=conditional_type,
        )

    def _handle_order_trade_update(self, raw: bytes) -> None:
        update = self._decoder_futures_order_update.decode(raw)
        data = update.o
        order = self._cache.order(ClientOrderId(data.c)) if data.c else None
        if order is not None and Decimal(data.z) > 0:
            # This also handles trade-before-identity and cumulative FILLED
            # notifications. The raw event never races an inferred fill ahead of
            # the signed native-trade chain.
            self._schedule_order_recovery(self._request_for_order(order))
            return
        super()._handle_order_trade_update(raw)

    async def generate_order_status_report(self, command: GenerateOrderStatusReport) -> OrderStatusReport | None:
        order = self._cache.order(command.client_order_id) if command.client_order_id is not None else None
        if order is None:
            return await super().generate_order_status_report(command)
        evidence = await self._read_order_evidence(self._request_for_order(order))
        if evidence.order is None:
            if evidence.parent is None:
                return None
            return evidence.parent.parse_to_order_status_report(
                account_id=self.account_id,
                instrument_id=order.instrument_id,
                report_id=UUID4(),
                enum_parser=self._futures_enum_parser,
                ts_init=self._clock.timestamp_ns(),
            )
        if not await self._apply_order_evidence(evidence):
            return None
        # Every economic increment has an actual Cache acknowledgement now. A
        # single-report consumer therefore sees no unfilled cumulative gap.
        return self._application_order_report(evidence)

    async def generate_order_status_reports(self, command: GenerateOrderStatusReports) -> list[OrderStatusReport]:
        if not command.open_only:
            return await super().generate_order_status_reports(command)
        symbol = None
        if command.instrument_id is not None:
            instrument = self._cache.instrument(command.instrument_id)
            symbol = (
                instrument.raw_symbol.value
                if instrument is not None
                else command.instrument_id.symbol.value.removesuffix("-PERP")
            )
        # Both authoritative lists must succeed. The upstream convenience method
        # converts an HTTP failure to [] and cannot prove that an order disappeared.
        regular = await self._futures_http_account.query_open_orders(symbol)
        parents = await self._futures_http_account.query_open_algo_orders(symbol)
        known = self._cache.orders_open(instrument_id=command.instrument_id)
        visible = {value.clientOrderId for value in regular} | {value.clientAlgoId for value in parents}
        reports: list[OrderStatusReport] = []
        for value in regular:
            order = self._cache.order(ClientOrderId(value.clientOrderId))
            if Decimal(value.executedQty) > 0:
                if order is not None:
                    self._schedule_order_recovery(self._request_for_order(order))
                continue  # Never infer a fill from this open-order aggregate.
            reports.extend(self._parse_order_status_reports([value], None, None))
        for parent in parents:
            if parent.actualOrderId:
                order = self._cache.order(ClientOrderId(parent.clientAlgoId))
                if order is not None:
                    self._schedule_order_recovery(self._request_for_order(order))
                continue
            report = self._parse_algo_order_report(parent, None, None)
            if report is not None:
                reports.append(report)
        for order in known:
            if order.account_id == self.account_id and order.client_order_id.value not in visible:
                self._schedule_order_recovery(self._request_for_order(order))
        return reports

    async def _apply_child_identity(self, evidence: BinanceOrderEvidence) -> bool:
        """A native acknowledgement, not task scheduling, establishes the new Cache ID."""
        order = self._cache.order(ClientOrderId(evidence.request.client_order_id))
        parent, child = evidence.parent, evidence.order
        if order is None or parent is None or child is None:
            return True
        child_id = VenueOrderId(str(child.orderId))
        already_mapped = order.venue_order_id == child_id
        if (
            (order.is_closed and not already_mapped)
            or not order.is_reduce_only
            or order.account_id != self.account_id
            or order.instrument_id != self._get_cached_instrument_id(child.symbol)
            or order.side.name != child.side.value
            or order.order_type not in (OrderType.STOP_MARKET, OrderType.MARKET_IF_TOUCHED)
            or order.quantity.as_decimal() != Decimal(parent.quantity)
            or order.trigger_price.as_decimal() != Decimal(parent.triggerPrice)
            or order.venue_order_id is None
            or (not already_mapped and order.venue_order_id.value != str(parent.algoId))
            or parent.orderType
            != ("STOP_MARKET" if order.order_type == OrderType.STOP_MARKET else "TAKE_PROFIT_MARKET")
        ):
            raise ValueError("binance_cached_algo_identity_conflict")
        if already_mapped:
            return True
        self.generate_order_updated(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=child_id,
            quantity=order.quantity,
            price=None,
            trigger_price=order.trigger_price,
            ts_event=(parent.triggerTime or parent.updateTime or child.updateTime) * 1_000_000,
            venue_order_id_modified=True,
        )
        deadline = self._loop.time() + 2.0
        while self._cache.order(order.client_order_id).venue_order_id != child_id:
            if self._loop.time() >= deadline:
                return False
            await asyncio.sleep(0.01)
        self._triggered_algo_order_ids.add(order.client_order_id)
        return True

    async def generate_mass_status(self, lookback_mins: int | None = None) -> ExecutionMassStatus | None:
        """Reconcile only a complete, signed parent/child/native trade chain.

        The same immutable exact-order evidence is used by event-triggered
        recovery. MARKET remains the native child's type; logical purpose belongs
        to the durable Plan/order binding, never a rewritten venue report.
        """
        status = await super().generate_mass_status(lookback_mins)
        if status is None:
            return None
        targeted_reads = 0
        for report in status.order_reports.values():
            if report.client_order_id is None or report.venue_order_id is None:
                continue
            order = self._cache.order(report.client_order_id)
            instrument = self._cache.instrument(report.instrument_id)
            fills = status.fill_reports.get(report.venue_order_id, [])
            requires_native_fills = (
                order is None and instrument is not None and instrument.raw_symbol.value in self._recovery_symbols
            ) or (order is not None and report.filled_qty > order.filled_qty)
            if (
                requires_native_fills
                and sum((fill.last_qty.as_decimal() for fill in fills), Decimal()) != report.filled_qty.as_decimal()
            ):
                self._log.error(f"Historical order lacks complete native trades {report.client_order_id}")
                return None
            conditional = order is not None and order.order_type in (OrderType.STOP_MARKET, OrderType.MARKET_IF_TOUCHED)
            cold_child = (
                order is None
                and report.order_type == OrderType.MARKET
                and report.reduce_only
                and instrument is not None
                and instrument.raw_symbol.value in self._recovery_symbols
            )
            if not (conditional or cold_child) or report.filled_qty.as_decimal() <= 0:
                continue
            if instrument is None or report.order_type != OrderType.MARKET:
                return None
            if targeted_reads >= 16:
                raise RuntimeError("binance_order_evidence_request_budget_exhausted")
            targeted_reads += 1
            request = OrderEvidenceRequest(
                symbol=instrument.raw_symbol.value,
                client_order_id=report.client_order_id.value,
                venue_order_id=int(report.venue_order_id.value),
                conditional_type=(
                    "STOP_MARKET"
                    if order is not None and order.order_type == OrderType.STOP_MARKET
                    else "TAKE_PROFIT_MARKET"
                    if conditional
                    else "CONDITIONAL"
                ),
            )
            try:
                evidence = await self._read_order_evidence(request)
            except BinanceClientError as exc:
                if cold_child and get_binance_error_code(exc) == BinanceErrorCode.NO_SUCH_ORDER:
                    continue  # This exact client order is an ordinary reduce-only exit.
                raise
            child = evidence.order
            if (
                not evidence.complete
                or child is None
                or child.side.value != report.order_side.name
                or Decimal(child.origQty) != report.quantity.as_decimal()
                or Decimal(child.executedQty) != report.filled_qty.as_decimal()
                or evidence.history is None
                or {str(trade.id) for trade in evidence.history.trades} != {fill.trade_id.value for fill in fills}
            ):
                self._log.error(f"Native child evidence remains incomplete {report.client_order_id}")
                return None
            if not await self._apply_child_identity(evidence):
                return None
        return status

    async def generate_fill_reports(self, command: GenerateFillReports) -> list[FillReport]:
        if command.instrument_id is not None:
            instrument = self._cache.instrument(command.instrument_id)
            symbols = {
                str(instrument.raw_symbol.value)
                if instrument is not None
                else command.instrument_id.symbol.value.removesuffix("-PERP")
            }
        else:
            if command.venue_order_id is not None:
                raise ValueError("binance_order_trade_instrument_required")
            symbols = {
                symbol.removesuffix("-PERP")
                for symbol in self._get_cache_active_symbols() | await self._get_binance_active_position_symbols(None)
            }
        end_ms = int(command.end.timestamp() * 1_000) if command.end is not None else self._clock.timestamp_ms()
        start_ms = int(command.start.timestamp() * 1_000) if command.start is not None else end_ms - TRADE_WINDOW_MS + 1
        order_id = int(command.venue_order_id.value) if command.venue_order_id is not None else None
        reports: list[FillReport] = []
        remaining_requests = 32
        for symbol in sorted(symbols):
            if remaining_requests == 0:
                raise RuntimeError("binance_trade_history_request_budget_exhausted")
            history = await read_trade_history(
                self._futures_http_account,
                symbol=symbol,
                cursors=(TradeHistoryCursor(start_ms, end_ms),),
                order_id=order_id,
                max_requests=remaining_requests,
            )
            remaining_requests -= history.requests_used
            if not history.complete:
                raise IncompleteTradeHistory(history)
            reports.extend(self._native_fill_report(trade) for trade in history.trades)
        return sorted(reports, key=lambda report: (report.ts_event, report.instrument_id.value, report.trade_id.value))

    async def generate_position_status_reports(
        self, command: GeneratePositionStatusReports
    ) -> list[PositionStatusReport]:
        """Preserve read errors; Nautilus 1.231.0's base method turns BinanceError into [].

        A successful instrument-specific empty read is a genuine flat report. The engine
        receives exceptions through its existing failed-venue path and will not spend a
        discrepancy retry on a failed read. This private adapter seam is pinned by the
        installed-version regression test.
        """

        if command.instrument_id is not None:
            instrument = self._cache.instrument(command.instrument_id)
            symbol = (
                str(instrument.raw_symbol.value)
                if instrument is not None
                else command.instrument_id.symbol.value.removesuffix("-PERP")
            )
            reports = await self._get_binance_position_status_reports(symbol)
            if not reports:
                now_ns = self._clock.timestamp_ns()
                reports = [
                    PositionStatusReport(
                        account_id=self.account_id,
                        instrument_id=command.instrument_id,
                        position_side=PositionSide.FLAT,
                        quantity=Quantity.zero(),
                        report_id=UUID4(),
                        ts_last=now_ns,
                        ts_init=now_ns,
                    )
                ]
        else:
            reports = await self._get_binance_position_status_reports()
        self._log_report_receipt(len(reports), "PositionStatusReport", command.log_receipt_level)
        return reports


class OiBinanceExecClientFactory(LiveExecClientFactory):
    """`BinanceLiveExecClientFactory.create`'s USD-M branch, building `OiBinanceFuturesExecutionClient`."""

    recovery_symbols: frozenset[str] = frozenset()

    @classmethod
    def with_recovery_symbols(cls, symbols: frozenset[str]) -> type[OiBinanceExecClientFactory]:
        # TradingNode takes a factory class. Make its immutable query scope local to one generation.
        return type("OiBinanceRecoveryExecClientFactory", (cls,), {"recovery_symbols": symbols})

    @classmethod
    def create(  # type: ignore[override]
        cls,
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: BinanceExecClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> OiBinanceFuturesExecutionClient:
        if config.account_type != BinanceAccountType.USDT_FUTURES:
            raise ValueError("oi_runtime_exec_account_type_invalid")
        if config.key_type == BinanceKeyType.RSA:
            raise ValueError("oi_runtime_exec_key_type_invalid")
        if not config.api_key or not config.api_secret:
            raise ValueError("oi_runtime_credentials_invalid")
        environment = config.environment or BinanceEnvironment.LIVE
        client = get_cached_binance_http_client(
            clock=clock,
            account_type=config.account_type,
            api_key=config.api_key,
            api_secret=config.api_secret,
            key_type=config.key_type,
            base_url=config.base_url_http,
            environment=environment,
            is_us=config.us,
            proxy_url=config.proxy_url,
        )
        provider = get_cached_binance_futures_instrument_provider(
            client=client,
            clock=clock,
            account_type=config.account_type,
            config=config.instrument_provider,
            venue=config.venue,
        )
        return OiBinanceFuturesExecutionClient(
            loop=loop,
            client=client,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            base_url_ws=config.base_url_ws
            or get_ws_base_url(account_type=config.account_type, environment=environment, is_us=config.us),
            account_type=config.account_type,
            name=name,
            config=config,
            environment=environment,
            api_key=config.api_key,
            api_secret=config.api_secret,
            recovery_symbols=cls.recovery_symbols,
        )


class BinanceVenuePositions:
    """The account's signed USD-M positions, straight from Binance `GET /fapi/v3/positionRisk`.

    One-way mode (which `use_reduce_only` requires) has one `BOTH` row per symbol; each row's
    `positionAmt` is already signed, so the rows of a symbol are summed. Symbols are Binance's own
    spelling (`APTUSDT`). Any failure -- a Binance error such as `-1021`, a transport error, a timeout the
    caller imposes -- propagates: an unreadable account is unknown, never flat.
    """

    def __init__(
        self,
        *,
        environment: BinanceEnvironment | None,
        credentials: BinanceRuntimeCredentials,
        clock: LiveClock | None = None,
        account: Any = None,
    ) -> None:
        live_clock = clock or LiveClock()
        self._account = account or BinanceFuturesAccountHttpAPI(
            client=get_cached_binance_http_client(
                clock=live_clock,
                account_type=BinanceAccountType.USDT_FUTURES,
                api_key=credentials.api_key,
                api_secret=credentials.api_secret,
                **({} if environment is None else {"environment": environment}),
            ),
            clock=live_clock,
            account_type=BinanceAccountType.USDT_FUTURES,
        )

    async def read(self) -> dict[str, Decimal]:
        rows = await self._account.query_futures_position_risk(recv_window=_POSITION_READ_RECV_WINDOW_MS)
        positions: dict[str, Decimal] = {}
        for row in rows:
            amount = Decimal(str(row.positionAmt))
            if amount:
                positions[str(row.symbol)] = positions.get(str(row.symbol), Decimal(0)) + amount
        return {symbol: amount for symbol, amount in positions.items() if amount}


__all__ = [
    "BinanceVenuePositions",
    "OiBinanceExecClientFactory",
    "OiBinanceFuturesExecutionClient",
]
