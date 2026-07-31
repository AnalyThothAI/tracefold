from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from tracefold.app.database import WorkerDatabase
from tracefold.app.market_providers import wire_asset_market
from tracefold.app.provider_types import AssetMarketProviders
from tracefold.app.runtime_resources import ProviderGovernor, RuntimeResources
from tracefold.market import (
    AssetProfileRefreshWorker,
    ResolutionRefreshWorker,
    TokenImageMirrorWorker,
)
from tracefold.platform.config.settings import Settings
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.workers.worker_base import WorkerBase
from tracefold.platform.workers.worker_result import WorkerResult


@dataclass(frozen=True, slots=True)
class WorkerExecution:
    worker_name: str
    processed: int
    failed: int
    dead: int
    skipped: int
    notes: dict[str, Any]
    preparation: dict[str, Any] | None = None

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "worker_name": self.worker_name,
            "processed": self.processed,
            "failed": self.failed,
            "dead": self.dead,
            "skipped": self.skipped,
            "notes": dict(self.notes),
        }
        if self.preparation is not None:
            payload["preparation"] = dict(self.preparation)
        return payload


def mirror_token_images_once(settings: Settings, *, limit: int) -> WorkerExecution:
    return asyncio.run(
        _run_maintenance(
            settings=settings,
            unit_name="token_image_mirror",
            limit=limit,
            reprocess_limit=500,
        )
    )


def refresh_resolutions_once(
    settings: Settings,
    *,
    limit: int,
    reprocess_limit: int,
) -> WorkerExecution:
    return asyncio.run(
        _run_maintenance(
            settings=settings,
            unit_name="resolution_refresh",
            limit=limit,
            reprocess_limit=reprocess_limit,
        )
    )


def refresh_asset_profiles_once(settings: Settings, *, limit: int) -> WorkerExecution:
    return asyncio.run(
        _run_maintenance(
            settings=settings,
            unit_name="asset_profile_refresh",
            limit=limit,
            reprocess_limit=500,
        )
    )


async def _run_maintenance(
    *,
    settings: Settings,
    unit_name: str,
    limit: int,
    reprocess_limit: int,
) -> WorkerExecution:
    if limit < 1:
        raise ValueError("maintenance_limit_must_be_positive")
    telemetry = TelemetryRegistry()
    db = WorkerDatabase.create(settings, telemetry=telemetry)
    resources = RuntimeResources()
    governor = ProviderGovernor()
    asset_market: AssetMarketProviders | None = None
    worker: WorkerBase | None = None
    primary_error: BaseException | None = None
    try:
        if unit_name in {"asset_profile_refresh", "resolution_refresh"}:
            asset_market = wire_asset_market(settings)
        worker = _construct_maintenance_worker(
            unit_name=unit_name,
            settings=settings,
            db=db,
            telemetry=telemetry,
            resources=resources,
            governor=governor,
            asset_market=asset_market,
            limit=limit,
            reprocess_limit=reprocess_limit,
        )
        preparation = (
            _enqueue_missing_asset_profile_targets(
                db=db,
                asset_market=asset_market,
                limit=limit,
                now_ms=_now_ms(),
            )
            if unit_name == "asset_profile_refresh"
            else None
        )
        iterations = limit if unit_name != "resolution_refresh" else 1
        results: list[WorkerResult] = []
        for _ in range(iterations):
            result = await worker.run_once()
            results.append(result)
            if result.skipped and not result.processed and not result.failed and not result.dead:
                break
        return _execution(
            worker_name=unit_name,
            results=results,
            preparation=preparation,
        )
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        errors = await _close_maintenance(
            worker=worker,
            asset_market=asset_market,
            db=db,
            resources=resources,
        )
        if errors:
            cleanup_error = ExceptionGroup("maintenance_cleanup_failed", errors)
            if primary_error is None:
                raise cleanup_error
            primary_error.add_note(str(cleanup_error))


def _construct_maintenance_worker(
    *,
    unit_name: str,
    settings: Settings,
    db: WorkerDatabase,
    telemetry: TelemetryRegistry,
    resources: RuntimeResources,
    governor: ProviderGovernor,
    asset_market: AssetMarketProviders | None,
    limit: int,
    reprocess_limit: int,
) -> WorkerBase:
    runtime_id = str(uuid4())
    if unit_name == "token_image_mirror":
        return TokenImageMirrorWorker(
            name=unit_name,
            db=db,
            telemetry=telemetry,
            app_home=settings.app_home,
            resources=resources,
            provider_governor=governor,
            runtime_id=runtime_id,
        )
    if asset_market is None:
        raise RuntimeError(f"maintenance_provider_required:{unit_name}")
    if unit_name == "asset_profile_refresh":
        if not asset_market.dex_profile_sources:
            raise RuntimeError("maintenance_asset_profile_provider_unavailable")
        return AssetProfileRefreshWorker(
            name=unit_name,
            db=db,
            telemetry=telemetry,
            resources=resources,
            provider_governor=governor,
            runtime_id=runtime_id,
            dex_profile_sources=asset_market.dex_profile_sources,
        )
    if unit_name == "resolution_refresh":
        if asset_market.dex_discovery_market is None:
            raise RuntimeError("maintenance_resolution_provider_unavailable")
        return ResolutionRefreshWorker(
            name=unit_name,
            db=db,
            telemetry=telemetry,
            dex_discovery_market=asset_market.dex_discovery_market,
            resources=resources,
            provider_governor=governor,
            runtime_id=runtime_id,
            claim_limit=limit,
            reprocess_limit=reprocess_limit,
        )
    raise ValueError(f"maintenance_unit_unsupported:{unit_name}")


def _enqueue_missing_asset_profile_targets(
    *,
    db: WorkerDatabase,
    asset_market: AssetMarketProviders | None,
    limit: int,
    now_ms: int,
) -> dict[str, Any]:
    if asset_market is None:
        raise RuntimeError("maintenance_asset_profile_provider_required")
    source_rows_scanned = 0
    targets_enqueued = 0
    sources: dict[str, dict[str, Any]] = {}
    for profile_source in asset_market.dex_profile_sources:
        with (
            db.worker_session(
                "ops_refresh_asset_profiles",
                statement_timeout_seconds=120.0,
            ) as repos,
            repos.transaction(),
        ):
            result = repos.asset_profile_refresh_targets.enqueue_missing_token_radar_current_targets_for_ops(
                provider=profile_source.provider,
                now_ms=now_ms,
                limit=limit,
            )
        source_rows_scanned += int(result.get("source_rows_scanned") or 0)
        targets_enqueued += int(result.get("targets") or 0)
        sources[profile_source.provider] = dict(result)
    return {
        "source_rows_scanned": source_rows_scanned,
        "targets_enqueued": targets_enqueued,
        "sources": sources,
    }


def _execution(
    *,
    worker_name: str,
    results: list[WorkerResult],
    preparation: dict[str, Any] | None,
) -> WorkerExecution:
    return WorkerExecution(
        worker_name=worker_name,
        processed=sum(int(result.processed) for result in results),
        failed=sum(int(result.failed) for result in results),
        dead=sum(int(result.dead) for result in results),
        skipped=sum(int(result.skipped) for result in results),
        notes={
            "iterations": len(results),
            "results": [dict(result.notes) for result in results],
        },
        preparation=preparation,
    )


async def _close_maintenance(
    *,
    worker: WorkerBase | None,
    asset_market: AssetMarketProviders | None,
    db: WorkerDatabase,
    resources: RuntimeResources,
) -> list[Exception]:
    errors: list[Exception] = []
    for closeable in (worker, asset_market, db):
        if closeable is None:
            continue
        try:
            await closeable.aclose()
        except Exception as exc:
            errors.append(exc)
    try:
        resources.close()
    except Exception as exc:
        errors.append(exc)
    return errors


def _now_ms() -> int:
    return int(time.time() * 1000)


__all__ = [
    "WorkerExecution",
    "mirror_token_images_once",
    "refresh_asset_profiles_once",
    "refresh_resolutions_once",
]
