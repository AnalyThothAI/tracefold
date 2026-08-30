"""Real PostgreSQL guards for CPU work at the Trading transaction boundary (#392)."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any

import pytest

from tests.postgres_test_utils import (
    connect_postgres_test,
)
from tests.postgres_test_utils import (
    test_postgres_dsn as _test_postgres_dsn,
)
from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers.wiring.database import WorkerTradingDatabase
from tracefold.platform.postgres.client import create_pool
from tracefold.trading.capital_lane import CapitalLane, CapitalLaneConfig
from tracefold.trading.catalog import (
    PreparedVenueCatalogSnapshot,
    VenueCatalog,
    VenueInstrumentCatalogEntryV1,
    VenueInstrumentCatalogSnapshotV1,
    build_venue_catalog_snapshot,
    prepare_venue_catalog_snapshot,
)
from tracefold.trading.contracts import VenueBinding, canonical_sha256

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]

NOW = 1_900_000_000_000


def _catalog(binding: VenueBinding) -> VenueInstrumentCatalogSnapshotV1:
    binance = binding == "BINANCE_USDM"
    symbol = "BTCUSDT" if binance else "BTC"
    return build_venue_catalog_snapshot(
        binding=binding,
        captured_at_ms=NOW,
        stale_after_ms=60_000,
        instruments=(
            VenueInstrumentCatalogEntryV1(
                provider_instrument_id=symbol,
                provider_symbol=symbol,
                venue="binance.usdm" if binance else "hyperliquid.perp",
                canonical_asset="BTC",
                canonical_namespace="native" if binance else "main",
                product_kind="linear_perpetual",
                active=True,
                settlement_asset="USDT" if binance else "USDC",
                margin_asset="USDT" if binance else "USDC",
                raw_metadata_sha256=canonical_sha256({"binding": binding, "symbol": symbol}),
            ),
        ),
    )


def _pool() -> Any:
    pool = create_pool(
        _test_postgres_dsn(),
        min_size=1,
        max_size=4,
        max_waiting=3,
        connect_timeout_seconds=5.0,
        application_name="tracefold_trading_transaction_cpu_boundary_test",
        statement_timeout_seconds=3.0,
        lock_timeout_seconds=0.25,
        idle_in_transaction_session_timeout_seconds=0.1,
    )
    pool.wait(timeout=5.0)
    return pool


def _forge_prepared(
    prepared: PreparedVenueCatalogSnapshot,
    **changes: Any,
) -> PreparedVenueCatalogSnapshot:
    """Bypass the validated constructor only to probe the repository's conflict defense."""

    forged = object.__new__(PreparedVenueCatalogSnapshot)
    for name in (
        "snapshot_sha256",
        "binding",
        "captured_at_ms",
        "stale_after_ms",
        "provider_instrument_count",
        "payload_json",
    ):
        object.__setattr__(forged, name, changes.get(name, getattr(prepared, name)))
    return forged


def _seed_active_catalogs() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        for snapshot in (_catalog("BINANCE_USDM"), _catalog("HYPERLIQUID_PERP")):
            payload = snapshot.model_dump(mode="json")
            digest = canonical_sha256(payload)
            conn.execute(
                """
                INSERT INTO trading_venue_catalog_snapshots (
                  snapshot_sha256, binding, captured_at_ms, stale_after_ms,
                  provider_instrument_count, payload, created_at_ms
                ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (snapshot_sha256) DO NOTHING
                """,
                (
                    digest,
                    snapshot.binding,
                    snapshot.captured_at_ms,
                    snapshot.stale_after_ms,
                    snapshot.provider_instrument_count,
                    json.dumps(payload),
                    NOW,
                ),
            )
            conn.execute(
                """
                UPDATE trading_binding_runtime
                   SET catalog_state = 'ready', catalog_snapshot_sha256 = %s,
                       catalog_captured_at_ms = %s, updated_at_ms = %s
                 WHERE binding = %s
                """,
                (digest, NOW, NOW, snapshot.binding),
            )
        conn.commit()
    finally:
        conn.close()


class _CountingConnection:
    def __init__(self, delegate: Any, counted: Callable[[], None]) -> None:
        self._delegate = delegate
        self._counted = counted

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        self._counted()
        return self._delegate.execute(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class _TightAuthorityIdleTimeout:
    def __init__(self, delegate: WorkerTradingDatabase) -> None:
        self._delegate = delegate
        self.authority_statement_count = 0
        self.inside_authority_callback = False

    async def read(
        self,
        name: str,
        fn: Callable[[Any], Any],
        *,
        timeout_seconds: float,
    ) -> Any:
        if name != "trading_capital_authority":
            return await self._delegate.read(name, fn, timeout_seconds=timeout_seconds)

        def with_tight_idle_timeout(repos: Any) -> Any:
            original_conn = repos.trading.conn
            repos.trading.conn = _CountingConnection(original_conn, self._count_authority_statement)
            self.inside_authority_callback = True
            try:
                return fn(repos)
            finally:
                self.inside_authority_callback = False
                repos.trading.conn = original_conn

        return await self._delegate.read(name, with_tight_idle_timeout, timeout_seconds=timeout_seconds)

    def _count_authority_statement(self) -> None:
        self.authority_statement_count += 1

    async def tx(
        self,
        name: str,
        fn: Callable[[Any], Any],
        *,
        timeout_seconds: float,
    ) -> Any:
        return await self._delegate.tx(name, fn, timeout_seconds=timeout_seconds)


class _TightWriteIdleTimeout:
    def __init__(self, delegate: WorkerTradingDatabase) -> None:
        self._delegate = delegate
        self.repository_statement_counts: list[int] = []

    async def tx(
        self,
        name: str,
        fn: Callable[[Any], Any],
        *,
        timeout_seconds: float,
    ) -> Any:
        def with_tight_idle_timeout(repos: Any) -> Any:
            repos.trading.conn.execute("SET LOCAL idle_in_transaction_session_timeout = '100ms'")
            statement_count = 0

            def count_statement() -> None:
                nonlocal statement_count
                statement_count += 1

            original_conn = repos.trading.conn
            repos.trading.conn = _CountingConnection(original_conn, count_statement)
            try:
                return fn(repos)
            finally:
                repos.trading.conn = original_conn
                self.repository_statement_counts.append(statement_count)

        return await self._delegate.tx(name, with_tight_idle_timeout, timeout_seconds=timeout_seconds)


def _delay_catalog_validation(
    monkeypatch: pytest.MonkeyPatch,
    *,
    boundary: _TightAuthorityIdleTimeout,
    observed_inside_callback: list[bool],
) -> None:
    original_validate = VenueInstrumentCatalogSnapshotV1.model_validate
    original_validate_json = VenueInstrumentCatalogSnapshotV1.model_validate_json

    def delayed_validate(cls: type[Any], value: Any, *args: Any, **kwargs: Any) -> Any:
        del cls
        observed_inside_callback.append(boundary.inside_authority_callback)
        time.sleep(0.15)
        return original_validate(value, *args, **kwargs)

    def delayed_validate_json(cls: type[Any], value: Any, *args: Any, **kwargs: Any) -> Any:
        del cls
        observed_inside_callback.append(boundary.inside_authority_callback)
        time.sleep(0.15)
        return original_validate_json(value, *args, **kwargs)

    monkeypatch.setattr(VenueInstrumentCatalogSnapshotV1, "model_validate", classmethod(delayed_validate))
    monkeypatch.setattr(VenueInstrumentCatalogSnapshotV1, "model_validate_json", classmethod(delayed_validate_json))


def test_capital_authority_materialization_runs_after_the_read_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow domain validator must not leave PostgreSQL idle inside the authority snapshot."""

    _seed_active_catalogs()
    pool = _pool()
    database = WorkerDatabase(worker_pool=pool, telemetry=None)
    boundary = _TightAuthorityIdleTimeout(WorkerTradingDatabase(database))
    observed_inside_callback: list[bool] = []
    _delay_catalog_validation(
        monkeypatch,
        boundary=boundary,
        observed_inside_callback=observed_inside_callback,
    )

    async def no_bars(*_args: Any, **_kwargs: Any) -> Sequence[Any]:
        raise AssertionError("an empty source projection must not call the provider")

    async def no_sources(_metric: str, _after: int, _until: int) -> Sequence[Any]:
        return ()

    lane = CapitalLane(
        db=boundary,  # type: ignore[arg-type]
        config=CapitalLaneConfig(),
        bars=no_bars,
        oi_projection=no_sources,
        news_generation="test-generation",
        release_revision="test-revision",
        clock=lambda: NOW,
    )
    try:
        turn = asyncio.run(lane.advance())
        assert (turn.outcome, turn.sources) == ("ADVANCED", 0)
        assert boundary.authority_statement_count == 1
        assert observed_inside_callback == [False, False]
    finally:
        database.close_executors()
        pool.close()


def test_catalog_publish_serializes_identity_before_the_write_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slow serialization must not leave PostgreSQL idle inside the catalog write transaction."""

    pool = _pool()
    database = WorkerDatabase(worker_pool=pool, telemetry=None)
    original_model_dump = VenueInstrumentCatalogSnapshotV1.model_dump

    def delayed_model_dump(self: VenueInstrumentCatalogSnapshotV1, *args: Any, **kwargs: Any) -> Any:
        time.sleep(0.15)
        return original_model_dump(self, *args, **kwargs)

    source = _catalog("BINANCE_USDM")
    trading = WorkerTradingDatabase(database)
    tight_writes = _TightWriteIdleTimeout(trading)
    monkeypatch.setattr(VenueInstrumentCatalogSnapshotV1, "model_dump", delayed_model_dump)
    catalog = VenueCatalog(
        db=tight_writes,  # type: ignore[arg-type]
        clock=lambda: NOW + 1,
        stale_after_ms=source.stale_after_ms,
    )

    async def publish_idempotently_and_reject_a_conflict() -> VenueInstrumentCatalogSnapshotV1:
        stored = await catalog.publish(binding="BINANCE_USDM", instruments=source.instruments)
        assert await catalog.publish(binding="BINANCE_USDM", instruments=source.instruments) == stored
        prepared_stored = prepare_venue_catalog_snapshot(stored)
        with pytest.raises(RuntimeError, match="venue_catalog_snapshot_identity_conflict"):
            await tight_writes.tx(
                "trading_venue_catalog_conflict_probe",
                lambda repos: repos.trading.store_venue_catalog_snapshot(
                    prepared=_forge_prepared(prepared_stored, payload_json="{}"),
                    now_ms=NOW + 2,
                ),
                timeout_seconds=3.0,
            )
        return stored

    try:
        stored = asyncio.run(publish_idempotently_and_reject_a_conflict())
        assert tight_writes.repository_statement_counts == [1, 1, 1]
        conn = connect_postgres_test(read_only=False)
        try:
            row = conn.execute(
                "SELECT catalog_snapshot_sha256 FROM trading_binding_runtime WHERE binding = 'BINANCE_USDM'"
            ).fetchone()
            assert row is not None and row["catalog_snapshot_sha256"] == stored.snapshot_sha256
        finally:
            conn.close()
    finally:
        database.close_executors()
        pool.close()


def test_20k_catalog_write_keeps_materialization_outside_the_transaction() -> None:
    """Production-size catalog validation/serialization is complete before the one SQL write."""

    instruments = tuple(
        VenueInstrumentCatalogEntryV1(
            provider_instrument_id=f"ASSET{index}USDT",
            provider_symbol=f"ASSET{index}USDT",
            venue="binance.usdm",
            canonical_asset=f"ASSET{index}",
            canonical_namespace="native",
            product_kind="linear_perpetual",
            active=True,
            settlement_asset="USDT",
            margin_asset="USDT",
            raw_metadata_sha256=canonical_sha256({"index": index}),
        )
        for index in range(20_000)
    )
    prepared = prepare_venue_catalog_snapshot(
        build_venue_catalog_snapshot(
            binding="BINANCE_USDM",
            captured_at_ms=NOW + 20_000,
            stale_after_ms=60_000,
            instruments=instruments,
        )
    )
    pool = _pool()
    database = WorkerDatabase(worker_pool=pool, telemetry=None)
    tight_writes = _TightWriteIdleTimeout(WorkerTradingDatabase(database))

    async def store() -> None:
        await tight_writes.tx(
            "trading_venue_catalog_20k",
            lambda repos: repos.trading.store_venue_catalog_snapshot(prepared=prepared, now_ms=NOW + 20_001),
            timeout_seconds=30.0,
        )

    try:
        asyncio.run(store())
        assert tight_writes.repository_statement_counts == [1]
    finally:
        database.close_executors()
        pool.close()


def test_prepared_catalog_rejects_an_unbound_first_write_identity() -> None:
    """A forged digest or metadata tuple must fail before a transaction can open."""

    prepared = prepare_venue_catalog_snapshot(_catalog("BINANCE_USDM"))
    for changes in (
        {"snapshot_sha256": "0" * 64},
        {"binding": "HYPERLIQUID_PERP"},
        {"captured_at_ms": prepared.captured_at_ms + 1},
        {"stale_after_ms": prepared.stale_after_ms + 1},
        {"provider_instrument_count": prepared.provider_instrument_count + 1},
        {"payload_json": "{}"},
    ):
        with pytest.raises(ValueError, match="venue_catalog_prepared_identity_invalid"):
            replace(prepared, **changes)


def test_catalog_publish_concurrent_identical_first_write_is_idempotent() -> None:
    """A retry blocked behind the first insert must observe that committed identical row."""

    source = _catalog("BINANCE_USDM").model_copy(update={"captured_at_ms": NOW + 10_000})
    prepared = prepare_venue_catalog_snapshot(source)
    blocker = connect_postgres_test(read_only=False)
    blocker.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (prepared.snapshot_sha256,),
    )
    blocker.execute(
        """
        INSERT INTO trading_venue_catalog_snapshots (
          snapshot_sha256, binding, captured_at_ms, stale_after_ms,
          provider_instrument_count, payload, created_at_ms
        ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
        """,
        (
            prepared.snapshot_sha256,
            prepared.binding,
            prepared.captured_at_ms,
            prepared.stale_after_ms,
            prepared.provider_instrument_count,
            prepared.payload_json,
            NOW,
        ),
    )
    pool = _pool()
    database = WorkerDatabase(worker_pool=pool, telemetry=None)
    trading = WorkerTradingDatabase(database)
    retry_started = threading.Event()

    def store_with_start_signal(repos: Any) -> None:
        original_conn = repos.trading.conn
        repos.trading.conn = _CountingConnection(original_conn, retry_started.set)
        try:
            repos.trading.store_venue_catalog_snapshot(
                prepared=prepared,
                now_ms=NOW + 1,
            )
        finally:
            repos.trading.conn = original_conn

    async def retry_while_the_first_insert_is_uncommitted() -> None:
        retry = asyncio.create_task(
            trading.tx(
                "trading_venue_catalog_concurrent_retry",
                store_with_start_signal,
                timeout_seconds=3.0,
            )
        )
        try:
            for _ in range(100):
                if retry_started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert retry_started.is_set(), "the identical retry never reached the repository write"
        finally:
            blocker.commit()
        await asyncio.wait_for(retry, timeout=3.0)

    try:
        asyncio.run(retry_while_the_first_insert_is_uncommitted())
        row = blocker.execute(
            "SELECT catalog_snapshot_sha256 FROM trading_binding_runtime WHERE binding = 'BINANCE_USDM'"
        ).fetchone()
        assert row is not None and row["catalog_snapshot_sha256"] == prepared.snapshot_sha256
    finally:
        blocker.rollback()
        blocker.close()
        database.close_executors()
        pool.close()
