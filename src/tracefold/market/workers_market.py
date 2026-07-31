from __future__ import annotations

from tracefold.market.identity.resolution_refresh_worker import ResolutionRefreshWorker
from tracefold.market.pricing.event_anchor_backfill_worker import EventAnchorBackfillWorker
from tracefold.market.pricing.market_tick_poll_worker import MarketTickPollWorker
from tracefold.market.pricing.market_tick_stream_worker import MarketTickStreamWorker
from tracefold.market.profiles.asset_profile_refresh_worker import AssetProfileRefreshWorker
from tracefold.market.profiles.token_image_mirror_worker import TokenImageMirrorWorker
from tracefold.platform.workers.factory import WorkerFactoryContext, mark_inactive
from tracefold.platform.workers.worker_base import WorkerBase


def construct_market_workers(ctx: WorkerFactoryContext) -> dict[str, WorkerBase]:
    asset_market = ctx.asset_market
    cex_market = asset_market.cex_market if asset_market is not None else None
    dex_quote_market = asset_market.dex_quote_market if asset_market is not None else None
    dex_profile_sources = tuple(asset_market.dex_profile_sources or ()) if asset_market is not None else ()
    dex_discovery_market = asset_market.dex_discovery_market if asset_market is not None else None
    stream_dex_market = asset_market.stream_dex_market if asset_market is not None else None
    constructed: dict[str, WorkerBase] = {}

    constructed["token_image_mirror"] = TokenImageMirrorWorker(
        name="token_image_mirror",
        db=ctx.db,
        telemetry=ctx.telemetry,
        app_home=ctx.settings.app_home,
        resources=ctx.resources,
        provider_governor=ctx.provider_governor,
        runtime_id=ctx.runtime_id,
    )
    if stream_dex_market is not None:
        constructed["market_tick_stream"] = MarketTickStreamWorker(
            name="market_tick_stream",
            pool_bundle=ctx.db,
            telemetry=ctx.telemetry,
            stream_dex_market=stream_dex_market,
            resources=ctx.resources,
            provider_governor=ctx.provider_governor,
        )
    else:
        mark_inactive(
            ctx,
            "market_tick_stream",
            effective_status="unavailable",
            reason="missing_asset_market_stream_provider",
        )
    if asset_market is not None and (cex_market is not None or dex_quote_market is not None):
        constructed["market_tick_poll"] = MarketTickPollWorker(
            name="market_tick_poll",
            pool_bundle=ctx.db,
            telemetry=ctx.telemetry,
            providers=asset_market,
            resources=ctx.resources,
            provider_governor=ctx.provider_governor,
        )
    else:
        mark_inactive(
            ctx,
            "market_tick_poll",
            effective_status="unavailable",
            reason="missing_asset_market_quote_provider",
        )
    if asset_market is not None:
        constructed["event_anchor_capture"] = EventAnchorBackfillWorker(
            name="event_anchor_capture",
            pool_bundle=ctx.db,
            telemetry=ctx.telemetry,
            providers=asset_market,
            resources=ctx.resources,
            provider_governor=ctx.provider_governor,
            runtime_id=ctx.runtime_id,
        )
    else:
        mark_inactive(
            ctx,
            "event_anchor_capture",
            effective_status="unavailable",
            reason="missing_asset_market_provider",
        )
    if dex_profile_sources:
        constructed["asset_profile_refresh"] = AssetProfileRefreshWorker(
            name="asset_profile_refresh",
            db=ctx.db,
            telemetry=ctx.telemetry,
            dex_profile_sources=dex_profile_sources,
            resources=ctx.resources,
            provider_governor=ctx.provider_governor,
            runtime_id=ctx.runtime_id,
        )
    else:
        mark_inactive(
            ctx,
            "asset_profile_refresh",
            effective_status="unavailable",
            reason="missing_asset_profile_provider",
        )
    if dex_discovery_market is not None:
        constructed["resolution_refresh"] = ResolutionRefreshWorker(
            name="resolution_refresh",
            db=ctx.db,
            telemetry=ctx.telemetry,
            dex_discovery_market=dex_discovery_market,
            resources=ctx.resources,
            provider_governor=ctx.provider_governor,
            runtime_id=ctx.runtime_id,
        )
    else:
        mark_inactive(
            ctx,
            "resolution_refresh",
            effective_status="unavailable",
            reason="missing_asset_discovery_provider",
        )
    return constructed
