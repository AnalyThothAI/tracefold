from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from tracefold.platform.config.settings import (
    load_settings,
    news_model_availability,
    news_push_availability,
    write_default_config,
)
from tracefold.platform.paths import config_path


def handle_init(args: object) -> tuple[int, dict[str, Any]]:
    existed = config_path().exists()
    path = write_default_config(force=args.force)
    password_paths = {
        role: _ensure_postgres_password_file(path.parent, role=role)
        for role in ("serve", "review", "workers", "migrate")
    }
    bootstrap_password_path = _ensure_bootstrap_postgres_password_file(path.parent)
    return (
        0,
        {
            "ok": True,
            "data": {
                "config_path": str(path),
                "app_home": str(path.parent),
                "postgres_password_files": {role: str(password_path) for role, password_path in password_paths.items()},
                "postgres_bootstrap_password_file": str(bootstrap_password_path),
                "created": args.force or not existed,
            },
        },
    )


def handle_config(_args: object) -> tuple[int, dict[str, Any]]:
    settings = load_settings(require_ws_token=False)
    push_availability = news_push_availability(settings)
    model_availability = news_model_availability(settings)
    return (
        0,
        {
            "ok": True,
            "data": {
                "config_path": str(settings.app_home / "config.yaml"),
                "api": {
                    "host": settings.api.host,
                    "port": settings.api.port,
                    "ws_token_configured": bool(settings.ws_token),
                },
                "store": {
                    "app_home": str(settings.app_home),
                    "engine": "postgresql",
                    "postgres_roles": {
                        role: {
                            "dsn": _redacted_postgres_dsn(settings.postgres_dsn(role)),
                            "password_file": (
                                str(settings.postgres_password_file(role))
                                if settings.postgres_password_file(role)
                                else None
                            ),
                        }
                        for role in ("serve", "review", "workers", "migrate")
                    },
                    "serve_pool_max_size": 7,
                    "review_pool_max_size": 1,
                    "workers_pool_max_size": 8,
                    "log_file": str(settings.log_file),
                },
                "news": {
                    "enabled": settings.news.enabled,
                    "opennews_token_configured": bool(settings.news.opennews_token),
                    "opennews_strategy_ids_configured": bool(settings.news.opennews_strategy_ids),
                    "opennews_strategy_count": len(settings.news.opennews_strategy_ids),
                    "broker": {
                        "url_configured": bool(settings.news.broker.url),
                        "name_prefix": settings.news.broker.name_prefix,
                    },
                    "models": {
                        "triage_configured": model_availability.triage_configured,
                        "triage_model": model_availability.triage_model,
                        "triage_fallback_model": model_availability.triage_fallback_model,
                    },
                    "triage": settings.news.triage.model_dump(),
                    "watchlist": sorted(settings.news.watchlist_symbols),
                    "policy": settings.news.policy.model_dump(),
                    "retention": settings.news.retention.model_dump(),
                    "gate": settings.news.gate.model_dump(),
                    "push": {
                        "requested": push_availability.requested,
                        "delivery_available": push_availability.delivery_available,
                        "reason": push_availability.reason,
                        "feishu_webhook_url_configured": (push_availability.feishu_webhook_url_configured),
                        "feishu_signing_secret_configured": (push_availability.feishu_signing_secret_configured),
                        "min_interval_seconds": settings.news.push.min_interval_seconds,
                    },
                },
            },
        },
    )


def _ensure_postgres_password_file(app_home: Path, *, role: str) -> Path:
    path = app_home / f"postgres_{role}_password"
    if not path.exists():
        path.write_text(secrets.token_urlsafe(32) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _ensure_bootstrap_postgres_password_file(app_home: Path) -> Path:
    path = app_home / "postgres_password"
    if not path.exists():
        path.write_text(
            secrets.token_urlsafe(32) + "\n",
            encoding="utf-8",
        )
    path.chmod(0o600)
    return path


def _redacted_postgres_dsn(dsn: str) -> str:
    from psycopg import conninfo

    try:
        parts = conninfo.conninfo_to_dict(dsn)
        if parts.get("password"):
            parts["password"] = "********"
        return conninfo.make_conninfo(**parts)
    except Exception:
        return "<invalid>"
