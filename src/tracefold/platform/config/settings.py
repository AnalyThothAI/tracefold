from __future__ import annotations

import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from tracefold.platform.paths import app_home, app_log_path, config_path

DEFAULT_UPSTREAM_CHAINS = ("sol", "eth", "base", "bsc", "robinhood")
DEFAULT_UPSTREAM_CHANNELS = ("twitter_monitor_basic", "twitter_monitor_token")
DEFAULT_GMGN_APP_VERSION = "20260429-12894-ccec416"
OPENNEWS_STRATEGY_ID_LIMIT = 32


class ApiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "0.0.0.0"  # noqa: S104 -- configurable API bind address; defaults to all interfaces intentionally
    port: int = 8765
    heartbeat_interval: int = 30
    replay_limit: int = Field(default=100, ge=0, le=100)


class PostgresConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    serve_dsn: str = "postgresql://tracefold_serve@postgres:5432/tracefold"
    workers_dsn: str = "postgresql://tracefold_workers@postgres:5432/tracefold"
    migrate_dsn: str = "postgresql://tracefold_migrate@postgres:5432/tracefold"
    serve_password_file: str | None = "postgres_serve_password"
    workers_password_file: str | None = "postgres_workers_password"
    migrate_password_file: str | None = "postgres_migrate_password"
    connect_timeout_seconds: float = 5.0

    @field_validator("serve_dsn", "workers_dsn", "migrate_dsn", mode="before")
    @classmethod
    def parse_dsn(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("postgres role DSN is required")
        return normalized

    @field_validator(
        "serve_password_file",
        "workers_password_file",
        "migrate_password_file",
        mode="before",
    )
    @classmethod
    def parse_optional_path(cls, value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    postgres: PostgresConfig = Field(default_factory=PostgresConfig)


class LlmConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    api_key: str | None = None
    base_url: str | None = None
    news_triage_model: str | None = None
    news_analyst_model: str | None = None
    macro_document_analysis_enabled: bool = False
    macro_document_analysis_model: str = "gpt-5.4-mini"

    @field_validator("api_key", "news_triage_model", "news_analyst_model", mode="before")
    @classmethod
    def parse_optional_string(cls, value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @field_validator("base_url", mode="before")
    @classmethod
    def parse_optional_base_url(cls, value: Any) -> str | None:
        normalized = str(value or "").strip().rstrip("/")
        return normalized or None

    @field_validator("macro_document_analysis_model", mode="before")
    @classmethod
    def parse_model(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("llm model is required")
        return normalized

    @model_validator(mode="after")
    def require_complete_direct_configuration(self) -> LlmConfig:
        configured = (self.api_key, self.base_url, self.news_triage_model)
        if any(configured) and not all(configured):
            raise ValueError("llm_direct_configuration_incomplete")
        if self.news_analyst_model is None and self.news_triage_model is not None:
            self.news_analyst_model = self.news_triage_model
        return self


class GmgnConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str | None = None
    openapi_base_url: str = "https://openapi.gmgn.ai"
    timeout_seconds: float = 5.0
    token_info_cache_ttl_seconds: int = 60

    @field_validator("api_key", mode="before")
    @classmethod
    def parse_optional_api_key(cls, value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @field_validator("openapi_base_url", mode="before")
    @classmethod
    def parse_openapi_base_url(cls, value: Any) -> str:
        normalized = str(value or "https://openapi.gmgn.ai").strip().rstrip("/")
        return normalized or "https://openapi.gmgn.ai"


class UpstreamConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chains: tuple[str, ...] = DEFAULT_UPSTREAM_CHAINS
    channels: tuple[str, ...] = DEFAULT_UPSTREAM_CHANNELS
    app_version: str = DEFAULT_GMGN_APP_VERSION
    proxy: str | None = None
    reconnect_delay: float = 3.0
    heartbeat_interval: float = 25.0
    idle_timeout: float = 90.0

    @field_validator("chains", "channels", mode="before")
    @classmethod
    def parse_tuple(cls, value: Any) -> tuple[str, ...]:
        return tuple(_split_values(value))

    @field_validator("proxy", mode="before")
    @classmethod
    def parse_optional_proxy(cls, value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        if normalized.lower() in {"", "none", "false", "off", "direct"}:
            return None
        return normalized


class BinanceProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    usdm_futures_base_url: str = "https://fapi.binance.com"
    cex_universe_quote_symbol: str = "USDT"
    cex_universe_contract_type: str = "PERPETUAL"
    timeout_seconds: float = 15.0

    @field_validator("usdm_futures_base_url", mode="before")
    @classmethod
    def parse_base_url(cls, value: Any) -> str:
        return str(value or "").strip().rstrip("/")

    @field_validator("cex_universe_quote_symbol", "cex_universe_contract_type", mode="before")
    @classmethod
    def parse_uppercase_string(cls, value: Any) -> str:
        return str(value or "").strip().upper()


class MacroSourcesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    fred_enabled: bool = True
    cboe_enabled: bool = True
    cftc_enabled: bool = True
    nasdaq_daily_enabled: bool = True
    yfinance_enabled: bool = True
    user_agent: str = "TracefoldMacro/1.0 research@localhost"


class ProvidersConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binance: BinanceProviderConfig = Field(default_factory=BinanceProviderConfig)
    macro_sources: MacroSourcesConfig = Field(default_factory=MacroSourcesConfig)


class NewsPushSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    feishu_webhook_url: str | None = None
    feishu_signing_secret: str | None = None
    min_interval_seconds: float = 0.6
    hourly_cap: int = 20

    @field_validator("feishu_webhook_url", "feishu_signing_secret", mode="before")
    @classmethod
    def parse_optional_secret(cls, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_limits(self) -> NewsPushSettings:
        if self.min_interval_seconds < 0 or self.min_interval_seconds > 60:
            raise ValueError("news_push_min_interval_invalid")
        if self.hourly_cap < 1 or self.hourly_cap > 1000:
            raise ValueError("news_push_hourly_cap_invalid")
        return self


class NewsBrokerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    url: str | None = Field(default=None, repr=False)
    name_prefix: str = ""
    connect_timeout_seconds: float = 10.0

    @field_validator("url", mode="before")
    @classmethod
    def parse_url(cls, value: Any) -> str | None:
        normalized = str(value or "").strip()
        if not normalized:
            return None
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"amqp", "amqps"} or not parsed.hostname:
            raise ValueError("news_broker_url_invalid")
        return normalized

    @field_validator("name_prefix", mode="before")
    @classmethod
    def parse_prefix(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if normalized and not re.fullmatch(r"[a-z0-9_.-]{1,32}", normalized):
            raise ValueError("news_broker_name_prefix_invalid")
        return normalized


class NewsTriageSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deadline_seconds: float = 6.0
    concurrency: int = 4
    circuit_failures: int = 3
    circuit_open_seconds: float = 60.0

    @model_validator(mode="after")
    def validate_bounds(self) -> NewsTriageSettings:
        if not 0.5 <= self.deadline_seconds <= 30:
            raise ValueError("news_triage_deadline_invalid")
        if not 1 <= self.concurrency <= 32:
            raise ValueError("news_triage_concurrency_invalid")
        return self


class NewsAnalystSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    deadline_seconds: float = 30.0
    concurrency: int = 2

    @model_validator(mode="after")
    def validate_bounds(self) -> NewsAnalystSettings:
        if not 5 <= self.deadline_seconds <= 300:
            raise ValueError("news_analyst_deadline_invalid")
        if not 1 <= self.concurrency <= 8:
            raise ValueError("news_analyst_concurrency_invalid")
        return self


class NewsWatchlistEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    market_type: str = "any"

    @field_validator("symbol", mode="before")
    @classmethod
    def parse_symbol(cls, value: Any) -> str:
        normalized = str(value or "").strip().upper().replace("XYZ-", "")
        if not re.fullmatch(r"[A-Z0-9._-]{1,16}", normalized):
            raise ValueError("news_watchlist_symbol_invalid")
        return normalized


class NewsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = True
    opennews_token: str | None = None
    opennews_strategy_ids: tuple[str, ...] = ()
    broker: NewsBrokerSettings = Field(default_factory=NewsBrokerSettings)
    triage: NewsTriageSettings = Field(default_factory=NewsTriageSettings)
    analyst: NewsAnalystSettings = Field(default_factory=NewsAnalystSettings)
    push: NewsPushSettings = Field(default_factory=NewsPushSettings)
    watchlist: tuple[NewsWatchlistEntry, ...] = ()

    @field_validator("opennews_token", mode="before")
    @classmethod
    def parse_opennews_token(cls, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @field_validator("opennews_strategy_ids", mode="before")
    @classmethod
    def parse_opennews_strategy_ids(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, list | tuple):
            raise ValueError("opennews_strategy_ids_invalid")
        if len(value) > OPENNEWS_STRATEGY_ID_LIMIT:
            raise ValueError("opennews_strategy_ids_too_many")
        normalized: list[str] = []
        for raw in value:
            if not isinstance(raw, str):
                raise ValueError("opennews_strategy_id_invalid")
            strategy_id = raw.strip()
            if not strategy_id or "\x00" in strategy_id or len(strategy_id) > 128:
                raise ValueError("opennews_strategy_id_invalid")
            try:
                strategy_id.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("opennews_strategy_id_invalid") from exc
            normalized.append(strategy_id)
        if len(set(normalized)) != len(normalized):
            raise ValueError("opennews_strategy_ids_duplicate")
        return tuple(sorted(normalized))

    @field_validator("watchlist", mode="before")
    @classmethod
    def parse_watchlist(cls, value: Any) -> tuple[Any, ...]:
        if value is None:
            return ()
        if not isinstance(value, list | tuple):
            raise ValueError("news_watchlist_invalid")
        return tuple(value)

    @model_validator(mode="after")
    def validate_opennews_strategy_configuration(self) -> NewsSettings:
        if self.enabled and self.opennews_token and not self.opennews_strategy_ids:
            raise ValueError("opennews_strategy_ids_required")
        return self

    @property
    def watchlist_symbols(self) -> frozenset[str]:
        return frozenset(entry.symbol for entry in self.watchlist)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    _config_dir: Path = PrivateAttr(default_factory=app_home)

    ws_token: str | None = None
    api: ApiConfig = Field(default_factory=ApiConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    gmgn: GmgnConfig = Field(default_factory=GmgnConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    news: NewsSettings = Field(default_factory=NewsSettings)
    upstream: UpstreamConfig = Field(default_factory=UpstreamConfig)

    def set_config_dir(self, value: Path) -> None:
        self._config_dir = value

    @property
    def app_home(self) -> Path:
        return self._config_dir

    def postgres_dsn(self, role: Literal["serve", "workers", "migrate"]) -> str:
        return cast(str, getattr(self.storage.postgres, f"{role}_dsn"))

    def postgres_password_file(
        self,
        role: Literal["serve", "workers", "migrate"],
    ) -> Path | None:
        value = cast(str | None, getattr(self.storage.postgres, f"{role}_password_file"))
        if not value:
            return None
        configured = Path(value).expanduser()
        if configured.is_absolute():
            return configured
        return self._config_dir / configured

    @property
    def log_file(self) -> Path:
        return app_log_path(self._config_dir)

    @property
    def gmgn_configured(self) -> bool:
        return bool(self.gmgn.api_key)

    @field_validator("ws_token", mode="before")
    @classmethod
    def parse_optional_ws_token(cls, value: Any) -> str | None:
        if value is None:
            return None
        token = str(value).strip()
        return token or None


@dataclass(frozen=True, slots=True)
class NewsPushAvailability:
    requested: bool
    delivery_available: bool
    reason: str | None
    feishu_webhook_url_configured: bool
    feishu_signing_secret_configured: bool


def news_push_availability(settings: Settings) -> NewsPushAvailability:
    push = settings.news.push
    requested = push.enabled
    webhook_configured = bool(push.feishu_webhook_url)
    reason: str | None = None
    if requested and not settings.news.enabled:
        reason = "news_item_push_news_disabled"
    elif requested and not webhook_configured:
        reason = "news_item_push_feishu_webhook_missing"
    elif requested and not _is_feishu_webhook_url(push.feishu_webhook_url):
        reason = "news_item_push_feishu_webhook_invalid"
    return NewsPushAvailability(
        requested=requested,
        delivery_available=requested and reason is None,
        reason=reason,
        feishu_webhook_url_configured=webhook_configured,
        feishu_signing_secret_configured=bool(push.feishu_signing_secret),
    )


@dataclass(frozen=True, slots=True)
class NewsModelAvailability:
    triage_configured: bool
    analyst_configured: bool
    triage_model: str | None
    analyst_model: str | None


def news_model_availability(settings: Settings) -> NewsModelAvailability:
    direct = bool(settings.llm.api_key and _is_http_base_url(settings.llm.base_url))
    triage = direct and bool(settings.llm.news_triage_model)
    analyst = direct and bool(settings.llm.news_analyst_model) and settings.news.analyst.enabled
    return NewsModelAvailability(
        triage_configured=triage,
        analyst_configured=analyst,
        triage_model=settings.llm.news_triage_model if triage else None,
        analyst_model=settings.llm.news_analyst_model if analyst else None,
    )


def _is_feishu_webhook_url(value: str | None) -> bool:
    if value is None:
        return False
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        return False
    hook_id = parsed.path.removeprefix("/open-apis/bot/v2/hook/")
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == "open.feishu.cn"
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and hook_id
        and hook_id != parsed.path
        and "/" not in hook_id
    )


def _is_http_base_url(value: str | None) -> bool:
    if value is None:
        return False
    try:
        parsed = urlsplit(value)
        _port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


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
  heartbeat_interval: 30
  replay_limit: 100

storage:
  postgres:
    serve_dsn: "postgresql://tracefold_serve@postgres:5432/tracefold"
    workers_dsn: "postgresql://tracefold_workers@postgres:5432/tracefold"
    migrate_dsn: "postgresql://tracefold_migrate@postgres:5432/tracefold"
    serve_password_file: "postgres_serve_password"
    workers_password_file: "postgres_workers_password"
    migrate_password_file: "postgres_migrate_password"
    connect_timeout_seconds: 5

llm:
  api_key:
  base_url:
  news_triage_model:
  news_analyst_model:
  macro_document_analysis_enabled: false
  macro_document_analysis_model: "gpt-5.4-mini"

gmgn:
  api_key:
  openapi_base_url: "https://openapi.gmgn.ai"
  timeout_seconds: 5
  token_info_cache_ttl_seconds: 60

providers:
  binance:
    enabled: true
    usdm_futures_base_url: "https://fapi.binance.com"
    cex_universe_quote_symbol: "USDT"
    cex_universe_contract_type: "PERPETUAL"
    timeout_seconds: 15
  macro_sources:
    enabled: true
    fred_enabled: true
    cboe_enabled: true
    cftc_enabled: true
    nasdaq_daily_enabled: true
    yfinance_enabled: true
    user_agent: "TracefoldMacro/1.0 research@localhost"

news:
  enabled: true
  opennews_token:
  opennews_strategy_ids: []
  broker:
    url: "amqp://tracefold:tracefold@rabbitmq:5672/"
    name_prefix: ""
  triage:
    deadline_seconds: 6.0
    concurrency: 4
  analyst:
    enabled: true
    deadline_seconds: 30
    concurrency: 2
  push:
    enabled: false
    feishu_webhook_url:
    feishu_signing_secret:
    min_interval_seconds: 0.6
    hourly_cap: 20
  watchlist: []

upstream:
  chains: ["sol", "eth", "base", "bsc", "robinhood"]
  channels: ["twitter_monitor_basic", "twitter_monitor_token"]
  app_version: "{DEFAULT_GMGN_APP_VERSION}"
  proxy:
  reconnect_delay: 3
  heartbeat_interval: 25
  idle_timeout: 90
"""


def _load_yaml_mapping(path: Path) -> Mapping[str, Any]:
    with path.open("r", encoding="utf-8") as config_file:
        data = yaml.safe_load(config_file)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a mapping at {path}")
    return data


def _split_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list | tuple | set):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]
