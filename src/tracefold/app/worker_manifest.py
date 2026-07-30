from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WorkerManifest:
    name: str
    start_priority: int
    start_phase: int = 0
    iteration_group: str | None = None
    queue_tables: tuple[str, ...] = ()
    current_read_model_identities: tuple[tuple[str, tuple[str, ...]], ...] = ()


_WORKER_MANIFESTS: tuple[WorkerManifest, ...] = (
    WorkerManifest(
        name="collector",
        start_priority=10,
    ),
    WorkerManifest(
        name="market_tick_stream",
        start_priority=30,
    ),
    WorkerManifest(
        name="market_tick_poll",
        start_priority=40,
    ),
    WorkerManifest(
        name="event_anchor_backfill",
        start_priority=45,
        start_phase=2,
        queue_tables=("event_anchor_backfill_jobs",),
    ),
    WorkerManifest(
        name="resolution_refresh",
        start_priority=60,
        queue_tables=("token_discovery_dirty_lookup_keys",),
    ),
    WorkerManifest(
        name="asset_profile_refresh",
        start_priority=70,
        start_phase=2,
        iteration_group="background_projection",
        queue_tables=("asset_profile_refresh_targets",),
    ),
    WorkerManifest(
        name="token_radar_projection",
        start_priority=80,
        iteration_group="latency_projection",
        queue_tables=("token_radar_dirty_targets",),
        current_read_model_identities=(
            (
                "token_radar_rank_source_events",
                ("projection_version", "target_type_key", "identity_id", "source_kind", "source_id"),
            ),
            (
                "token_radar_target_features",
                ("projection_version", "window", "lane", "target_type_key", "identity_id"),
            ),
            (
                "token_radar_current_rows",
                ("projection_version", "window", "venue", "lane", "target_type_key", "identity_id"),
            ),
            (
                "token_radar_publication_state",
                ("projection_version", "window", "venue"),
            ),
            (
                "token_radar_target_first_seen",
                ("projection_version", "window", "venue", "target_type_key", "identity_id"),
            ),
        ),
    ),
    WorkerManifest(
        name="macro_intraday_market",
        start_priority=74,
        start_phase=1,
        queue_tables=("macro_acquisition_targets",),
    ),
    WorkerManifest(
        name="macro_settlements",
        start_priority=75,
        start_phase=1,
        queue_tables=("macro_acquisition_targets",),
    ),
    WorkerManifest(
        name="macro_economic_releases",
        start_priority=76,
        start_phase=1,
        queue_tables=("macro_acquisition_targets",),
    ),
    WorkerManifest(
        name="macro_official_state",
        start_priority=77,
        start_phase=1,
        queue_tables=("macro_acquisition_targets",),
    ),
    WorkerManifest(
        name="macro_official_documents",
        start_priority=78,
        start_phase=1,
        queue_tables=("macro_acquisition_targets",),
    ),
    WorkerManifest(
        name="macro_backfill",
        start_priority=80,
        start_phase=2,
        queue_tables=("macro_acquisition_targets",),
    ),
    WorkerManifest(
        name="macro_document_analysis",
        start_priority=80,
        start_phase=2,
        queue_tables=("macro_document_analysis_jobs",),
    ),
    WorkerManifest(
        name="macro_projection",
        start_priority=81,
        start_phase=1,
        iteration_group="background_projection",
        current_read_model_identities=(
            ("macro_feature_series", ("feature_id", "as_of_date")),
            ("macro_module_current", ("module_id",)),
            ("macro_projection_state", ("singleton_key",)),
        ),
    ),
    WorkerManifest(
        name="token_image_mirror",
        start_priority=82,
        start_phase=2,
        iteration_group="background_projection",
        queue_tables=("token_image_source_dirty_targets",),
    ),
    WorkerManifest(
        name="token_profile_current",
        start_priority=85,
        start_phase=2,
        iteration_group="background_projection",
        queue_tables=("token_profile_current_dirty_targets",),
        current_read_model_identities=(("token_profile_current", ("target_type", "target_id")),),
    ),
    WorkerManifest(
        name="news_pipeline",
        start_priority=90,
        start_phase=1,
        iteration_group="background_projection",
        current_read_model_identities=(
            ("news_items", ("source_id", "source_item_key")),
            ("news_stories", ("story_id",)),
            ("news_story_members", ("story_id", "item_id")),
            ("news_story_input_state", ("singleton_key",)),
        ),
    ),
    WorkerManifest(
        name="news_world_brief",
        start_priority=95,
        start_phase=2,
        queue_tables=("news_brief_runs",),
        current_read_model_identities=(("news_brief_current", ("singleton_key",)),),
    ),
    WorkerManifest(
        name="macro_thesis",
        start_priority=100,
        start_phase=2,
        queue_tables=("macro_thesis_runs",),
        current_read_model_identities=(("macro_thesis_publications", ("session_date",)),),
    ),
)


def all_worker_manifests() -> tuple[WorkerManifest, ...]:
    return _WORKER_MANIFESTS


def manifest_by_name() -> dict[str, WorkerManifest]:
    return {manifest.name: manifest for manifest in _WORKER_MANIFESTS}


def require_worker_manifest(name: str) -> WorkerManifest:
    try:
        return manifest_by_name()[str(name)]
    except KeyError as exc:
        raise ValueError(f"unknown worker manifest: {name}") from exc


def worker_start_priority() -> dict[str, int]:
    return {manifest.name: manifest.start_priority for manifest in _WORKER_MANIFESTS}


def worker_start_phase() -> dict[str, int]:
    return {manifest.name: manifest.start_phase for manifest in _WORKER_MANIFESTS}


def worker_iteration_group() -> dict[str, str]:
    return {
        manifest.name: manifest.iteration_group
        for manifest in _WORKER_MANIFESTS
        if manifest.iteration_group is not None
    }


def worker_queue_tables() -> dict[str, tuple[str, ...]]:
    return {manifest.name: manifest.queue_tables for manifest in _WORKER_MANIFESTS if manifest.queue_tables}


def worker_names() -> tuple[str, ...]:
    return tuple(manifest.name for manifest in _WORKER_MANIFESTS)
