from __future__ import annotations

from pathlib import Path
from typing import Any

from tracefold.app.provider_ownership import configured_profile_provider_ids
from tracefold.macro import (
    rebuild_all_macro_modules_for_maintenance,
)
from tracefold.market import (
    TOKEN_RADAR_PROJECTION_VERSION,
    TOKEN_RADAR_VENUES,
    WINDOW_MS,
    rebuild_all_profiles_for_maintenance,
    rebuild_all_token_radar_for_maintenance,
)
from tracefold.news import rebuild_all_news_for_maintenance
from tracefold.platform.postgres.postgres_audit import (
    PostgresOperationalAudit,
    ProjectionValidationAudit,
)


def rebuild_hard_cut_read_models(
    *,
    db: Any,
    settings: Any,
    now_ms: int,
) -> dict[str, Any]:
    """Run the four explicit cutover rebuilds while maintenance owns the gate."""

    radar = rebuild_all_token_radar_for_maintenance(
        db=db,
        now_ms=int(now_ms),
    )
    news = rebuild_all_news_for_maintenance(
        db=db,
        now_ms=int(now_ms),
    )
    macro = rebuild_all_macro_modules_for_maintenance(
        db=db,
        now_ms=int(now_ms),
    )
    profile = rebuild_all_profiles_for_maintenance(
        db=db,
        app_home=Path(settings.app_home),
        active_profile_provider_ids=configured_profile_provider_ids(settings),
        now_ms=int(now_ms),
    )
    audits = run_hard_cut_audits(db=db)
    if not audits["ok"]:
        raise RuntimeError("hard_cut_audit_failed:" + ",".join(str(reason) for reason in audits["reasons"]))
    return {
        "status": "rebuilt_and_audited",
        "radar": radar,
        "news": news,
        "macro": macro,
        "profile": profile,
        "audits": audits,
    }


def run_hard_cut_audits(*, db: Any) -> dict[str, Any]:
    with db.worker_session(
        "hard_cut_audit",
        statement_timeout_seconds=30.0,
    ) as repos:
        operational = PostgresOperationalAudit(repos.conn).run()
        projections = ProjectionValidationAudit(repos.conn).run(sample=100)
        invariants = _hard_cut_invariants(repos.conn)
    reasons = [
        name
        for name, result in (
            ("postgres_operational", operational),
            ("projection_validation", projections),
            ("hard_cut_invariants", invariants),
        )
        if not bool(result.get("ok"))
    ]
    return {
        "ok": not reasons,
        "reasons": reasons,
        "postgres_operational": operational,
        "projection_validation": projections,
        "hard_cut_invariants": invariants,
    }


def _hard_cut_invariants(conn: Any) -> dict[str, Any]:
    row = dict(
        conn.execute(
            """
            SELECT
              (
                SELECT count(*)
                FROM (
                  SELECT status FROM radar_projection_frontiers
                  UNION ALL
                  SELECT status FROM token_profile_projection_frontiers
                  UNION ALL
                  SELECT status FROM macro_module_frontiers
                ) projection
                WHERE status = 'quarantined'
              ) AS projection_quarantines,
              (
                SELECT count(*)
                FROM token_profile_current profile
                WHERE NOT EXISTS (
                  SELECT 1
                  FROM token_radar_current_rows radar
                  WHERE radar.projection_version = %(projection_version)s
                    AND radar."window" = ANY(%(windows)s)
                    AND radar.venue = ANY(%(venues)s)
                    AND radar.target_type_key = profile.target_type
                    AND radar.identity_id = profile.target_id
                )
              ) AS outside_serving_profiles,
              (
                SELECT count(*)
                FROM asset_profile_refresh_targets target
                WHERE NOT EXISTS (
                  SELECT 1
                  FROM token_radar_current_rows radar
                  WHERE radar.projection_version = %(projection_version)s
                    AND radar."window" = ANY(%(windows)s)
                    AND radar.venue = ANY(%(venues)s)
                    AND radar.target_type_key = target.target_type
                    AND radar.identity_id = target.target_id
                )
              ) AS outside_serving_profile_refresh,
              (
                SELECT count(*)
                FROM token_image_source_dirty_targets target
                WHERE NOT EXISTS (
                  SELECT 1
                  FROM token_radar_current_rows radar
                  WHERE radar.projection_version = %(projection_version)s
                    AND radar."window" = ANY(%(windows)s)
                    AND radar.venue = ANY(%(venues)s)
                    AND radar.target_type_key = target.target_type
                    AND radar.identity_id = target.target_id
                )
              ) AS outside_serving_image_refresh,
              (
                SELECT count(*)
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND state = 'idle in transaction'
                  AND now() - xact_start > interval '1 second'
              ) AS idle_transactions_over_one_second,
              to_regclass('public.token_radar_dirty_targets') IS NOT NULL
                AS old_radar_queue_exists,
              to_regclass('public.token_profile_current_dirty_targets') IS NOT NULL
                AS old_profile_queue_exists,
              to_regclass('public.token_radar_rank_source_events') IS NOT NULL
                AS old_radar_source_table_exists,
              to_regclass('public.news_identity_features') IS NOT NULL
                AS old_news_identity_features_exists,
              to_regclass('public.news_similarity_edges') IS NOT NULL
                AS old_news_similarity_edges_exists,
              to_regclass('public.news_story_aliases') IS NOT NULL
                AS old_news_story_aliases_exists,
              to_regclass('public.news_story_input_state') IS NOT NULL
                AS old_news_story_input_state_exists,
              to_regclass('public.news_projection_frontiers') IS NOT NULL
                AS old_news_projection_frontiers_exists
            """,
            {
                "projection_version": TOKEN_RADAR_PROJECTION_VERSION,
                "windows": list(WINDOW_MS),
                "venues": list(TOKEN_RADAR_VENUES),
            },
        ).fetchone()
    )
    failures = [
        name
        for name in (
            "projection_quarantines",
            "outside_serving_profiles",
            "outside_serving_profile_refresh",
            "outside_serving_image_refresh",
            "idle_transactions_over_one_second",
        )
        if int(row[name] or 0) != 0
    ]
    failures.extend(
        name
        for name in (
            "old_radar_queue_exists",
            "old_profile_queue_exists",
            "old_radar_source_table_exists",
            "old_news_identity_features_exists",
            "old_news_similarity_edges_exists",
            "old_news_story_aliases_exists",
            "old_news_story_input_state_exists",
            "old_news_projection_frontiers_exists",
        )
        if bool(row[name])
    )
    return {
        "ok": not failures,
        "failures": failures,
        **row,
    }


__all__ = [
    "rebuild_hard_cut_read_models",
    "run_hard_cut_audits",
]
