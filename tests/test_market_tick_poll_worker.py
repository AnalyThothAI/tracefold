from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

from tracefold.market.pricing.market_tick_poll_worker import MarketTickPoll
from tracefold.market.provider_contracts import DexTokenQuote


class _Registry:
    def __init__(self) -> None:
        self.rows = [
            {
                "target_type": "chain_token",
                "target_id": f"solana:token-{index:03d}",
            }
            for index in range(101)
        ]

    def ranked_market_targets(
        self,
        *,
        since_ms: int,
        target_types: tuple[str, ...],
        limit: int,
    ) -> list[dict[str, str]]:
        del since_ms, target_types
        return self.rows[:limit]


class _Database:
    def __init__(self) -> None:
        self.registry = _Registry()

    async def run_business(self, operation_name, function, /, *args, **kwargs):
        del kwargs
        if operation_name == "market_tick_poll_load":
            return function()
        if operation_name == "market_tick_poll_publish":
            return args[0]
        raise AssertionError(operation_name)

    @contextmanager
    def worker_session(self, _application_name: str):
        yield SimpleNamespace(registry=self.registry)


class _DexQuotes:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def token_quotes(self, requests):
        self.calls.append([request.address for request in requests])
        return [
            DexTokenQuote(
                chain_id=request.chain_id,
                address=request.address,
                observed_at_ms=1_000,
                price_usd=1.0,
                raw={},
            )
            for request in requests
        ]


class _FiniteOperations:
    async def run(self, _operation_name, function, /, *args, **kwargs):
        kwargs.pop("timeout_seconds", None)
        return function(*args, **kwargs)


def test_consecutive_samples_refresh_the_same_recent_hot_targets() -> None:
    quotes = _DexQuotes()
    poll = MarketTickPoll(
        db=_Database(),
        providers=SimpleNamespace(dex_quote_market=quotes, cex_market=None),
        finite_operations=_FiniteOperations(),
        clock=lambda: 1_000,
    )

    async def scenario() -> None:
        await poll.sample()
        await poll.sample()

    asyncio.run(scenario())

    assert len(quotes.calls) == 2
    assert quotes.calls[0] == quotes.calls[1]
    assert quotes.calls[0][0] == "token-000"
    assert len(quotes.calls[0]) == 100
