from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any, Literal, cast

from tracefold.platform.config.loader import load_settings, write_default_config
from tracefold.platform.config.models import (
    news_model_availability,
    news_push_availability,
)
from tracefold.platform.config.secret_file import secret_file_configured
from tracefold.platform.paths import config_path

# The closed role vocabulary the Settings accessors are keyed by.
_POSTGRES_ROLES: tuple[Literal["serve", "workers", "migrate"], ...] = ("serve", "workers", "migrate")


def handle_init(args: object) -> tuple[int, dict[str, Any]]:
    existed = config_path().exists()
    path = write_default_config(force=args.force)
    password_paths = {
        role: _ensure_postgres_password_file(path.parent, role=role) for role in ("serve", "workers", "migrate")
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
    opentrade_token_file = settings.trading_opentrade_token_file()
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
                        for role in _POSTGRES_ROLES
                    },
                    "serve_pool_max_size": 7,
                    "workers_pool_max_size": 8,
                    "log_file": str(settings.log_file),
                },
                "news": {
                    "enabled": settings.news.enabled,
                    "opennews_token_configured": bool(settings.news.opennews_token),
                    "broker": {
                        "url_configured": bool(settings.news.broker.url),
                        "name_prefix": settings.news.broker.name_prefix,
                    },
                    "models": {
                        "triage_configured": model_availability.triage_configured,
                        "triage_model": model_availability.triage_model,
                        "reader_card_model": model_availability.reader_card_model,
                        "reader_card_dedicated": model_availability.reader_card_dedicated,
                        "triage_fallback_model": model_availability.triage_fallback_model,
                        "reader_card_fallback_model": model_availability.reader_card_fallback_model,
                        "reader_card_fallback_dedicated": (model_availability.reader_card_fallback_dedicated),
                        "compiler_reflection_configured": settings.llm.news_compiler_reflection.configured,
                        "compiler_reflection_model": settings.llm.news_compiler_reflection.model,
                        "compiler_tariff_configured": settings.llm.news_compiler_tariff.configured,
                        "compiler_tariff_id": settings.llm.news_compiler_tariff.tariff_id,
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
                "trading": {
                    "enabled": settings.trading.enabled,
                    "mode": settings.trading.mode,
                    "account_ref": settings.trading.account_ref,
                    "live_symbol": settings.trading.live_symbol,
                    "venues": list(settings.trading.venues.enabled),
                    "nominal_daily_stop_loss_usd": str(settings.trading.order.nominal_daily_stop_loss_usd),
                    "opentrade": {
                        "base_url_configured": bool(settings.trading.opentrade.base_url),
                        "token_file": None if opentrade_token_file is None else str(opentrade_token_file),
                        "token_file_configured": secret_file_configured(opentrade_token_file),
                    },
                },
            },
        },
    )


def _ensure_postgres_password_file(app_home: Path, *, role: str) -> Path:
    path = app_home / f"postgres_{role}_password"
    return _ensure_password_file(path)


def _ensure_bootstrap_postgres_password_file(app_home: Path) -> Path:
    path = app_home / "postgres_password"
    return _ensure_password_file(path)


def _ensure_password_file(path: Path) -> Path:
    if path.exists() and not path.is_file():
        raise ValueError(f"postgres_password_path_not_file:{path.name}")
    if not path.exists():
        path.write_text(secrets.token_urlsafe(32) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _redacted_postgres_dsn(dsn: str) -> str:
    from psycopg import conninfo

    try:
        parts = conninfo.conninfo_to_dict(dsn)
        if parts.get("password"):
            parts["password"] = "********"
        # `conninfo_to_dict` is typed as returning ints for numeric keywords, which `make_conninfo`'s
        # own stub does not accept back. Round-tripping is exactly what this redaction does.
        return conninfo.make_conninfo(**cast(dict[str, str], parts))
    except Exception:
        return "<invalid>"
