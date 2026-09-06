"""Application-owned repository session composition for News, Price, and Trading."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any

from tracefold.news.market_review.instrument_storage import InstrumentsRepository
from tracefold.news.market_review.storage import PriceRepository
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


# A session that outlives a single operation needs both halves of the keepalive contract. The
# client half makes this process notice a dead peer; the server-side GUCs make PostgreSQL notice a
# dead client and reap the backend — which is what releases an account-slot advisory lock held by a
# container that was SIGKILLed. Ninety seconds of budget (30 + 3 x 10 + a little) is well inside the
# execution runtime's own restart cadence (#537 D2).
_LONG_LIVED_KEEPALIVE_IDLE_SECONDS = 30
_LONG_LIVED_KEEPALIVE_INTERVAL_SECONDS = 10
_LONG_LIVED_KEEPALIVE_COUNT = 3
_LONG_LIVED_SESSION_SETTINGS = {
    "tcp_keepalives_idle": str(_LONG_LIVED_KEEPALIVE_IDLE_SECONDS),
    "tcp_keepalives_interval": str(_LONG_LIVED_KEEPALIVE_INTERVAL_SECONDS),
    "tcp_keepalives_count": str(_LONG_LIVED_KEEPALIVE_COUNT),
}


@contextmanager
def postgres_connection(
    settings: Any,
    *,
    application_name: str = "tracefold_cli",
    long_lived: bool = False,
) -> Iterator[Any]:
    """Open the PostgreSQL connection used by application operations.

    `long_lived=True` is for a session that is meant to stay open for the life of a process rather
    than for one operation, and adds TCP keepalives on both ends.
    """
    postgres = settings.storage.postgres
    dsn = with_password_from_file(
        postgres.dsn,
        settings.postgres_password_file(),
    )
    keepalives: dict[str, Any] = (
        {
            "keepalives": True,
            "keepalives_idle": _LONG_LIVED_KEEPALIVE_IDLE_SECONDS,
            "keepalives_interval": _LONG_LIVED_KEEPALIVE_INTERVAL_SECONDS,
            "keepalives_count": _LONG_LIVED_KEEPALIVE_COUNT,
            "session_settings": _LONG_LIVED_SESSION_SETTINGS,
        }
        if long_lived
        else {}
    )
    conn = connect_postgres(
        dsn,
        connect_timeout_seconds=postgres.connect_timeout_seconds,
        application_name=application_name,
        **keepalives,
    )
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def repositories(
    settings: Any,
    *,
    application_name: str = "tracefold_cli",
    long_lived: bool = False,
) -> Iterator[RepositorySession]:
    """Open one repository session for a CLI/application operation."""
    with postgres_connection(settings, application_name=application_name, long_lived=long_lived) as conn:
        yield repositories_for_connection(conn)


__all__ = [
    "NewsSearchPlan",
    "RepositorySession",
    "postgres_connection",
    "repositories",
    "repositories_for_connection",
]
