from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.trading_v3_fixtures import binance_catalog
from tracefold.app.workers.wiring.execution_capabilities import (
    ExecutionCapabilityCompileError,
    ExecutionCapabilityCompiler,
)
from tracefold.trading import ExecutionInstrumentEvidenceV1


class _Trading:
    def __init__(self) -> None:
        self.published: list[Any] = []
        self.errors: list[dict[str, Any]] = []

    def append_and_activate_execution_capability_snapshot(self, snapshot: Any, *, created_at_ms: int) -> bool:
        self.published.append((snapshot, created_at_ms))
        return True

    def mark_execution_capability_compile_error(self, **values: Any) -> None:
        self.errors.append(values)


class _Database:
    def __init__(self, trading: _Trading) -> None:
        self.trading = trading
        self.operations: list[str] = []

    async def tx(self, name: str, fn: Any, *, timeout_seconds: float) -> Any:
        assert timeout_seconds == 10.0
        self.operations.append(name)
        return fn(type("Repositories", (), {"trading": self.trading})())


def _evidence(catalog: Any) -> ExecutionInstrumentEvidenceV1:
    row = catalog.instruments[0]
    return ExecutionInstrumentEvidenceV1(
        provider_instrument_id=row.provider_instrument_id,
        catalog_raw_metadata_sha256=row.raw_metadata_sha256,
        instrument_id=f"{row.provider_symbol}-PERP.BINANCE",
        native_symbol=row.provider_symbol,
        price_precision=1,
        size_precision=3,
        price_increment="0.1",
        size_increment="0.001",
        min_quantity="0.001",
        min_notional="5",
        execution_eligible=True,
        protection_eligible=True,
    )


def test_compiler_publishes_one_complete_v2_partition(monkeypatch: pytest.MonkeyPatch) -> None:
    catalog = binance_catalog(captured_at_ms=1_900_000_000_000, symbols=("BTCUSDT",))
    trading = _Trading()
    database = _Database(trading)

    async def load(_: Any) -> tuple[list[ExecutionInstrumentEvidenceV1], str]:
        return [_evidence(catalog)], "a" * 64

    monkeypatch.setattr(ExecutionCapabilityCompiler, "_load", staticmethod(load))
    snapshot = asyncio.run(ExecutionCapabilityCompiler(database).compile(catalog))  # type: ignore[arg-type]

    assert (snapshot.snapshot_version, snapshot.binding) == (
        "execution_capability_snapshot_v2",
        "BINANCE_USDM",
    )
    assert snapshot.catalog_instrument_count == snapshot.included_count + snapshot.excluded_count == 1
    assert database.operations == ["trading_execution_capability_publish"]
    assert trading.published[0][0] == snapshot
    assert trading.errors == []


def test_compiler_persists_a_secret_free_error_code_before_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = binance_catalog(captured_at_ms=1_900_000_000_000, symbols=("BTCUSDT",))
    trading = _Trading()
    database = _Database(trading)

    async def fail(_: Any) -> tuple[list[ExecutionInstrumentEvidenceV1], str]:
        raise RuntimeError("api_key=must-not-be-persisted")

    monkeypatch.setattr(ExecutionCapabilityCompiler, "_load", staticmethod(fail))
    with pytest.raises(ExecutionCapabilityCompileError, match="execution_capability_runtimeerror_failed"):
        asyncio.run(ExecutionCapabilityCompiler(database).compile(catalog))  # type: ignore[arg-type]

    assert database.operations == ["trading_execution_capability_error"]
    assert trading.errors[0]["binding"] == "BINANCE_USDM"
    assert trading.errors[0]["reason"] == "execution_capability_runtimeerror_failed"
    assert "must-not-be-persisted" not in str(trading.errors)
