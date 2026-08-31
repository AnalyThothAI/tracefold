from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import BoundedSemaphore
from typing import Any

from psycopg_pool import PoolClosed, PoolTimeout

from tracefold.app.repository_session import NewsSearchPlan, RepositorySession, repositories_for_connection
from tracefold.app.workers.runtime import WorkersRuntimeRepository
from tracefold.news.market_review.storage import InstrumentsRepository, PriceRepository
from tracefold.news.storage.root import NewsRepository
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.postgres.client import create_pool, postgres_health_check, with_password_from_file
from tracefold.trading.storage.root import TradingRepository

_SERVE_POOL_SIZE = 7  # 6 ordinary read permits + 1 control permit
_SERVE_CHECKOUT_TIMEOUT_SECONDS = 0.250
_SERVE_STATEMENT_TIMEOUT_SECONDS = 1.0
_SERVE_SESSION_CONFIG = {
    "jit": "off",
    "max_parallel_workers_per_gather": "0",
    "work_mem": "8MB",
}
_SERVE_LANE_CAPACITIES = {
    "ordinary": 6,
    "control": 1,
}
_SERVE_PERMIT_TIMEOUT_SECONDS = 0.050


class ServeDatabaseBusy(RuntimeError):
    pass


class ServeRepositories:
    """Read-only Serve capabilities with infrastructure probes instead of a public raw connection."""

    __slots__ = ("_conn", "_session", "instruments", "news", "price", "trading")

    def __init__(self, session: RepositorySession) -> None:
        self._conn = session.conn
        self._session = session
        self.news: NewsRepository = session.news
        self.instruments: InstrumentsRepository = session.instruments
        self.price: PriceRepository = session.price
        self.trading: TradingRepository = session.trading

    def database_health(self, *, expected_migration_version: str) -> dict[str, Any]:
        return postgres_health_check(self._conn, expected_migration_version=expected_migration_version)

    def workers_runtime_row(self) -> dict[str, Any] | None:
        return WorkersRuntimeRepository(self._conn).read()

    def compile_news_search(self, *, q: str | None, symbol: str | None) -> NewsSearchPlan | None:
        return self._session.compile_news_search(q=q, symbol=symbol)

    def session_policy(self) -> dict[str, str]:
        row = self._conn.execute(
            "SELECT current_setting('jit') AS jit, "
            "current_setting('max_parallel_workers_per_gather') AS max_parallel_workers_per_gather, "
            "current_setting('work_mem') AS work_mem"
        ).fetchone()
        return {str(key): str(value) for key, value in dict(row or {}).items()}


@dataclass(slots=True)
class ServeDatabase:
    """The public serving database boundary.

    Connections default to read-only and nothing on this pool ever opens a
    read-write transaction: the two ReviewDesk mutations that used to were
    removed with the console page they served (#256).  `tracefold news review
    submit` is the one remaining writer of the append-only review fact tables
    and opens its own connection under the same role.
    """

    api_pool: Any
    telemetry: TelemetryRegistry | None = field(default_factory=TelemetryRegistry)
    admission: dict[str, BoundedSemaphore] = field(
        default_factory=lambda: {lane: BoundedSemaphore(capacity) for lane, capacity in _SERVE_LANE_CAPACITIES.items()}
    )

    @classmethod
    def create(cls, settings: Any, *, telemetry: TelemetryRegistry | None = None) -> ServeDatabase:
        postgres = settings.storage.postgres
        dsn = with_password_from_file(
            postgres.dsn,
            settings.postgres_password_file(),
        )
        pool = create_pool(
            dsn,
            min_size=_SERVE_POOL_SIZE,
            max_size=_SERVE_POOL_SIZE,
            connect_timeout_seconds=postgres.connect_timeout_seconds,
            application_name="tracefold_serve",
            statement_timeout_seconds=_SERVE_STATEMENT_TIMEOUT_SECONDS,
            lock_timeout_seconds=0.250,
            read_only=True,
            idle_in_transaction_session_timeout_seconds=5.0,
            session_settings=_SERVE_SESSION_CONFIG,
        )
        pool.wait(timeout=float(postgres.connect_timeout_seconds))
        return cls(
            api_pool=pool,
            telemetry=telemetry if telemetry is not None else TelemetryRegistry(),
        )

    @contextmanager
    def api_session(self, lane: str = "ordinary") -> Iterator[ServeRepositories]:
        try:
            gate = self.admission[lane]
        except KeyError as exc:
            raise ValueError(f"serve_database_lane_invalid:{lane}") from exc
        started = time.perf_counter()
        if not gate.acquire(timeout=_SERVE_PERMIT_TIMEOUT_SECONDS):
            if self.telemetry is not None:
                self.telemetry.record_pool_wait(
                    f"serve_{lane}_permit",
                    (time.perf_counter() - started) * 1000,
                )
            raise ServeDatabaseBusy(f"serve_database_busy:{lane}")
        try:
            permit_acquired_at = time.perf_counter()
            if self.telemetry is not None:
                self.telemetry.record_pool_wait(
                    f"serve_{lane}_permit",
                    (permit_acquired_at - started) * 1000,
                )
            try:
                with self.api_pool.connection(timeout=_SERVE_CHECKOUT_TIMEOUT_SECONDS) as conn:
                    if self.telemetry is not None:
                        self.telemetry.record_pool_wait(
                            "serve",
                            (time.perf_counter() - permit_acquired_at) * 1000,
                        )
                    yield ServeRepositories(repositories_for_connection(conn))
            except (PoolClosed, PoolTimeout) as exc:
                raise ServeDatabaseBusy(f"serve_database_pool_busy:{lane}") from exc
        finally:
            gate.release()

    async def aclose(self) -> None:
        await _close_pool(self.api_pool)


async def _close_pool(pool: Any) -> None:
    result = pool.close()
    if result is not None:
        raise RuntimeError("db_pool_close_must_be_sync")
