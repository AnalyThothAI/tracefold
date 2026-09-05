"""The composition root's database adapters: one per capability port, none shared.

`WorkerDatabase` is an App type with lanes, executors, pools and admission semantics. `tracefold.news`
and `tracefold.trading` depend on `platform` only, so neither may name it — yet until #162 PR7-B both
called `worker_session`, `run_news` and `heavy_business` on an `Any`, which is the same dependency
without the import edge that would have made it visible.

Each capability declares the narrow port it needs and this module satisfies it. The ports look alike
because bounded read / bounded transaction is genuinely what both need; they stay separate because the
answers differ where it matters — which physical lane admits the work, whether a deadline may default,
and whose error vocabulary an admission timeout speaks.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from tracefold.app.repository_session import RepositorySession
from tracefold.app.worker_database import WorkerDatabase
from tracefold.news.bus import DeferError, TransientError
from tracefold.news.chain_tape.loop import ChainTapeRepositories
from tracefold.news.market_review.loops import PriceRepositories
from tracefold.news.market_review.storage import InstrumentsRepository, PriceRepository
from tracefold.news.pipeline.runtime import NewsRepositories
from tracefold.news.storage.root import NewsRepository
from tracefold.platform.resource import ResourceAdmissionTimeout, ResourceOperationOverrun
from tracefold.trading.storage.root import TradingRepositories, TradingRepository

_NEWS_DEFAULT_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True, slots=True)
class _NewsCallbackRepositories:
    news: NewsRepository
    instruments: InstrumentsRepository
    price: PriceRepository


@dataclass(frozen=True, slots=True)
class _PriceCallbackRepositories:
    price: PriceRepository


@dataclass(frozen=True, slots=True)
class _TradingCallbackRepositories:
    trading: TradingRepository


def _news_repositories(repos: RepositorySession) -> NewsRepositories:
    return _NewsCallbackRepositories(news=repos.news, instruments=repos.instruments, price=repos.price)


def _price_repositories(repos: RepositorySession) -> PriceRepositories:
    return _PriceCallbackRepositories(price=repos.price)


def _trading_repositories(repos: RepositorySession) -> TradingRepositories:
    return _TradingCallbackRepositories(trading=repos.trading)


def _in_session[T, RepositoriesT](
    database: WorkerDatabase,
    name: str,
    fn: Callable[[RepositoriesT], T],
    timeout: float,
    select_repositories: Callable[[RepositorySession], RepositoriesT],
) -> Callable[[], T]:
    def _run() -> T:
        with database.worker_session(name, timeout) as repos:
            return fn(select_repositories(repos))

    return _run


class WorkerNewsDatabase:
    """`NewsDatabasePort` on the four-slot News lane, in the News error vocabulary.

    A lane that cannot admit the work is a `DeferError`: the broker requeues the message uncounted,
    because nothing about it was wrong. An operation that overruns its envelope is a `TransientError`
    and burns one of the three attempts before the dead-letter queue.
    """

    def __init__(self, database: WorkerDatabase) -> None:
        self._database = database

    async def read[T](
        self, name: str, fn: Callable[[NewsRepositories], T], *, timeout_seconds: float = _NEWS_DEFAULT_TIMEOUT_SECONDS
    ) -> T:
        return await self._run(
            name,
            _in_session(self._database, name, fn, timeout_seconds, _news_repositories),
            timeout_seconds,
        )

    async def tx[T](
        self, name: str, fn: Callable[[NewsRepositories], T], *, timeout_seconds: float = _NEWS_DEFAULT_TIMEOUT_SECONDS
    ) -> T:
        return await self._run(
            name,
            _in_session(self._database, name, fn, timeout_seconds, _news_repositories),
            timeout_seconds,
        )

    async def _run[T](self, name: str, run: Callable[[], T], timeout_seconds: float) -> T:
        try:
            return await self._database.run_news(name, run, operation_timeout_seconds=timeout_seconds)
        except ResourceAdmissionTimeout as exc:
            raise DeferError(f"db_admission_timeout:{name}") from exc
        except ResourceOperationOverrun as exc:
            raise TransientError(f"db_overrun:{name}") from exc


class WorkerNewsColdDatabase:
    """`NewsDatabasePort` for the Janitor's measured-heavy work: one slot on the business lane.

    Same error vocabulary as the hot lane — the Janitor's callers classify failures identically — and a
    different physical slot, so a retention sweep can never take one of the four the Deduper, Triage and
    the Deliverer were budgeted.
    """

    def __init__(self, database: WorkerDatabase) -> None:
        self._database = database
        self._lane = database.heavy_business()

    async def read[T](
        self, name: str, fn: Callable[[NewsRepositories], T], *, timeout_seconds: float = _NEWS_DEFAULT_TIMEOUT_SECONDS
    ) -> T:
        return await self._run(
            name,
            _in_session(self._database, name, fn, timeout_seconds, _news_repositories),
            timeout_seconds,
        )

    async def tx[T](
        self, name: str, fn: Callable[[NewsRepositories], T], *, timeout_seconds: float = _NEWS_DEFAULT_TIMEOUT_SECONDS
    ) -> T:
        return await self._run(
            name,
            _in_session(self._database, name, fn, timeout_seconds, _news_repositories),
            timeout_seconds,
        )

    async def _run[T](self, name: str, run: Callable[[], T], timeout_seconds: float) -> T:
        try:
            return await self._lane.run_business(name, run, operation_timeout_seconds=timeout_seconds)
        except ResourceAdmissionTimeout as exc:
            raise DeferError(f"db_admission_timeout:{name}") from exc
        except ResourceOperationOverrun as exc:
            raise TransientError(f"db_overrun:{name}") from exc


class WorkerQuoteDatabase:
    """`QuoteDatabasePort` (#304): latest-state quote plan/store on the ordinary business lane."""

    def __init__(self, database: WorkerDatabase) -> None:
        self._database = database

    async def read[T](self, name: str, fn: Callable[[PriceRepositories], T], *, timeout_seconds: float) -> T:
        return await self._run(
            name,
            _in_session(self._database, name, fn, timeout_seconds, _price_repositories),
            timeout_seconds,
        )

    async def tx[T](self, name: str, fn: Callable[[PriceRepositories], T], *, timeout_seconds: float) -> T:
        return await self._run(
            name,
            _in_session(self._database, name, fn, timeout_seconds, _price_repositories),
            timeout_seconds,
        )

    async def _run[T](self, name: str, run: Callable[[], T], timeout_seconds: float) -> T:
        try:
            return await self._database.run_business(name, run, operation_timeout_seconds=timeout_seconds)
        except ResourceAdmissionTimeout as exc:
            raise DeferError(f"quote_db_admission_timeout:{name}") from exc
        except ResourceOperationOverrun as exc:
            raise TransientError(f"quote_db_overrun:{name}") from exc


class WorkerReactionDatabase:
    """`ReactionDatabasePort` (#304): deterministic candle review on the one-slot heavy admission.

    Quote collection is intentionally absent from this adapter: a slow Reaction backlog must not hold the
    ordinary permit that keeps current display quotes moving.
    """

    def __init__(self, database: WorkerDatabase) -> None:
        self._database = database
        self._lane = database.heavy_business()

    async def read[T](self, name: str, fn: Callable[[PriceRepositories], T], *, timeout_seconds: float) -> T:
        return await self._run(
            name,
            _in_session(self._database, name, fn, timeout_seconds, _price_repositories),
            timeout_seconds,
        )

    async def tx[T](self, name: str, fn: Callable[[PriceRepositories], T], *, timeout_seconds: float) -> T:
        return await self._run(
            name,
            _in_session(self._database, name, fn, timeout_seconds, _price_repositories),
            timeout_seconds,
        )

    async def _run[T](self, name: str, run: Callable[[], T], timeout_seconds: float) -> T:
        try:
            return await self._lane.run_business(name, run, operation_timeout_seconds=timeout_seconds)
        except ResourceAdmissionTimeout as exc:
            raise DeferError(f"reaction_db_admission_timeout:{name}") from exc
        except ResourceOperationOverrun as exc:
            raise TransientError(f"reaction_db_overrun:{name}") from exc


class WorkerChainTapeDatabase:
    """`ChainTapeDatabasePort` (#572 PR-1): the wallet tape on ordinary business admission.

    Ordinary rather than heavy, and never the four-slot News lane. A tape turn is one small read and one
    short write of at most twenty receipts' worth of rows, so it does not need the heavy slot the Janitor
    and Event Reactions share -- and it must not be able to take a slot the Deduper, Triage and the
    Deliverer were budgeted, because ingestion is the thing every capability reads.
    """

    def __init__(self, database: WorkerDatabase) -> None:
        self._database = database

    async def read[T](self, name: str, fn: Callable[[ChainTapeRepositories], T], *, timeout_seconds: float = 3.0) -> T:
        return await self._run(
            name,
            _in_session(self._database, name, fn, timeout_seconds, _news_repositories),
            timeout_seconds,
        )

    async def tx[T](self, name: str, fn: Callable[[ChainTapeRepositories], T], *, timeout_seconds: float = 3.0) -> T:
        return await self._run(
            name,
            _in_session(self._database, name, fn, timeout_seconds, _news_repositories),
            timeout_seconds,
        )

    async def _run[T](self, name: str, run: Callable[[], T], timeout_seconds: float) -> T:
        try:
            return await self._database.run_business(name, run, operation_timeout_seconds=timeout_seconds)
        except ResourceAdmissionTimeout as exc:
            raise DeferError(f"chain_tape_db_admission_timeout:{name}") from exc
        except ResourceOperationOverrun as exc:
            raise TransientError(f"chain_tape_db_overrun:{name}") from exc


class WorkerTradingDatabase:
    """`TradingDatabasePort` (#104): the same one-slot heavy admission Event Reaction uses.

    Deliberately no error translation. Trading has no broker to requeue into and no retry vocabulary of
    its own: a refused or overrun operation surfaces as the platform error it is, the runner's turn logs
    and ends, and the next turn re-reads the same durable state.
    """

    def __init__(self, database: WorkerDatabase) -> None:
        self._database = database
        self._lane = database.heavy_business()

    async def read[T](self, name: str, fn: Callable[[TradingRepositories], T], *, timeout_seconds: float) -> T:
        return await self._lane.run_business(
            name,
            _in_session(self._database, name, fn, timeout_seconds, _trading_repositories),
            operation_timeout_seconds=timeout_seconds,
        )

    async def tx[T](self, name: str, fn: Callable[[TradingRepositories], T], *, timeout_seconds: float) -> T:
        return await self._lane.run_business(
            name,
            _in_session(self._database, name, fn, timeout_seconds, _trading_repositories),
            operation_timeout_seconds=timeout_seconds,
        )


__all__ = [
    "WorkerChainTapeDatabase",
    "WorkerNewsColdDatabase",
    "WorkerNewsDatabase",
    "WorkerQuoteDatabase",
    "WorkerReactionDatabase",
    "WorkerTradingDatabase",
]
