from __future__ import annotations

from tracefold.app.llm import configured_chat_model, llm_is_configured
from tracefold.integrations.news_feeds import RssFeedReader
from tracefold.integrations.news_story_analysis import DeepSeekStoryAnalyzer
from tracefold.news import NewsAnalysisWorker, NewsIngestWorker
from tracefold.platform.workers.factory import WorkerFactoryContext, disabled_worker, unavailable_worker
from tracefold.platform.workers.worker_base import WorkerBase


def construct_news_workers(ctx: WorkerFactoryContext) -> dict[str, WorkerBase]:
    workers = ctx.settings.workers
    if not ctx.settings.news.enabled:
        return {
            "news_ingest": disabled_worker(ctx, "news_ingest"),
            "news_analysis": disabled_worker(ctx, "news_analysis"),
        }

    constructed: dict[str, WorkerBase] = {}
    if workers.news_ingest.enabled:
        constructed["news_ingest"] = NewsIngestWorker(
            settings=workers.news_ingest,
            db=ctx.db,
            telemetry=ctx.telemetry,
            sources=ctx.settings.news.sources,
            feed_reader=RssFeedReader(timeout_seconds=workers.news_ingest.fetch_timeout_seconds),
        )
    else:
        constructed["news_ingest"] = disabled_worker(ctx, "news_ingest")

    if not workers.news_analysis.enabled:
        constructed["news_analysis"] = disabled_worker(ctx, "news_analysis")
    elif not llm_is_configured(ctx.settings):
        constructed["news_analysis"] = unavailable_worker(
            ctx,
            "news_analysis",
            "llm_not_configured",
        )
    else:
        model, model_name = configured_chat_model(ctx.settings, workers.news_analysis)
        constructed["news_analysis"] = NewsAnalysisWorker(
            settings=workers.news_analysis,
            db=ctx.db,
            telemetry=ctx.telemetry,
            analyzer=DeepSeekStoryAnalyzer(model=model, model_name=model_name),
            model_name=model_name,
        )
    return constructed


__all__ = ["construct_news_workers"]
