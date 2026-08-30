from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from argparse import Namespace
from typing import Any

from tracefold.app.query_audit import query_audit_for_connection
from tracefold.app.repository_session import postgres_connection
from tracefold.app.workers.wiring.news import configured_runtime_manifest_sha
from tracefold.platform.config.loader import load_settings
from tracefold.platform.postgres.audit import PostgresOperationalAudit
from tracefold.platform.postgres.client import (
    connect_postgres,
    local_docker_host_dsn,
    postgres_health_check,
    with_password_from_file,
)
from tracefold.platform.postgres.migrations import latest_migration_version, upgrade_head
from tracefold.platform.runtime_identity import runtime_identity

_GENESIS_PREFLIGHT_ENV = "TRACEFOLD_NEWS_GENESIS_PREFLIGHT_JSON"
_GENESIS_BROKER_OBSERVATION_ENV = "TRACEFOLD_NEWS_GENESIS_BROKER_OBSERVATION_SHA256"
_GENESIS_RUNTIME_MANIFEST_ENV = "TRACEFOLD_NEWS_GENESIS_EXPECTED_RUNTIME_MANIFEST_SHA256"
_GENESIS_FRESH_INSTALL_ENV = "TRACEFOLD_NEWS_GENESIS_FRESH_INSTALL"


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
        dsn = local_docker_host_dsn(
            with_password_from_file(
                settings.postgres_dsn("migrate"),
                settings.postgres_password_file("migrate"),
            )
        )
        _prepare_news_genesis_evidence(settings, fresh_install=_database_is_unmigrated(dsn))
        upgrade_head(dsn)
        return 0, {"ok": True, "data": {"migration": "head"}}

    if args.db_command == "health":
        with postgres_connection(settings, role="serve") as conn:
            health = postgres_health_check(conn, expected_migration_version=latest_migration_version())
        return (0 if health.get("ok") else 1), {"ok": bool(health.get("ok")), "data": health}

    if args.db_command == "audit":
        with postgres_connection(settings, role="workers") as conn:
            audit = PostgresOperationalAudit(conn).run(deep=bool(args.deep))
        return (0 if audit.get("ok") else 1), {"ok": bool(audit.get("ok")), "data": audit}

    if args.db_command == "query-audit":
        with postgres_connection(settings, role="serve") as conn:
            audit = query_audit_for_connection(conn).run(analyze=bool(args.analyze))
        return (0 if audit.get("ok") else 1), {"ok": bool(audit.get("ok")), "data": audit}

    return 2, {"ok": False, "error": f"unknown db command: {args.db_command}"}


def _database_is_unmigrated(dsn: str) -> bool:
    with connect_postgres(dsn) as conn:
        exists = conn.execute("SELECT to_regclass('public.alembic_version') AS relation").fetchone()
        if exists is None or exists["relation"] is None:
            return True
        row = conn.execute("SELECT version_num AS revision FROM alembic_version LIMIT 1").fetchone()
        return row is None or row["revision"] is None


def _prepare_news_genesis_evidence(settings: Any, *, fresh_install: bool = False) -> None:
    """Bind the caller's cutover claim to this image/config and a live, drained broker."""

    for name in (_GENESIS_BROKER_OBSERVATION_ENV, _GENESIS_RUNTIME_MANIFEST_ENV, _GENESIS_FRESH_INSTALL_ENV):
        os.environ.pop(name, None)
    raw = os.environ.get(_GENESIS_PREFLIGHT_ENV, "").strip()
    if not raw and not fresh_install:
        return
    preflight: dict[str, Any] | None = None
    if raw:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{_GENESIS_PREFLIGHT_ENV} must be valid JSON") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"{_GENESIS_PREFLIGHT_ENV} must be a JSON object")
        preflight = value

    observation = asyncio.run(_observe_drained_news_broker(settings))
    if preflight is not None:
        totals = observation["totals"]
        claimed = {
            "ready": preflight.get("queue_ready"),
            "unacked": preflight.get("queue_unacked"),
            "dead_letter": preflight.get("queue_dead_letter"),
            "stale_reference_count": preflight.get("queue_stale_reference_count"),
        }
        if claimed != totals:
            raise RuntimeError(f"news genesis broker claim does not match live observation: {claimed} != {totals}")

    os.environ[_GENESIS_BROKER_OBSERVATION_ENV] = _canonical_sha(observation)
    os.environ[_GENESIS_RUNTIME_MANIFEST_ENV] = configured_runtime_manifest_sha(settings)
    if fresh_install:
        os.environ[_GENESIS_FRESH_INSTALL_ENV] = "1"


async def _observe_drained_news_broker(settings: Any) -> dict[str, Any]:
    from tracefold.integrations.rabbitmq import RabbitMQBus

    url = settings.news.broker.url
    if not url:
        raise RuntimeError("news genesis requires a configured RabbitMQ broker")
    bus = RabbitMQBus(
        url=url,
        name_prefix=settings.news.broker.name_prefix,
        connect_timeout_seconds=settings.news.broker.connect_timeout_seconds,
        management_url=settings.news.broker.management_url,
    )
    try:
        await bus.connect()
        await bus.verify_policies()
        depths = await bus.queue_depths()
        queues = await bus.broker_snapshot()
        drift = await bus.topology_drift()
    finally:
        await bus.close()

    if drift["queues"] or drift["exchanges"]:
        raise RuntimeError(f"news genesis broker topology drift: {drift}")
    for name, row in depths.items():
        for field in ("messages", "consumers"):
            if type(row.get(field)) is not int or int(row[field]) != 0:
                raise RuntimeError(f"news genesis requires drained queue {name}.{field}=0")
    fields = ("messages", "consumers", "ready", "unacked", "delayed", "dead_letter_pending")
    for name, row in queues.items():
        if row.get("missing") or row.get("policy_ok") is not True:
            raise RuntimeError(f"news genesis queue is missing or has policy drift: {name}")
        for field in fields:
            if type(row.get(field)) is not int or int(row[field]) != 0:
                raise RuntimeError(f"news genesis requires drained queue {name}.{field}=0")

    return {
        "schema": "news_genesis_broker_observation_v1",
        "queues": {name: {field: row[field] for field in fields} for name, row in sorted(queues.items())},
        "topology_drift": drift,
        "totals": {"ready": 0, "unacked": 0, "dead_letter": 0, "stale_reference_count": 0},
    }


def _canonical_sha(value: Any) -> str:
    document = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(document.encode()).hexdigest()
