"""Bounded episode price observations, independently scheduled from detection."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any, Protocol

from ..bus import now_ms
from ..wallet_contracts import OUTCOME_MAX_DELAY_MS, WalletOutcome
from .loop import ChainTapeDatabasePort
from .tape_io import FAILED, TapePasses

PRICE_SOURCE = "dexscreener_robinhood_chain_base_token"


class PricePort(Protocol):
    async def token_price(self, address: str) -> Decimal | None: ...
    async def aclose(self) -> None: ...


class WalletPriceSampler(TapePasses):
    _read_timeout_seconds = 5.0
    _write_timeout_seconds = 10.0
    _failure_stage = "wallet_prices"

    def __init__(self, *, db: ChainTapeDatabasePort, prices: PricePort, clock: Callable[[], int] = now_ms) -> None:
        self.db = db
        self.prices = prices
        self._clock = clock

    async def aclose(self) -> None:
        await self.prices.aclose()

    async def advance(self) -> int:
        errors: list[str] = []
        due = await self._read(
            "news_wallet_prices_due",
            lambda repos: repos.news.chain_tape_due_outcomes(now_ms=self._clock(), limit=6),
            errors,
        )
        if due is FAILED or not due:
            return 0
        outcomes = []
        for row in due:
            price = None
            late = self._clock() - row["target_at_ms"] >= OUTCOME_MAX_DELAY_MS
            if not late:
                try:
                    price = await self.prices.token_price(row["token"])
                except Exception:
                    errors.append("price_unavailable")
            sampled = self._clock()
            late = sampled - row["target_at_ms"] >= OUTCOME_MAX_DELAY_MS
            if late:
                price = None
            if price is not None and (not price.is_finite() or price <= 0):
                price = None
            if price is None and not late:
                continue
            status = "late" if late else ("missing_reference" if row["reference_price"] is None else "comparable")
            outcomes.append(
                WalletOutcome(
                    item_id=row["item_id"],
                    horizon=row["horizon"],
                    price=price,
                    at_ms=sampled,
                    source=PRICE_SOURCE if price is not None else "unavailable",
                    reference_price=row["reference_price"],
                    reference_at_ms=row["reference_at_ms"],
                    target_at_ms=row["target_at_ms"],
                    status=status,
                    delivery_key=row["delivery_key"],
                )
            )
        stamp = self._clock()

        def record(repos: Any) -> int:
            repos.news.chain_tape_mark_outcome_attempted([row["item_id"] for row in due], now_ms=stamp)
            return sum(repos.news.chain_tape_record_outcome(outcome) for outcome in outcomes)

        written = await self._write("news_wallet_prices_record", record, errors)
        return 0 if written is FAILED else int(written)
