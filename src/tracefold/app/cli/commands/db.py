from __future__ import annotations

from typing import Any

from tracefold.app.query_audit import query_audit_for_connection
from tracefold.app.repository_session import postgres_connection
from tracefold.platform.config.loader import load_settings
from tracefold.platform.postgres.audit import PostgresOperationalAudit
from tracefold.platform.postgres.client import (
    local_docker_host_dsn,
    postgres_health_check,
    with_password_from_file,
)
from tracefold.platform.postgres.migrations import latest_migration_version, upgrade_head


def handle_db(args: object) -> tuple[int, dict[str, Any]]:
    settings = load_settings(require_ws_token=False)
    if args.db_command == "migrate":
        dsn = local_docker_host_dsn(
            with_password_from_file(
                settings.postgres_dsn("migrate"),
                settings.postgres_password_file("migrate"),
            )
        )
        upgrade_head(dsn)
        return 0, {"ok": True, "data": {"migration": "head"}}

    if args.db_command == "health":
        with postgres_connection(settings, role="serve") as conn:
            health = postgres_health_check(conn, expected_migration_version=latest_migration_version())
        return (0 if health.get("ok") else 1), {"ok": bool(health.get("ok")), "data": health}

    if args.db_command == "audit":
        with postgres_connection(settings, role="workers") as conn:
            audit = PostgresOperationalAudit(conn).run()
        return (0 if audit.get("ok") else 1), {"ok": bool(audit.get("ok")), "data": audit}

    if args.db_command == "query-audit":
        with postgres_connection(settings, role="serve") as conn:
            audit = query_audit_for_connection(conn).run(analyze=bool(args.analyze))
        return (0 if audit.get("ok") else 1), {"ok": bool(audit.get("ok")), "data": audit}

    return 2, {"ok": False, "error": f"unknown db command: {args.db_command}"}
