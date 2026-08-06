from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from tracefold.app.cli.commands import queue_ops
from tracefold.app.database import WorkerDatabase
from tracefold.app.hard_cut import rebuild_hard_cut_read_models
from tracefold.app.read_models import rebuild_market_tick_current_batch
from tracefold.app.reference_data import (
    sync_binance_cex_profiles_once,
    sync_binance_usdt_perp_universe,
    sync_us_equity_symbols_once,
)
from tracefold.app.repositories import repositories
from tracefold.app.run_worker_once import (
    mirror_token_images_once,
    refresh_asset_profiles_once,
    refresh_resolutions_once,
)
from tracefold.app.workers_runtime_acceptance_v2 import (
    seal_workers_runtime_evidence,
    workers_runtime_evidence_template,
)
from tracefold.app.workers_runtime_collector import collect_workers_runtime_acceptance
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

_READ_ONLY_OPS_COMMANDS = frozenset(
    {
        "audit-token-intent",
        "factor-diagnostics",
        "projection-status",
        "queue-inspect",
        "validate-projections",
    }
)


def handle_ops(args: object, _parser: object) -> tuple[int, dict[str, Any]]:
    if args.ops_command == "seal-workers-runtime-acceptance":
        if bool(args.template):
            return 0, {
                "ok": True,
                "data": {
                    "template": workers_runtime_evidence_template(),
                },
            }
        seal = seal_workers_runtime_evidence(Path(args.bundle))
        return 0, {"ok": True, "data": seal}
    if args.ops_command == "collect-workers-runtime-acceptance":
        settings = load_settings(require_ws_token=False)
        collection = collect_workers_runtime_acceptance(Path(args.bundle), settings)
        passed = collection.get("status") == "passed"
        return (0 if passed else 1), {"ok": passed, "data": collection}
    settings = load_settings(require_ws_token=False)
    if args.ops_command in _READ_ONLY_OPS_COMMANDS:
        with repositories(settings, role="serve") as repos:
            return _handle_ops_read_only(args, repos=repos)
    lock_db = WorkerDatabase.create(settings)
    lock_conn = None
    try:
        lock_conn = lock_db.acquire_maintenance_runtime_lock()
        return _handle_ops_exclusive(args, settings=settings, lock_db=lock_db)
    finally:
        if lock_conn is not None:
            lock_db.release_maintenance_runtime_lock(lock_conn)
        asyncio.run(lock_db.aclose())


def _handle_ops_exclusive(
    args: object,
    *,
    settings: Any,
    lock_db: WorkerDatabase,
) -> tuple[int, dict[str, Any]]:
    if args.ops_command == "hard-cut-rebuild":
        data = rebuild_hard_cut_read_models(
            db=lock_db,
            settings=settings,
            now_ms=_now_ms(),
        )
        return 0, {"ok": True, "data": data}

    if args.ops_command == "refresh-asset-profiles":
        data = refresh_asset_profiles_once(settings, limit=args.limit, db=lock_db)
        return 0, {"ok": True, "data": data}

    if args.ops_command == "mirror-token-images":
        data = mirror_token_images_once(settings, limit=args.limit, db=lock_db)
        return 0, {"ok": True, "data": data}

    if args.ops_command == "run-resolution-refresh":
        data = refresh_resolutions_once(
            settings,
            limit=args.limit,
            reprocess_limit=args.reprocess_limit,
            db=lock_db,
        )
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
        return 0, {"ok": True, "data": {"reprocess": reprocess}}

    if args.ops_command == "rebuild-token-intents":
        now_ms = _now_ms()
        with repositories(settings) as repos:
            data = rebuild_recent_token_intents(
                repos=repos,
                now_ms=now_ms,
                window=args.window,
                limit=args.limit,
            )
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

    return 2, {"ok": False, "error": f"unknown ops command: {args.ops_command}"}


def _handle_ops_read_only(args: object, *, repos: Any) -> tuple[int, dict[str, Any]]:
    if args.ops_command == "queue-inspect":
        return queue_ops.handle_queue_inspect(args, repos)

    if args.ops_command == "factor-diagnostics":
        rows = repos.token_radar.latest_current_rows(
            window=args.window,
            venue=TOKEN_RADAR_DEFAULT_VENUE,
            limit=args.limit,
            projection_version=TOKEN_RADAR_PROJECTION_VERSION,
        )
        data = factor_distribution_report(rows)
        return (0 if data["ok"] else 1), {"ok": data["ok"], "data": data}

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

    raise ValueError(f"read_only_ops_command_invalid:{args.ops_command}")


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
