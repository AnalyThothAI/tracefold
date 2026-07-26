from __future__ import annotations

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from tracefold.app.llm import configured_chat_model, llm_is_configured
from tracefold.integrations.deepagents.macro_research_deepagent import (
    MacroResearchDeepAgent,
)
from tracefold.integrations.macrodata.runner import MacrodataBundleRunner
from tracefold.macro import (
    CompletedSessionMacro,
    MacroResearchWorker,
    MacroSyncWorker,
    PostgresMacroResearchReadPort,
)
from tracefold.platform.postgres.postgres_client import (
    local_docker_host_dsn,
    with_password_from_file,
)
from tracefold.platform.workers.factory import WorkerFactoryContext, disabled_worker, unavailable_worker
from tracefold.platform.workers.worker_base import WorkerBase


def construct_macro_workers(ctx: WorkerFactoryContext) -> dict[str, WorkerBase]:
    constructed: dict[str, WorkerBase] = {}
    workers = ctx.settings.workers
    if not workers.macro_sync.enabled:
        constructed["macro_sync"] = disabled_worker(ctx, "macro_sync")
    elif ctx.settings.providers.macrodata.enabled:
        worker_name = "macro_sync"
        constructed[worker_name] = MacroSyncWorker(
            name=worker_name,
            settings=workers.macro_sync,
            db=ctx.db,
            telemetry=ctx.telemetry,
            settings_root=ctx.settings,
            runner=MacrodataBundleRunner(settings=ctx.settings),
        )
    else:
        constructed["macro_sync"] = disabled_worker(ctx, "macro_sync")
    if not workers.macro_research.enabled:
        constructed["macro_research"] = disabled_worker(ctx, "macro_research")
    elif not llm_is_configured(ctx.settings):
        constructed["macro_research"] = unavailable_worker(
            ctx,
            "macro_research",
            "llm_not_configured",
        )
    else:
        worker_name = "macro_research"
        research_settings = workers.macro_research
        model, effective_model = configured_chat_model(ctx.settings, research_settings)
        reader = PostgresMacroResearchReadPort(
            db=ctx.db,
            worker_name=worker_name,
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
            worker_name=worker_name,
        )
        constructed[worker_name] = MacroResearchWorker(
            name=worker_name,
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
