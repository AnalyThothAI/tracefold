"""Application-owned repository session composition for News, Price, and Trading."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any, Literal

from tracefold.news.market_review.storage import InstrumentsRepository, PriceRepository
from tracefold.news.search import NewsSearchPlan, compile_news_search
from tracefold.news.storage.root import NewsRepository
from tracefold.platform.postgres.client import (
    connect_postgres,
    require_transaction,
    transaction,
    with_password_from_file,
)
from tracefold.trading.storage.root import TradingRepository


@dataclass(frozen=True, slots=True)
class RepositorySession:
    conn: Any
    news: NewsRepository
    instruments: InstrumentsRepository
    price: PriceRepository
    trading: TradingRepository

    def transaction(self) -> AbstractContextManager[None]:
        return transaction(self.conn)

    def require_transaction(self, *, operation: str) -> None:
        require_transaction(self.conn, operation=operation)

    def compile_news_search(self, *, q: str | None, symbol: str | None) -> NewsSearchPlan | None:
        """Wire the News search interface to the session's existing instrument adapter."""

        return compile_news_search(q=q, symbol=symbol, instruments=self.instruments)


def repositories_for_connection(conn: Any) -> RepositorySession:
    return RepositorySession(
        conn=conn,
        news=NewsRepository(conn),
        instruments=InstrumentsRepository(conn),
        price=PriceRepository(conn),
        trading=TradingRepository(conn),
    )


@contextmanager
def postgres_connection(
    settings: Any,
    *,
    role: Literal["serve", "workers", "migrate", "nautilus"],
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
    role: Literal["serve", "workers", "nautilus"] = "workers",
) -> Iterator[RepositorySession]:
    """Open one short-lived repository session for a CLI/application operation."""
    with postgres_connection(settings, role=role) as conn:
        yield repositories_for_connection(conn)


__all__ = [
    "NewsSearchPlan",
    "RepositorySession",
    "postgres_connection",
    "repositories",
    "repositories_for_connection",
]
