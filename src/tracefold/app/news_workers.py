from __future__ import annotations

from tracefold.integrations.news_ai import (
    ProviderChainNewsBriefPublisher,
    ProviderChainNewsClassificationPublisher,
)
from tracefold.integrations.news_feeds import RssFeedReader
from tracefold.news import NewsAiClassifyWorker, NewsPipelineWorker, NewsWorldBriefWorker, default_sources
from tracefold.platform.workers.factory import WorkerFactoryContext, disabled_worker
from tracefold.platform.workers.worker_base import WorkerBase


def construct_news_workers(ctx: WorkerFactoryContext) -> dict[str, WorkerBase]:
    settings = ctx.settings
    workers = settings.workers
    names = ("news_pipeline", "news_ai_classify", "news_world_brief")
    if not settings.news.enabled:
        return {name: disabled_worker(ctx, name) for name in names}

    constructed: dict[str, WorkerBase] = {}
    if workers.news_pipeline.enabled:
        constructed["news_pipeline"] = NewsPipelineWorker(
            settings=workers.news_pipeline,
            db=ctx.db,
            telemetry=ctx.telemetry,
            sources=settings.news.sources or default_sources(),
            feed_reader=RssFeedReader(
                timeout_seconds=workers.news_pipeline.fetch_timeout_seconds,
                max_attempts=1,
            ),
        )
    else:
        constructed["news_pipeline"] = disabled_worker(ctx, "news_pipeline")

    if workers.news_ai_classify.enabled:
        brief_settings = workers.news_world_brief
        configured_base_url = settings.llm.base_url or ("https://api.deepseek.com/v1" if settings.llm.api_key else "")
        constructed["news_ai_classify"] = NewsAiClassifyWorker(
            settings=workers.news_ai_classify,
            db=ctx.db,
            telemetry=ctx.telemetry,
            publisher=ProviderChainNewsClassificationPublisher(
                configured_base_url=configured_base_url,
                configured_api_key=settings.llm.api_key,
                configured_model=brief_settings.model,
                ollama_base_url=brief_settings.ollama_base_url,
                ollama_model=brief_settings.ollama_model,
                openrouter_base_url=brief_settings.openrouter_base_url,
                openrouter_model=brief_settings.openrouter_model,
                openrouter_api_key=settings.llm.openrouter_api_key,
                groq_base_url=brief_settings.groq_base_url,
                groq_model=brief_settings.groq_model,
                groq_api_key=settings.llm.groq_api_key,
                total_timeout_seconds=workers.news_ai_classify.total_timeout_seconds,
            ),
        )
    else:
        constructed["news_ai_classify"] = disabled_worker(ctx, "news_ai_classify")

    if workers.news_world_brief.enabled:
        configured_base_url = settings.llm.base_url or ("https://api.deepseek.com/v1" if settings.llm.api_key else "")
        constructed["news_world_brief"] = NewsWorldBriefWorker(
            settings=workers.news_world_brief,
            db=ctx.db,
            telemetry=ctx.telemetry,
            publisher=ProviderChainNewsBriefPublisher(
                configured_base_url=configured_base_url,
                configured_api_key=settings.llm.api_key,
                configured_model=workers.news_world_brief.model,
                ollama_base_url=workers.news_world_brief.ollama_base_url,
                ollama_model=workers.news_world_brief.ollama_model,
                openrouter_base_url=workers.news_world_brief.openrouter_base_url,
                openrouter_model=workers.news_world_brief.openrouter_model,
                openrouter_api_key=settings.llm.openrouter_api_key,
                groq_base_url=workers.news_world_brief.groq_base_url,
                groq_model=workers.news_world_brief.groq_model,
                groq_api_key=settings.llm.groq_api_key,
                total_timeout_seconds=workers.news_world_brief.total_timeout_seconds,
            ),
        )
    else:
        constructed["news_world_brief"] = disabled_worker(ctx, "news_world_brief")
    return constructed


__all__ = ["construct_news_workers"]
