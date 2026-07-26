from __future__ import annotations

from tracefold.app.llm import configured_chat_model, llm_is_configured
from tracefold.integrations.news_ai import StructuredNewsPublisher
from tracefold.integrations.news_feeds import RssFeedReader
from tracefold.integrations.news_pages import BoundedNewsPageReader
from tracefold.news import (
    NewsAiPublishWorker,
    NewsBriefPlanWorker,
    NewsIngestWorker,
    NewsPublicationContract,
    NewsStoryProjectWorker,
    brief_publication_contract,
    story_analysis_contract,
)
from tracefold.platform.workers.factory import WorkerFactoryContext, disabled_worker, unavailable_worker
from tracefold.platform.workers.worker_base import WorkerBase


def construct_news_workers(ctx: WorkerFactoryContext) -> dict[str, WorkerBase]:
    workers = ctx.settings.workers
    if not ctx.settings.news.enabled:
        return {
            "news_ingest": disabled_worker(ctx, "news_ingest"),
            "news_story_project": disabled_worker(ctx, "news_story_project"),
            "news_brief_plan": disabled_worker(ctx, "news_brief_plan"),
            "news_ai_publish": disabled_worker(ctx, "news_ai_publish"),
        }

    constructed: dict[str, WorkerBase] = {}
    if workers.news_ingest.enabled:
        constructed["news_ingest"] = NewsIngestWorker(
            settings=workers.news_ingest,
            db=ctx.db,
            telemetry=ctx.telemetry,
            sources=ctx.settings.news.sources,
            feed_reader=RssFeedReader(timeout_seconds=workers.news_ingest.fetch_timeout_seconds),
            page_reader=(
                BoundedNewsPageReader(
                    timeout_seconds=workers.news_ingest.page_enrichment_timeout_seconds,
                    max_bytes=workers.news_ingest.page_enrichment_max_bytes,
                )
                if workers.news_ingest.page_enrichment_enabled
                else None
            ),
        )
    else:
        constructed["news_ingest"] = disabled_worker(ctx, "news_ingest")

    if workers.news_story_project.enabled:
        constructed["news_story_project"] = NewsStoryProjectWorker(
            settings=workers.news_story_project,
            db=ctx.db,
            telemetry=ctx.telemetry,
        )
    else:
        constructed["news_story_project"] = disabled_worker(ctx, "news_story_project")

    if workers.news_brief_plan.enabled:
        constructed["news_brief_plan"] = NewsBriefPlanWorker(
            settings=workers.news_brief_plan,
            db=ctx.db,
            telemetry=ctx.telemetry,
        )
    else:
        constructed["news_brief_plan"] = disabled_worker(ctx, "news_brief_plan")

    if not workers.news_ai_publish.enabled:
        constructed["news_ai_publish"] = disabled_worker(ctx, "news_ai_publish")
    elif not llm_is_configured(ctx.settings):
        constructed["news_ai_publish"] = unavailable_worker(
            ctx,
            "news_ai_publish",
            "llm_not_configured",
        )
    else:
        model, model_name = configured_chat_model(ctx.settings, workers.news_ai_publish)
        brief_contract: NewsPublicationContract = brief_publication_contract(model_name)
        story_contract: NewsPublicationContract = story_analysis_contract(model_name)
        constructed["news_ai_publish"] = NewsAiPublishWorker(
            settings=workers.news_ai_publish,
            db=ctx.db,
            telemetry=ctx.telemetry,
            publisher=StructuredNewsPublisher(model=model, model_name=model_name),
            brief_contract=brief_contract,
            story_contract=story_contract,
        )
    return constructed


__all__ = ["construct_news_workers"]
