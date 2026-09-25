"""Bounded episode price observations, independently scheduled from detection.

Two stages share one turn and one budget of provider calls:

* **t0** writes the episode's baseline once, from the first price that is really available after the
  trigger, and only while the episode is still fresh (`REFERENCE_MAX_DELAY_MS`). The sampler is the
  only writer of `reference_*`; the detector records the trigger and never a price (#649 §8).
* **horizons** record 15m/1h/4h observations against that baseline, and say `missing_reference`
  rather than inventing a 0% change when there is nothing comparable to measure against.

Neither stage blocks a first report, neither backfills an episode that is already past its budget,
and an episode whose token cannot be priced goes to the back of the queue instead of holding the
others: both stages order by the same last-attempt stamp.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any, Final, Protocol

from ..bus import now_ms
from ..wallet_contracts import (
    OUTCOME_MAX_DELAY_MS,
    REFERENCE_MAX_DELAY_MS,
    WalletOutcome,
    WalletReference,
)
from .contracts import ChainTapeDatabasePort
from .tape_io import FAILED, TapePasses, log

PRICE_SOURCE = "dexscreener_robinhood_chain_base_token"
# One turn's provider calls, unchanged by the t0 stage: baselines come out of the same six, and take
# nothing from the horizons on a turn with no fresh episode to price.
PRICE_CALLS_PER_TURN: Final = 6
REFERENCE_CALLS_PER_TURN: Final = 2


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
        fresh = await self._read(
            "news_wallet_reference_due",
            lambda repos: repos.news.chain_tape_due_references(
                now_ms=self._clock(), max_delay_ms=REFERENCE_MAX_DELAY_MS, limit=REFERENCE_CALLS_PER_TURN
            ),
            errors,
        )
        fresh = [] if fresh is FAILED else list(fresh)
        references = await self._references(fresh, errors)
        due = await self._read(
            "news_wallet_prices_due",
            lambda repos: repos.news.chain_tape_due_outcomes(
                now_ms=self._clock(), limit=PRICE_CALLS_PER_TURN - len(fresh)
            ),
            errors,
        )
        due = [] if due is FAILED else list(due)
        outcomes = self._outcomes(due, await self._horizon_prices(due, errors))
        attempted = [row["item_id"] for row in (*fresh, *due)]
        if not attempted:
            return 0
        stamp = self._clock()

        def record(repos: Any) -> int:
            repos.news.chain_tape_mark_outcome_attempted(attempted, now_ms=stamp)
            written = sum(bool(repos.news.chain_tape_record_reference(reference)) for reference in references)
            return written + sum(bool(repos.news.chain_tape_record_outcome(outcome)) for outcome in outcomes)

        written = await self._write("news_wallet_prices_record", record, errors)
        return 0 if written is FAILED else int(written)

    async def _references(self, rows: Sequence[dict[str, Any]], errors: list[str]) -> list[WalletReference]:
        """The first really available price per fresh episode, with the delay it cost.

        A provider that answers nothing, answers an unusable number, or answers after the budget has
        run out leaves the episode without a baseline: the next turn tries again while the episode is
        still fresh, and never after.
        """

        references = []
        for row in rows:
            price = await self._price(row["token"], errors)
            sampled = self._clock()
            if price is None:
                continue
            if sampled - row["event_at_ms"] > REFERENCE_MAX_DELAY_MS:
                continue
            reference = WalletReference(
                item_id=row["item_id"],
                price=price,
                at_ms=sampled,
                source=PRICE_SOURCE,
                trigger_at_ms=row["event_at_ms"],
            )
            log.info(
                "wallet reference item_id=%s delay_ms=%s source=%s",
                reference.item_id,
                reference.delay_ms,
                reference.source,
            )
            references.append(reference)
        return references

    async def _horizon_prices(
        self, rows: Sequence[dict[str, Any]], errors: list[str]
    ) -> list[tuple[Decimal | None, int]]:
        samples = []
        for row in rows:
            late = self._clock() - row["target_at_ms"] >= OUTCOME_MAX_DELAY_MS
            price = None if late else await self._price(row["token"], errors)
            samples.append((price, self._clock()))
        return samples

    def _outcomes(
        self, rows: Sequence[dict[str, Any]], samples: Sequence[tuple[Decimal | None, int]]
    ) -> list[WalletOutcome]:
        outcomes = []
        for row, (sample, sampled) in zip(rows, samples, strict=True):
            late = sampled - row["target_at_ms"] >= OUTCOME_MAX_DELAY_MS
            price = None if late else sample
            if price is None and not late:
                continue
            # A baseline recorded at or after the horizon's own target time is not a baseline for it,
            # so the observation stays unknown rather than reporting a change it cannot measure.
            comparable = row["reference_price"] is not None and int(row["reference_at_ms"]) < row["target_at_ms"]
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
                    status="late" if late else ("comparable" if comparable else "missing_reference"),
                    delivery_key=row["delivery_key"],
                )
            )
        return outcomes

    async def _price(self, token: str, errors: list[str]) -> Decimal | None:
        try:
            price = await self.prices.token_price(token)
        except Exception:
            errors.append("price_unavailable")
            return None
        return price if price is not None and price.is_finite() and price > 0 else None
