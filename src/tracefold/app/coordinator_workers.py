from __future__ import annotations

from tracefold.app.llm import (
    configured_chat_model,
    litellm_proxy_model_name,
    llm_is_configured,
)
from tracefold.app.model_generation_coordinator import ModelGenerationCoordinator
from tracefold.app.projection_coordinator import (
    SteadyProjectionCoordinator,
)
from tracefold.integrations.deepagents.fed_document_analysis import (
    FedDocumentAnalysisAgent,
)
from tracefold.integrations.deepagents.macro_thesis_deepagent import (
    MacroThesisDeepAgent,
    require_supported_macro_thesis_model,
)
from tracefold.integrations.news_ai import ProviderChainNewsBriefPublisher
from tracefold.macro import (
    MacroDocumentAnalysisService,
    MacroDocumentAnalysisWorker,
    MacroProjectionCandidate,
    MacroThesisService,
    MacroThesisWorker,
)
from tracefold.market import (
    ProfileProjectionCandidate,
    RadarProjectionCandidate,
)
from tracefold.news import (
    NewsProjectionCandidate,
    NewsWorldBriefWorker,
)
from tracefold.platform.workers.factory import WorkerFactoryContext
from tracefold.platform.workers.worker_base import WorkerBase


def construct_coordinator_workers(ctx: WorkerFactoryContext) -> dict[str, WorkerBase]:
    policies = ctx.settings.workers
    projection_candidates = (
        RadarProjectionCandidate(
            db=ctx.db,
            resources=ctx.resources,
            runtime_id=ctx.runtime_id,
            stable_order=10,
        ),
        ProfileProjectionCandidate(
            db=ctx.db,
            resources=ctx.resources,
            runtime_id=ctx.runtime_id,
            stable_order=20,
        ),
        MacroProjectionCandidate(
            settings=policies.macro_projection,
            db=ctx.db,
            resources=ctx.resources,
            runtime_id=ctx.runtime_id,
            stable_order=30,
        ),
        NewsProjectionCandidate(
            db=ctx.db,
            resources=ctx.resources,
            runtime_id=ctx.runtime_id,
            stable_order=40,
        ),
    )
    return {
        "steady_projection_coordinator": SteadyProjectionCoordinator(
            name="steady_projection_coordinator",
            settings=policies.steady_projection_coordinator,
            candidates=projection_candidates,
            db=ctx.db,
            telemetry=ctx.telemetry,
        ),
        "model_generation_coordinator": ModelGenerationCoordinator(
            name="model_generation_coordinator",
            settings=policies.model_generation_coordinator,
            db=ctx.db,
            telemetry=ctx.telemetry,
            runtime_id=ctx.runtime_id,
            runners=_construct_model_runners(ctx),
        ),
    }


def _construct_model_runners(ctx: WorkerFactoryContext) -> dict[str, WorkerBase]:
    settings = ctx.settings
    policies = settings.workers
    runners: dict[str, WorkerBase] = {}

    brief_settings = policies.news_world_brief
    if settings.news.enabled and brief_settings.enabled:
        configured_base_url = settings.llm.base_url or ("https://api.deepseek.com/v1" if settings.llm.api_key else "")
        runners["news_brief"] = NewsWorldBriefWorker(
            name="news_brief_candidate",
            settings=brief_settings,
            db=ctx.db,
            telemetry=ctx.telemetry,
            publisher=ProviderChainNewsBriefPublisher(
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
                total_timeout_seconds=brief_settings.total_timeout_seconds,
            ),
        )

    document_settings = policies.macro_document_analysis
    if document_settings.enabled and llm_is_configured(settings):
        model, effective_model = configured_chat_model(
            settings,
            document_settings,
        )
        document_service = MacroDocumentAnalysisService(
            db=ctx.db,
            settings=document_settings,
            agent=FedDocumentAnalysisAgent(
                model=model,
                model_name=effective_model,
            ),
            worker_name="model_generation_coordinator",
            lease_owner=f"model_generation_coordinator:{ctx.runtime_id}",
            resources=ctx.resources,
        )
        runners["macro_document_analysis"] = MacroDocumentAnalysisWorker(
            name="macro_document_analysis_candidate",
            settings=document_settings,
            db=ctx.db,
            telemetry=ctx.telemetry,
            service=document_service,
        )

    thesis_settings = policies.macro_thesis
    if thesis_settings.enabled:
        effective_model = litellm_proxy_model_name(
            thesis_settings.model,
            base_url=settings.llm.base_url,
        )
        configuration_error: str | None = None
        if not llm_is_configured(settings):
            configuration_error = "llm_not_configured"
        else:
            try:
                require_supported_macro_thesis_model(effective_model)
            except ValueError as exc:
                configuration_error = str(exc)
        thesis_agent = None
        if configuration_error is None:
            model, effective_model = configured_chat_model(
                settings,
                thesis_settings,
            )
            thesis_agent = MacroThesisDeepAgent(
                model=model,
                model_name=effective_model,
            )
        thesis_service = MacroThesisService(
            db=ctx.db,
            settings=thesis_settings,
            agent=thesis_agent,
            configuration_error=configuration_error,
            backfill_worker_enabled=False,
            worker_name="model_generation_coordinator",
            lease_owner=f"model_generation_coordinator:{ctx.runtime_id}",
            resources=ctx.resources,
        )
        runners["macro_thesis"] = MacroThesisWorker(
            name="macro_thesis_candidate",
            settings=thesis_settings,
            db=ctx.db,
            telemetry=ctx.telemetry,
            service=thesis_service,
        )
    return runners


__all__ = ["construct_coordinator_workers"]
