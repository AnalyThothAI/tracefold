from __future__ import annotations

from tracefold.market.identity.resolution_refresh_worker import ResolutionRefreshWorker
from tracefold.market.pricing.event_anchor_backfill_worker import EventAnchorBackfillWorker
from tracefold.market.pricing.market_tick_poll_worker import MarketTickPollWorker
from tracefold.market.pricing.market_tick_stream_worker import MarketTickStreamWorker
from tracefold.market.profiles.asset_profile_refresh_worker import AssetProfileRefreshWorker
from tracefold.market.profiles.token_image_mirror_worker import TokenImageMirrorWorker
from tracefold.platform.workers.factory import WorkerFactoryContext, disabled_worker, unavailable_worker
from tracefold.platform.workers.worker_base import WorkerBase


def construct_market_workers(ctx: WorkerFactoryContext) -> dict[str, WorkerBase]:
    workers = ctx.settings.workers
    asset_market = ctx.asset_market
    cex_market = asset_market.cex_market if asset_market is not None else None
    dex_quote_market = asset_market.dex_quote_market if asset_market is not None else None
    dex_profile_sources = tuple(asset_market.dex_profile_sources or ()) if asset_market is not None else ()
    dex_discovery_market = asset_market.dex_discovery_market if asset_market is not None else None
    stream_dex_market = asset_market.stream_dex_market if asset_market is not None else None
    constructed: dict[str, WorkerBase] = {}

    if workers.token_image_mirror.enabled:
        constructed["token_image_mirror"] = TokenImageMirrorWorker(
            name="token_image_mirror",
            settings=workers.token_image_mirror,
            db=ctx.db,
            telemetry=ctx.telemetry,
            app_home=ctx.settings.app_home,
        )
    else:
        constructed["token_image_mirror"] = disabled_worker(ctx, "token_image_mirror")
    if workers.market_tick_stream.enabled:
        if stream_dex_market is not None:
            constructed["market_tick_stream"] = MarketTickStreamWorker(
                name="market_tick_stream",
                settings=workers.market_tick_stream,
                pool_bundle=ctx.db,
                telemetry=ctx.telemetry,
                stream_dex_market=stream_dex_market,
            )
        else:
            constructed["market_tick_stream"] = unavailable_worker(
                ctx, "market_tick_stream", "missing_asset_market_stream_provider"
            )
    else:
        constructed["market_tick_stream"] = disabled_worker(ctx, "market_tick_stream")
    if workers.market_tick_poll.enabled:
        if asset_market is not None and (cex_market is not None or dex_quote_market is not None):
            constructed["market_tick_poll"] = MarketTickPollWorker(
                name="market_tick_poll",
                settings=workers.market_tick_poll,
                pool_bundle=ctx.db,
                telemetry=ctx.telemetry,
                providers=asset_market,
            )
        else:
            constructed["market_tick_poll"] = unavailable_worker(
                ctx, "market_tick_poll", "missing_asset_market_quote_provider"
            )
    else:
        constructed["market_tick_poll"] = disabled_worker(ctx, "market_tick_poll")
    if workers.event_anchor_capture.enabled:
        if asset_market is not None:
            constructed["event_anchor_capture"] = EventAnchorBackfillWorker(
                name="event_anchor_capture",
                settings=workers.event_anchor_capture,
                pool_bundle=ctx.db,
                telemetry=ctx.telemetry,
                providers=asset_market,
            )
        else:
            constructed["event_anchor_capture"] = unavailable_worker(
                ctx, "event_anchor_capture", "missing_asset_market_provider"
            )
    else:
        constructed["event_anchor_capture"] = disabled_worker(ctx, "event_anchor_capture")
    if workers.asset_profile_refresh.enabled:
        if dex_profile_sources:
            constructed["asset_profile_refresh"] = AssetProfileRefreshWorker(
                name="asset_profile_refresh",
                settings=workers.asset_profile_refresh,
                db=ctx.db,
                telemetry=ctx.telemetry,
                dex_profile_sources=dex_profile_sources,
            )
        else:
            constructed["asset_profile_refresh"] = unavailable_worker(
                ctx, "asset_profile_refresh", "missing_asset_profile_provider"
            )
    else:
        constructed["asset_profile_refresh"] = disabled_worker(ctx, "asset_profile_refresh")
    if workers.resolution_refresh.enabled:
        if dex_discovery_market is not None:
            constructed["resolution_refresh"] = ResolutionRefreshWorker(
                name="resolution_refresh",
                settings=workers.resolution_refresh,
                db=ctx.db,
                telemetry=ctx.telemetry,
                dex_discovery_market=dex_discovery_market,
            )
        else:
            constructed["resolution_refresh"] = unavailable_worker(
                ctx, "resolution_refresh", "missing_asset_discovery_provider"
            )
    else:
        constructed["resolution_refresh"] = disabled_worker(ctx, "resolution_refresh")
    return constructed
