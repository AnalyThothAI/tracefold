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

_NEWS_BRIEF_TOTAL_TIMEOUT_SECONDS = 60.0
_NEWS_OLLAMA_BASE_URL = "http://host.docker.internal:11434/v1"
_NEWS_OLLAMA_MODEL = "deepseek-r1:8b"
_NEWS_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_NEWS_OPENROUTER_MODEL = "deepseek/deepseek-v4-flash"
_NEWS_GROQ_BASE_URL = "https://api.groq.com/openai/v1"
_NEWS_GROQ_MODEL = "llama-3.3-70b-versatile"
_DOCUMENT_MODEL_TIMEOUT_SECONDS = 180.0
_THESIS_MODEL_TIMEOUT_SECONDS = 480.0
_MODEL_MAX_TOKENS = 6_000


def construct_coordinator_workers(ctx: WorkerFactoryContext) -> dict[str, WorkerBase]:
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
            candidates=projection_candidates,
            telemetry=ctx.telemetry,
        ),
        "model_generation_coordinator": ModelGenerationCoordinator(
            name="model_generation_coordinator",
            db=ctx.db,
            telemetry=ctx.telemetry,
            runtime_id=ctx.runtime_id,
            resources=ctx.resources,
            runners=_construct_model_runners(ctx),
        ),
    }


def _construct_model_runners(ctx: WorkerFactoryContext) -> dict[str, WorkerBase]:
    settings = ctx.settings
    runners: dict[str, WorkerBase] = {}

    if settings.news.enabled:
        configured_base_url = settings.llm.base_url or ("https://api.deepseek.com/v1" if settings.llm.api_key else "")
        runners["news_brief"] = NewsWorldBriefWorker(
            name="news_brief_candidate",
            db=ctx.db,
            telemetry=ctx.telemetry,
            publisher=ProviderChainNewsBriefPublisher(
                configured_base_url=configured_base_url,
                configured_api_key=settings.llm.api_key,
                configured_model=settings.llm.news_brief_model,
                ollama_base_url=_NEWS_OLLAMA_BASE_URL,
                ollama_model=_NEWS_OLLAMA_MODEL,
                openrouter_base_url=_NEWS_OPENROUTER_BASE_URL,
                openrouter_model=_NEWS_OPENROUTER_MODEL,
                openrouter_api_key=settings.llm.openrouter_api_key,
                groq_base_url=_NEWS_GROQ_BASE_URL,
                groq_model=_NEWS_GROQ_MODEL,
                groq_api_key=settings.llm.groq_api_key,
                total_timeout_seconds=_NEWS_BRIEF_TOTAL_TIMEOUT_SECONDS,
            ),
            resources=ctx.resources,
            runtime_id=ctx.runtime_id,
        )

    if settings.llm.macro_document_analysis_enabled and llm_is_configured(settings):
        model, effective_model = configured_chat_model(
            settings,
            model_name=settings.llm.macro_document_analysis_model,
            request_timeout_seconds=_DOCUMENT_MODEL_TIMEOUT_SECONDS,
            max_tokens=_MODEL_MAX_TOKENS,
        )
        document_service = MacroDocumentAnalysisService(
            db=ctx.db,
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
            telemetry=ctx.telemetry,
            service=document_service,
        )

    if settings.llm.macro_thesis_enabled:
        effective_model = litellm_proxy_model_name(
            settings.llm.macro_thesis_model,
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
                model_name=settings.llm.macro_thesis_model,
                request_timeout_seconds=_THESIS_MODEL_TIMEOUT_SECONDS,
                max_tokens=_MODEL_MAX_TOKENS,
            )
            thesis_agent = MacroThesisDeepAgent(
                model=model,
                model_name=effective_model,
            )
        thesis_service = MacroThesisService(
            db=ctx.db,
            agent=thesis_agent,
            configuration_error=configuration_error,
            worker_name="model_generation_coordinator",
            lease_owner=f"model_generation_coordinator:{ctx.runtime_id}",
            resources=ctx.resources,
        )
        runners["macro_thesis"] = MacroThesisWorker(
            name="macro_thesis_candidate",
            telemetry=ctx.telemetry,
            service=thesis_service,
        )
    return runners


__all__ = ["construct_coordinator_workers"]
