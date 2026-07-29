from __future__ import annotations

import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator

from tracefold.platform.paths import app_home, app_log_path, config_path, workers_config_path

DEFAULT_UPSTREAM_CHAINS = ("sol", "eth", "base", "bsc")
DEFAULT_UPSTREAM_CHANNELS = ("twitter_monitor_basic", "twitter_monitor_token")
DEFAULT_GMGN_APP_VERSION = "20260429-12894-ccec416"


class ApiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "0.0.0.0"  # noqa: S104 -- configurable API bind address; defaults to all interfaces intentionally
    port: int = 8765
    heartbeat_interval: int = 30
    replay_limit: int = 100


class PostgresConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dsn: str = "postgresql://tracefold_app@postgres:5432/tracefold"
    password_file: str | None = "postgres_password"
    pool_min_size: int = 1
    pool_max_size: int = 16
    connect_timeout_seconds: float = 5.0

    @field_validator("dsn", mode="before")
    @classmethod
    def parse_dsn(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        return normalized or "postgresql://tracefold_app@postgres:5432/tracefold"

    @field_validator("password_file", mode="before")
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
    dex_ws_url: str = "wss://wsdex.okx.com/ws/v6/dex"
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

    @field_validator("dex_ws_url", mode="before")
    @classmethod
    def parse_ws_url(cls, value: Any) -> str:
        return str(value or "wss://wsdex.okx.com/ws/v6/dex").strip()


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


class NewsRelaySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = ""
    auth_header: str = "x-relay-key"
    auth_token: str | None = None

    @field_validator("base_url", "auth_header", mode="before")
    @classmethod
    def parse_relay_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("auth_token", mode="before")
    @classmethod
    def parse_relay_token(cls, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None


class NewsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    relay: NewsRelaySettings = Field(default_factory=NewsRelaySettings)


class BackoffPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_ms: int = Field(default=1000, ge=0)
    max_ms: int = Field(default=60_000, ge=0)


class PerWorkerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    interval_seconds: float = Field(default=5.0, ge=0)
    backoff: BackoffPolicy = Field(default_factory=BackoffPolicy)


class CollectorWorkerSettings(PerWorkerSettings):
    mode: Literal["continuous"] = "continuous"
    interval_seconds: float = Field(default=3.0, ge=0)
    snapshot_timeout_seconds: float = Field(default=0.5, ge=0)
    watchdog_interval_seconds: float = Field(default=30.0, ge=0)
    stale_timeout_seconds: float = Field(default=180.0, ge=0)


class MarketTickStreamWorkerSettings(PerWorkerSettings):
    interval_seconds: float = Field(default=5.0, ge=0)
    subscription_limit: int = Field(default=100, ge=1)
    stream_cycle_seconds: float = Field(default=30.0, ge=0.001)


class MarketTickPollWorkerSettings(PerWorkerSettings):
    interval_seconds: float = Field(default=15.0, ge=0)
    batch_size: int = Field(default=100, ge=1)
    concurrency: int = Field(default=4, ge=1)


class EventAnchorBackfillWorkerSettings(PerWorkerSettings):
    interval_seconds: float = Field(default=1.0, ge=0)
    batch_size: int = Field(default=50, ge=1)
    concurrency: int = Field(default=8, ge=1)
    max_attempts: int = Field(default=3, ge=1)
    lease_ms: int = Field(default=120_000, ge=1)
    statement_timeout_seconds: float = Field(default=30.0, ge=0)
    min_age_ms: int = Field(default=250, ge=0)
    active_window_ms: int = Field(default=300_000, ge=1)
    max_anchor_lag_ms: int = Field(default=60_000, ge=1)


class ResolutionRefreshWorkerSettings(PerWorkerSettings):
    interval_seconds: float = Field(default=30.0, ge=0)
    batch_size: int = Field(default=50, ge=1)
    max_attempts: int = Field(default=3, ge=1)
    lease_ms: int = Field(default=300_000, ge=1)
    hot_not_found_retry_ms: int = Field(default=60_000, ge=1)
    reprocess_limit: int = Field(default=500, ge=1)
    chain_ids: tuple[str, ...] = ("solana", "eip155:1", "eip155:56", "eip155:8453", "ton")

    @field_validator("chain_ids", mode="before")
    @classmethod
    def parse_chain_ids(cls, value: Any) -> tuple[str, ...]:
        return tuple(_split_values(value))


class AssetProfileRefreshWorkerSettings(PerWorkerSettings):
    interval_seconds: float = Field(default=60.0, ge=0)
    batch_size: int = Field(default=50, ge=1)
    lease_ms: int = Field(default=120_000, ge=1)
    provider_retry_ms: int = Field(default=300_000, ge=1)
    ready_refresh_ms: int = Field(default=21_600_000, ge=1)
    missing_refresh_ms: int = Field(default=900_000, ge=1)
    error_refresh_ms: int = Field(default=900_000, ge=1)
    statement_timeout_seconds: float = Field(default=120.0, ge=0)


class TokenImageMirrorWorkerSettings(PerWorkerSettings):
    interval_seconds: float = Field(default=60.0, ge=0)
    batch_size: int = Field(default=100, ge=1)
    lease_ms: int = Field(default=120_000, ge=1)
    source_limit: int = Field(default=5000, ge=0)
    retry_ms: int = Field(default=300_000, ge=1)
    max_attempts: int = Field(default=3, ge=1)
    statement_timeout_seconds: float = Field(default=120.0, ge=0)


class TokenProfileCurrentWorkerSettings(PerWorkerSettings):
    interval_seconds: float = Field(default=60.0, ge=0)
    batch_size: int = Field(default=500, ge=1)
    max_attempts: int = Field(default=3, ge=1)
    lease_ms: int = Field(default=120_000, ge=1)
    statement_timeout_seconds: float = Field(default=30.0, ge=0)
    retry_ms: int = Field(default=30_000, ge=1)


class TokenRadarProjectionWorkerSettings(PerWorkerSettings):
    interval_seconds: float = Field(default=10.0, ge=0)
    batch_size: int = Field(default=100, ge=1)
    max_attempts: int = Field(default=3, ge=1)
    lease_ms: int = Field(default=120_000, ge=1)
    retry_ms: int = Field(default=30_000, ge=1)
    private_cache_retention_ms: int = Field(default=172_800_000, ge=1)
    statement_timeout_seconds: float = Field(default=120.0, ge=0)
    windows: tuple[str, ...] = ("5m", "1h", "4h", "24h")
    venues: tuple[str, ...] = ("all", "sol", "eth", "base", "bsc", "cex")
    hot_windows: tuple[str, ...] = ("5m",)
    cold_interval_seconds: float = Field(default=60.0, ge=0)

    @field_validator("windows", "venues", "hot_windows", mode="before")
    @classmethod
    def parse_tuple(cls, value: Any) -> tuple[str, ...]:
        return tuple(_split_values(value))


class MacroThesisWorkerSettings(PerWorkerSettings):
    enabled: bool = False
    interval_seconds: float = Field(default=300.0, ge=0)
    statement_timeout_seconds: float = Field(default=120.0, ge=0)
    lease_ms: int = Field(default=900_000, ge=1)
    retry_ms: int = Field(default=900_000, ge=1)
    max_attempts: int = Field(default=3, ge=1)
    model: str = "gpt-5.4-mini"
    model_request_timeout_seconds: float = Field(default=480.0, ge=1)
    max_tokens: int = Field(default=6_000, ge=1)

    @field_validator("model", mode="before")
    @classmethod
    def parse_model(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("macro_thesis model is required")
        return normalized


class MacroDocumentAnalysisWorkerSettings(PerWorkerSettings):
    enabled: bool = False
    interval_seconds: float = Field(default=30.0, ge=0)
    statement_timeout_seconds: float = Field(default=120.0, ge=0)
    lease_ms: int = Field(default=600_000, ge=1)
    retry_ms: int = Field(default=300_000, ge=1)
    max_attempts: int = Field(default=3, ge=1)
    model: str = "gpt-5.4-mini"
    model_request_timeout_seconds: float = Field(default=180.0, ge=1)
    max_tokens: int = Field(default=6_000, ge=1)

    @field_validator("model", mode="before")
    @classmethod
    def parse_model(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("macro_document_analysis.model is required")
        return normalized


class MacroAcquisitionWorkerSettings(PerWorkerSettings):
    interval_seconds: float = Field(default=300.0, ge=0)
    batch_size: int = Field(default=2, ge=1)
    statement_timeout_seconds: float = Field(default=30.0, ge=0)
    lease_ms: int = Field(default=300_000, ge=1)
    retry_ms: int = Field(default=900_000, ge=1)
    max_attempts: int = Field(default=5, ge=1)


class MacroProjectionWorkerSettings(PerWorkerSettings):
    interval_seconds: float = Field(default=300.0, ge=0)
    statement_timeout_seconds: float = Field(default=120.0, ge=0)


class NewsPipelineWorkerSettings(PerWorkerSettings):
    interval_seconds: float = Field(default=120.0, ge=0)
    batch_size: int = Field(default=200, ge=1)
    fetch_concurrency: int = Field(default=16, ge=1, le=64)
    statement_timeout_seconds: float = Field(default=180.0, ge=0)
    fetch_timeout_seconds: float = Field(default=20.0, ge=1)


class NewsWorldBriefWorkerSettings(PerWorkerSettings):
    interval_seconds: float = Field(default=300.0, ge=0)
    statement_timeout_seconds: float = Field(default=120.0, ge=0)
    max_attempts: int = Field(default=3, ge=1, le=10)
    model: str = "deepseek-chat"
    total_timeout_seconds: float = Field(default=60.0, ge=1, le=60)
    ollama_base_url: str = "http://host.docker.internal:11434/v1"
    ollama_model: str = "deepseek-r1:8b"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "deepseek/deepseek-v4-flash"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model: str = "llama-3.3-70b-versatile"


class WorkersSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collector: CollectorWorkerSettings = Field(default_factory=CollectorWorkerSettings)
    market_tick_stream: MarketTickStreamWorkerSettings = Field(default_factory=MarketTickStreamWorkerSettings)
    market_tick_poll: MarketTickPollWorkerSettings = Field(default_factory=MarketTickPollWorkerSettings)
    event_anchor_backfill: EventAnchorBackfillWorkerSettings = Field(default_factory=EventAnchorBackfillWorkerSettings)
    resolution_refresh: ResolutionRefreshWorkerSettings = Field(default_factory=ResolutionRefreshWorkerSettings)
    asset_profile_refresh: AssetProfileRefreshWorkerSettings = Field(default_factory=AssetProfileRefreshWorkerSettings)
    token_image_mirror: TokenImageMirrorWorkerSettings = Field(default_factory=TokenImageMirrorWorkerSettings)
    token_profile_current: TokenProfileCurrentWorkerSettings = Field(default_factory=TokenProfileCurrentWorkerSettings)
    token_radar_projection: TokenRadarProjectionWorkerSettings = Field(
        default_factory=TokenRadarProjectionWorkerSettings
    )
    macro_intraday_market: MacroAcquisitionWorkerSettings = Field(
        default_factory=lambda: MacroAcquisitionWorkerSettings(interval_seconds=300.0, batch_size=32, retry_ms=300_000)
    )
    macro_settlements: MacroAcquisitionWorkerSettings = Field(
        default_factory=lambda: MacroAcquisitionWorkerSettings(interval_seconds=21_600.0, batch_size=32)
    )
    macro_economic_releases: MacroAcquisitionWorkerSettings = Field(
        default_factory=lambda: MacroAcquisitionWorkerSettings(interval_seconds=3_600.0, batch_size=4)
    )
    macro_official_state: MacroAcquisitionWorkerSettings = Field(
        default_factory=lambda: MacroAcquisitionWorkerSettings(interval_seconds=10_800.0, batch_size=4)
    )
    macro_official_documents: MacroAcquisitionWorkerSettings = Field(
        default_factory=lambda: MacroAcquisitionWorkerSettings(interval_seconds=3_600.0, batch_size=2)
    )
    macro_backfill: MacroAcquisitionWorkerSettings = Field(
        default_factory=lambda: MacroAcquisitionWorkerSettings(enabled=False, interval_seconds=5.0, batch_size=1)
    )
    macro_projection: MacroProjectionWorkerSettings = Field(default_factory=MacroProjectionWorkerSettings)
    macro_document_analysis: MacroDocumentAnalysisWorkerSettings = Field(
        default_factory=MacroDocumentAnalysisWorkerSettings
    )
    macro_thesis: MacroThesisWorkerSettings = Field(default_factory=MacroThesisWorkerSettings)
    news_pipeline: NewsPipelineWorkerSettings = Field(default_factory=NewsPipelineWorkerSettings)
    news_world_brief: NewsWorldBriefWorkerSettings = Field(default_factory=NewsWorldBriefWorkerSettings)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    _config_dir: Path = PrivateAttr(default_factory=app_home)

    ws_token: str | None = None
    api: ApiConfig = Field(default_factory=ApiConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    gmgn: GmgnConfig = Field(default_factory=GmgnConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    news: NewsSettings = Field(default_factory=NewsSettings)
    upstream: UpstreamConfig = Field(default_factory=UpstreamConfig)
    workers: WorkersSettings = Field(default_factory=WorkersSettings)

    def set_config_dir(self, value: Path) -> None:
        self._config_dir = value

    @property
    def app_home(self) -> Path:
        return self._config_dir

    @property
    def postgres_password_file(self) -> Path | None:
        value = self.storage.postgres.password_file
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

    @property
    def okx_dex_ws_configured(self) -> bool:
        return bool(
            self.providers.okx.dex_ws_url
            and self.providers.okx.dex_api_key
            and self.providers.okx.dex_secret_key
            and self.providers.okx.dex_passphrase
        )

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
    workers_path = workers_config_path(path.parent)
    if not workers_path.exists():
        raise FileNotFoundError(f"workers.yaml not found at {workers_path}; run `tracefold init` first")
    data = _load_yaml_mapping(path)
    if "workers" in data:
        raise ValueError("workers runtime settings must be configured in workers.yaml, not config.yaml")
    workers = WorkersSettings(**_load_yaml_mapping(workers_path))
    settings = Settings(**dict(data), workers=workers)
    settings.set_config_dir(path.parent)
    if require_ws_token and not settings.ws_token:
        raise ValueError("ws_token is required in config.yaml")
    return settings


def write_default_config(*, force: bool = False) -> Path:
    home = app_home()
    path = config_path(home)
    workers_path = workers_config_path(home)
    home.mkdir(parents=True, exist_ok=True)
    (home / "logs").mkdir(parents=True, exist_ok=True)
    if force or not path.exists():
        path.write_text(default_config_yaml(), encoding="utf-8")
    if force or not workers_path.exists():
        workers_path.write_text(default_workers_yaml(), encoding="utf-8")
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
    dsn: "postgresql://tracefold_app@postgres:5432/tracefold"
    password_file: "postgres_password"
    pool_min_size: 1
    pool_max_size: 16
    connect_timeout_seconds: 5

llm:
  api_key:
  base_url: ""
  openrouter_api_key:
  groq_api_key:

gmgn:
  api_key:
  openapi_base_url: "https://openapi.gmgn.ai"
  timeout_seconds: 5
  token_info_cache_ttl_seconds: 60

providers:
  okx:
    dex_base_url: "https://web3.okx.com"
    dex_chain_indexes: ["501", "1", "56", "8453", "607"]
    dex_ws_url: "wss://wsdex.okx.com/ws/v6/dex"
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
  relay:
    base_url: ""
    auth_header: "x-relay-key"
    auth_token:

upstream:
  chains: ["sol", "eth", "base", "bsc"]
  channels: ["twitter_monitor_basic", "twitter_monitor_token"]
  app_version: "{DEFAULT_GMGN_APP_VERSION}"
  proxy:
  reconnect_delay: 3
  heartbeat_interval: 25
  idle_timeout: 90
"""


def default_workers_yaml() -> str:
    payload = WorkersSettings().model_dump(mode="json")
    rendered = yaml.safe_dump(payload, sort_keys=False)
    return f"# Tracefold worker runtime\n{rendered}"


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
