from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from tracefold.platform.config.settings import load_settings, write_default_config
from tracefold.platform.paths import config_path, workers_config_path


def handle_init(args: object) -> tuple[int, dict[str, Any]]:
    existed = config_path().exists() and workers_config_path().exists()
    path = write_default_config(force=args.force)
    password_path = _ensure_postgres_password_file(path.parent)
    return (
        0,
        {
            "ok": True,
            "data": {
                "config_path": str(path),
                "workers_config_path": str(workers_config_path(path.parent)),
                "app_home": str(path.parent),
                "postgres_password_file": str(password_path),
                "created": args.force or not existed,
            },
        },
    )


def handle_config(_args: object) -> tuple[int, dict[str, Any]]:
    settings = load_settings(require_ws_token=False)
    return (
        0,
        {
            "ok": True,
            "data": {
                "config_path": str(settings.app_home / "config.yaml"),
                "workers_config_path": str(workers_config_path(settings.app_home)),
                "api": {
                    "host": settings.api.host,
                    "port": settings.api.port,
                    "replay_limit": settings.api.replay_limit,
                    "ws_token_configured": bool(settings.ws_token),
                },
                "store": {
                    "app_home": str(settings.app_home),
                    "engine": "postgresql",
                    "postgres_dsn": _redacted_postgres_dsn(settings.storage.postgres.dsn),
                    "postgres_password_file": (
                        str(settings.postgres_password_file) if settings.postgres_password_file else None
                    ),
                    "pool_min_size": settings.storage.postgres.pool_min_size,
                    "pool_max_size": settings.storage.postgres.pool_max_size,
                    "log_file": str(settings.log_file),
                },
                "upstream": {
                    "channels": list(settings.upstream.channels),
                    "chains": list(settings.upstream.chains),
                },
                "providers": {
                    "gmgn": {
                        "configured": settings.gmgn_configured,
                        "openapi_base_url": settings.gmgn.openapi_base_url,
                        "timeout_seconds": settings.gmgn.timeout_seconds,
                        "token_info_cache_ttl_seconds": settings.gmgn.token_info_cache_ttl_seconds,
                    },
                    "okx": {
                        "dex_base_url": settings.providers.okx.dex_base_url,
                        "dex_chain_indexes": list(settings.providers.okx.dex_chain_indexes),
                        "dex_configured": settings.okx_dex_configured,
                    },
                    "binance": {
                        "enabled": settings.providers.binance.enabled,
                        "web3_base_url": settings.providers.binance.web3_base_url,
                        "cex_profile_base_url": settings.providers.binance.cex_profile_base_url,
                        "usdm_futures_base_url": settings.providers.binance.usdm_futures_base_url,
                        "cex_universe_quote_symbol": settings.providers.binance.cex_universe_quote_symbol,
                        "cex_universe_contract_type": settings.providers.binance.cex_universe_contract_type,
                        "timeout_seconds": settings.providers.binance.timeout_seconds,
                    },
                    "macro_sources": {
                        "enabled": settings.providers.macro_sources.enabled,
                        "fred_enabled": settings.providers.macro_sources.fred_enabled,
                        "cboe_enabled": settings.providers.macro_sources.cboe_enabled,
                        "cftc_enabled": settings.providers.macro_sources.cftc_enabled,
                        "nasdaq_daily_enabled": settings.providers.macro_sources.nasdaq_daily_enabled,
                        "yfinance_enabled": settings.providers.macro_sources.yfinance_enabled,
                    },
                },
                "workers": settings.workers.model_dump(mode="json"),
            },
        },
    )


def _ensure_postgres_password_file(app_home: Path) -> Path:
    path = app_home / "postgres_password"
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
        return conninfo.make_conninfo(**parts)
    except Exception:
        return "<invalid>"
