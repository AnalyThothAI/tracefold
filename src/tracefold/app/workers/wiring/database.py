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
from typing import Any

from tracefold.app.worker_database import WorkerDatabase
from tracefold.news.bus import DeferError, TransientError
from tracefold.platform.resource import ResourceAdmissionTimeout, ResourceOperationOverrun

_NEWS_DEFAULT_TIMEOUT_SECONDS = 3.0


def _read_in_session[T](database: WorkerDatabase, name: str, fn: Callable[[Any], T], timeout: float) -> Callable[[], T]:
    def _run() -> T:
        with database.worker_session(name, timeout) as repos:
            return fn(repos)

    return _run


def _write_in_session[T](
    database: WorkerDatabase, name: str, fn: Callable[[Any], T], timeout: float
) -> Callable[[], T]:
    def _run() -> T:
        with database.worker_session(name, timeout) as repos, repos.transaction():
            return fn(repos)

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
        self, name: str, fn: Callable[[Any], T], *, timeout_seconds: float = _NEWS_DEFAULT_TIMEOUT_SECONDS
    ) -> T:
        return await self._run(name, _read_in_session(self._database, name, fn, timeout_seconds), timeout_seconds)

    async def tx[T](
        self, name: str, fn: Callable[[Any], T], *, timeout_seconds: float = _NEWS_DEFAULT_TIMEOUT_SECONDS
    ) -> T:
        return await self._run(name, _write_in_session(self._database, name, fn, timeout_seconds), timeout_seconds)

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
        self, name: str, fn: Callable[[Any], T], *, timeout_seconds: float = _NEWS_DEFAULT_TIMEOUT_SECONDS
    ) -> T:
        return await self._run(name, _read_in_session(self._database, name, fn, timeout_seconds), timeout_seconds)

    async def tx[T](
        self, name: str, fn: Callable[[Any], T], *, timeout_seconds: float = _NEWS_DEFAULT_TIMEOUT_SECONDS
    ) -> T:
        return await self._run(name, _write_in_session(self._database, name, fn, timeout_seconds), timeout_seconds)

    async def _run[T](self, name: str, run: Callable[[], T], timeout_seconds: float) -> T:
        try:
            return await self._lane.run_business(name, run, operation_timeout_seconds=timeout_seconds)
        except ResourceAdmissionTimeout as exc:
            raise DeferError(f"db_admission_timeout:{name}") from exc
        except ResourceOperationOverrun as exc:
            raise TransientError(f"db_overrun:{name}") from exc


class WorkerMarketReviewDatabase:
    """`MarketReviewDatabasePort` (#88): the price plane's own one-slot cold admission.

    Its error codes are distinct from the News lane's on purpose — a `cold_db_*` failure in a log names
    the plane that was refused, and the two loops that read them are not broker consumers.
    """

    def __init__(self, database: WorkerDatabase) -> None:
        self._database = database
        self._lane = database.heavy_business()

    async def read[T](self, name: str, fn: Callable[[Any], T], *, timeout_seconds: float) -> T:
        return await self._run(name, _read_in_session(self._database, name, fn, timeout_seconds), timeout_seconds)

    async def tx[T](self, name: str, fn: Callable[[Any], T], *, timeout_seconds: float) -> T:
        return await self._run(name, _write_in_session(self._database, name, fn, timeout_seconds), timeout_seconds)

    async def _run[T](self, name: str, run: Callable[[], T], timeout_seconds: float) -> T:
        try:
            return await self._lane.run_business(name, run, operation_timeout_seconds=timeout_seconds)
        except ResourceAdmissionTimeout as exc:
            raise DeferError(f"cold_db_admission_timeout:{name}") from exc
        except ResourceOperationOverrun as exc:
            raise TransientError(f"cold_db_overrun:{name}") from exc


class WorkerTradingDatabase:
    """`TradingDatabasePort` (#104): the same one-slot cold admission the price plane uses.

    Deliberately no error translation. Trading has no broker to requeue into and no retry vocabulary of
    its own: a refused or overrun operation surfaces as the platform error it is, the runner's turn logs
    and ends, and the next turn re-reads the same durable state.
    """

    def __init__(self, database: WorkerDatabase) -> None:
        self._database = database
        self._lane = database.heavy_business()

    async def read[T](self, name: str, fn: Callable[[Any], T], *, timeout_seconds: float) -> T:
        return await self._lane.run_business(
            name,
            _read_in_session(self._database, name, fn, timeout_seconds),
            operation_timeout_seconds=timeout_seconds,
        )

    async def tx[T](self, name: str, fn: Callable[[Any], T], *, timeout_seconds: float) -> T:
        return await self._lane.run_business(
            name,
            _write_in_session(self._database, name, fn, timeout_seconds),
            operation_timeout_seconds=timeout_seconds,
        )


__all__ = [
    "WorkerMarketReviewDatabase",
    "WorkerNewsColdDatabase",
    "WorkerNewsDatabase",
    "WorkerTradingDatabase",
]
