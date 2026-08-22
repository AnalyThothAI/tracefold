"""Provider-neutral liquidation snapshots and the bounded CoinGlass shadow loop (#144)."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager, nullcontext
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tracefold.integrations.coinglass import COINGLASS_CLI_COMMIT, snapshot_from_envelope
from tracefold.news.liquidation import (
    LIQUIDATION_LEVEL_MAX,
    LIQUIDATION_MODEL_VERSION,
    LIQUIDATION_RANGE,
    LIQUIDATION_TARGETS,
    ProviderLiquidationSnapshot,
)
from tracefold.news.liquidation_loops import LiquidationSnapshotLoop

NOW = 1_800_000_000_000
ROOT = Path(__file__).resolve().parents[2]


def _envelope(*, levels: list[dict[str, Any]], freshness: str = "fresh") -> dict[str, Any]:
    return {
        "ok": True,
        "data": {
            "symbol": "Binance_DOGEUSDT",
            "range": "3d",
            "source": {"provider": "coinglass", "endpoint": "liquidation_levels_v2"},
            "levels": levels,
            "prices": [
                {
                    "timestamp": 1_799_999_990,
                    "open": 0.2,
                    "high": 0.21,
                    "low": 0.19,
                    "close": 0.205,
                    "volume": 1,
                }
            ],
        },
        "meta": {
            "freshness": freshness,
            "degraded": freshness != "fresh",
            "upstream_status": {"class": "ok" if freshness == "fresh" else "upstream_timeout"},
        },
    }


def test_coinglass_envelope_is_bounded_without_inventing_side_or_size_units() -> None:
    levels = [
        {
            "begin_date": 1_799_999_900 + index,
            "level": 3,
            "level2": "h1",
            "price": str(Decimal("0.071") + Decimal(index) / 100_000),
            "side": 1 if index % 2 else 2,
            "size": str(index + 1),
            "x": index,
        }
        for index in range(LIQUIDATION_LEVEL_MAX + 10)
    ]

    snapshot = snapshot_from_envelope(
        _envelope(levels=levels),
        target=LIQUIDATION_TARGETS[-1],
        received_at_ms=NOW,
        model_version=LIQUIDATION_MODEL_VERSION,
        range_key=LIQUIDATION_RANGE,
    )

    assert snapshot.provider == "coinglass_web"
    assert snapshot.contract == "undocumented_public_web_http"
    assert snapshot.authenticated is False and snapshot.completeness == "unknown"
    assert snapshot.raw_level_count == LIQUIDATION_LEVEL_MAX + 10
    assert len(snapshot.zones) == LIQUIDATION_LEVEL_MAX
    assert snapshot.zones[0].size == Decimal(str(LIQUIDATION_LEVEL_MAX + 10))
    assert snapshot.zones[0].raw_side in {1, 2}
    assert snapshot.source_at_ms == 1_799_999_990_000
    assert len(snapshot.payload_sha256) == 64


def test_coinglass_shape_drift_is_unavailable_and_cannot_blank_a_good_snapshot() -> None:
    envelope = _envelope(levels=[])
    del envelope["data"]["levels"]

    snapshot = snapshot_from_envelope(
        envelope,
        target=LIQUIDATION_TARGETS[-1],
        received_at_ms=NOW,
        model_version=LIQUIDATION_MODEL_VERSION,
        range_key=LIQUIDATION_RANGE,
    )

    assert snapshot.freshness == "unavailable"
    assert snapshot.error_class == "provider_contract_drift"
    assert snapshot.zones == () and snapshot.payload_sha256 is None


def test_image_installs_the_same_isolated_commit_the_adapter_documents() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert f"COINGLASS_CLI_COMMIT={COINGLASS_CLI_COMMIT}" in dockerfile
    assert "COPY --from=python-deps /opt/coinglass-cli /opt/coinglass-cli" in dockerfile
    assert "coinglass-cli" not in (ROOT / "uv.lock").read_text(encoding="utf-8")


class _LiquidationRepo:
    def __init__(self) -> None:
        self.stored: list[ProviderLiquidationSnapshot] = []
        self.due = list(LIQUIDATION_TARGETS)

    def due_targets(self, _targets: Any, **kwargs: Any) -> list[Any]:
        return list(self.due[: int(kwargs["limit"])])

    def store_snapshot(self, snapshot: ProviderLiquidationSnapshot) -> None:
        self.stored.append(snapshot)


class _FakeDb:
    def __init__(self, liquidation: _LiquidationRepo) -> None:
        self.liquidation = liquidation

    def heavy_business(self) -> Any:
        return self

    async def run_business(self, _name: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
        del kwargs
        return fn(*args)

    @contextmanager
    def worker_session(self, _name: str, _timeout: float):
        yield SimpleNamespace(liquidation=self.liquidation, transaction=nullcontext)


class _Provider:
    def __init__(self, snapshot: ProviderLiquidationSnapshot) -> None:
        self.snapshot = snapshot
        self.calls: list[Any] = []

    async def fetch(self, target: Any, *, model_version: str, range_key: str) -> ProviderLiquidationSnapshot:
        self.calls.append((target, model_version, range_key))
        return self.snapshot


def test_shadow_turn_fetches_only_one_due_pair_and_writes_no_card_or_decision() -> None:
    target = LIQUIDATION_TARGETS[-1]
    snapshot = snapshot_from_envelope(
        _envelope(levels=[]),
        target=target,
        received_at_ms=NOW,
        model_version=LIQUIDATION_MODEL_VERSION,
        range_key=LIQUIDATION_RANGE,
    )
    repo = _LiquidationRepo()
    repo.due = [target]
    provider = _Provider(snapshot)
    loop = LiquidationSnapshotLoop(db=_FakeDb(repo), provider=provider, clock_ms=lambda: NOW)

    result = asyncio.run(loop.turn())

    assert provider.calls == [(target, LIQUIDATION_MODEL_VERSION, LIQUIDATION_RANGE)]
    assert repo.stored == [snapshot]
    assert result == {"due": 1, "attempted": 1, "written": 1, "fresh": 1, "error": None}
