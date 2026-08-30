from __future__ import annotations

import argparse


def add_database_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    db = subcommands.add_parser("db", help="database lifecycle commands")
    db_subcommands = db.add_subparsers(dest="db_command", required=True)
    db_subcommands.add_parser("migrate", help="apply PostgreSQL migrations")
    db_subcommands.add_parser(
        "news-genesis-manifest",
        help="compute the News genesis target runtime manifest from this image and config",
    )
    db_subcommands.add_parser("health", help="check PostgreSQL liveness and migration version")
    db_audit = db_subcommands.add_parser("audit", help="run the fast PostgreSQL schema/role/catalog audit")
    db_audit.add_argument("--deep", action="store_true", help="also run offline exact counts over every table")
    query_audit = db_subcommands.add_parser("query-audit", help="explain PostgreSQL hot read paths")
    query_audit.add_argument("--analyze", action="store_true", help="run EXPLAIN ANALYZE with buffers")
