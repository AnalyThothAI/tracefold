from __future__ import annotations

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from tracefold.app.llm import (
    configured_chat_model,
    litellm_proxy_model_name,
    llm_is_configured,
)
from tracefold.integrations.deepagents.fed_document_analysis import FedDocumentAnalysisAgent
from tracefold.integrations.deepagents.macro_thesis_deepagent import (
    MacroThesisDeepAgent,
    MacroThesisIndependentReviewer,
    require_supported_macro_thesis_model,
)
from tracefold.integrations.macro_sources import MacroSourceClient
from tracefold.macro import (
    MacroAcquisitionWorker,
    MacroDocumentAnalysisService,
    MacroDocumentAnalysisWorker,
    MacroProjectionWorker,
    MacroThesisService,
    MacroThesisWorker,
)
from tracefold.platform.postgres.postgres_client import (
    local_docker_host_dsn,
    with_password_from_file,
)
from tracefold.platform.workers.factory import WorkerFactoryContext, disabled_worker, unavailable_worker
from tracefold.platform.workers.worker_base import WorkerBase

_ACQUISITION_WORKERS = {
    "macro_intraday_market": "intraday_market",
    "macro_settlements": "daily_settlement",
    "macro_economic_releases": "scheduled_release",
    "macro_official_state": "official_state",
    "macro_official_documents": "official_document",
    "macro_backfill": "backfill",
}


def construct_macro_workers(ctx: WorkerFactoryContext) -> dict[str, WorkerBase]:
    constructed: dict[str, WorkerBase] = {}
    source_config = ctx.settings.providers.macro_sources
    for worker_name, clock_kind in _ACQUISITION_WORKERS.items():
        worker_settings = getattr(ctx.settings.workers, worker_name)
        if not worker_settings.enabled or not source_config.enabled:
            constructed[worker_name] = disabled_worker(ctx, worker_name)
            continue
        constructed[worker_name] = MacroAcquisitionWorker(
            name=worker_name,
            clock_kind=clock_kind,
            settings=worker_settings,
            db=ctx.db,
            telemetry=ctx.telemetry,
            source_client=MacroSourceClient(
                timeout_seconds=float(source_config.request_timeout_seconds),
                user_agent=str(source_config.user_agent),
                fred_enabled=source_config.fred_enabled,
                cboe_enabled=source_config.cboe_enabled,
                cftc_enabled=source_config.cftc_enabled,
                nasdaq_daily_enabled=source_config.nasdaq_daily_enabled,
                yfinance_enabled=source_config.yfinance_enabled,
            ),
        )

    projection_settings = ctx.settings.workers.macro_projection
    if projection_settings.enabled:
        constructed["macro_projection"] = MacroProjectionWorker(
            name="macro_projection",
            settings=projection_settings,
            backfill_worker_enabled=bool(ctx.settings.workers.macro_backfill.enabled),
            db=ctx.db,
            telemetry=ctx.telemetry,
        )
    else:
        constructed["macro_projection"] = disabled_worker(ctx, "macro_projection")

    analysis_settings = ctx.settings.workers.macro_document_analysis
    if not analysis_settings.enabled:
        constructed["macro_document_analysis"] = disabled_worker(ctx, "macro_document_analysis")
    elif not llm_is_configured(ctx.settings):
        constructed["macro_document_analysis"] = unavailable_worker(
            ctx,
            "macro_document_analysis",
            "llm_not_configured",
        )
    else:
        model, effective_model = configured_chat_model(ctx.settings, analysis_settings)
        analysis_agent = FedDocumentAnalysisAgent(model=model, model_name=effective_model)
        analysis_service = MacroDocumentAnalysisService(
            db=ctx.db,
            settings=analysis_settings,
            agent=analysis_agent,
            worker_name="macro_document_analysis",
        )
        constructed["macro_document_analysis"] = MacroDocumentAnalysisWorker(
            name="macro_document_analysis",
            settings=analysis_settings,
            db=ctx.db,
            telemetry=ctx.telemetry,
            service=analysis_service,
        )

    thesis_settings = ctx.settings.workers.macro_thesis
    if not thesis_settings.enabled:
        constructed["macro_thesis"] = disabled_worker(ctx, "macro_thesis")
    else:
        effective_model = litellm_proxy_model_name(
            thesis_settings.model,
            base_url=ctx.settings.llm.base_url,
        )
        effective_reviewer_model = litellm_proxy_model_name(
            thesis_settings.reviewer_model,
            base_url=ctx.settings.llm.base_url,
        )
        configuration_error: str | None = None
        if not llm_is_configured(ctx.settings):
            configuration_error = "llm_not_configured"
        else:
            try:
                require_supported_macro_thesis_model(effective_model)
                require_supported_macro_thesis_model(effective_reviewer_model)
            except ValueError as exc:
                configuration_error = str(exc)
        agent = None
        reviewer = None
        if configuration_error is None:
            model, effective_model = configured_chat_model(ctx.settings, thesis_settings)
            reviewer_settings = thesis_settings.model_copy(update={"model": thesis_settings.reviewer_model})
            reviewer_model, effective_reviewer_model = configured_chat_model(ctx.settings, reviewer_settings)
            checkpoint_dsn = _checkpoint_dsn(ctx)

            def checkpointer_context_factory() -> object:
                return AsyncPostgresSaver.from_conn_string(checkpoint_dsn)

            agent = MacroThesisDeepAgent(
                model=model,
                model_name=effective_model,
                checkpointer_context_factory=checkpointer_context_factory,
                graph_recursion_limit=thesis_settings.graph_recursion_limit,
            )
            reviewer = MacroThesisIndependentReviewer(
                model=reviewer_model,
                model_name=effective_reviewer_model,
                checkpointer_context_factory=checkpointer_context_factory,
                graph_recursion_limit=thesis_settings.graph_recursion_limit,
            )
        service = MacroThesisService(
            db=ctx.db,
            settings=thesis_settings,
            agent=agent,
            reviewer=reviewer,
            configuration_error=configuration_error,
            backfill_worker_enabled=bool(ctx.settings.workers.macro_backfill.enabled),
            worker_name="macro_thesis",
        )
        constructed["macro_thesis"] = MacroThesisWorker(
            name="macro_thesis",
            settings=thesis_settings,
            db=ctx.db,
            telemetry=ctx.telemetry,
            service=service,
        )
    return constructed


def _checkpoint_dsn(ctx: WorkerFactoryContext) -> str:
    postgres = ctx.settings.storage.postgres
    return local_docker_host_dsn(
        with_password_from_file(
            postgres.dsn,
            ctx.settings.postgres_password_file,
        )
    )


__all__ = ["construct_macro_workers"]
