from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WorkerManifest:
    name: str
    start_priority: int
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
        queue_tables=("asset_profile_refresh_targets",),
    ),
    WorkerManifest(
        name="token_radar_projection",
        start_priority=80,
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
        name="macro_settlements",
        start_priority=75,
        queue_tables=("macro_acquisition_targets",),
    ),
    WorkerManifest(
        name="macro_economic_releases",
        start_priority=76,
        queue_tables=("macro_acquisition_targets",),
    ),
    WorkerManifest(
        name="macro_official_state",
        start_priority=77,
        queue_tables=("macro_acquisition_targets",),
    ),
    WorkerManifest(
        name="macro_official_documents",
        start_priority=78,
        queue_tables=("macro_acquisition_targets",),
    ),
    WorkerManifest(
        name="macro_backfill",
        start_priority=80,
        queue_tables=("macro_acquisition_targets",),
    ),
    WorkerManifest(
        name="macro_projection",
        start_priority=81,
        current_read_model_identities=(
            ("macro_feature_series", ("feature_id", "as_of_date")),
            ("macro_module_current", ("module_id",)),
        ),
    ),
    WorkerManifest(
        name="macro_judgment",
        start_priority=82,
        current_read_model_identities=(("macro_daily_judgments", ("session_date",)),),
    ),
    WorkerManifest(
        name="token_image_mirror",
        start_priority=82,
        queue_tables=("token_image_source_dirty_targets",),
    ),
    WorkerManifest(
        name="token_profile_current",
        start_priority=85,
        queue_tables=("token_profile_current_dirty_targets",),
        current_read_model_identities=(("token_profile_current", ("target_type", "target_id")),),
    ),
    WorkerManifest(
        name="news_pipeline",
        start_priority=90,
        current_read_model_identities=(
            ("news_items", ("source_id", "source_item_key")),
            ("news_stories", ("story_id",)),
            ("news_story_members", ("story_id", "item_id")),
        ),
    ),
    WorkerManifest(
        name="news_world_brief",
        start_priority=95,
        queue_tables=("news_brief_runs",),
        current_read_model_identities=(("news_brief_current", ("singleton_key",)),),
    ),
    WorkerManifest(
        name="macro_research",
        start_priority=100,
        queue_tables=("macro_research_runs",),
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


def worker_queue_tables() -> dict[str, tuple[str, ...]]:
    return {manifest.name: manifest.queue_tables for manifest in _WORKER_MANIFESTS if manifest.queue_tables}


def worker_names() -> tuple[str, ...]:
    return tuple(manifest.name for manifest in _WORKER_MANIFESTS)
