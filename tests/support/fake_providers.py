from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from typing import Any

from tracefold.market import CexTicker, DexTokenQuote, DexTokenQuoteRequest


class FakeGmgnUpstreamClient:
    """Deterministically emits captured GMGN frames into the collector boundary."""

    def __init__(
        self,
        frames: Sequence[Any],
        on_frame: Callable[..., Any],
        *,
        received_at_ms: int,
    ) -> None:
        self.frames = list(frames)
        self.on_frame = on_frame
        self.received_at_ms = int(received_at_ms)
        self.closed = False

    async def run(self) -> None:
        for frame in self.frames:
            result = self.on_frame(frame, received_at_ms=self.received_at_ms)
            if inspect.isawaitable(result):
                await result

    async def aclose(self) -> None:
        self.closed = True


class FakeDexQuoteProvider:
    """Current DexTokenQuoteProvider protocol, with deterministic current-fact output."""

    def __init__(
        self,
        *,
        observed_at_ms: int,
        price_usd: float = 0.129,
        market_cap_usd: float = 1_234_567.0,
        liquidity_usd: float = 456_789.0,
        volume_24h_usd: float = 98_765.0,
        holders: int = 4321,
    ) -> None:
        self.observed_at_ms = int(observed_at_ms)
        self.price_usd = price_usd
        self.market_cap_usd = market_cap_usd
        self.liquidity_usd = liquidity_usd
        self.volume_24h_usd = volume_24h_usd
        self.holders = holders
        self.requests: list[list[tuple[str, str]]] = []

    def token_quotes(self, tokens: list[DexTokenQuoteRequest]) -> list[DexTokenQuote]:
        self.requests.append([(token.chain_id, token.address.lower()) for token in tokens])
        return [
            DexTokenQuote(
                chain_id=token.chain_id,
                address=token.address.lower(),
                observed_at_ms=self.observed_at_ms,
                price_usd=self.price_usd,
                market_cap_usd=self.market_cap_usd,
                liquidity_usd=self.liquidity_usd,
                volume_24h_usd=self.volume_24h_usd,
                holders=self.holders,
                raw={
                    "provider": "fake_dex_quote",
                    "chain_id": token.chain_id,
                    "address": token.address.lower(),
                },
            )
            for token in tokens
        ]


class FakeCexQuoteProvider:
    """Minimal CEX provider for hot-path wiring completeness."""

    def __init__(self, tickers: Sequence[CexTicker] = ()) -> None:
        self._tickers = list(tickers)
        self.requests: list[str] = []

    def tickers(self, *, inst_type: str) -> list[CexTicker]:
        self.requests.append(inst_type)
        return [ticker for ticker in self._tickers if ticker.inst_type == inst_type]

    def ticker(self, *, inst_id: str) -> CexTicker | None:
        self.requests.append(inst_id)
        return next((ticker for ticker in self._tickers if ticker.inst_id == inst_id), None)
