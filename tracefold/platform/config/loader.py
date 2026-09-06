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
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("config_path_not_regular_file")
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
  # The console's public origin, as a reader outside this host reaches it, e.g.
  # "https://tracefold.example.com". Left unset, market push cards carry the item
  # id instead of a 打开明细 button. Not a bind address, and no default is guessed.
  public_url:

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
  # #572: the Robinhood Chain wallet tape. Public read-only endpoints, no credentials. When it is on it
  # stores what followed wallets did and, under `rules`, sends exit and crowding cards through the same
  # market notification loop every other market card goes through. Off until an operator turns it on.
  chain_tape:
    enabled: false
    rpc_url: "https://rpc.mainnet.chain.robinhood.com"
    poll_interval_s: 2.0
    roster_provider_url: "https://robinhoodtrenches.com"
    roster:
      min_closed_trades: 10
      min_profit_factor: 1.2
      top_quality: 20
      top_whale_by_open_cost: 20
    rules:
      exit_ratio_bps: 3000
      exit_min_position_usd: 20000.0
      exit_cascade_window_s: 7200
      exit_cascade_min_usd: 5000.0
      crowding_n: 3
      crowding_window_s: 900
      crowding_min_usd: 1000.0
      crowding_premium_late_bps: 3000
      trigger_max_age_s: 600
    digest:
      enabled: true
      interval_s: 14400
      max_calls_per_day: 24
    retention_days: 90
  watchlist: []

trading:
  enabled: false
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
