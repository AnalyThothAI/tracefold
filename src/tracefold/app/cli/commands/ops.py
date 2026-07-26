from __future__ import annotations

from typing import Any

from tracefold.app.cli.commands import queue_ops
from tracefold.app.read_models import (
    rebuild_market_tick_current_batch,
    rebuild_news_story_projection,
)
from tracefold.app.reference_data import (
    sync_binance_cex_profiles_once,
    sync_binance_usdt_perp_universe,
    sync_us_equity_symbols_once,
)
from tracefold.app.repositories import repositories
from tracefold.app.run_worker_once import (
    refresh_asset_profiles_once,
    repair_token_profile_images_once,
    run_worker_once,
)
from tracefold.market import (
    TOKEN_RADAR_DEFAULT_VENUE,
    TOKEN_RADAR_PROJECTION_VERSION,
    factor_distribution_report,
    rebuild_recent_token_intents,
    reprocess_recent_token_intents,
    token_radar_publication_status,
)
from tracefold.platform.config.settings import load_settings
from tracefold.platform.postgres.postgres_audit import ProjectionValidationAudit
from tracefold.platform.validation import require_nonnegative_int, require_positive_int


def handle_ops(args: object, _parser: object) -> tuple[int, dict[str, Any]]:
    settings = load_settings(require_ws_token=False)

    if args.ops_command == "refresh-asset-profiles":
        data = refresh_asset_profiles_once(settings, limit=args.limit).payload()
        return 0, {"ok": True, "data": data}

    if args.ops_command == "rebuild-token-profiles":
        data = run_worker_once(settings, "token_profile_current", {"batch_size": args.limit}).payload()
        return 0, {"ok": True, "data": data}

    if args.ops_command == "mirror-token-images":
        data = run_worker_once(settings, "token_image_mirror", {"batch_size": args.limit}).payload()
        return 0, {"ok": True, "data": data}

    if args.ops_command == "repair-token-profile-images":
        data = repair_token_profile_images_once(settings, limit=args.limit).payload()
        return 0, {"ok": True, "data": data}

    if args.ops_command == "run-resolution-refresh":
        data = run_worker_once(
            settings,
            "resolution_refresh",
            {"batch_size": args.limit, "reprocess_limit": args.reprocess_limit},
        ).payload()
        return 0, {"ok": True, "data": data}

    if args.ops_command == "reprocess-token-intents":
        now_ms = _now_ms()
        with repositories(settings) as repos:
            reprocess = reprocess_recent_token_intents(
                repos=repos,
                now_ms=now_ms,
                window=args.window,
                limit=args.limit,
                lookup_keys=args.lookup_key or None,
            )
        projection = run_worker_once(
            settings,
            "token_radar_projection",
            {"batch_size": args.projection_limit},
        ).payload()
        return 0, {"ok": True, "data": {"reprocess": reprocess, "projection": projection}}

    if args.ops_command == "rebuild-token-intents":
        now_ms = _now_ms()
        with repositories(settings) as repos:
            data = rebuild_recent_token_intents(
                repos=repos,
                now_ms=now_ms,
                window=args.window,
                limit=args.limit,
            )
        data["projection"] = run_worker_once(
            settings,
            "token_radar_projection",
            {"batch_size": args.projection_limit},
        ).payload()
        return 0, {"ok": True, "data": data}

    if args.ops_command == "rebuild-token-radar":
        data = run_worker_once(
            settings,
            "token_radar_projection",
            {"windows": (args.window,), "scopes": (args.scope,), "batch_size": args.limit},
        ).payload()
        return 0, {"ok": True, "data": data}

    if args.ops_command == "rebuild-market-current":
        after_target_type = args.after_target_type.strip()
        after_target_id = args.after_target_id.strip()
        if bool(after_target_type) != bool(after_target_id):
            return 2, {"ok": False, "error": "market_current_rebuild_cursor_pair_required"}
        data = rebuild_market_tick_current_batch(
            settings,
            after=(after_target_type, after_target_id) if after_target_type else None,
            limit=args.limit,
        )
        return 0, {"ok": True, "data": data}

    if args.ops_command == "rebuild-news-stories":
        data = rebuild_news_story_projection(
            settings,
            batch_size=args.batch_size,
        )
        return 0, {"ok": True, "data": data}

    if args.ops_command == "sync-binance-usdt-perp-universe":
        data = sync_binance_usdt_perp_universe(
            settings,
            dry_run=bool(args.dry_run),
            execute=bool(args.execute),
        )
        return 0, {"ok": True, "data": data}

    if args.ops_command == "sync-binance-cex-profiles":
        data = sync_binance_cex_profiles_once(settings)
        return 0, {"ok": True, "data": data}

    if args.ops_command == "sync-us-equity-symbols":
        data = sync_us_equity_symbols_once(settings)
        return 0, {"ok": True, "data": data}

    with repositories(settings) as repos:
        if args.ops_command == "queue-inspect":
            return queue_ops.handle_queue_inspect(args, repos)

        if args.ops_command == "queue-resolve":
            return queue_ops.handle_queue_resolve(args, repos, now_ms=_now_ms())

        if args.ops_command == "queue-resolve-bucket":
            return queue_ops.handle_queue_resolve_bucket(args, repos, now_ms=_now_ms())

        if args.ops_command == "reconcile-event-anchor-jobs":
            if args.execute:
                with repos.transaction():
                    data = repos.event_anchor_jobs.reconcile_ready_historical_jobs(
                        limit=args.limit,
                        now_ms=_now_ms(),
                        execute=True,
                    )
            else:
                data = repos.event_anchor_jobs.reconcile_ready_historical_jobs(
                    limit=args.limit,
                    now_ms=_now_ms(),
                    execute=False,
                )
            return 0, {"ok": True, "data": data}

        if args.ops_command == "factor-diagnostics":
            rows = repos.token_radar.latest_current_rows(
                window=args.window,
                scope=args.scope,
                venue=TOKEN_RADAR_DEFAULT_VENUE,
                limit=args.limit,
                projection_version=TOKEN_RADAR_PROJECTION_VERSION,
            )
            data = factor_distribution_report(rows)
            return (0 if data["ok"] else 1), {"ok": data["ok"], "data": data}

        if args.ops_command == "enqueue-token-radar-dirty-targets":
            data = _enqueue_token_radar_dirty_targets(
                repos,
                source=args.source,
                since_ms=args.since_ms,
                limit=args.limit,
                dry_run=bool(args.dry_run),
                execute=bool(args.execute),
                now_ms=_now_ms(),
            )
            return 0, {"ok": True, "data": data}

        if args.ops_command == "projection-status":
            return 0, {
                "ok": True,
                "data": token_radar_publication_status(
                    repos.conn,
                    projection_version=TOKEN_RADAR_PROJECTION_VERSION,
                ),
            }

        if args.ops_command == "validate-projections":
            data = ProjectionValidationAudit(repos.conn).run(sample=args.sample)
            return (0 if data.get("ok") else 1), {"ok": bool(data.get("ok")), "data": data}

        if args.ops_command == "audit-token-intent":
            data = _audit_token_intent(repos, event_id=args.event_id or None, intent_id=args.intent_id or None)
            return 0, {"ok": True, "data": data}

    return 2, {"ok": False, "error": f"unknown ops command: {args.ops_command}"}


def _enqueue_token_radar_dirty_targets(
    repos: object,
    *,
    source: str,
    since_ms: int,
    limit: int,
    dry_run: bool,
    execute: bool,
    now_ms: int,
) -> dict[str, Any]:
    parsed_since_ms = require_nonnegative_int(
        since_ms,
        error_code="ops_token_radar_dirty_targets_since_ms_required",
    )
    parsed_limit = require_positive_int(
        limit,
        error_code="ops_token_radar_dirty_targets_limit_required",
    )
    data: dict[str, Any] = {
        "source": str(source),
        "since_ms": parsed_since_ms,
        "limit": parsed_limit,
        "dry_run": bool(dry_run),
        "execute": bool(execute),
    }
    if source == "events":
        repository = repos.token_radar_dirty_targets
        candidates = repository.count_recent_resolved_target_candidates(
            since_ms=parsed_since_ms,
            now_ms=now_ms,
            limit=parsed_limit,
        )
        data["candidates"] = int(candidates)
        if dry_run:
            data["would_enqueue"] = int(
                repository.count_recent_resolved_target_enqueue_candidates(
                    since_ms=parsed_since_ms,
                    now_ms=now_ms,
                    limit=parsed_limit,
                )
            )
            return data
        with repos.transaction():
            data["enqueued"] = int(
                repository.enqueue_recent_resolved_targets(
                    since_ms=parsed_since_ms,
                    now_ms=now_ms,
                    limit=parsed_limit,
                    reason="ops_events_repair",
                )
            )
        return data
    if source == "market-current":
        repository = repos.token_radar_dirty_targets
        candidates = repository.count_market_current_target_candidates(
            since_ms=parsed_since_ms,
            now_ms=now_ms,
            limit=parsed_limit,
        )
        data["candidates"] = int(candidates)
        if dry_run:
            data["would_enqueue"] = int(
                repository.count_market_current_target_enqueue_candidates(
                    since_ms=parsed_since_ms,
                    now_ms=now_ms,
                    limit=parsed_limit,
                )
            )
            return data
        with repos.transaction():
            data["enqueued"] = int(
                repository.enqueue_market_current_targets(
                    since_ms=parsed_since_ms,
                    now_ms=now_ms,
                    limit=parsed_limit,
                    reason="ops_market_current_repair",
                )
            )
        return data
    raise ValueError(f"unknown token radar dirty target source: {source}")


def _audit_token_intent(repos: object, *, event_id: str | None, intent_id: str | None) -> dict:
    if intent_id:
        intents = [repos.token_intents.get(intent_id)]
        intents = [item for item in intents if item]
    else:
        intents = repos.token_intents.intents_for_event(str(event_id))
    intent_ids = [str(item["intent_id"]) for item in intents]
    evidence = []
    resolutions = []
    for current_intent_id in intent_ids:
        evidence.extend(repos.token_intents.evidence_links_for_intent(current_intent_id))
        resolution = repos.intent_resolutions.active_resolution_for_intent(current_intent_id)
        if resolution:
            resolutions.append(resolution)
    return {
        "event_id": event_id,
        "intent_id": intent_id,
        "intents": intents,
        "intent_evidence": evidence,
        "active_resolutions": resolutions,
    }


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)
