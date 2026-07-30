from __future__ import annotations

from tracefold.integrations.news_feeds import (
    RssFeedReader,
    is_public_https_feed_url,
)
from tracefold.news import NewsIngestWorker, default_sources
from tracefold.platform.workers.factory import WorkerFactoryContext, disabled_worker
from tracefold.platform.workers.worker_base import WorkerBase


def construct_news_workers(ctx: WorkerFactoryContext) -> dict[str, WorkerBase]:
    settings = ctx.settings
    workers = settings.workers
    names = ("news_ingest",)
    if not settings.news.enabled:
        return {name: disabled_worker(ctx, name) for name in names}

    constructed: dict[str, WorkerBase] = {}
    sources = default_sources()
    if workers.news_ingest.enabled:
        constructed["news_ingest"] = NewsIngestWorker(
            settings=workers.news_ingest,
            db=ctx.db,
            telemetry=ctx.telemetry,
            sources=sources,
            feed_reader=RssFeedReader(
                timeout_seconds=workers.news_ingest.fetch_timeout_seconds,
                max_attempts=1,
                relay_base_url=settings.news.relay.base_url,
                relay_auth_header=settings.news.relay.auth_header,
                relay_auth_token=settings.news.relay.auth_token,
                relay_allowed_urls={source.feed_url for source in sources if is_public_https_feed_url(source.feed_url)},
            ),
        )
    else:
        constructed["news_ingest"] = disabled_worker(ctx, "news_ingest")
    return constructed


__all__ = ["construct_news_workers"]
