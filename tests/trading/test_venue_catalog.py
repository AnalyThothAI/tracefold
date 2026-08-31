from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tracefold.app.workers.wiring import trading as trading_wiring
from tracefold.app.workers.wiring.execution_capabilities import ExecutionCapabilityCompileError
from tracefold.integrations.trading_catalog import VenueExpectedError
from tracefold.platform.observability import TelemetryRegistry
from tracefold.trading.catalog import (
    VenueCatalog,
    VenueInstrumentCatalogEntryV1,
    build_venue_catalog_snapshot,
)
from tracefold.trading.contracts import canonical_sha256


def _row(instrument_id: str, *, raw: str, error: str | None = None) -> VenueInstrumentCatalogEntryV1:
    return VenueInstrumentCatalogEntryV1(
        provider_instrument_id=instrument_id,
        provider_symbol=instrument_id,
        venue="binance.usdm",
        canonical_asset=None if error else "BTC",
        canonical_namespace=None if error else "native",
        product_kind="unknown" if error else "linear_perpetual",
        active=error is None,
        settlement_asset=None if error else "USDT",
        margin_asset=None if error else "USDT",
        raw_metadata_sha256=canonical_sha256({"raw": raw}),
        normalization_error=error,
    )


def _hyperliquid_row(instrument_id: str, *, raw: str) -> VenueInstrumentCatalogEntryV1:
    return VenueInstrumentCatalogEntryV1(
        provider_instrument_id=instrument_id,
        provider_symbol="BTC",
        venue="hyperliquid.perp",
        canonical_asset="BTC",
        canonical_namespace="main",
        product_kind="linear_perpetual",
        active=True,
        settlement_asset="USDC",
        margin_asset="USDC",
        raw_metadata_sha256=canonical_sha256({"raw": raw}),
    )


def test_catalog_digest_is_order_independent_and_preserves_every_provider_row() -> None:
    rows = (
        _row("BTCUSDT", raw="first"),
        _row("BTCUSDT", raw="duplicate"),
        _row("unknown:1", raw="malformed", error="provider_instrument_identity_missing"),
    )

    first = build_venue_catalog_snapshot(
        binding="BINANCE_USDM", captured_at_ms=1, stale_after_ms=21_600_000, instruments=rows
    )
    second = build_venue_catalog_snapshot(
        binding="BINANCE_USDM", captured_at_ms=1, stale_after_ms=21_600_000, instruments=tuple(reversed(rows))
    )

    assert first.snapshot_sha256 == second.snapshot_sha256
    assert first.provider_instrument_count == 3
    assert first.normalised_count == 2
    assert [row.raw_metadata_sha256 for row in first.instruments].count(rows[0].raw_metadata_sha256) == 1
    assert first.resolve("BTC") is not None


def test_catalog_loop_measures_each_provider_and_retains_one_venue_when_the_other_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots: list[dict[str, Any]] = []
    unavailable: list[tuple[str, str]] = []

    class Trading:
        def store_venue_catalog_snapshot(self, **values: Any) -> None:
            snapshots.append(values)

        def mark_venue_catalog_unavailable(self, *, binding: str, reason: str, now_ms: int) -> None:
            unavailable.append((binding, reason))

    class Database:
        async def tx(self, _name: str, fn: Any, *, timeout_seconds: float) -> Any:
            del timeout_seconds
            return fn(type("Repositories", (), {"trading": Trading()})())

    stop = asyncio.Event()

    async def binance() -> tuple[VenueInstrumentCatalogEntryV1, ...]:
        return (_row("BTCUSDT", raw="first"),)

    async def hyperliquid() -> tuple[VenueInstrumentCatalogEntryV1, ...]:
        stop.set()
        raise VenueExpectedError("venue_timeout", venue="hyperliquid.perp")

    monkeypatch.setattr(trading_wiring, "fetch_binance_usdm_catalog", binance)
    monkeypatch.setattr(trading_wiring, "fetch_hyperliquid_perp_catalog", hyperliquid)
    telemetry = TelemetryRegistry()
    catalog = VenueCatalog(
        db=Database(),  # type: ignore[arg-type]
        clock=lambda: 1_900_000_000_000,
        stale_after_ms=21_600_000,
        telemetry=telemetry,
    )

    asyncio.run(trading_wiring.run_venue_catalog(catalog, stop_event=stop, period_seconds=0.05))

    assert snapshots[0]["prepared"].binding == "BINANCE_USDM"
    assert unavailable == [("HYPERLIQUID_PERP", "venue_timeout")]
    metrics = telemetry.render_prometheus_text()
    assert (
        'tracefold_external_data_provider_call_total{name="trading_venue_catalog",outcome="success",'
        'source="binance"} 1.0' in metrics
    )
    assert (
        'tracefold_external_data_provider_call_total{name="trading_venue_catalog",outcome="error",'
        'source="hyperliquid"} 1.0' in metrics
    )
    assert 'tracefold_external_data_turn_total{name="trading_venue_catalog",outcome="partial"} 1.0' in metrics
    assert 'tracefold_external_data_source_count{name="trading_venue_catalog"} 2.0' in metrics
    assert 'tracefold_external_data_target_count{name="trading_venue_catalog"} 1.0' in metrics


def test_capability_compile_is_attempted_only_for_the_enabled_execution_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled: list[str] = []
    stop = asyncio.Event()

    class Trading:
        def store_venue_catalog_snapshot(self, **values: Any) -> None:
            del values

    class Database:
        async def tx(self, _name: str, fn: Any, *, timeout_seconds: float) -> Any:
            del timeout_seconds
            return fn(type("Repositories", (), {"trading": Trading()})())

    class Compiler:
        async def compile(self, snapshot: Any) -> None:
            compiled.append(snapshot.binding)
            if snapshot.binding == "BINANCE_USDM":
                stop.set()
                raise ExecutionCapabilityCompileError(
                    "execution_capability_compile_failed:BINANCE_USDM:provider_parse_failed"
                )

    async def binance() -> tuple[VenueInstrumentCatalogEntryV1, ...]:
        return (_row("BTCUSDT", raw="binance"),)

    async def hyperliquid() -> tuple[VenueInstrumentCatalogEntryV1, ...]:
        return (_hyperliquid_row("main:BTC", raw="hyperliquid"),)

    monkeypatch.setattr(trading_wiring, "fetch_binance_usdm_catalog", binance)
    monkeypatch.setattr(trading_wiring, "fetch_hyperliquid_perp_catalog", hyperliquid)
    telemetry = TelemetryRegistry()
    catalog = VenueCatalog(
        db=Database(),  # type: ignore[arg-type]
        clock=lambda: 1_900_000_000_000,
        stale_after_ms=21_600_000,
        telemetry=telemetry,
    )

    asyncio.run(
        trading_wiring.run_venue_catalog(
            catalog,
            capability_compiler=Compiler(),  # type: ignore[arg-type]
            stop_event=stop,
            period_seconds=0.05,
        )
    )

    assert compiled == ["BINANCE_USDM"]
    assert 'tracefold_external_data_turn_total{name="trading_venue_catalog",outcome="partial"} 1.0' in (
        telemetry.render_prometheus_text()
    )


def test_catalog_stop_interrupts_an_inflight_capability_compile(monkeypatch: pytest.MonkeyPatch) -> None:
    stop = asyncio.Event()
    compile_started = asyncio.Event()
    compile_cancelled = asyncio.Event()

    class Trading:
        def store_venue_catalog_snapshot(self, **values: Any) -> None:
            del values

    class Database:
        async def tx(self, _name: str, fn: Any, *, timeout_seconds: float) -> Any:
            del timeout_seconds
            return fn(type("Repositories", (), {"trading": Trading()})())

    class Compiler:
        async def compile(self, _snapshot: Any) -> None:
            compile_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                compile_cancelled.set()
                raise

    async def binance() -> tuple[VenueInstrumentCatalogEntryV1, ...]:
        return (_row("BTCUSDT", raw="binance"),)

    async def hyperliquid() -> tuple[VenueInstrumentCatalogEntryV1, ...]:
        raise AssertionError("stop_must_prevent_the_next_provider_call")

    monkeypatch.setattr(trading_wiring, "fetch_binance_usdm_catalog", binance)
    monkeypatch.setattr(trading_wiring, "fetch_hyperliquid_perp_catalog", hyperliquid)
    catalog = VenueCatalog(
        db=Database(),  # type: ignore[arg-type]
        clock=lambda: 1_900_000_000_000,
        stale_after_ms=21_600_000,
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            trading_wiring.run_venue_catalog(
                catalog,
                capability_compiler=Compiler(),  # type: ignore[arg-type]
                stop_event=stop,
                period_seconds=0.05,
            )
        )
        await compile_started.wait()
        stop.set()
        await asyncio.wait_for(task, timeout=0.25)

    asyncio.run(scenario())
    assert compile_cancelled.is_set()
