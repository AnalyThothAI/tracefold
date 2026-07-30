from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WorkerManifest:
    name: str
    queue_tables: tuple[str, ...] = ()
    current_read_model_identities: tuple[tuple[str, tuple[str, ...]], ...] = ()


_WORKER_MANIFESTS: tuple[WorkerManifest, ...] = (
    WorkerManifest(name="collector"),
    WorkerManifest(name="market_tick_stream"),
    WorkerManifest(name="market_tick_poll"),
    WorkerManifest(
        name="event_anchor_capture",
        queue_tables=("event_anchor_backfill_jobs",),
    ),
    WorkerManifest(
        name="resolution_refresh",
        queue_tables=("token_discovery_dirty_lookup_keys",),
    ),
    WorkerManifest(
        name="macro_intraday_market",
        queue_tables=("macro_acquisition_targets",),
    ),
    WorkerManifest(
        name="macro_settlements",
        queue_tables=("macro_acquisition_targets",),
    ),
    WorkerManifest(
        name="macro_economic_releases",
        queue_tables=("macro_acquisition_targets",),
    ),
    WorkerManifest(
        name="macro_official_state",
        queue_tables=("macro_acquisition_targets",),
    ),
    WorkerManifest(
        name="macro_official_documents",
        queue_tables=("macro_acquisition_targets",),
    ),
    WorkerManifest(name="news_ingest"),
    WorkerManifest(
        name="asset_profile_refresh",
        queue_tables=("asset_profile_refresh_targets",),
    ),
    WorkerManifest(
        name="token_image_mirror",
        queue_tables=("token_image_source_dirty_targets",),
    ),
    WorkerManifest(
        name="steady_projection_coordinator",
        queue_tables=(
            "radar_projection_frontiers",
            "token_profile_projection_frontiers",
            "macro_module_frontiers",
            "news_projection_frontiers",
        ),
        current_read_model_identities=(
            (
                "token_radar_current_rows",
                ("projection_version", "window", "venue", "lane", "target_type_key", "identity_id"),
            ),
            ("macro_module_current", ("module_id",)),
            ("news_stories", ("story_id",)),
            ("news_story_members", ("story_id", "item_id")),
            ("token_profile_current", ("target_type", "target_id")),
        ),
    ),
    WorkerManifest(
        name="model_generation_coordinator",
        queue_tables=("model_generation_frontiers",),
        current_read_model_identities=(
            ("news_brief_current", ("singleton_key",)),
            ("macro_thesis_publications", ("session_date",)),
        ),
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


def worker_queue_tables() -> dict[str, tuple[str, ...]]:
    return {manifest.name: manifest.queue_tables for manifest in _WORKER_MANIFESTS if manifest.queue_tables}


def worker_names() -> tuple[str, ...]:
    return tuple(manifest.name for manifest in _WORKER_MANIFESTS)
