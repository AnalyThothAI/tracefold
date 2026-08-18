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


class _CurrentTicks:
    def __init__(self, observed_at: dict[str, int] | None = None) -> None:
        self.observed_at = dict(observed_at or {})

    def observed_at_by_target(self, *, target_type: str, target_ids) -> dict[str, int]:
        assert target_type == "chain_token"
        return {target_id: self.observed_at[target_id] for target_id in target_ids if target_id in self.observed_at}


class _Database:
    def __init__(self, observed_at: dict[str, int] | None = None) -> None:
        self.registry = _Registry()
        self.market_tick_current = _CurrentTicks(observed_at)

    async def run_business(self, operation_name, function, /, *args, **kwargs):
        del kwargs
        if operation_name == "market_tick_poll_load":
            return function()
        if operation_name == "market_tick_poll_publish":
            return args[0]
        raise AssertionError(operation_name)

    @contextmanager
    def worker_session(self, _application_name: str):
        yield SimpleNamespace(registry=self.registry, market_tick_current=self.market_tick_current)


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


def test_chain_targets_are_quoted_stalest_first_so_paced_batches_rotate() -> None:
    quotes = _DexQuotes()
    observed_at = {f"solana:token-{index:03d}": 5_000 - index for index in range(100)}  # token-099 is the oldest tick
    del observed_at["solana:token-042"]  # never quoted
    poll = MarketTickPoll(
        db=_Database(observed_at),
        providers=SimpleNamespace(dex_quote_market=quotes, cex_market=None),
        finite_operations=_FiniteOperations(),
        clock=lambda: 1_000,
    )

    asyncio.run(poll.sample())

    assert quotes.calls[0][:3] == ["token-042", "token-099", "token-098"]
    assert quotes.calls[0][-1] == "token-000"
    assert len(quotes.calls[0]) == 100
