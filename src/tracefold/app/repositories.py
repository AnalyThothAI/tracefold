from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any, Literal

from tracefold.macro import GeneralMarketRepository, MacroRepository
from tracefold.news.repository import NewsRepository
from tracefold.platform.postgres.postgres_client import (
    connect_postgres,
    require_transaction,
    transaction,
    with_password_from_file,
)
from tracefold.platform.postgres.projection_frontier import ProjectionFrontierRepository


@dataclass(frozen=True, slots=True)
class RepositorySession:
    conn: Any
    news: NewsRepository
    macro: MacroRepository
    macro_market: GeneralMarketRepository
    projection_frontiers: ProjectionFrontierRepository
    transaction_observer: Callable[[float], None] | None = None
    projection_transitions: list[tuple[str, str]] | None = None

    def transaction(self) -> AbstractContextManager[None]:
        return self._observed_transaction()

    @contextmanager
    def _observed_transaction(self) -> Iterator[None]:
        started = time.perf_counter()
        transition_marker = len(self.projection_transitions or ())
        try:
            with transaction(self.conn):
                yield
        except BaseException:
            if self.projection_transitions is not None:
                del self.projection_transitions[transition_marker:]
            raise
        finally:
            if self.transaction_observer is not None:
                self.transaction_observer(max(0.0, time.perf_counter() - started))

    def require_transaction(self, *, operation: str) -> None:
        require_transaction(self.conn, operation=operation)


def repositories_for_connection(
    conn: Any,
    *,
    transaction_observer: Callable[[float], None] | None = None,
    projection_transitions: list[tuple[str, str]] | None = None,
) -> RepositorySession:
    projection_transition_observer = projection_transitions.append if projection_transitions is not None else None
    return RepositorySession(
        conn=conn,
        news=NewsRepository(conn),
        macro=MacroRepository(conn),
        macro_market=GeneralMarketRepository(conn),
        projection_frontiers=ProjectionFrontierRepository(
            conn,
            transition_observer=projection_transition_observer,
        ),
        transaction_observer=transaction_observer,
        projection_transitions=projection_transitions,
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
