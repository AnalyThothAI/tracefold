from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from tracefold.app.runtime_capabilities import macro_document_analysis_runtime
from tracefold.platform.config.settings import (
    load_settings,
    news_push_availability,
    news_title_presentation_availability,
    write_default_config,
)
from tracefold.platform.paths import config_path


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
    title_availability = news_title_presentation_availability(settings)
    return (
        0,
        {
            "ok": True,
            "data": {
                "config_path": str(settings.app_home / "config.yaml"),
                "api": {
                    "host": settings.api.host,
                    "port": settings.api.port,
                    "replay_limit": settings.api.replay_limit,
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
                        for role in ("serve", "workers", "migrate")
                    },
                    "serve_pool_max_size": 8,
                    "workers_pool_max_size": 4,
                    "log_file": str(settings.log_file),
                },
                "upstream": {
                    "channels": list(settings.upstream.channels),
                    "chains": list(settings.upstream.chains),
                },
                "news": {
                    "enabled": settings.news.enabled,
                    "rss_enabled": settings.news.rss_enabled,
                    "opennews_token_configured": bool(settings.news.opennews_token),
                    "opennews_strategy_ids_configured": bool(settings.news.opennews_strategy_ids),
                    "opennews_strategy_count": len(settings.news.opennews_strategy_ids),
                    "brief": {
                        "direct_configured": bool(settings.llm.api_key),
                        "groq_configured": bool(settings.llm.groq_api_key),
                    },
                    "title_presentation": {
                        "deepl_configured": title_availability.deepl_configured,
                        "deepl_key_count": title_availability.deepl_key_count,
                        "deepseek_configured": title_availability.deepseek_configured,
                    },
                    "push": {
                        "requested": push_availability.requested,
                        "delivery_available": push_availability.delivery_available,
                        "reason": push_availability.reason,
                        "feishu_webhook_url_configured": (push_availability.feishu_webhook_url_configured),
                        "feishu_signing_secret_configured": (push_availability.feishu_signing_secret_configured),
                    },
                },
                "macro": {"document_analysis": macro_document_analysis_runtime(settings)},
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
