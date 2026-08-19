from __future__ import annotations

from typing import Any

from tracefold.app.repositories import repositories
from tracefold.platform.config.settings import load_settings
from tracefold.platform.postgres.postgres_audit import ProjectionValidationAudit


def handle_ops(args: object, _parser: object) -> tuple[int, dict[str, Any]]:
    settings = load_settings(require_ws_token=False)
    with repositories(settings, role="serve") as repos:
        if args.ops_command == "validate-projections":
            data = ProjectionValidationAudit(repos.conn).run(sample=args.sample)
            return (0 if data.get("ok") else 1), {"ok": bool(data.get("ok")), "data": data}
    return 2, {"ok": False, "error": f"unknown ops command: {args.ops_command}"}
