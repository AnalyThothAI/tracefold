"""The Binance USD-M adapter this Runtime runs on, and the one venue read it makes itself (#680 PR-3).

Nautilus' own Binance execution client, with one public method made idempotent. On 1.231.0
`generate_fill_reports` asks Binance for the trades of every "active" symbol and unions two spellings
of the same symbol -- the Cache's `APTUSDT-PERP` and positionRisk's `APTUSDT` -- so `userTrades` is
called twice for one market and every fill comes back twice. Nautilus' mass-status reconciliation then
replays twice the venue's quantity and, taking the surplus for missing opening fills, adds a synthetic
opposite fill that closes the Cache position the venue still holds (the 2026-09-23 APT close, Path A).
The duplicate is dropped here, on the method every caller of the adapter goes through: the mass
status a user-data re-subscribe requests, startup reconciliation and the position check's
missing-fill query alike.

`BinanceVenuePositions` is the other half: the account's signed positions, read through the same
Nautilus HTTP stack and credentials, for the Strategy to compare with the Cache. A read that fails
raises; it never answers "flat".

The adapter keeps the installed-version private symbol helper local to this file; the factory builds
the client from the same public helpers `BinanceLiveExecClientFactory.create` uses.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from nautilus_trader.adapters.binance import BinanceAccountType, BinanceExecClientConfig
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment, BinanceErrorCode, BinanceKeyType
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
from nautilus_trader.execution.messages import GenerateFillReports, GeneratePositionStatusReports
from nautilus_trader.execution.reports import ExecutionMassStatus, FillReport, PositionStatusReport
from nautilus_trader.live.factories import LiveExecClientFactory
from nautilus_trader.model.enums import OrderSide, OrderStatus, OrderType, PositionSide, TriggerType
from nautilus_trader.model.objects import Price, Quantity

from .config import ActiveRuntimeMode, BinanceRuntimeCredentials, binance_environment

# How long a signed positionRisk read stays valid at Binance. The venue's default is 5 s, and this
# host has measured 9-27 s round trips (`-1021`, #680); a read has no side effect a late arrival
# could repeat, so it gets the venue's maximum and the caller's timeout bounds it instead.
_POSITION_READ_RECV_WINDOW_MS = "60000"


def unique_fill_reports(reports: Iterable[FillReport]) -> list[FillReport]:
    """Each venue trade once, first report kept, in the order the adapter returned them.

    A Binance trade id is unique per symbol, so the instrument, the venue order and the trade together
    name one fill.
    """

    seen: set[tuple[str, str, str]] = set()
    unique: list[FillReport] = []
    for report in reports:
        key = (report.instrument_id.value, report.venue_order_id.value, report.trade_id.value)
        if key in seen:
            continue
        seen.add(key)
        unique.append(report)
    return unique


class OiBinanceFuturesExecutionClient(BinanceFuturesExecutionClient):
    """Nautilus' Binance USD-M execution client, whose fill reports name each venue trade once."""

    def __init__(self, *, recovery_symbols: frozenset[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._recovery_symbols = set(recovery_symbols)

    def _get_cache_active_symbols(self) -> set[str]:
        # A generation starts with an empty Cache. PG-open plans are query scope for historical
        # venue reports even when both the venue position and its child order are already closed.
        return super()._get_cache_active_symbols() | self._recovery_symbols

    async def generate_mass_status(self, lookback_mins: int | None = None) -> ExecutionMassStatus | None:
        """Join a triggered conditional order to its venue child before replaying real fills.

        Binance reports a triggered Algo order's fills under a new regular order ID.
        Nautilus 1.231.0 skips the triggered Algo report, then cannot apply the child
        fills to the cached parent. A matching client ID alone is insufficient proof:
        query the original Algo order and require its signed actualOrderId to match.
        """

        status = await super().generate_mass_status(lookback_mins)
        if status is None:
            return None
        for report in status.order_reports.values():
            if report.client_order_id is None or report.venue_order_id is None:
                continue
            order = self._cache.order(report.client_order_id)
            instrument = self._cache.instrument(report.instrument_id)
            if order is None and instrument is not None and instrument.raw_symbol.value in self._recovery_symbols:
                # A newly rebuilt Cache has no earlier fill to account for a gap
                # between the cumulative order report and actual native trades.
                fills = status.fill_reports.get(report.venue_order_id, [])
                if sum((fill.last_qty.as_decimal() for fill in fills), Decimal()) != report.filled_qty.as_decimal():
                    self._log.error(f"Historical order lacks complete native trades {report.client_order_id}")
                    return None
            if order is None and report.order_type == OrderType.MARKET and report.reduce_only:
                # At generation start the old Cache is gone. The PG-open plan only widens
                # the venue query; a signed Algo receipt must prove that this regular
                # reduce-only order was its triggered protection, never a plain exit.
                if instrument is None or instrument.raw_symbol.value not in self._recovery_symbols:
                    continue
                fills = status.fill_reports.get(report.venue_order_id, [])
                if report.filled_qty.as_decimal() <= 0:
                    continue
                try:
                    algo = await self._futures_http_account.query_algo_order(
                        client_algo_id=report.client_order_id.value,
                    )
                except BinanceClientError as exc:
                    if get_binance_error_code(exc) == BinanceErrorCode.NO_SUCH_ORDER:
                        continue  # A regular reduce-only exit has no Algo parent.
                    raise
                if (
                    algo.clientAlgoId != report.client_order_id.value
                    or algo.algoId <= 0
                    or algo.algoType != "CONDITIONAL"
                    or algo.actualOrderId != report.venue_order_id.value
                    or algo.symbol != instrument.raw_symbol.value
                    or algo.side != report.order_side.name
                    or algo.orderType not in ("STOP_MARKET", "TAKE_PROFIT_MARKET")
                    or algo.algoStatus not in ("TRIGGERED", "FINISHED")
                    or algo.positionSide != "BOTH"
                    or algo.reduceOnly is not True
                    or algo.workingType != "MARK_PRICE"
                    or algo.quantity is None
                    or Decimal(algo.quantity) != report.quantity.as_decimal()
                    or algo.triggerPrice is None
                    or sum((fill.last_qty.as_decimal() for fill in fills), Decimal()) != report.filled_qty.as_decimal()
                    or any(
                        fill.account_id != report.account_id
                        or fill.instrument_id != report.instrument_id
                        or fill.venue_order_id != report.venue_order_id
                        or fill.order_side != report.order_side
                        for fill in fills
                    )
                ):
                    self._log.error(f"Signed Algo receipt does not match historical child {report.client_order_id}")
                    return None
                report.order_type = (
                    OrderType.STOP_MARKET if algo.orderType == "STOP_MARKET" else OrderType.MARKET_IF_TOUCHED
                )
                report.trigger_price = Price.from_str(algo.triggerPrice)
                report.trigger_type = TriggerType.MARK_PRICE
                report.ts_triggered = (algo.triggerTime or algo.updateTime or report.ts_last // 1_000_000) * 1_000_000
                continue
            if order is None or order.venue_order_id == report.venue_order_id:
                continue
            if order.order_type not in (OrderType.STOP_MARKET, OrderType.MARKET_IF_TOUCHED):
                continue
            fills = status.fill_reports.get(report.venue_order_id, [])
            expected_type = "STOP_MARKET" if order.order_type == OrderType.STOP_MARKET else "TAKE_PROFIT_MARKET"
            if (
                order.is_closed
                or not order.is_reduce_only
                or order.account_id != report.account_id
                or order.instrument_id != report.instrument_id
                or order.side != report.order_side
                or report.order_side not in (OrderSide.BUY, OrderSide.SELL)
                or report.order_type != OrderType.MARKET
                or report.order_status
                not in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED, OrderStatus.EXPIRED, OrderStatus.CANCELED)
                or not report.reduce_only
                or order.quantity != report.quantity
                or report.filled_qty.as_decimal() <= 0
                or report.filled_qty > order.quantity
                or (report.order_status == OrderStatus.FILLED and report.filled_qty != order.quantity)
                or not fills
                or sum((fill.last_qty.as_decimal() for fill in fills), Decimal()) != report.filled_qty.as_decimal()
                or any(
                    fill.account_id != report.account_id
                    or fill.instrument_id != report.instrument_id
                    or fill.venue_order_id != report.venue_order_id
                    or fill.order_side != report.order_side
                    for fill in fills
                )
                or order.venue_order_id is None
                or not order.venue_order_id.value.isdecimal()
            ):
                self._log.error(f"Cannot verify triggered Algo child for {report.client_order_id}")
                return None
            algo = await self._futures_http_account.query_algo_order(
                algo_id=int(order.venue_order_id.value),
            )
            instrument = self._cache.instrument(order.instrument_id)
            if (
                instrument is None
                or algo.algoId != int(order.venue_order_id.value)
                or algo.clientAlgoId != order.client_order_id.value
                or algo.algoType != "CONDITIONAL"
                or algo.actualOrderId != report.venue_order_id.value
                or algo.symbol != instrument.raw_symbol.value
                or algo.side != order.side.name
                or algo.orderType != expected_type
                or algo.algoStatus not in ("TRIGGERED", "FINISHED")
                or algo.positionSide != "BOTH"
                or algo.reduceOnly is not True
                or algo.workingType != "MARK_PRICE"
                or algo.quantity is None
                or Decimal(algo.quantity) != order.quantity.as_decimal()
                or algo.triggerPrice is None
                or Decimal(algo.triggerPrice) != order.trigger_price.as_decimal()
            ):
                self._log.error(f"Signed Algo receipt does not match child for {report.client_order_id}")
                return None
            self.generate_order_updated(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                venue_order_id=report.venue_order_id,
                quantity=order.quantity,
                price=None,
                trigger_price=order.trigger_price,
                ts_event=(algo.triggerTime or algo.updateTime or report.ts_last // 1_000_000) * 1_000_000,
                venue_order_id_modified=True,
            )
            deadline = self._loop.time() + 2.0
            while self._cache.order(order.client_order_id).venue_order_id != report.venue_order_id:
                if self._loop.time() >= deadline:
                    self._log.error(f"Timed out applying triggered Algo child mapping for {report.client_order_id}")
                    return None
                await asyncio.sleep(0.01)
        return status

    async def generate_fill_reports(self, command: GenerateFillReports) -> list[FillReport]:
        return unique_fill_reports(await super().generate_fill_reports(command))

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
        mode: ActiveRuntimeMode,
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
                environment=binance_environment(mode),
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
    "unique_fill_reports",
]
