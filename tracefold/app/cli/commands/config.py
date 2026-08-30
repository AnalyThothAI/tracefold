from __future__ import annotations

import secrets
from argparse import Namespace
from pathlib import Path
from typing import Any, Literal, cast

from tracefold.app.trading_bindings import inspect_binding_credentials
from tracefold.platform.config.loader import load_settings, write_default_config
from tracefold.platform.config.models import (
    manual_trading_availability,
    manual_trading_profile_availability,
    news_model_availability,
    news_push_availability,
    onchain_trading_availability,
    onchain_trading_profile_availability,
)
from tracefold.platform.paths import config_path

# The closed role vocabulary the Settings accessors are keyed by.
_POSTGRES_ROLES: tuple[Literal["serve", "workers", "migrate", "nautilus", "onchain"], ...] = (
    "serve",
    "workers",
    "migrate",
    "nautilus",
    "onchain",
)


def handle_init(args: Namespace) -> tuple[int, dict[str, Any]]:
    existed = config_path().exists()
    path = write_default_config(force=args.force)
    password_paths = {role: _ensure_postgres_password_file(path.parent, role=role) for role in _POSTGRES_ROLES}
    bootstrap_password_path = _ensure_bootstrap_postgres_password_file(path.parent)
    telegram_bot_token_path = _ensure_optional_secret_file(path.parent / "telegram_bot_token")
    trading_binding_secret_paths = {
        name: _ensure_optional_secret_file(path.parent / name)
        for name in ("binance_usdm_api_key", "binance_usdm_api_secret", "hyperliquid_private_key")
    }
    profile_directories = {
        lane: _ensure_secret_directory(path.parent / "trading_profiles" / lane)
        for lane in ("manual", "quotes", "onchain")
    }
    return (
        0,
        {
            "ok": True,
            "data": {
                "config_path": str(path),
                "app_home": str(path.parent),
                "postgres_password_files": {role: str(password_path) for role, password_path in password_paths.items()},
                "postgres_bootstrap_password_file": str(bootstrap_password_path),
                "telegram_bot_token_file": str(telegram_bot_token_path),
                "trading_binding_secret_files": {
                    name: str(secret_path) for name, secret_path in trading_binding_secret_paths.items()
                },
                "trading_profile_directories": {
                    name: str(directory) for name, directory in profile_directories.items()
                },
                "created": args.force or not existed,
            },
        },
    )


def handle_config(_args: Namespace) -> tuple[int, dict[str, Any]]:
    settings = load_settings(require_ws_token=False)
    push_availability = news_push_availability(settings)
    model_availability = news_model_availability(settings)
    binding_facts = {fact.binding: fact for fact in inspect_binding_credentials(settings)}
    binance_key_file = settings.trading_binance_usdm_api_key_file()
    binance_secret_file = settings.trading_binance_usdm_api_secret_file()
    hyperliquid_private_key_file = settings.trading_hyperliquid_private_key_file()
    manual_availability = manual_trading_availability(settings)
    onchain_availability = onchain_trading_availability(settings)
    profile_rows: list[dict[str, Any]] = []
    for profile in settings.trading.telegram_profiles:
        manual_profile = manual_trading_profile_availability(settings, profile)
        onchain_profile = onchain_trading_profile_availability(settings, profile)
        manual_key = settings.trading_manual_api_key_file(profile)
        manual_secret = settings.trading_manual_api_secret_file(profile)
        okx_key = settings.trading_onchain_okx_api_key_file(profile)
        okx_secret = settings.trading_onchain_okx_api_secret_file(profile)
        okx_passphrase = settings.trading_onchain_okx_passphrase_file(profile)
        oneinch_key = settings.trading_onchain_oneinch_api_key_file(profile)
        wallet_key = settings.trading_onchain_wallet_private_key_file(profile)
        profile_rows.append(
            {
                "user_id": profile.user_id,
                "private_delivery_target_configured": profile.user_id in settings.news.push.telegram_chat_ids,
                "manual": {
                    "requested": manual_profile.requested,
                    "interaction_available": manual_profile.interaction_available,
                    "reason": manual_profile.reason,
                    "venue": profile.manual.venue,
                    "account_ref": profile.manual.account_ref,
                    "api_key_file": None if manual_key is None else str(manual_key),
                    "api_secret_file": None if manual_secret is None else str(manual_secret),
                    "credentials_configured": manual_profile.credentials_configured,
                    "live_trading_acknowledged": profile.manual.live_trading_acknowledged,
                },
                "onchain": {
                    "requested": onchain_profile.requested,
                    "interaction_available": onchain_profile.interaction_available,
                    "reason": onchain_profile.reason,
                    "execution_available": onchain_profile.execution_available,
                    "execution_reason": onchain_profile.execution_reason,
                    "executable_providers": list(onchain_profile.executable_providers),
                    "wallet": {
                        "address": profile.onchain.wallet.address,
                        "private_key_file": None if wallet_key is None else str(wallet_key),
                        "private_key_configured": onchain_profile.wallet_private_key_configured,
                        "live_trading_acknowledged": profile.onchain.wallet.live_trading_acknowledged,
                    },
                    "providers": {
                        "okx": {
                            "enabled": profile.onchain.providers.okx.enabled,
                            "credentials_configured": onchain_profile.okx_credentials_configured,
                            "api_key_file": None if okx_key is None else str(okx_key),
                            "api_secret_file": None if okx_secret is None else str(okx_secret),
                            "passphrase_file": None if okx_passphrase is None else str(okx_passphrase),
                        },
                        "oneinch": {
                            "enabled": profile.onchain.providers.oneinch.enabled,
                            "credentials_configured": onchain_profile.oneinch_credentials_configured,
                            "api_key_file": None if oneinch_key is None else str(oneinch_key),
                        },
                        "binance": {
                            "enabled": profile.onchain.providers.binance.enabled,
                            "available": False,
                            "reason": onchain_profile.binance_reason,
                        },
                    },
                },
            }
        )
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
                        "triage_fallback_models": model_availability.triage_fallback_models,
                        "reader_card_fallback_models": model_availability.reader_card_fallback_models,
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
                        "telegram_target_count": push_availability.telegram_target_count,
                        "telegram_private_target_count": sum(
                            1 for target in settings.news.push.telegram_chat_ids if target > 0
                        ),
                        "min_interval_seconds": settings.news.push.min_interval_seconds,
                    },
                },
                "trading": {
                    "enabled": settings.trading.enabled,
                    "target_notional_usd": str(settings.trading.order.fixed_notional_usd),
                    "bindings": {
                        "BINANCE_USDM": {
                            "credential_state": binding_facts["BINANCE_USDM"].state,
                            "api_key_file": None if binance_key_file is None else str(binance_key_file),
                            "api_secret_file": None if binance_secret_file is None else str(binance_secret_file),
                        },
                        "HYPERLIQUID_PERP": {
                            "credential_state": binding_facts["HYPERLIQUID_PERP"].state,
                            "private_key_file": (
                                None if hyperliquid_private_key_file is None else str(hyperliquid_private_key_file)
                            ),
                            "account_address_configured": bool(
                                settings.trading.bindings.hyperliquid_perp.account_address
                            ),
                        },
                    },
                    "manual": {
                        "requested": manual_availability.requested,
                        "interaction_available": manual_availability.interaction_available,
                        "reason": manual_availability.reason,
                        "venue": manual_availability.venue,
                        "authorized_user_count": manual_availability.authorized_user_count,
                        "credentials_configured": manual_availability.credentials_configured,
                        "risk": settings.trading.manual.risk.model_dump(),
                        "tight_stop": settings.trading.manual.tight_stop.model_dump(mode="json"),
                        "wide_stop": settings.trading.manual.wide_stop.model_dump(mode="json"),
                    },
                    "onchain": {
                        "requested": onchain_availability.requested,
                        "interaction_available": onchain_availability.interaction_available,
                        "reason": onchain_availability.reason,
                        "execution_available": onchain_availability.execution_available,
                        "execution_reason": onchain_availability.execution_reason,
                        "executable_providers": list(onchain_availability.executable_providers),
                        "authorized_user_count": onchain_availability.authorized_user_count,
                        "slippage_bps": settings.trading.onchain.slippage_bps,
                        "discovery_chain_ids": list(settings.trading.onchain.chain_ids),
                        "settlement_assets": [
                            {
                                "chain_id": asset.chain_id,
                                "chain_name": asset.chain_name,
                                "symbol": asset.symbol,
                                "contract_address": asset.contract_address,
                                "decimals": asset.decimals,
                                "quote_amount": str(asset.quote_amount),
                                "rpc_configured": asset.rpc_url is not None,
                            }
                            for asset in settings.trading.onchain.settlement_assets
                        ],
                    },
                    "telegram_profiles": profile_rows,
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


def _ensure_optional_secret_file(path: Path) -> Path:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"optional_secret_path_not_file:{path.name}")
    if not path.exists():
        path.touch(mode=0o600)
    path.chmod(0o600)
    return path


def _ensure_secret_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
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
