from __future__ import annotations

from tracefold.integrations.news_feeds import (
    RssFeedReader,
    is_public_https_feed_url,
)
from tracefold.news import NewsIngestWorker, default_sources
from tracefold.platform.workers.factory import WorkerFactoryContext, mark_inactive
from tracefold.platform.workers.worker_base import WorkerBase


def construct_news_workers(ctx: WorkerFactoryContext) -> dict[str, WorkerBase]:
    settings = ctx.settings
    if not settings.news.enabled:
        mark_inactive(
            ctx,
            "news_ingest",
            effective_status="disabled",
            reason="news_disabled",
        )
        return {}

    sources = default_sources()
    return {
        "news_ingest": NewsIngestWorker(
            db=ctx.db,
            telemetry=ctx.telemetry,
            sources=sources,
            feed_reader=RssFeedReader(
                timeout_seconds=20.0,
                max_attempts=1,
                relay_base_url=settings.news.relay.base_url,
                relay_auth_header=settings.news.relay.auth_header,
                relay_auth_token=settings.news.relay.auth_token,
                relay_allowed_urls={source.feed_url for source in sources if is_public_https_feed_url(source.feed_url)},
            ),
            resources=ctx.resources,
            provider_governor=ctx.provider_governor,
        )
    }


__all__ = ["construct_news_workers"]
