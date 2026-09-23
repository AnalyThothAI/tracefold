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

Only public Nautilus API is used: the overridden method calls its own `super()`, and the factory builds
the client from the same public helpers `BinanceLiveExecClientFactory.create` uses.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from nautilus_trader.adapters.binance import BinanceAccountType, BinanceExecClientConfig
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment, BinanceKeyType
from nautilus_trader.adapters.binance.common.urls import get_ws_base_url
from nautilus_trader.adapters.binance.factories import (
    get_cached_binance_futures_instrument_provider,
    get_cached_binance_http_client,
)
from nautilus_trader.adapters.binance.futures.execution import BinanceFuturesExecutionClient
from nautilus_trader.adapters.binance.futures.http.account import BinanceFuturesAccountHttpAPI
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.execution.messages import GenerateFillReports
from nautilus_trader.execution.reports import FillReport
from nautilus_trader.live.factories import LiveExecClientFactory

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

    async def generate_fill_reports(self, command: GenerateFillReports) -> list[FillReport]:
        return unique_fill_reports(await super().generate_fill_reports(command))


class OiBinanceExecClientFactory(LiveExecClientFactory):
    """`BinanceLiveExecClientFactory.create`'s USD-M branch, building `OiBinanceFuturesExecutionClient`."""

    @staticmethod
    def create(  # type: ignore[override]
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
