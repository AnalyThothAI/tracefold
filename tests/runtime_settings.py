from __future__ import annotations

from tracefold.platform.config.settings import WorkersSettings


def workers_settings_with_enabled(*enabled_workers: str) -> WorkersSettings:
    workers = WorkersSettings()
    return workers.model_copy(
        update={
            name: getattr(workers, name).model_copy(update={"enabled": name in enabled_workers})
            for name in WorkersSettings.model_fields
        }
    )


def disabled_workers_settings() -> WorkersSettings:
    return workers_settings_with_enabled()


def runtime_workers_settings() -> WorkersSettings:
    workers = WorkersSettings()
    disabled_macro_workers = (
        "macro_settlements",
        "macro_economic_releases",
        "macro_official_state",
        "macro_official_documents",
        "macro_backfill",
        "macro_projection",
        "macro_judgment",
        "macro_research",
    )
    return workers.model_copy(
        update={name: getattr(workers, name).model_copy(update={"enabled": False}) for name in disabled_macro_workers}
    )
