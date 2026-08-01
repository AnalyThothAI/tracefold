from __future__ import annotations

from pathlib import Path
from typing import Any

from tracefold.app.cutover import execute_hard_cut
from tracefold.app.repositories import postgres_connection
from tracefold.market import TOKEN_RADAR_PROJECTION_VERSION
from tracefold.platform.config.settings import load_settings
from tracefold.platform.postgres.postgres_audit import PostgresOperationalAudit, PostgresQueryAudit
from tracefold.platform.postgres.postgres_client import (
    local_docker_host_dsn,
    postgres_health_check,
    with_password_from_file,
)
from tracefold.platform.postgres.postgres_migrations import latest_migration_version, upgrade_head


def handle_db(args: object) -> tuple[int, dict[str, Any]]:
    settings = load_settings(require_ws_token=False)
    if args.db_command == "hard-cut":
        password_file = Path(args.bootstrap_password_file).expanduser()
        if not password_file.is_absolute():
            password_file = settings.app_home / password_file
        data = execute_hard_cut(
            settings=settings,
            bootstrap_dsn=args.bootstrap_dsn,
            bootstrap_password_file=password_file,
        )
        return 0, {"ok": True, "data": data}

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
            audit = PostgresQueryAudit(
                conn,
                token_radar_projection_version=TOKEN_RADAR_PROJECTION_VERSION,
            ).run(analyze=bool(args.analyze))
        return (0 if audit.get("ok") else 1), {"ok": bool(audit.get("ok")), "data": audit}

    return 2, {"ok": False, "error": f"unknown db command: {args.db_command}"}
