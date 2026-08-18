from __future__ import annotations

import asyncio
import time
from typing import Any

from tracefold.app.cli.commands import queue_ops
from tracefold.app.database import WorkerDatabase
from tracefold.app.repositories import repositories
from tracefold.platform.config.settings import load_settings
from tracefold.platform.postgres.postgres_audit import ProjectionValidationAudit

_READ_ONLY_OPS_COMMANDS = frozenset({"queue-inspect", "validate-projections"})


def handle_ops(args: object, _parser: object) -> tuple[int, dict[str, Any]]:
    settings = load_settings(require_ws_token=False)
    if args.ops_command in _READ_ONLY_OPS_COMMANDS:
        with repositories(settings, role="serve") as repos:
            return _handle_ops_read_only(args, repos=repos)
    lock_db = WorkerDatabase.create(settings)
    lock_conn = None
    try:
        lock_conn = lock_db.acquire_maintenance_runtime_lock()
        return _handle_ops_exclusive(args, settings=settings)
    finally:
        if lock_conn is not None:
            lock_db.release_maintenance_runtime_lock(lock_conn)
        asyncio.run(lock_db.aclose())


def _handle_ops_exclusive(args: object, *, settings: Any) -> tuple[int, dict[str, Any]]:
    with repositories(settings) as repos:
        if args.ops_command == "queue-resolve":
            return queue_ops.handle_queue_resolve(args, repos, now_ms=_now_ms())
        if args.ops_command == "queue-resolve-bucket":
            return queue_ops.handle_queue_resolve_bucket(args, repos, now_ms=_now_ms())
    return 2, {"ok": False, "error": f"unknown ops command: {args.ops_command}"}


def _handle_ops_read_only(args: object, *, repos: Any) -> tuple[int, dict[str, Any]]:
    if args.ops_command == "queue-inspect":
        return queue_ops.handle_queue_inspect(args, repos)
    if args.ops_command == "validate-projections":
        data = ProjectionValidationAudit(repos.conn).run(sample=args.sample)
        return (0 if data.get("ok") else 1), {"ok": bool(data.get("ok")), "data": data}
    raise ValueError(f"read_only_ops_command_invalid:{args.ops_command}")


def _now_ms() -> int:
    return int(time.time() * 1000)
