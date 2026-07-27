from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from tracefold.app.database import DBPoolBundle
from tracefold.app.market_providers import wire_asset_market
from tracefold.app.provider_types import AssetMarketProviders
from tracefold.app.worker_manifest import require_worker_manifest, worker_names
from tracefold.app.workers import construct_worker
from tracefold.market import token_profile_image_repair_targets
from tracefold.platform.config.settings import Settings
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.workers.worker_base import WorkerBase
from tracefold.platform.workers.worker_result import WorkerResult

_ASSET_PROVIDER_WORKERS = frozenset({"asset_profile_refresh", "resolution_refresh"})
_SUPPORTED_WORKERS = frozenset(
    {
        "asset_profile_refresh",
        "resolution_refresh",
        "token_image_mirror",
        "token_profile_current",
        "token_radar_projection",
        "macro_market_intraday",
        "macro_settlements",
        "macro_economic_releases",
        "macro_official_state",
        "macro_official_documents",
        "macro_backfill",
        "macro_projection",
        "macro_judgment",
        "macro_research",
    }
)


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


def run_worker_once(
    settings: Settings,
    worker_name: str,
    overrides: Mapping[str, object] | None = None,
) -> WorkerExecution:
    """Compose and run one production-equivalent worker iteration."""
    return asyncio.run(
        _run_worker_once(
            settings=settings,
            worker_name=worker_name,
            overrides=overrides,
        )
    )


def refresh_asset_profiles_once(settings: Settings, *, limit: int) -> WorkerExecution:
    return asyncio.run(
        _run_worker_once(
            settings=settings,
            worker_name="asset_profile_refresh",
            overrides={"batch_size": limit},
            prepare=_enqueue_missing_asset_profile_targets,
        )
    )


def repair_token_profile_images_once(settings: Settings, *, limit: int) -> WorkerExecution:
    return asyncio.run(
        _run_worker_once(
            settings=settings,
            worker_name="token_profile_current",
            overrides={"batch_size": limit},
            prepare=_enqueue_token_profile_image_repair_targets,
        )
    )


@dataclass(slots=True)
class _OneShotComposition:
    settings: Settings
    db: DBPoolBundle
    asset_market: AssetMarketProviders | None
    worker: WorkerBase

    async def aclose(self) -> None:
        errors: list[Exception] = []
        resources = [self.worker, self.asset_market, self.db]
        closed: set[int] = set()
        for resource in resources:
            if resource is None:
                continue
            if id(resource) in closed:
                continue
            closed.add(id(resource))
            try:
                await resource.aclose()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("worker_once_cleanup_failed", errors)


Preparation = Callable[[_OneShotComposition, int], dict[str, Any]]


async def _run_worker_once(
    *,
    settings: Settings,
    worker_name: str,
    overrides: Mapping[str, object] | None,
    prepare: Preparation | None = None,
) -> WorkerExecution:
    composition = await _compose_worker(settings=settings, worker_name=worker_name, overrides=overrides)
    primary_error: BaseException | None = None
    try:
        now_ms = _now_ms()
        preparation = prepare(composition, now_ms) if prepare is not None else None
        result = await composition.worker.run_one_iteration()
        return _execution(worker_name=worker_name, result=result, preparation=preparation)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            await composition.aclose()
        except Exception as cleanup_exc:
            if primary_error is None:
                raise
            primary_error.add_note(f"worker_once_cleanup_failed: {cleanup_exc}")


async def _compose_worker(
    *,
    settings: Settings,
    worker_name: str,
    overrides: Mapping[str, object] | None,
) -> _OneShotComposition:
    require_worker_manifest(worker_name)
    if worker_name not in _SUPPORTED_WORKERS:
        raise ValueError(f"worker_once_unsupported:{worker_name}")

    one_shot_settings = _one_shot_settings(settings, worker_name=worker_name, overrides=overrides)
    telemetry = TelemetryRegistry()
    db = DBPoolBundle.create(one_shot_settings, telemetry=telemetry)
    asset_market: AssetMarketProviders | None = None
    worker: WorkerBase | None = None
    try:
        asset_market = wire_asset_market(one_shot_settings) if worker_name in _ASSET_PROVIDER_WORKERS else None
        worker = construct_worker(
            worker_name=worker_name,
            settings=one_shot_settings,
            db=db,
            telemetry=telemetry,
            asset_market=asset_market,
            hub=None,
            collector=None,
            collector_enabled=False,
            collector_start_requested=False,
        )
        if worker.effective_status == "unavailable":
            raise RuntimeError(f"worker_once_unavailable:{worker_name}:{worker.unavailable_reason or 'unknown'}")
        return _OneShotComposition(
            settings=one_shot_settings,
            db=db,
            asset_market=asset_market,
            worker=worker,
        )
    except BaseException as exc:
        await _close_partial_composition(
            exc,
            worker=worker,
            asset_market=asset_market,
            db=db,
        )
        raise


def _one_shot_settings(
    settings: Settings,
    *,
    worker_name: str,
    overrides: Mapping[str, object] | None,
) -> Settings:
    worker_payload = settings.workers.model_dump()
    for name in worker_names():
        worker_payload[name]["enabled"] = name == worker_name
    worker_payload[worker_name].update(dict(overrides or {}))
    worker_payload[worker_name]["enabled"] = True
    configured_workers = type(settings.workers).model_validate(worker_payload)
    return settings.model_copy(update={"workers": configured_workers})


def _execution(
    *,
    worker_name: str,
    result: WorkerResult,
    preparation: dict[str, Any] | None,
) -> WorkerExecution:
    return WorkerExecution(
        worker_name=worker_name,
        processed=int(result.processed),
        failed=int(result.failed),
        dead=int(result.dead),
        skipped=int(result.skipped),
        notes=dict(result.notes),
        preparation=preparation,
    )


def _enqueue_missing_asset_profile_targets(composition: _OneShotComposition, now_ms: int) -> dict[str, Any]:
    source_rows_scanned = 0
    targets_enqueued = 0
    sources: dict[str, dict[str, Any]] = {}
    limit = int(composition.settings.workers.asset_profile_refresh.batch_size)
    if composition.asset_market is None:
        raise RuntimeError("worker_once_asset_profile_provider_required")
    for profile_source in composition.asset_market.dex_profile_sources:
        with (
            composition.db.worker_session(
                "ops_refresh_asset_profiles",
                statement_timeout_seconds=composition.settings.workers.asset_profile_refresh.statement_timeout_seconds,
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


def _enqueue_token_profile_image_repair_targets(
    composition: _OneShotComposition,
    now_ms: int,
) -> dict[str, Any]:
    limit = int(composition.settings.workers.token_profile_current.batch_size)
    with composition.db.worker_session("token_profile_image_repair") as repos, repos.transaction():
        targets = token_profile_image_repair_targets(repos.conn, limit=limit)
        enqueue_result = repos.token_profile_current_dirty_targets.enqueue_targets(
            targets,
            reason="token_profile_image_repair",
            now_ms=now_ms,
        )
    return {
        "selected_targets": len(targets),
        "profile_targets_enqueued": int(enqueue_result.get("targets") or 0),
    }


async def _close_partial_composition(
    primary_error: BaseException,
    *,
    worker: WorkerBase | None,
    asset_market: AssetMarketProviders | None,
    db: DBPoolBundle,
) -> None:
    resources = [worker, asset_market, db]
    closed: set[int] = set()
    for resource in resources:
        if resource is None:
            continue
        if id(resource) in closed:
            continue
        closed.add(id(resource))
        try:
            await resource.aclose()
        except Exception as exc:
            primary_error.add_note(f"worker_once_partial_cleanup_failed: {exc}")


def _now_ms() -> int:
    return int(time.time() * 1000)


__all__ = [
    "WorkerExecution",
    "refresh_asset_profiles_once",
    "repair_token_profile_images_once",
    "run_worker_once",
]
