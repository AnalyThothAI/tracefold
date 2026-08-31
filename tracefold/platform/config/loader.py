from __future__ import annotations

import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any

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
    if force or not path.exists():
        path.write_text(default_config_yaml(), encoding="utf-8")
    path.chmod(0o600)
    return path


def default_config_yaml() -> str:
    token = secrets.token_urlsafe(32)
    return f"""# Tracefold
ws_token: "{token}"

api:
  host: "0.0.0.0"
  port: 8765

storage:
  postgres:
    serve_dsn: "postgresql://tracefold_serve@postgres:5432/tracefold"
    workers_dsn: "postgresql://tracefold_workers@postgres:5432/tracefold"
    migrate_dsn: "postgresql://tracefold_owner@postgres:5432/tracefold"
    nautilus_dsn: "postgresql://tracefold_nautilus@postgres:5432/tracefold"
    serve_password_file: "postgres_serve_password"
    workers_password_file: "postgres_workers_password"
    migrate_password_file: "postgres_migrate_password"
    nautilus_password_file: "postgres_nautilus_password"
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
  oi:                             # #137 open-interest telemetry, judged by rule instead of by model
    window_ms: 14400000           # 4 h rolling window the rank counts within
    max_rank_in_window: 2         # push a symbol's first N eligible signals in that window
    whale_oi_ratio_above_bps: 8000  # a frame must exceed this ratio; 8000 = 80.00% does not qualify
    oi_change_at_least_bps: 0     # 0 disables the OI-move floor
  retention:
    raw_days: 30
    judged_days: 365
  gate:
    suppress_low_signal: false
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
  order:
    fixed_notional_usd: 10
  bindings:
    binance_usdm:
      api_key_file: "binance_usdm_api_key"
      api_secret_file: "binance_usdm_api_secret"
    hyperliquid_perp:
      private_key_file: "hyperliquid_private_key"
      account_address:
"""


def _load_yaml_mapping(path: Path) -> Mapping[str, Any]:
    with path.open("r", encoding="utf-8") as config_file:
        data = yaml.safe_load(config_file)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a mapping at {path}")
    return data
