from __future__ import annotations

from argparse import Namespace
from typing import Any

from tracefold.app.repository_session import repositories
from tracefold.platform.config.loader import load_settings
from tracefold.platform.postgres.audit import ProjectionValidationAudit


def handle_ops(args: Namespace, _parser: object) -> tuple[int, dict[str, Any]]:
    settings = load_settings(require_ws_token=False)
    with repositories(settings) as repos:
        if args.ops_command == "validate-projections":
            data = ProjectionValidationAudit(repos.conn).run(sample=args.sample)
            return (0 if data.get("ok") else 1), {"ok": bool(data.get("ok")), "data": data}
    return 2, {"ok": False, "error": f"unknown ops command: {args.ops_command}"}
