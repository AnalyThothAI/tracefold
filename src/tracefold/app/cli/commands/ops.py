from __future__ import annotations

import asyncio
from typing import Any

from tracefold.app.cli.commands import queue_ops
from tracefold.app.database import WorkerDatabase
from tracefold.app.read_models import rebuild_market_tick_current_batch
from tracefold.app.reference_data import sync_binance_usdt_perp_universe, sync_us_equity_symbols_once
from tracefold.app.repositories import repositories
from tracefold.market import rebuild_recent_token_intents
from tracefold.platform.config.settings import load_settings
from tracefold.platform.postgres.postgres_audit import ProjectionValidationAudit

_READ_ONLY_OPS_COMMANDS = frozenset(
    {
        "audit-token-intent",
        "queue-inspect",
        "validate-projections",
    }
)


def handle_ops(args: object, _parser: object) -> tuple[int, dict[str, Any]]:
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
