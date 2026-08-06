from __future__ import annotations

import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from tracefold.platform.paths import app_home, app_log_path, config_path

DEFAULT_UPSTREAM_CHAINS = ("sol", "eth", "base", "bsc")
DEFAULT_UPSTREAM_CHANNELS = ("twitter_monitor_basic", "twitter_monitor_token")
DEFAULT_GMGN_APP_VERSION = "20260429-12894-ccec416"


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
    model_config = ConfigDict(extra="forbid")

    api_key: str | None = None
    base_url: str = ""
    openrouter_api_key: str | None = None
    groq_api_key: str | None = None
    news_brief_model: str = "deepseek-chat"
    macro_document_analysis_enabled: bool = False
    macro_document_analysis_model: str = "gpt-5.4-mini"

    @field_validator("api_key", "openrouter_api_key", "groq_api_key", mode="before")
    @classmethod
    def parse_optional_string(cls, value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @field_validator("base_url", mode="before")
    @classmethod
    def parse_base_url(cls, value: Any) -> str:
        return str(value or "").strip().rstrip("/")

    @field_validator(
        "news_brief_model",
        "macro_document_analysis_model",
        mode="before",
    )
    @classmethod
    def parse_model(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("llm model is required")
        return normalized


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


class OkxProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dex_base_url: str = "https://web3.okx.com"
    dex_chain_indexes: tuple[str, ...] = ("501", "1", "56", "8453", "607")
    dex_api_key: str | None = None
    dex_secret_key: str | None = None
    dex_passphrase: str | None = None
    timeout_seconds: float = 15.0

    @field_validator("dex_base_url", mode="before")
    @classmethod
    def parse_base_url(cls, value: Any) -> str:
        normalized = str(value or "").strip().rstrip("/")
        return normalized

    @field_validator("dex_chain_indexes", mode="before")
    @classmethod
    def parse_tuple(cls, value: Any) -> tuple[str, ...]:
        return tuple(_split_values(value))

    @field_validator("dex_api_key", "dex_secret_key", "dex_passphrase", mode="before")
    @classmethod
    def parse_optional_secret(cls, value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None


class BinanceProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    web3_base_url: str = "https://web3.binance.com"
    cex_profile_base_url: str = "https://www.binance.com"
    usdm_futures_base_url: str = "https://fapi.binance.com"
    cex_universe_quote_symbol: str = "USDT"
    cex_universe_contract_type: str = "PERPETUAL"
    timeout_seconds: float = 15.0

    @field_validator("web3_base_url", "cex_profile_base_url", "usdm_futures_base_url", mode="before")
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
    request_timeout_seconds: float = Field(default=60.0, ge=1)
    user_agent: str = "TracefoldMacro/1.0 research@localhost"


class ProvidersConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    okx: OkxProviderConfig = Field(default_factory=OkxProviderConfig)
    binance: BinanceProviderConfig = Field(default_factory=BinanceProviderConfig)
    macro_sources: MacroSourcesConfig = Field(default_factory=MacroSourcesConfig)


class NewsPushSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    feishu_webhook_url: str | None = None
    feishu_signing_secret: str | None = None

    @field_validator("feishu_webhook_url", "feishu_signing_secret", mode="before")
    @classmethod
    def parse_optional_secret(cls, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @field_validator("feishu_webhook_url")
    @classmethod
    def validate_feishu_webhook_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        try:
            port = parsed.port
        except ValueError:
            raise ValueError("news_push_feishu_webhook_url_invalid") from None
        valid_path = parsed.path.startswith("/open-apis/bot/v2/hook/") and bool(
            parsed.path.removeprefix("/open-apis/bot/v2/hook/")
        )
        if (
            parsed.scheme != "https"
            or parsed.hostname != "open.feishu.cn"
            or port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not valid_path
            or "/" in parsed.path.removeprefix("/open-apis/bot/v2/hook/")
        ):
            raise ValueError("news_push_feishu_webhook_url_invalid")
        return value

    @model_validator(mode="after")
    def require_enabled_webhook(self) -> NewsPushSettings:
        if self.enabled and not self.feishu_webhook_url:
            raise ValueError("news_push_feishu_webhook_url_required")
        return self


class NewsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = True
    opennews_token: str | None = None
    push: NewsPushSettings = Field(default_factory=NewsPushSettings)

    @field_validator("opennews_token", mode="before")
    @classmethod
    def parse_opennews_token(cls, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @model_validator(mode="after")
    def require_news_for_push(self) -> NewsSettings:
        if not self.enabled and self.push.enabled:
            raise ValueError("news_push_requires_news_enabled")
        return self


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

    @property
    def okx_dex_configured(self) -> bool:
        return bool(self.providers.okx.dex_base_url)

    @field_validator("ws_token", mode="before")
    @classmethod
    def parse_optional_ws_token(cls, value: Any) -> str | None:
        if value is None:
            return None
        token = str(value).strip()
        return token or None


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
  base_url: ""
  openrouter_api_key:
  groq_api_key:
  news_brief_model: "deepseek-chat"
  macro_document_analysis_enabled: false
  macro_document_analysis_model: "gpt-5.4-mini"

gmgn:
  api_key:
  openapi_base_url: "https://openapi.gmgn.ai"
  timeout_seconds: 5
  token_info_cache_ttl_seconds: 60

providers:
  okx:
    dex_base_url: "https://web3.okx.com"
    dex_chain_indexes: ["501", "1", "56", "8453", "607"]
    dex_api_key:
    dex_secret_key:
    dex_passphrase:
    timeout_seconds: 15
  binance:
    enabled: true
    web3_base_url: "https://web3.binance.com"
    cex_profile_base_url: "https://www.binance.com"
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
    request_timeout_seconds: 60
    user_agent: "TracefoldMacro/1.0 research@localhost"

news:
  enabled: true
  opennews_token:
  push:
    enabled: false
    feishu_webhook_url:
    feishu_signing_secret:

upstream:
  chains: ["sol", "eth", "base", "bsc"]
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
