"""Application-owned repository session composition for News, Price, and Trading."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any, Literal

from tracefold.news.market_review.storage import InstrumentsRepository, PriceRepository
from tracefold.news.storage.root import NewsRepository
from tracefold.platform.postgres.postgres_client import (
    connect_postgres,
    require_transaction,
    transaction,
    with_password_from_file,
)
from tracefold.trading.repository import TradingRepository


@dataclass(frozen=True, slots=True)
class RepositorySession:
    conn: Any
    news: NewsRepository
    instruments: InstrumentsRepository
    price: PriceRepository
    trading: TradingRepository
    transaction_observer: Callable[[float], None] | None = None

    def transaction(self) -> AbstractContextManager[None]:
        return self._observed_transaction()

    @contextmanager
    def _observed_transaction(self) -> Iterator[None]:
        started = time.perf_counter()
        try:
            with transaction(self.conn):
                yield
        finally:
            if self.transaction_observer is not None:
                self.transaction_observer(max(0.0, time.perf_counter() - started))

    def require_transaction(self, *, operation: str) -> None:
        require_transaction(self.conn, operation=operation)


def repositories_for_connection(
    conn: Any,
    *,
    transaction_observer: Callable[[float], None] | None = None,
) -> RepositorySession:
    return RepositorySession(
        conn=conn,
        news=NewsRepository(conn),
        instruments=InstrumentsRepository(conn),
        price=PriceRepository(conn),
        trading=TradingRepository(conn),
        transaction_observer=transaction_observer,
    )


@contextmanager
def postgres_connection(
    settings: Any,
    *,
    role: Literal["serve", "workers", "migrate"],
) -> Iterator[Any]:
    """Open the short-lived PostgreSQL connection used by application operations."""
    postgres = settings.storage.postgres
    dsn = with_password_from_file(
        settings.postgres_dsn(role),
        settings.postgres_password_file(role),
    )
    conn = connect_postgres(dsn, connect_timeout_seconds=postgres.connect_timeout_seconds)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def repositories(
    settings: Any,
    *,
    role: Literal["serve", "workers"] = "workers",
) -> Iterator[RepositorySession]:
    """Open one short-lived repository session for a CLI/application operation."""
    with postgres_connection(settings, role=role) as conn:
        yield repositories_for_connection(conn)
