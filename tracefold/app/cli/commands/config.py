from __future__ import annotations

import secrets
from argparse import Namespace
from pathlib import Path
from typing import Any, cast

from tracefold.platform.config.loader import (
    load_settings,
    migrate_pre_433c_trading_config,
    migrate_pre_449_postgres_config,
    write_default_config,
)
from tracefold.platform.config.models import (
    news_model_availability,
    news_push_availability,
)
from tracefold.platform.paths import config_path


def handle_init(args: Namespace) -> tuple[int, dict[str, Any]]:
    existed = config_path().exists()
    path = write_default_config(force=args.force)
    config_backup_paths = (
        [
            backup
            for migration in (migrate_pre_433c_trading_config, migrate_pre_449_postgres_config)
            if (backup := migration(path)) is not None
        ]
        if existed and not args.force
        else []
    )
    config_backup_path = config_backup_paths[-1] if config_backup_paths else None
    password_path = _ensure_postgres_password_file(path.parent)
    bootstrap_password_path = _ensure_bootstrap_postgres_password_file(path.parent)
    telegram_bot_token_path = _ensure_optional_secret_file(path.parent / "telegram_bot_token")
    telegram_webhook_secret_path = _ensure_optional_secret_file(path.parent / "telegram_webhook_secret")
    trading_execution_secret_paths = {
        name: _ensure_optional_secret_file(path.parent / name)
        for name in ("binance_usdm_api_key", "binance_usdm_api_secret")
    }
    return (
        0,
        {
            "ok": True,
            "data": {
                "config_path": str(path),
                "app_home": str(path.parent),
                "postgres_database_password_file": str(password_path),
                "postgres_bootstrap_password_file": str(bootstrap_password_path),
                "telegram_bot_token_file": str(telegram_bot_token_path),
                "telegram_webhook_secret_file": str(telegram_webhook_secret_path),
                "trading_execution_secret_files": {
                    name: str(secret_path) for name, secret_path in trading_execution_secret_paths.items()
                },
                "created": args.force or not existed,
                "config_migrated": bool(config_backup_paths),
                "config_backup_path": None if config_backup_path is None else str(config_backup_path),
                "config_backup_paths": [str(value) for value in config_backup_paths],
            },
        },
    )


def handle_config(_args: Namespace) -> tuple[int, dict[str, Any]]:
    settings = load_settings(require_ws_token=False)
    push_availability = news_push_availability(settings)
    model_availability = news_model_availability(settings)
    binance_key_file = settings.trading_binance_usdm_api_key_file()
    binance_secret_file = settings.trading_binance_usdm_api_secret_file()
    trading_bot_token_file = settings.trading_telegram_bot_token_file()
    trading_webhook_secret_file = settings.trading_telegram_webhook_secret_file()
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
                    "postgres": {
                        "dsn": _redacted_postgres_dsn(settings.storage.postgres.dsn),
                        "password_file": (
                            str(settings.postgres_password_file()) if settings.postgres_password_file() else None
                        ),
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
                        "provider": push_availability.provider,
                        "feishu_webhook_url_configured": (push_availability.feishu_webhook_url_configured),
                        "feishu_signing_secret_configured": (push_availability.feishu_signing_secret_configured),
                        "telegram_bot_token_file_configured": (push_availability.telegram_bot_token_file_configured),
                        "telegram_chat_id_configured": push_availability.telegram_chat_id_configured,
                        "min_interval_seconds": settings.news.push.min_interval_seconds,
                    },
                },
                "trading": {
                    "enabled": settings.trading.enabled,
                    "control": {
                        "enabled": settings.trading.control.enabled,
                        "telegram_bot_token_file": (
                            None if trading_bot_token_file is None else str(trading_bot_token_file)
                        ),
                        "telegram_webhook_secret_file": (
                            None if trading_webhook_secret_file is None else str(trading_webhook_secret_file)
                        ),
                        "allowed_chat_count": len(settings.trading.control.allowed_chat_ids),
                        "allowed_user_count": len(settings.trading.control.allowed_user_ids),
                        "notification_chat_configured": settings.trading.control.notification_chat_id is not None,
                    },
                    "execution": {
                        "mode": settings.trading.execution.mode,
                        "profile_id": settings.trading.execution.profile_id,
                        "account_slot": settings.trading.execution.account_slot,
                        "credentials": {
                            "api_key_file": None if binance_key_file is None else str(binance_key_file),
                            "api_secret_file": None if binance_secret_file is None else str(binance_secret_file),
                        },
                    },
                },
            },
        },
    )


def _ensure_postgres_password_file(app_home: Path) -> Path:
    path = app_home / "postgres_database_password"
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


def _ensure_optional_secret_file(path: Path) -> Path:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"optional_secret_path_not_file:{path.name}")
    if not path.exists():
        path.touch(mode=0o600)
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
