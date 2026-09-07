from __future__ import annotations

import re
from argparse import Namespace
from typing import Any

from tracefold.app.query_audit import query_audit_for_connection
from tracefold.app.repository_session import postgres_connection
from tracefold.app.workers.wiring.news import configured_runtime_manifest_sha
from tracefold.platform.config.loader import load_settings
from tracefold.platform.postgres.audit import PostgresOperationalAudit
from tracefold.platform.postgres.client import postgres_health_check, with_password_from_file
from tracefold.platform.postgres.migrations import latest_migration_version, upgrade_head
from tracefold.platform.runtime_identity import runtime_identity


def handle_db(args: Namespace) -> tuple[int, dict[str, Any]]:
    settings = load_settings(require_ws_token=False)
    if args.db_command == "news-genesis-manifest":
        identity = runtime_identity()
        if not re.fullmatch(r"[0-9a-f]{40}", identity.runtime_revision) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", identity.image_digest
        ):
            return 1, {"ok": False, "error": "news_genesis_exact_runtime_identity_required"}
        return 0, {
            "ok": True,
            "data": {
                "runtime_manifest_sha": configured_runtime_manifest_sha(settings, identity=identity),
                "runtime_revision": identity.runtime_revision,
                "image_digest": identity.image_digest,
            },
        }
    if args.db_command == "migrate":
        dsn = with_password_from_file(
            settings.storage.postgres.dsn,
            settings.postgres_password_file(),
        )
        upgrade_head(dsn)
        return 0, {"ok": True, "data": {"migration": "head"}}

    if args.db_command == "health":
        with postgres_connection(settings) as conn:
            health = postgres_health_check(conn, expected_migration_version=latest_migration_version())
        return (0 if health.get("ok") else 1), {"ok": bool(health.get("ok")), "data": health}

    if args.db_command == "audit":
        with postgres_connection(settings) as conn:
            audit = PostgresOperationalAudit(conn).run(deep=bool(args.deep))
        return (0 if audit.get("ok") else 1), {"ok": bool(audit.get("ok")), "data": audit}

    if args.db_command == "query-audit":
        with postgres_connection(settings) as conn:
            audit = query_audit_for_connection(conn).run(analyze=bool(args.analyze))
        return (0 if audit.get("ok") else 1), {"ok": bool(audit.get("ok")), "data": audit}

    return 2, {"ok": False, "error": f"unknown db command: {args.db_command}"}
