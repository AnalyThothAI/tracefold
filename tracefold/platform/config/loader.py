from __future__ import annotations

import os
import secrets
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, quote, urlsplit, urlunsplit

import yaml

from tracefold.platform.paths import app_home, config_path

from .models import Settings


def load_settings(*, require_ws_token: bool = True) -> Settings:
    path = config_path()
    if not path.exists():
        raise FileNotFoundError(f"config.yaml not found at {path}; run `tracefold init` first")
    data = _load_yaml_mapping(path)
    settings = Settings(**dict(data))
    settings.set_config_dir(path.parent)
    if require_ws_token and not settings.ws_token:
        raise ValueError("ws_token is required in config.yaml")
    return settings


def write_default_config(*, force: bool = False) -> Path:
    home = app_home()
    path = config_path(home)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    home.chmod(0o700)
    for directory_name in ("logs", "cache"):
        directory = home / directory_name
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("config_path_not_regular_file")
    if force or not path.exists():
        path.write_text(default_config_yaml(), encoding="utf-8")
    path.chmod(0o600)
    return path


def migrate_pre_433c_trading_config(path: Path) -> Path | None:
    """Atomically hard-cut the exact pre-433-C Trading config shape once."""

    if path.is_symlink() or not path.is_file():
        raise ValueError("config_path_not_regular_file")
    original = path.read_bytes()
    loaded = yaml.safe_load(original)
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{path.name} must contain a mapping at {path}")
    current = dict(loaded)
    trading_value = current.get("trading")
    if not isinstance(trading_value, Mapping):
        return None
    trading = dict(trading_value)
    retired = {"order", "bindings"}.intersection(trading)
    if not retired:
        return None
    if "execution" in trading:
        raise ValueError("trading_config_cutover_mixed_shape")
    _require_exact_keys(
        trading,
        allowed={"enabled", "candidates", "order", "bindings"},
        error_code="trading_config_cutover_unknown_key",
    )
    if "order" in trading:
        order = _require_mapping(trading["order"], error_code="trading_config_cutover_order_invalid")
        _require_exact_keys(
            order,
            allowed={"fixed_notional_usd"},
            error_code="trading_config_cutover_order_invalid",
        )
    bindings = _require_mapping(
        trading.get("bindings", {}),
        error_code="trading_config_cutover_bindings_invalid",
    )
    _require_exact_keys(
        bindings,
        allowed={"binance_usdm", "hyperliquid_perp"},
        error_code="trading_config_cutover_bindings_invalid",
    )
    binance = _require_mapping(
        bindings.get("binance_usdm", {}),
        error_code="trading_config_cutover_binance_invalid",
    )
    _require_exact_keys(
        binance,
        allowed={"api_key_file", "api_secret_file"},
        error_code="trading_config_cutover_binance_invalid",
    )
    if "hyperliquid_perp" in bindings:
        hyperliquid = _require_mapping(
            bindings["hyperliquid_perp"],
            error_code="trading_config_cutover_hyperliquid_invalid",
        )
        _require_exact_keys(
            hyperliquid,
            allowed={"private_key_file", "account_address"},
            error_code="trading_config_cutover_hyperliquid_invalid",
        )

    migrated_trading = {key: value for key, value in trading.items() if key not in retired}
    migrated_trading["execution"] = {
        "mode": "disabled",
        "account_slot": "binance_usdm_primary",
        "credentials": {
            "api_key_file": binance.get("api_key_file", "binance_usdm_api_key"),
            "api_secret_file": binance.get("api_secret_file", "binance_usdm_api_secret"),
        },
    }
    migrated = {**current, "trading": migrated_trading}
    Settings.model_validate(migrated)
    rendered = yaml.safe_dump(migrated, sort_keys=False, allow_unicode=True).encode()
    if path.is_symlink() or path.read_bytes() != original:
        raise ValueError("trading_config_cutover_source_changed")
    backup_path = path.parent / "config.pre-433c.yaml"
    _write_private_backup(backup_path, original)
    if path.is_symlink() or path.read_bytes() != original:
        raise ValueError("trading_config_cutover_source_changed")
    _replace_private_file(path, rendered)
    return backup_path


def migrate_pre_449_postgres_config(path: Path) -> Path | None:
    """Atomically hard-cut the exact retired multi-login PostgreSQL config."""

    if path.is_symlink() or not path.is_file():
        raise ValueError("config_path_not_regular_file")
    original = path.read_bytes()
    loaded = yaml.safe_load(original)
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{path.name} must contain a mapping at {path}")
    current = dict(loaded)
    storage = _require_mapping(current.get("storage", {}), error_code="postgres_config_cutover_storage_invalid")
    postgres_value = storage.get("postgres")
    if not isinstance(postgres_value, Mapping):
        return None
    postgres = dict(postgres_value)
    retired_dsn_keys = {"serve_dsn", "workers_dsn", "migrate_dsn", "nautilus_dsn"}
    if not retired_dsn_keys.intersection(postgres):
        return None
    if "dsn" in postgres or "password_file" in postgres:
        raise ValueError("postgres_config_cutover_mixed_shape")
    _require_exact_keys(
        postgres,
        allowed={
            *retired_dsn_keys,
            "serve_password_file",
            "workers_password_file",
            "migrate_password_file",
            "nautilus_password_file",
            "connect_timeout_seconds",
        },
        error_code="postgres_config_cutover_unknown_key",
    )
    required = ("serve_dsn", "workers_dsn", "migrate_dsn")
    if any(key not in postgres for key in required):
        raise ValueError("postgres_config_cutover_dsn_missing")
    parsed = [_parse_legacy_postgres_dsn(postgres[key]) for key in required]
    if "nautilus_dsn" in postgres:
        parsed.append(_parse_legacy_postgres_dsn(postgres["nautilus_dsn"]))
    targets = {(value.scheme, value.hostname, value.port, value.path, value.query, value.fragment) for value in parsed}
    if len(targets) != 1:
        raise ValueError("postgres_config_cutover_target_mismatch")
    selected = parsed[0]
    host = selected.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if selected.port is not None:
        host = f"{host}:{selected.port}"
    dsn = urlunsplit(
        (
            selected.scheme,
            f"{quote('tracefold', safe='')}@{host}",
            selected.path,
            selected.query,
            selected.fragment,
        )
    )
    migrated_postgres = {
        "dsn": dsn,
        "password_file": "postgres_database_password",
        "connect_timeout_seconds": postgres.get("connect_timeout_seconds", 5),
    }
    migrated = {**current, "storage": {**storage, "postgres": migrated_postgres}}
    Settings.model_validate(migrated)
    rendered = yaml.safe_dump(migrated, sort_keys=False, allow_unicode=True).encode()
    if path.is_symlink() or path.read_bytes() != original:
        raise ValueError("postgres_config_cutover_source_changed")
    backup_path = path.parent / "config.pre-449.yaml"
    _write_private_backup(backup_path, original)
    if path.is_symlink() or path.read_bytes() != original:
        raise ValueError("postgres_config_cutover_source_changed")
    _replace_private_file(path, rendered)
    return backup_path


def _parse_legacy_postgres_dsn(value: object) -> SplitResult:
    parsed = urlsplit(str(value or "").strip())
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("postgres_config_cutover_dsn_invalid") from exc
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or parsed.hostname is None
        or not parsed.path
        or parsed.username is None
        or parsed.password is not None
    ):
        raise ValueError("postgres_config_cutover_dsn_invalid")
    del port
    return parsed


def _require_mapping(value: object, *, error_code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(error_code)
    return dict(value)


def _require_exact_keys(value: Mapping[str, Any], *, allowed: set[str], error_code: str) -> None:
    if not set(value).issubset(allowed):
        raise ValueError(error_code)


def _write_private_backup(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".config-pre-433c-", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as backup_file:
            backup_file.write(content)
            backup_file.flush()
            os.fsync(backup_file.fileno())
        temporary_path.chmod(0o600)
        try:
            os.link(temporary_path, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                raise ValueError("trading_config_cutover_backup_conflict") from None
            path.chmod(0o600)
            return
        _sync_directory(path.parent)
    finally:
        temporary_path.unlink(missing_ok=True)


def _replace_private_file(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".config-433c-", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as config_file:
            config_file.write(content)
            config_file.flush()
            os.fsync(config_file.fileno())
        temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
        _sync_directory(path.parent)
    finally:
        temporary_path.unlink(missing_ok=True)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def default_config_yaml() -> str:
    token = secrets.token_urlsafe(32)
    return f"""# Tracefold
ws_token: "{token}"

api:
  host: "0.0.0.0"
  port: 8765

storage:
  postgres:
    dsn: "postgresql://tracefold@postgres:5432/tracefold"
    password_file: "postgres_database_password"
    connect_timeout_seconds: 5

llm:
  api_key:
  base_url:
  news_triage_model:
  request:
    send_temperature:
    temperature: 0
    structured_output: "auto"
    extra_body: {{}}
  news_reader_card:
    api_key:
    base_url:
    model:
    request:
      send_temperature:
      temperature: 0
      structured_output: "auto"
      extra_body: {{}}
  news_triage_fallback:
    api_key:
    base_url:
    model:
    request:
      send_temperature:
      temperature: 0
      structured_output: "auto"
      extra_body: {{}}
  news_reader_card_fallback:
    api_key:
    base_url:
    model:
    request:
      send_temperature:
      temperature: 0
      structured_output: "auto"
      extra_body: {{}}

news:
  enabled: true
  opennews_token:
  broker:
    url: "amqp://tracefold:tracefold@rabbitmq:5672/"
    name_prefix: ""
  triage:
    concurrency: 4
  push:
    enabled: false
    feishu_webhook_url:
    feishu_signing_secret:
    telegram_bot_token_file:
    telegram_chat_id:
    min_interval_seconds: 0.6
  policy:
    restatement_drop: true
    similarity_max: 0.25
    listing_exempt_from_duplicate: true
    stale_source_max_age_s: 43200  # #154: an x/twitter artifact older than this on arrival is a replay
    storyline_budget_window_s: 3600  # #504: per-storyline budget window; 0 disables
    storyline_budget_max: 2  # #504: delivered cards per storyline inside the window; 0 disables
  retention:
    raw_days: 30
    judged_days: 365
  venues:
    enabled: true
    binance: true
    hyperliquid: true
    okx: true
    lighter: true
    bitget: true
    us_reference: true
    snapshot_period_hours: 6.0
  watchlist: []

trading:
  enabled: false
  control:
    enabled: false
    telegram_bot_token_file: "telegram_bot_token"
    telegram_webhook_secret_file: "telegram_webhook_secret"
    allowed_chat_ids: []
    allowed_user_ids: []
    notification_chat_id:
  execution:
    mode: disabled
    account_slot: binance_usdm_primary
    credentials:
      api_key_file: "binance_usdm_api_key"
      api_secret_file: "binance_usdm_api_secret"
"""


def _load_yaml_mapping(path: Path) -> Mapping[str, Any]:
    with path.open("r", encoding="utf-8") as config_file:
        data = yaml.safe_load(config_file)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a mapping at {path}")
    return data
