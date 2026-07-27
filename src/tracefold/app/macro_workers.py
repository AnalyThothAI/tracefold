from __future__ import annotations

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from tracefold.app.llm import configured_chat_model, llm_is_configured
from tracefold.integrations.deepagents.macro_research_deepagent import MacroResearchDeepAgent
from tracefold.integrations.macro_sources import MacroSourceClient
from tracefold.macro import (
    CompletedSessionMacro,
    MacroAcquisitionWorker,
    MacroJudgmentWorker,
    MacroProjectionWorker,
    MacroResearchWorker,
    PostgresMacroResearchReadPort,
)
from tracefold.platform.postgres.postgres_client import (
    local_docker_host_dsn,
    with_password_from_file,
)
from tracefold.platform.workers.factory import WorkerFactoryContext, disabled_worker, unavailable_worker
from tracefold.platform.workers.worker_base import WorkerBase

_ACQUISITION_WORKERS = {
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
                nasdaq_public_enabled=source_config.nasdaq_public_enabled,
            ),
        )

    projection_settings = ctx.settings.workers.macro_projection
    if projection_settings.enabled:
        constructed["macro_projection"] = MacroProjectionWorker(
            name="macro_projection",
            settings=projection_settings,
            db=ctx.db,
            telemetry=ctx.telemetry,
        )
    else:
        constructed["macro_projection"] = disabled_worker(ctx, "macro_projection")

    judgment_settings = ctx.settings.workers.macro_judgment
    if judgment_settings.enabled:
        constructed["macro_judgment"] = MacroJudgmentWorker(
            name="macro_judgment",
            settings=judgment_settings,
            db=ctx.db,
            telemetry=ctx.telemetry,
        )
    else:
        constructed["macro_judgment"] = disabled_worker(ctx, "macro_judgment")

    research_settings = ctx.settings.workers.macro_research
    if not research_settings.enabled:
        constructed["macro_research"] = disabled_worker(ctx, "macro_research")
    elif not llm_is_configured(ctx.settings):
        constructed["macro_research"] = unavailable_worker(
            ctx,
            "macro_research",
            "llm_not_configured",
        )
    else:
        model, effective_model = configured_chat_model(ctx.settings, research_settings)
        reader = PostgresMacroResearchReadPort(
            db=ctx.db,
            worker_name="macro_research",
            statement_timeout_seconds=research_settings.statement_timeout_seconds,
        )
        checkpoint_dsn = _checkpoint_dsn(ctx)
        agent = MacroResearchDeepAgent(
            model=model,
            model_name=effective_model,
            reader=reader,
            checkpointer_context_factory=lambda: AsyncPostgresSaver.from_conn_string(
                checkpoint_dsn,
            ),
            workspace_root=ctx.settings.app_home / "macro-agent-workspaces",
        )
        completed_session_macro = CompletedSessionMacro(
            db=ctx.db,
            settings=research_settings,
            agent=agent,
            worker_name="macro_research",
        )
        constructed["macro_research"] = MacroResearchWorker(
            name="macro_research",
            settings=research_settings,
            db=ctx.db,
            telemetry=ctx.telemetry,
            completed_session_macro=completed_session_macro,
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
