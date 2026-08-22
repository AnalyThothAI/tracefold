"""Provider-neutral liquidation snapshots and the bounded CoinGlass shadow loop (#144)."""

from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager, nullcontext
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tracefold.integrations import coinglass
from tracefold.integrations.coinglass import CoinglassCliLiquidationProvider, snapshot_from_envelope
from tracefold.news.liquidation import (
    LIQUIDATION_LEVEL_MAX,
    LIQUIDATION_MODEL_VERSION,
    LIQUIDATION_RANGE,
    LIQUIDATION_TARGETS,
    ProviderLiquidationSnapshot,
)
from tracefold.news.liquidation_loops import LiquidationSnapshotLoop

NOW = 1_800_000_000_000


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


@pytest.mark.parametrize("missing", ["meta", "timestamp"])
def test_coinglass_missing_health_or_provider_timestamp_is_never_reported_fresh(missing: str) -> None:
    envelope = _envelope(levels=[])
    if missing == "meta":
        del envelope["meta"]
    else:
        envelope["data"]["prices"] = []

    snapshot = snapshot_from_envelope(
        envelope,
        target=LIQUIDATION_TARGETS[-1],
        received_at_ms=NOW,
        model_version=LIQUIDATION_MODEL_VERSION,
        range_key=LIQUIDATION_RANGE,
    )

    assert snapshot.freshness == "unavailable" and snapshot.degraded is True
    assert snapshot.error_class in {"provider_contract_drift", "provider_timestamp_missing"}


def test_coinglass_overflow_kills_and_reaps_the_real_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "overflow-provider"
    executable.write_text(
        "#!/usr/bin/env python3\nimport os\nos.write(1, b'x' * (6 * 1024 * 1024))\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setattr(coinglass, "_PACKAGED_EXECUTABLE", str(executable))

    snapshot = asyncio.run(
        CoinglassCliLiquidationProvider(timeout_seconds=5).fetch(
            LIQUIDATION_TARGETS[0],
            model_version=LIQUIDATION_MODEL_VERSION,
            range_key=LIQUIDATION_RANGE,
        )
    )

    assert snapshot.freshness == "unavailable" and snapshot.error_class == "payload_too_large"


def test_coinglass_cancellation_kills_and_reaps_the_real_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_path = tmp_path / "provider.pid"
    executable = tmp_path / "never-ending-provider"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import os, time\n"
        f"open({str(pid_path)!r}, 'w', encoding='utf-8').write(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setattr(coinglass, "_PACKAGED_EXECUTABLE", str(executable))

    async def _cancel() -> int:
        task = asyncio.create_task(
            CoinglassCliLiquidationProvider(timeout_seconds=30).fetch(
                LIQUIDATION_TARGETS[0],
                model_version=LIQUIDATION_MODEL_VERSION,
                range_key=LIQUIDATION_RANGE,
            )
        )
        for _ in range(100):
            if await asyncio.to_thread(pid_path.exists):
                break
            await asyncio.sleep(0.01)
        pid = int(await asyncio.to_thread(pid_path.read_text, encoding="utf-8"))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        return pid

    pid = asyncio.run(_cancel())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


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
        self.open_sessions = 0

    def heavy_business(self) -> Any:
        return self

    async def run_business(self, _name: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
        del kwargs
        return fn(*args)

    @contextmanager
    def worker_session(self, _name: str, _timeout: float):
        self.open_sessions += 1
        try:
            yield SimpleNamespace(liquidation=self.liquidation, transaction=nullcontext)
        finally:
            self.open_sessions -= 1


class _Provider:
    def __init__(self, snapshot: ProviderLiquidationSnapshot, *, db: _FakeDb | None = None) -> None:
        self.snapshot = snapshot
        self.db = db
        self.calls: list[Any] = []

    async def fetch(self, target: Any, *, model_version: str, range_key: str) -> ProviderLiquidationSnapshot:
        if self.db is not None:
            assert self.db.open_sessions == 0, "external I/O must not hold a database session"
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
    db = _FakeDb(repo)
    provider = _Provider(snapshot, db=db)
    loop = LiquidationSnapshotLoop(db=db, provider=provider, clock_ms=lambda: NOW)

    result = asyncio.run(loop.turn())

    assert provider.calls == [(target, LIQUIDATION_MODEL_VERSION, LIQUIDATION_RANGE)]
    assert repo.stored == [snapshot]
    assert result == {"due": 1, "attempted": 1, "written": 1, "fresh": 1, "error": None}


def test_shadow_turns_skip_instead_of_overlapping_or_queueing() -> None:
    target = LIQUIDATION_TARGETS[0]
    snapshot = snapshot_from_envelope(
        {**_envelope(levels=[]), "data": {**_envelope(levels=[])["data"], "symbol": "Binance_BTCUSDT"}},
        target=target,
        received_at_ms=NOW,
        model_version=LIQUIDATION_MODEL_VERSION,
        range_key=LIQUIDATION_RANGE,
    )
    repo = _LiquidationRepo()
    repo.due = [target]
    started = asyncio.Event()
    release = asyncio.Event()

    class _BlockingProvider(_Provider):
        async def fetch(self, target: Any, *, model_version: str, range_key: str) -> ProviderLiquidationSnapshot:
            self.calls.append((target, model_version, range_key))
            started.set()
            await release.wait()
            return self.snapshot

    async def _exercise() -> tuple[dict[str, Any], dict[str, Any]]:
        provider = _BlockingProvider(snapshot)
        loop = LiquidationSnapshotLoop(db=_FakeDb(repo), provider=provider, clock_ms=lambda: NOW)
        first = asyncio.create_task(loop.turn())
        await started.wait()
        skipped = await loop.turn()
        release.set()
        completed = await first
        assert len(provider.calls) == 1
        return skipped, completed

    skipped, completed = asyncio.run(_exercise())
    assert skipped["error"] == "turn_in_progress" and skipped["attempted"] == 0
    assert completed["attempted"] == 1 and completed["written"] == 1


def test_shadow_deadline_and_code_owned_pacer_bounds_are_enforced() -> None:
    repo = _LiquidationRepo()
    repo.due = [LIQUIDATION_TARGETS[0]]

    class _NeverAnswers:
        async def fetch(self, target: Any, *, model_version: str, range_key: str) -> ProviderLiquidationSnapshot:
            del target, model_version, range_key
            await asyncio.sleep(10)
            raise AssertionError("unreachable")

    loop = LiquidationSnapshotLoop(
        db=_FakeDb(repo),
        provider=_NeverAnswers(),
        period_seconds=0,
        refresh_seconds=0,
        turn_deadline_seconds=1,
        clock_ms=lambda: NOW,
    )

    result = asyncio.run(loop.turn())
    assert loop.period == 60.0 and loop.refresh_ms == 60_000.0
    assert result["attempted"] == 1 and result["written"] == 1 and result["error"] == "turn_deadline"
    assert repo.stored[0].freshness == "unavailable"
