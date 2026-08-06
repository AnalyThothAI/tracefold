from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import uuid4

from tracefold.app.database import WorkerDatabase
from tracefold.app.market_providers import AssetMarketProviders, wire_asset_market
from tracefold.app.worker_capabilities import FiniteOperations
from tracefold.market import AssetProfileRefresh, ResolutionRefresh, TokenImageMirror
from tracefold.platform.config.settings import Settings

_OPERATIONS = frozenset(
    {
        "resolution_refresh",
        "asset_profile_refresh",
        "token_image_mirror",
    }
)


def mirror_token_images_once(
    settings: Settings,
    *,
    limit: int,
    db: WorkerDatabase | None = None,
) -> dict[str, Any]:
    return asyncio.run(
        _run_maintenance(
            settings=settings,
            operation="token_image_mirror",
            limit=limit,
            reprocess_limit=500,
            db=db,
        )
    )


def refresh_resolutions_once(
    settings: Settings,
    *,
    limit: int,
    reprocess_limit: int,
    db: WorkerDatabase | None = None,
) -> dict[str, Any]:
    return asyncio.run(
        _run_maintenance(
            settings=settings,
            operation="resolution_refresh",
            limit=limit,
            reprocess_limit=reprocess_limit,
            db=db,
        )
    )


def refresh_asset_profiles_once(
    settings: Settings,
    *,
    limit: int,
    db: WorkerDatabase | None = None,
) -> dict[str, Any]:
    return asyncio.run(
        _run_maintenance(
            settings=settings,
            operation="asset_profile_refresh",
            limit=limit,
            reprocess_limit=500,
            db=db,
        )
    )


async def _run_maintenance(
    *,
    settings: Settings,
    operation: str,
    limit: int,
    reprocess_limit: int,
    db: WorkerDatabase | None,
) -> dict[str, Any]:
    if operation not in _OPERATIONS:
        raise ValueError(f"maintenance_operation_unsupported:{operation}")
    if int(limit) < 1 or int(reprocess_limit) < 1:
        raise ValueError("maintenance_limit_must_be_positive")

    owns_db = db is None
    database = db or WorkerDatabase.create(settings)
    lock_conn: Any | None = None
    finite = FiniteOperations()
    providers: AssetMarketProviders | None = None
    primary_error: BaseException | None = None
    try:
        if owns_db:
            lock_conn = database.acquire_maintenance_runtime_lock()
        if operation in {"asset_profile_refresh", "resolution_refresh"}:
            providers = wire_asset_market(settings)
        turn = _maintenance_turn(
            operation=operation,
            settings=settings,
            db=database,
            finite=finite,
            providers=providers,
            reprocess_limit=reprocess_limit,
        )
        preparation = (
            await database.run_business(
                "ops_refresh_asset_profiles_prepare",
                _enqueue_missing_asset_profile_targets,
                db=database,
                asset_market=providers,
                limit=limit,
                now_ms=_now_ms(),
                operation_timeout_seconds=120.0,
            )
            if operation == "asset_profile_refresh"
            else None
        )
        counters = {"processed": 0, "failed": 0, "terminal": 0, "skipped": 0}
        for _ in range(int(limit)):
            outcome = await turn()
            if outcome is False:
                counters["skipped"] += 1
                break
            if outcome is None:
                counters["skipped"] += 1
                continue
            if outcome is True:
                counters["processed"] += 1
                continue
            if outcome not in {"processed", "failed", "terminal"}:
                raise RuntimeError(f"maintenance_outcome_invalid:{outcome}")
            counters[outcome] += 1
        return {
            "operation": operation,
            **counters,
            "preparation": preparation,
        }
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        errors = await _close_maintenance(
            providers=providers,
            database=database,
            finite=finite,
            owns_db=owns_db,
            lock_conn=lock_conn,
        )
        if errors:
            cleanup_error = ExceptionGroup("maintenance_cleanup_failed", errors)
            if primary_error is None:
                raise cleanup_error
            primary_error.add_note(str(cleanup_error))


def _maintenance_turn(
    *,
    operation: str,
    settings: Settings,
    db: WorkerDatabase,
    finite: FiniteOperations,
    providers: AssetMarketProviders | None,
    reprocess_limit: int,
) -> Any:
    runtime_id = str(uuid4())
    if operation == "token_image_mirror":
        return TokenImageMirror(
            db=db,
            app_home=settings.app_home,
            finite_operations=finite,
            runtime_id=runtime_id,
        ).turn
    if providers is None:
        raise RuntimeError(f"maintenance_provider_required:{operation}")
    if operation == "asset_profile_refresh":
        if not providers.dex_profile_sources:
            raise RuntimeError("maintenance_asset_profile_provider_unavailable")
        return AssetProfileRefresh(
            db=db,
            finite_operations=finite,
            runtime_id=runtime_id,
            dex_profile_sources=providers.dex_profile_sources,
        ).turn
    if providers.dex_discovery_market is None:
        raise RuntimeError("maintenance_resolution_provider_unavailable")
    return ResolutionRefresh(
        db=db,
        dex_discovery_market=providers.dex_discovery_market,
        finite_operations=finite,
        runtime_id=runtime_id,
        claim_limit=1,
        reprocess_limit=reprocess_limit,
    ).turn


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


async def _close_maintenance(
    *,
    providers: AssetMarketProviders | None,
    database: WorkerDatabase,
    finite: FiniteOperations,
    owns_db: bool,
    lock_conn: Any | None,
) -> list[Exception]:
    errors: list[Exception] = []
    finite.close_admission()
    if providers is not None:
        seen: set[int] = set()
        sync_providers = [
            providers.cex_market,
            providers.dex_discovery_market,
            providers.dex_quote_market,
            *(source.market for source in providers.dex_profile_sources),
        ]
        for provider in sync_providers:
            if provider is None or id(provider) in seen:
                continue
            seen.add(id(provider))
            try:
                await finite.run(
                    "maintenance_provider_close",
                    provider.close,
                    timeout_seconds=5.0,
                    allow_shutdown=True,
                )
            except Exception as exc:
                errors.append(exc)
    try:
        if not await database.drain_business(timeout_seconds=5.0):
            raise RuntimeError("maintenance_database_drain_timeout")
        if not await finite.drain(timeout_seconds=5.0):
            raise RuntimeError("maintenance_finite_drain_timeout")
    except Exception as exc:
        errors.append(exc)
    finite.close()
    if owns_db:
        try:
            if lock_conn is not None:
                database.release_maintenance_runtime_lock(lock_conn)
            await database.aclose()
            database.close_executors()
        except Exception as exc:
            errors.append(exc)
    return errors


def _now_ms() -> int:
    return int(time.time() * 1_000)


__all__ = [
    "mirror_token_images_once",
    "refresh_asset_profiles_once",
    "refresh_resolutions_once",
]
